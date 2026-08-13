import hashlib, http.server, json, socketserver, tarfile, threading, io
from pathlib import Path
import pytest
import bootstrap


def _sha(b): return hashlib.sha256(b).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    blobs = {}          # name -> bytes
    manifest = {}       # dict
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
