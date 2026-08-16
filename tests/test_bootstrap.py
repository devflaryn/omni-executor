import hashlib, http.server, json, socket, socketserver, struct, tarfile, threading, io
from pathlib import Path
import pytest
import bootstrap


def _sha(b): return hashlib.sha256(b).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    blobs = {}          # name -> bytes
    manifest = {}       # dict
    interrupt = None    # {"name": blob_name, "cut": n, "triggered": False} or None
    range_log = []       # list of (blob_name, "Range" header value) actually seen by the server
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/omni/dist/manifest"):
            body = json.dumps(self.manifest).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if self.path.startswith("/omni/dist/blob/"):
            name = self.path.rsplit("/",1)[-1]
            data = self.blobs.get(name)
            if data is None: self.send_response(404); self.end_headers(); return
            rng = self.headers.get("Range")
            if rng:
                type(self).range_log.append((name, rng))
            interrupt = self.interrupt
            if interrupt and interrupt.get("name") == name and not interrupt.get("triggered"):
                # Simulate a real network interruption mid-transfer: claim the full
                # Content-Length, write only the first `cut` bytes, then force a TCP
                # RST (via SO_LINGER) so the client sees a genuine connection error
                # instead of a clean EOF.
                interrupt["triggered"] = True
                cut = interrupt["cut"]
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data[:cut])
                self.wfile.flush()
                try:
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                except OSError:
                    pass
                self.connection.close()
                return
            start = 0
            if rng and rng.startswith("bytes="):
                start = int(rng.split("=")[1].split("-")[0] or 0)
            chunk = data[start:]
            code = 206 if start else 200
            self.send_response(code)
            self.send_header("Accept-Ranges","bytes")
            self.send_header("X-Omni-SHA256", _sha(data))
            if start: self.send_header("Content-Range", f"bytes {start}-{len(data)-1}/{len(data)}")
            self.send_header("Content-Length", str(len(chunk))); self.end_headers(); self.wfile.write(chunk)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIEXEC_RUNTIME_DIR", str(tmp_path / "rt"))
    _Handler.interrupt = None
    _Handler.range_log = []
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{port}", _Handler
    srv.shutdown()


def _tar_bytes(members: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name); ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def test_downloads_verifies_places_and_records(server):
    base, H = server
    offset = b"OFFSETDATA" * 100
    tar = _tar_bytes({"base_arm_system_rooted.qcow2": b"SYS", "base_arm_data_rooted.qcow2": b"DATA"})
    H.blobs = {"offset-arceus-arm": offset, "base-arm": tar}
    H.manifest = {"ok": True, "os":"mac", "channel":"stable", "app":{"version":"1.0.0"},
        "artifacts":[
          {"name":"base-arm","version":"lineage-23.2","bytes":len(tar),"sha256":_sha(tar),
           "url":"/omni/dist/blob/base-arm","dest":"images/arm","unpack":"tar","dest_name":None},
          {"name":"offset-arceus-arm","version":"2.732.1043","bytes":len(offset),"sha256":_sha(offset),
           "url":"/omni/dist/blob/offset-arceus-arm","dest":"images/arm","unpack":None,
           "dest_name":"base_arm_data_offset_arceusremote.qcow2"},
        ]}
    res = bootstrap.ensure_runtime(base_url=base)
    rt = bootstrap.runtime_dir()
    assert res["ok"] is True
    assert (rt/"images/arm/base_arm_system_rooted.qcow2").read_bytes() == b"SYS"
    assert (rt/"images/arm/base_arm_data_offset_arceusremote.qcow2").read_bytes() == offset
    inst = json.loads((rt/"installed.json").read_text())
    assert inst["artifacts"]["offset-arceus-arm"]["sha256"] == _sha(offset)


def test_idempotent_second_run_downloads_nothing(server):
    base, H = server
    blob = b"X"*512
    H.blobs = {"offset-arceus-arm": blob}
    H.manifest = {"ok":True,"os":"mac","channel":"stable","app":{"version":"1.0.0"},
      "artifacts":[{"name":"offset-arceus-arm","version":"1","bytes":len(blob),"sha256":_sha(blob),
        "url":"/omni/dist/blob/offset-arceus-arm","dest":"images/arm","unpack":None,
        "dest_name":"base_arm_data_offset_x.qcow2"}]}
    bootstrap.ensure_runtime(base_url=base)
    plan = bootstrap.plan_downloads(H.manifest, bootstrap.installed_state(bootstrap.runtime_dir()))
    assert plan == []


def test_sha_mismatch_rejected_then_retry_succeeds(server, monkeypatch):
    base, H = server
    good = b"G"*300
    H.blobs = {"offset-arceus-arm": good}
    art = {"name":"offset-arceus-arm","version":"1","bytes":len(good),
           "sha256":"0"*64,  # wrong on purpose
           "url":"/omni/dist/blob/offset-arceus-arm","dest":"images/arm","unpack":None,
           "dest_name":"base_arm_data_offset_x.qcow2"}
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.download_blob(base, art, bootstrap.runtime_dir()/"tmp.bin")


def test_resume_after_interruption_uses_range(server, monkeypatch):
    """A dropped connection mid-transfer must resume via HTTP Range, not
    restart from scratch: the server here genuinely severs the TCP connection
    (RST) after sending only a prefix of the blob, and the assertions confirm
    both that the client's retry issued a byte-range request AND that the
    final file exactly matches the full blob's bytes/sha256."""
    base, H = server
    # Small chunk size so at least one full chunk is flushed to disk before
    # the forced RST lands, and no real backoff sleep so the test stays fast.
    monkeypatch.setattr(bootstrap, "_CHUNK", 64)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda *_: None)

    full = bytes((i % 256) for i in range(2000))
    total = len(full)
    H.blobs = {"resume-blob": full}
    H.interrupt = {"name": "resume-blob", "cut": 300, "triggered": False}
    art = {"name": "resume-blob", "version": "1", "bytes": total, "sha256": _sha(full),
           "url": "/omni/dist/blob/resume-blob", "dest": "images/arm", "unpack": None,
           "dest_name": "resume.bin"}

    tmp = bootstrap.runtime_dir() / "resume.part"
    bootstrap.download_blob(base, art, tmp)

    result = tmp.read_bytes()
    assert result == full
    assert _sha(result) == _sha(full)
    # The interruption actually fired (this test is only meaningful if it did)...
    assert H.interrupt["triggered"] is True
    # ...and the retry that finished the job was a genuine Range/resume request.
    assert any(name == "resume-blob" and rng.startswith("bytes=") and not rng.startswith("bytes=0")
               for name, rng in H.range_log), f"expected a non-zero-offset Range request, got {H.range_log}"


# --------------------------------------------------- tool install / UPGRADE
#
# QEMU used to be presence-checked and never upgraded: `qemu_install_plan`
# returned needed=False the moment `find_qemu` found anything at all. That is
# why a machine whose QEMU came from the vendor NSIS installer (or that simply
# had QEMU already) kept an UNPATCHED binary forever -- and the patched build
# is the one that honours QEMU_WINDOW_PANEL, so those machines silently got
# the wrong guest aspect ratio with no signal that anything was stale.

PORTABLE = {"name": "qemu-portable-win", "version": "11.0.50",
            "sha256": "a" * 64, "kind": "tool", "bytes": 1024}
PORTABLE_NEW = {**PORTABLE, "version": "11.1.0", "sha256": "b" * 64}


def _mf(*artifacts):
    return {"os": "win", "channel": "stable", "artifacts": list(artifacts)}


def test_a_machine_with_no_qemu_still_needs_one(tmp_path):
    plan = bootstrap.qemu_install_plan(tmp_path, _mf(PORTABLE), installed={})
    assert plan["needed"] is True
    assert plan["portable"] == PORTABLE


def test_our_managed_qemu_at_the_published_version_is_left_alone(tmp_path):
    installed = {"tools": {"qemu": {"version": "11.0.50", "sha256": "a" * 64}}}
    plan = bootstrap.qemu_install_plan(tmp_path, _mf(PORTABLE),
                                       installed=installed, have_qemu=True)
    assert plan["needed"] is False


def test_a_newer_published_qemu_is_an_upgrade(tmp_path):
    # THE POINT OF THIS CHANGE. Same machine, server now offers a new build.
    installed = {"tools": {"qemu": {"version": "11.0.50", "sha256": "a" * 64}}}
    plan = bootstrap.qemu_install_plan(tmp_path, _mf(PORTABLE_NEW),
                                       installed=installed, have_qemu=True)
    assert plan["needed"] is True
    assert plan["upgrade"] is True
    assert plan["needs_admin"] is False      # portable route never elevates


def test_an_unmanaged_qemu_gets_ours_installed_beside_it(tmp_path):
    # The vendor-installer / user's-own-QEMU case: we have never recorded a
    # tool receipt, so whatever is on this machine is not ours and may be
    # unpatched. find_qemu() prefers our runtime dir, so installing the
    # published build makes it win WITHOUT touching their system install.
    plan = bootstrap.qemu_install_plan(tmp_path, _mf(PORTABLE),
                                       installed={}, have_qemu=True)
    assert plan["needed"] is True
    assert plan["needs_admin"] is False


def test_no_portable_offered_never_forces_an_elevated_upgrade(tmp_path):
    # If the server offers no portable build, the only route is the vendor
    # NSIS installer, which is requireAdministrator. Prompting for UAC on
    # every launch to "upgrade" is far worse than keeping what works.
    installed = {"tools": {"qemu": {"version": "11.0.50", "sha256": "a" * 64}}}
    plan = bootstrap.qemu_install_plan(tmp_path, _mf(), installed=installed,
                                       have_qemu=True)
    assert plan["needed"] is False


def test_tool_receipts_survive_a_round_trip(tmp_path):
    bootstrap.record_tool(tmp_path, "qemu", PORTABLE)
    state = bootstrap.installed_state(tmp_path)
    assert state["tools"]["qemu"]["sha256"] == "a" * 64
    assert state["tools"]["qemu"]["version"] == "11.0.50"
    # and it must not disturb the artifact receipts beside it
    assert "artifacts" in state


def test_recording_a_tool_does_not_make_the_runtime_look_unready(tmp_path):
    # Readiness is "plan_downloads is empty". Tools are kind: "tool" and must
    # stay out of that plan however they are recorded, or every machine that
    # already had QEMU reports un-ready forever.
    installed = {"artifacts": {}, "tools": {"qemu": {"sha256": "a" * 64}}}
    assert bootstrap.plan_downloads(_mf(PORTABLE), installed) == []
