"""The Windows version resource every PyInstaller exe here carries.

Why it exists: an unsigned executable with an empty Details tab (no product,
no company, no version) is one of the signals antivirus heuristics score, and
the Omni Executor setup file was being reported as a trojan on VirusTotal. The
resource costs nothing and makes the binary say what it is.

The version comes from updates.APP_VERSION -- the one number the release
tooling already checks (scripts/push-app.mjs refuses a mismatch) -- so the file
properties can never disagree with what the app reports about itself.
"""
import os
import re

COMPANY = "Omni Executor"
PRODUCT = "Omni Executor"
COPYRIGHT = "Copyright (c) 2026 Omni Executor"


def app_version(project_dir):
    text = open(os.path.join(project_dir, "updates.py"), encoding="utf-8-sig").read()
    m = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        raise SystemExit("updates.py has no APP_VERSION")
    return m.group(1)


def version_resource(project_dir, filename, description):
    """A PyInstaller VSVersionInfo for `filename`."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo)

    version = app_version(project_dir)
    parts = [int(p) for p in re.findall(r"\d+", version)[:4]]
    parts += [0] * (4 - len(parts))
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=tuple(parts), prodvers=tuple(parts),
                          mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                          subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", COMPANY),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", version),
                StringStruct("InternalName", os.path.splitext(filename)[0]),
                StringStruct("LegalCopyright", COPYRIGHT),
                StringStruct("OriginalFilename", filename),
                StringStruct("ProductName", PRODUCT),
                StringStruct("ProductVersion", version),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ])
