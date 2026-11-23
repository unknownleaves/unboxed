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
    _spawn_detached_ps(script)


def remove_shortcuts_and_entry() -> None:
    """Remove Start Menu/desktop shortcuts and the Settings > Apps entry (Windows)."""
    import ctypes
    from ctypes import wintypes
    import winreg

    def known_folder(csidl):
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
        return buf.value

    for csidl in (0x0002, 0x0010):  # CSIDL_PROGRAMS, CSIDL_DESKTOPDIRECTORY
        try:
            os.remove(os.path.join(known_folder(csidl), "Unboxed.lnk"))
        except OSError:
            pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Unboxed")
    except OSError:
        pass


def schedule_uninstall() -> None:
    """Remove shortcuts, menu entries and the install folder after the app exits."""
    try:
        remove_shortcuts_and_entry()
    except Exception:  # noqa: BLE001  (the script below retries)
        pass
    target = install_dir()
    script = os.path.join(tempfile.gettempdir(), "unboxed-uninstall.ps1")
    # Delete the whole folder only when it is the standard install location.
    whole = os.path.normcase(target).endswith(os.path.normcase(os.path.join("Programs", "Unboxed")))
    items = "@($target)" if whole else "@((Join-Path $target '_internal'), (Join-Path $target 'Unboxed.exe'))"
    remove = f"""
for ($i = 0; $i -lt 30; $i++) {{
    $left = {items} | Where-Object {{ Test-Path -LiteralPath $_ }}
    if (-not $left) {{ Write-Output 'removed install folder'; break }}
    foreach ($p in $left) {{
        try {{ Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction Stop }}
        catch {{ Write-Output "retry $i : $($_.Exception.Message)"; Start-Sleep -Milliseconds 700 }}
    }}
}}"""
    log = os.path.join(tempfile.gettempdir(), "unboxed-uninstall.log")
    ps = f"""
$target = {_ps_quote(target)}
Start-Transcript -Path {_ps_quote(log)} -Force | Out-Null
Write-Output "uninstalling from $target (waiting for pid {os.getpid()})"
Wait-Process -Id {os.getpid()} -Timeout 30 -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 600
foreach ($p in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {{
    Remove-Item -LiteralPath (Join-Path $p 'Unboxed.lnk') -Force -ErrorAction SilentlyContinue
}}
Remove-Item -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Unboxed' -Recurse -Force -ErrorAction SilentlyContinue
{remove}
Stop-Transcript | Out-Null
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
"""
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(ps)
    note = os.path.join(tempfile.gettempdir(), "unboxed-uninstall-app.log")
    with open(note, "w", encoding="utf-8") as f:
        f.write(f"target={target}\nwhole={whole}\nscript={script}\n")
        try:
            p = _spawn_detached_ps(script)
            f.write(f"started powershell pid={p.pid}\n")
        except Exception as ex:  # noqa: BLE001
            f.write(f"FAILED to start powershell: {ex!r}\n")
            raise


def _spawn_detached_ps(script: str) -> subprocess.Popen:
    """Start a hidden PowerShell script that outlives this process."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI", "_MEI", "TCL_", "TK_"))}
    system_ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                             "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    exe = system_ps if os.path.isfile(system_ps) else "powershell.exe"
    flags = 0x00000008 | 0x00000200 | 0x01000000  # DETACHED | NEW_PROCESS_GROUP | BREAKAWAY_FROM_JOB
    args = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-File", script]
    out = open(os.path.splitext(script)[0] + ".out.txt", "w", encoding="utf-8")
    kw = dict(close_fds=True, env=env, cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
              stdout=out, stderr=subprocess.STDOUT)
    try:
        return subprocess.Popen(args, creationflags=flags, **kw)
    except OSError:
        # breakaway not allowed in this job object: start normally
        return subprocess.Popen(args, creationflags=flags & ~0x01000000, **kw)
    finally:
        out.close()
