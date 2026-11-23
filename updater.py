"""
Unboxed self-updater.

Updates are published as GitHub Releases on REPO. Each release carries a
Windows build named ASSET (a zip of the app folder). The app checks the
latest release, downloads it, and hands off to a small PowerShell script
that waits for the app to close, swaps the files and restarts it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from typing import Callable, Optional

from version import VERSION

REPO = "unknownleaves/unboxed"
ASSET = "Unboxed-windows-x64.zip"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
UA = {"User-Agent": f"Unboxed-Updater/{VERSION}", "Accept": "application/vnd.github+json"}


def parse_version(v: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def is_newer(remote: str, local: str = VERSION) -> bool:
    return parse_version(remote) > parse_version(local)


def latest_release(timeout: float = 8.0) -> Optional[dict]:
    """Return info on the newest release, or None if there isn't one."""
    req = urllib.request.Request(API_LATEST, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            return None
        raise
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET), None)
    return {
        "version": data.get("tag_name", "").lstrip("vV"),
        "notes": (data.get("body") or "").strip(),
        "page": data.get("html_url") or RELEASES_PAGE,
        "url": asset["browser_download_url"] if asset else None,
        "size": asset.get("size", 0) if asset else 0,
    }


def install_dir() -> str:
    return os.path.dirname(os.path.abspath(sys.executable))


def can_self_update() -> bool:
    """Only an installed Windows build (Unboxed.exe + _internal folder) can swap itself."""
    if not (getattr(sys, "frozen", False) and sys.platform.startswith("win")):
        return False
    d = install_dir()
    return os.path.isfile(os.path.join(d, "Unboxed.exe")) and os.path.isdir(os.path.join(d, "_internal"))


def download(url: str, dest: str, progress: Optional[Callable[[int, int, str], None]] = None,
             cancel: Optional[threading.Event] = None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Cancelled")
            buf = r.read(256 * 1024)
            if not buf:
                break
            f.write(buf)
            done += len(buf)
            if progress:
                progress(done, total or done, "Downloading update")
    return dest


def prepare(zip_path: str) -> str:
    """Unpack a downloaded update and check it looks like a real build."""
    stage = os.path.join(os.path.dirname(zip_path), "new")
    shutil.rmtree(stage, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/"):
                raise ValueError("Update package contains an unsafe path")
        z.extractall(stage)
    if not os.path.isfile(os.path.join(stage, "Unboxed.exe")) or \
            not os.path.isdir(os.path.join(stage, "_internal")):
        raise ValueError("Update package is incomplete")
    return stage


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def apply_and_restart(stage: str, new_version: str) -> None:
    """Launch the swap script, detached. The caller must then exit the app."""
    target = install_dir()
    work = os.path.dirname(stage)
    script = os.path.join(work, "apply-update.ps1")
    log = os.path.join(tempfile.gettempdir(), "unboxed-update.log")
    ps = f"""
$ErrorActionPreference = 'Continue'
$stage = {_ps_quote(stage)}
$target = {_ps_quote(target)}
Start-Transcript -Path {_ps_quote(log)} -Force | Out-Null
Wait-Process -Id {os.getpid()} -Timeout 60 -ErrorAction SilentlyContinue
Get-Process -Name Unboxed -ErrorAction SilentlyContinue |
    Where-Object {{ $_.Path -and $_.Path.StartsWith($target, [StringComparison]::OrdinalIgnoreCase) }} |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400
# Only touch files that belong to Unboxed: the _internal folder and the exe.
robocopy (Join-Path $stage '_internal') (Join-Path $target '_internal') /MIR /R:10 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
$ok = $LASTEXITCODE -lt 8
for ($i = 0; $i -lt 20; $i++) {{
    try {{ Copy-Item (Join-Path $stage 'Unboxed.exe') (Join-Path $target 'Unboxed.exe') -Force -ErrorAction Stop; break }}
    catch {{ Start-Sleep -Milliseconds 500; if ($i -eq 19) {{ $ok = $false }} }}
}}
if ($ok) {{
    Set-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Unboxed' `
        -Name DisplayVersion -Value {_ps_quote(new_version)} -ErrorAction SilentlyContinue
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    Start-Process -FilePath (Join-Path $target 'Unboxed.exe') -ArgumentList '--updated'
}} else {{
    Start-Process -FilePath (Join-Path $target 'Unboxed.exe') -ArgumentList '--update-failed'
}}
Stop-Transcript | Out-Null
Remove-Item -LiteralPath {_ps_quote(work)} -Recurse -Force -ErrorAction SilentlyContinue
"""
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(ps)
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-WindowStyle", "Hidden", "-File", script],
                     creationflags=flags, close_fds=True, env=env, cwd=tempfile.gettempdir())


def schedule_uninstall() -> None:
    """Remove shortcuts, menu entries and the install folder after the app exits."""
    target = install_dir()
    script = os.path.join(tempfile.gettempdir(), "unboxed-uninstall.ps1")
    # Delete the whole folder only when it is the standard install location.
    whole = os.path.normcase(target).endswith(os.path.normcase(os.path.join("Programs", "Unboxed")))
    remove = (f"Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue" if whole else
              "Remove-Item -LiteralPath (Join-Path $target '_internal') -Recurse -Force -ErrorAction SilentlyContinue\n"
              "Remove-Item -LiteralPath (Join-Path $target 'Unboxed.exe') -Force -ErrorAction SilentlyContinue")
    ps = f"""
$target = {_ps_quote(target)}
Wait-Process -Id {os.getpid()} -Timeout 30 -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400
foreach ($p in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {{
    Remove-Item -LiteralPath (Join-Path $p 'Unboxed.lnk') -Force -ErrorAction SilentlyContinue
}}
Remove-Item -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Unboxed' -Recurse -Force -ErrorAction SilentlyContinue
{remove}
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"""
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(ps)
    flags = 0x00000008 | 0x00000200 | 0x08000000
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-WindowStyle", "Hidden", "-File", script],
                     creationflags=flags, close_fds=True, cwd=tempfile.gettempdir())
