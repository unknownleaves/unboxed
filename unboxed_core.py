"""
Unboxed - archive engine.

Handles listing, testing, extracting and creating archives with:
  * progress reporting (bytes based) and cancellation
  * protection against "zip slip" / path traversal and absolute paths
  * password support (zip, 7z, rar)
  * smart extraction (no "tarbomb" folders, no double-nested folders)

Formats (built in):     zip, jar, apk, docx/xlsx/pptx, tar, tar.gz/tgz,
                        tar.bz2/tbz2, tar.xz/txz, gz, bz2, xz, lzma
Formats (optional):     7z (py7zr), AES-encrypted zip (pyzipper)
Formats (via 7-Zip):    rar, iso, cab, arj, lzh, wim, dmg, ... anything 7-Zip
                        understands, if 7-Zip is installed or bundled.

This module has no GUI dependencies so it can be tested and reused.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, List, Optional

try:  # optional: 7z support
    import py7zr  # type: ignore
except Exception:  # pragma: no cover
    py7zr = None

try:  # optional: AES zip support
    import pyzipper  # type: ignore
except Exception:  # pragma: no cover
    pyzipper = None

CHUNK = 1024 * 256


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ArchiveError(Exception):
    """Generic, user-presentable archive error."""


class PasswordRequired(ArchiveError):
    """Archive is encrypted and no (or a wrong) password was given."""


class Cancelled(ArchiveError):
    """Operation cancelled by the user."""


class UnsupportedFormat(ArchiveError):
    pass


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Entry:
    path: str                 # always forward slashes, no leading slash
    size: int = 0
    packed: int = 0
    is_dir: bool = False
    mtime: Optional[datetime] = None
    encrypted: bool = False

    @property
    def name(self) -> str:
        return self.path.rstrip("/").rsplit("/", 1)[-1]


ProgressFn = Callable[[int, int, str], None]   # (done_bytes, total_bytes, current_name)


@dataclass
class Options:
    overwrite: str = "rename"     # "overwrite" | "skip" | "rename"
    password: Optional[str] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: Optional[ProgressFn] = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def human_size(n: Optional[int]) -> str:
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def normalize_member(name: str) -> str:
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def safe_join(dest: str, member: str) -> str:
    """Join member path to dest, refusing anything that escapes dest."""
    member = normalize_member(member)
    parts = [p for p in member.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or (parts and len(parts[0]) == 2 and parts[0][1] == ":"):
        raise ArchiveError(f"Blocked unsafe path in archive: {member}")
    # strip characters Windows can't use in file names
    if os.name == "nt":
        bad = '<>:"|?*'
        parts = ["".join("_" if c in bad or ord(c) < 32 else c for c in p).rstrip(" .") or "_"
                 for p in parts]
    target = os.path.abspath(os.path.join(dest, *parts)) if parts else os.path.abspath(dest)
    root = os.path.abspath(dest)
    if os.path.commonpath([root, target]) != root:
        raise ArchiveError(f"Blocked unsafe path in archive: {member}")
    return target


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    if base.endswith(".tar"):  # foo.tar.gz -> foo (2).tar.gz
        base, ext = base[:-4], ".tar" + ext
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def resolve_target(path: str, policy: str) -> Optional[str]:
    """Apply overwrite policy. Returns None if file should be skipped."""
    if not os.path.exists(path):
        return path
    if policy == "skip":
        return None
    if policy == "overwrite":
        if os.path.isdir(path) and not os.path.islink(path):
            return path
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        return path
    return unique_path(path)


def archive_stem(path: str) -> str:
    name = os.path.basename(path)
    low = name.lower()
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.lzma", ".tar.zst"):
        if low.endswith(ext):
            return name[: -len(ext)]
    # multi-part rar: foo.part1.rar -> foo
    stem = os.path.splitext(name)[0]
    if stem.lower().rsplit(".", 1)[-1].startswith("part") and "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return stem or name


def _copy_stream(src, dst, opts: Options, state: dict, name: str) -> None:
    while True:
        if opts.cancel.is_set():
            raise Cancelled("Cancelled")
        buf = src.read(CHUNK)
        if not buf:
            break
        dst.write(buf)
        state["done"] += len(buf)
        if opts.progress:
            opts.progress(state["done"], state["total"], name)


def _set_mtime(path: str, mtime: Optional[datetime]) -> None:
    if mtime:
        try:
            ts = mtime.timestamp()
            os.utime(path, (ts, ts))
        except (OSError, ValueError, OverflowError):
            pass


# --------------------------------------------------------------------------- #
# 7-Zip discovery (for rar/iso/cab/... and as a fallback)
# --------------------------------------------------------------------------- #
def find_7zip() -> Optional[str]:
    roots = [getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(sys.executable)),
             os.path.dirname(os.path.abspath(__file__))]
    candidates = []
    for here in filter(None, roots):
        candidates += [os.path.join(here, "bin", "7z.exe"), os.path.join(here, "bin", "7zz"),
                       os.path.join(here, "7z.exe")]
    if os.name == "nt":
        for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = os.environ.get(env)
            if base:
                candidates.append(os.path.join(base, "7-Zip", "7z.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    for exe in ("7z", "7zz", "7za"):
        p = shutil.which(exe)
        if p:
            return p
    return None


def _run_hidden(args: List[str], **kw) -> subprocess.Popen:
    if os.name == "nt":
        kw["creationflags"] = kw.get("creationflags", 0) | 0x08000000  # CREATE_NO_WINDOW
    return subprocess.Popen(args, **kw)


# --------------------------------------------------------------------------- #
# Archive back-ends
# --------------------------------------------------------------------------- #
class Archive:
    kind = "archive"
    can_extract_selected = True

    def __init__(self, path: str, password: Optional[str] = None):
        self.path = path
        self.password = password
        self.entries: List[Entry] = []

    # -- interface --------------------------------------------------------- #
    def list(self) -> List[Entry]:
        raise NotImplementedError

    def extract(self, dest: str, members: Optional[Iterable[str]], opts: Options) -> List[str]:
        raise NotImplementedError

    def test(self, opts: Options) -> List[str]:
        """Read every file fully. Returns list of problems (empty == OK)."""
        tmp_null = _NullSink()
        problems: List[str] = []
        files = [e for e in self.entries if not e.is_dir]
        state = {"done": 0, "total": sum(e.size for e in files) or 1}
        for e in files:
            try:
                with self.open_member(e.path) as f:
                    _copy_stream(f, tmp_null, opts, state, e.path)
            except (Cancelled, PasswordRequired):
                raise
            except Exception as ex:  # noqa: BLE001
                problems.append(f"{e.path}: {ex}")
        return problems

    def open_member(self, name: str):
        raise NotImplementedError

    @property
    def encrypted(self) -> bool:
        return any(e.encrypted for e in self.entries)

    def close(self) -> None:
        pass

    # -- shared helpers ---------------------------------------------------- #
    def _select(self, members: Optional[Iterable[str]]) -> List[Entry]:
        if members is None:
            return list(self.entries)
        wanted = [normalize_member(m).rstrip("/") for m in members]
        out = []
        for e in self.entries:
            p = e.path.rstrip("/")
            if any(p == w or p.startswith(w + "/") for w in wanted):
                out.append(e)
        return out

    @staticmethod
    def _strip_prefix(path: str, prefix: str) -> str:
        """When extracting a selected sub-folder, drop its parents."""
        if prefix and path.startswith(prefix):
            return path[len(prefix):]
        return path


class _NullSink:
    def write(self, b):
        return len(b)


def _common_parent(members: Optional[Iterable[str]]) -> str:
    """For selected items, return the parent folder they share ('' if root)."""
    if not members:
        return ""
    paths = [normalize_member(m).rstrip("/") for m in members]
    parents = {p.rsplit("/", 1)[0] + "/" if "/" in p else "" for p in paths}
    return parents.pop() if len(parents) == 1 else ""


# ---------------------------- ZIP ------------------------------------------ #
class ZipArchive(Archive):
    kind = "ZIP"

    def __init__(self, path, password=None):
        super().__init__(path, password)
        mod = pyzipper if pyzipper else zipfile
        try:
            self.zf = mod.AESZipFile(path) if pyzipper else zipfile.ZipFile(path)
        except Exception as ex:  # zipfile.BadZipFile / pyzipper.BadZipFile / OSError
            raise ArchiveError(f"Not a valid ZIP file ({ex})") from ex
        if password:
            self.zf.setpassword(password.encode("utf-8"))

    def list(self):
        out = []
        for i in self.zf.infolist():
            name = i.filename
            # Fix names that were stored in a local code page without the UTF-8 flag
            if not (i.flag_bits & 0x800):
                try:
                    name = name.encode("cp437").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            try:
                mt = datetime(*i.date_time)
            except ValueError:
                mt = None
            out.append(Entry(normalize_member(name), i.file_size, i.compress_size,
                             i.is_dir(), mt, bool(i.flag_bits & 0x1)))
            out[-1]._info = i  # type: ignore[attr-defined]
        self.entries = out
        return out

    def open_member(self, name):
        info = next(e for e in self.entries if e.path == name)._info  # type: ignore
        return self._open(info)

    def _open(self, info):
        try:
            return self.zf.open(info)
        except RuntimeError as ex:  # stdlib: "password required" / "Bad password"
            raise PasswordRequired(str(ex)) from ex
        except NotImplementedError as ex:
            raise ArchiveError(
                "This ZIP uses AES encryption or an unusual compression method. "
                "Install 'pyzipper' (included in the built app) or 7-Zip to open it.") from ex

    def extract(self, dest, members, opts):
        sel = self._select(members)
        prefix = _common_parent(members)
        state = {"done": 0, "total": sum(e.size for e in sel if not e.is_dir) or 1}
        written = []
        for e in sel:
            if opts.cancel.is_set():
                raise Cancelled("Cancelled")
            rel = self._strip_prefix(e.path, prefix)
            if not rel.strip("/"):
                continue
            target = safe_join(dest, rel)
            if e.is_dir:
                os.makedirs(target, exist_ok=True)
                continue
            target = resolve_target(target, opts.overwrite)
            if target is None:
                state["done"] += e.size
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            info = e._info  # type: ignore[attr-defined]
            # symlinks stored by unix zip tools: write as plain file (safe)
            try:
                with self._open(info) as src, open(target, "wb") as dst:
                    _copy_stream(src, dst, opts, state, e.path)
            except (zipfile.BadZipFile, lzma.LZMAError, OSError) as ex:
                if "password" in str(ex).lower():
                    raise PasswordRequired(str(ex)) from ex
                raise ArchiveError(f"Failed on {e.path}: {ex}") from ex
            except Exception:
                if os.path.exists(target):
                    try:
                        os.remove(target)
                    except OSError:
                        pass
                raise
            _set_mtime(target, e.mtime)
            written.append(target)
        return written

    def test(self, opts):
        if self.encrypted and not self.password:
            raise PasswordRequired("Password required")
        return super().test(opts)

    def close(self):
        self.zf.close()


# ---------------------------- TAR ------------------------------------------ #
class TarArchive(Archive):
    kind = "TAR"

    def __init__(self, path, password=None):
        super().__init__(path, password)
        try:
            self.tf = tarfile.open(path, "r:*")
        except tarfile.TarError as ex:
            raise ArchiveError(f"Not a valid TAR archive ({ex})") from ex
        low = path.lower()
        self.kind = ("TAR.GZ" if low.endswith(("gz", "tgz")) else
                     "TAR.BZ2" if low.endswith(("bz2", "tbz", "tbz2")) else
                     "TAR.XZ" if low.endswith(("xz", "txz")) else "TAR")

    def list(self):
        out = []
        for m in self.tf.getmembers():
            if not (m.isfile() or m.isdir()):
                continue  # skip links/devices: safer
            e = Entry(normalize_member(m.name), m.size, m.size, m.isdir(),
                      datetime.fromtimestamp(m.mtime) if m.mtime else None)
            e._info = m  # type: ignore[attr-defined]
            out.append(e)
        self.entries = [e for e in out if e.path]
        return self.entries

    def open_member(self, name):
        m = next(e for e in self.entries if e.path == name)._info  # type: ignore
        f = self.tf.extractfile(m)
        if f is None:
            raise ArchiveError(f"Cannot read {name}")
        return f

    def extract(self, dest, members, opts):
        sel = self._select(members)
        prefix = _common_parent(members)
        state = {"done": 0, "total": sum(e.size for e in sel if not e.is_dir) or 1}
        written = []
        # Sequential access is much faster for compressed tars: walk in archive order.
        for e in sel:
            if opts.cancel.is_set():
                raise Cancelled("Cancelled")
            rel = self._strip_prefix(e.path, prefix)
            if not rel.strip("/"):
                continue
            target = safe_join(dest, rel)
            if e.is_dir:
                os.makedirs(target, exist_ok=True)
                continue
            target = resolve_target(target, opts.overwrite)
            if target is None:
                state["done"] += e.size
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with self.open_member(e.path) as src, open(target, "wb") as dst:
                _copy_stream(src, dst, opts, state, e.path)
            _set_mtime(target, e.mtime)
            written.append(target)
        return written

    def close(self):
        self.tf.close()


# ------------------------- single-file compressors ------------------------- #
class SingleFileArchive(Archive):
    """.gz / .bz2 / .xz / .lzma containing one file."""

    OPENERS = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open, ".lzma": lzma.open}

    def __init__(self, path, password=None):
        super().__init__(path, password)
        self.ext = os.path.splitext(path)[1].lower()
        self.opener = self.OPENERS[self.ext]
        self.kind = self.ext[1:].upper()

    def list(self):
        name = os.path.basename(self.path)[: -len(self.ext)] or "file"
        size = 0
        packed = os.path.getsize(self.path)
        if self.ext == ".gz" and packed >= 4:  # ISIZE trailer (mod 2^32)
            with open(self.path, "rb") as f:
                f.seek(-4, 2)
                size = int.from_bytes(f.read(4), "little")
        self.entries = [Entry(name, size or packed, packed, False,
                              datetime.fromtimestamp(os.path.getmtime(self.path)))]
        return self.entries

    def open_member(self, name):
        return self.opener(self.path, "rb")

    def extract(self, dest, members, opts):
        e = self.entries[0]
        target = resolve_target(safe_join(dest, e.path), opts.overwrite)
        if target is None:
            return []
        os.makedirs(os.path.dirname(target), exist_ok=True)
        state = {"done": 0, "total": e.size or 1}
        try:
            with self.opener(self.path, "rb") as src, open(target, "wb") as dst:
                _copy_stream(src, dst, opts, state, e.path)
        except (OSError, EOFError, lzma.LZMAError) as ex:
            raise ArchiveError(f"Corrupt or unsupported data: {ex}") from ex
        return [target]


# ----------------------------- 7z (py7zr) ---------------------------------- #
class SevenZipArchive(Archive):
    kind = "7Z"

    def __init__(self, path, password=None):
        super().__init__(path, password)
        if py7zr is None:
            raise UnsupportedFormat("py7zr not installed")
        try:
            self.z = py7zr.SevenZipFile(path, "r", password=password)
        except py7zr.exceptions.PasswordRequired as ex:
            raise PasswordRequired("Password required") from ex
        except Exception as ex:  # noqa: BLE001
            raise ArchiveError(f"Not a valid 7z archive ({ex})") from ex

    def _protected(self) -> bool:
        try:
            return bool(self.z.needs_password())
        except AttributeError:
            return bool(getattr(self.z, "password_protected", False))

    def list(self):
        out = []
        for i in self.z.list():
            out.append(Entry(normalize_member(i.filename), i.uncompressed or 0,
                             i.compressed or 0, i.is_directory,
                             i.creationtime if isinstance(i.creationtime, datetime) else None,
                             self._protected()))
        self.entries = out
        return out

    def extract(self, dest, members, opts):
        sel = self._select(members)
        prefix = _common_parent(members)
        total = sum(e.size for e in sel if not e.is_dir) or 1
        names = [e.path for e in sel]
        # py7zr writes directly; extract to a staging dir then move (applies safety + policy)
        stage = os.path.join(dest, f".unboxed-tmp-{os.getpid()}-{int(time.time())}")
        os.makedirs(stage, exist_ok=True)
        done = {"v": 0}
        z = self.z
        stop = threading.Event()

        def watcher():
            while not stop.wait(0.25):
                d = 0
                for root, _, files in os.walk(stage):
                    for f in files:
                        try:
                            d += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                done["v"] = d
                if opts.progress:
                    opts.progress(min(d, total), total, "")

        t = threading.Thread(target=watcher, daemon=True)
        t.start()
        try:
            for n in names:
                safe_join(stage, n)  # validate first
            try:
                z.reset()
            except Exception:  # noqa: BLE001
                pass
            if members is None:
                z.extractall(path=stage)
            else:
                z.extract(path=stage, targets=names)
        except Exception as ex:  # noqa: BLE001
            stop.set()
            shutil.rmtree(stage, ignore_errors=True)
            msg = str(ex).lower()
            if "password" in msg or "crc" in msg and self.encrypted:
                raise PasswordRequired(str(ex)) from ex
            raise ArchiveError(str(ex)) from ex
        finally:
            stop.set()
        try:
            return _move_tree(stage, dest, prefix, opts)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def open_member(self, name):
        import io
        import tempfile
        tmp = tempfile.mkdtemp(prefix="unboxed-read-")
        try:
            self.extract(tmp, [name], Options(overwrite="overwrite"))
            with open(safe_join(tmp, name.rsplit("/", 1)[-1]), "rb") as f:
                return io.BytesIO(f.read())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test(self, opts):
        try:
            self.z.reset()
            bad = self.z.testzip()
        except Exception as ex:  # noqa: BLE001
            if "password" in str(ex).lower():
                raise PasswordRequired(str(ex)) from ex
            return [str(ex)]
        return [f"{bad}: CRC error"] if bad else []

    def close(self):
        self.z.close()


def _move_tree(stage: str, dest: str, prefix: str, opts: Options) -> List[str]:
    written = []
    src_root = os.path.join(stage, *prefix.strip("/").split("/")) if prefix.strip("/") else stage
    for root, dirs, files in os.walk(src_root):
        rel_root = os.path.relpath(root, src_root)
        rel_root = "" if rel_root == "." else rel_root.replace(os.sep, "/")
        for d in dirs:
            os.makedirs(safe_join(dest, f"{rel_root}/{d}" if rel_root else d), exist_ok=True)
        for f in files:
            rel = f"{rel_root}/{f}" if rel_root else f
            target = resolve_target(safe_join(dest, rel), opts.overwrite)
            if target is None:
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.exists(target) and not os.path.isdir(target):
                os.remove(target)
            shutil.move(os.path.join(root, f), target)
            written.append(target)
    return written


# ----------------------------- 7-Zip CLI ----------------------------------- #
class CliArchive(Archive):
    """Anything 7-Zip can read: rar, rar5, iso, cab, arj, lzh, wim, dmg, ..."""

    def __init__(self, path, password=None):
        super().__init__(path, password)
        self.exe = find_7zip()
        if not self.exe:
            raise UnsupportedFormat(
                "This format (e.g. RAR) needs 7-Zip. Install the free 7-Zip from 7-zip.org "
                "or place 7z.exe + 7z.dll in Unboxed's 'bin' folder, then try again.")
        self.kind = os.path.splitext(path)[1][1:].upper() or "ARCHIVE"

    def _pw(self):
        return [f"-p{self.password or ''}"]

    def list(self):
        p = _run_hidden([self.exe, "l", "-slt", "-sccUTF-8", *self._pw(), "--", self.path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        out, err = p.communicate()
        text = out.decode("utf-8", "replace")
        errs = (err.decode("utf-8", "replace") + text).lower()
        if p.returncode != 0:
            if "wrong password" in errs or "can not open encrypted" in errs or "enter password" in errs:
                raise PasswordRequired("Password required")
            raise ArchiveError(err.decode("utf-8", "replace").strip() or "7-Zip could not open file")
        entries, block = [], {}
        body = text.split("----------", 1)[-1]
        for line in body.splitlines() + [""]:
            if not line.strip():
                if "Path" in block:
                    mt = None
                    try:
                        mt = datetime.strptime(block.get("Modified", "")[:19], "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        pass
                    entries.append(Entry(
                        normalize_member(block["Path"]),
                        int(block.get("Size") or 0), int(block.get("Packed Size") or 0),
                        "D" in block.get("Attributes", "") or block.get("Folder") == "+",
                        mt, block.get("Encrypted") == "+"))
                block = {}
                continue
            if " = " in line:
                k, v = line.split(" = ", 1)
                block[k.strip()] = v.strip()
        self.entries = entries
        return entries

    def extract(self, dest, members, opts):
        sel = self._select(members)
        prefix = _common_parent(members)
        total = sum(e.size for e in sel if not e.is_dir) or 1
        for e in sel:
            safe_join(dest, e.path)
        stage = os.path.join(dest, f".unboxed-tmp-{os.getpid()}-{int(time.time())}")
        os.makedirs(stage, exist_ok=True)
        args = [self.exe, "x", "-y", "-bsp1", "-bb0", "-sccUTF-8", *self._pw(), f"-o{stage}", "--", self.path]
        if members is not None:
            args += [e.path for e in sel if not e.is_dir] or [e.path for e in sel]
        p = _run_hidden(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        buf = b""
        try:
            while True:
                if opts.cancel.is_set():
                    p.kill()
                    raise Cancelled("Cancelled")
                ch = p.stdout.read(64)
                if not ch:
                    break
                buf = (buf + ch)[-256:]
                txt = buf.decode("utf-8", "replace")
                import re
                m = re.findall(r"(\d+)%", txt)
                if m and opts.progress:
                    opts.progress(int(total * int(m[-1]) / 100), total, "")
            p.wait()
            err = p.stderr.read().decode("utf-8", "replace")
            if p.returncode not in (0, 1):
                if "wrong password" in err.lower():
                    raise PasswordRequired("Wrong password")
                raise ArchiveError(err.strip() or f"7-Zip failed (code {p.returncode})")
            return _move_tree(stage, dest, prefix, opts)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def open_member(self, name):
        import io
        p = _run_hidden([self.exe, "e", "-so", *self._pw(), "--", self.path, name],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        data, err = p.communicate()
        if p.returncode != 0:
            raise ArchiveError(err.decode("utf-8", "replace").strip())
        return io.BytesIO(data)

    def test(self, opts):
        p = _run_hidden([self.exe, "t", *self._pw(), "--", self.path],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        out, err = p.communicate()
        if p.returncode == 0:
            return []
        msg = (err or out).decode("utf-8", "replace")
        if "wrong password" in msg.lower():
            raise PasswordRequired("Wrong password")
        return [line for line in msg.splitlines() if line.strip()][-5:] or ["Archive has errors"]


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
ZIP_EXTS = (".zip", ".jar", ".apk", ".docx", ".xlsx", ".pptx", ".odt", ".epub", ".whl", ".cbz", ".xpi")
TAR_EXTS = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tbz2", ".tar.xz", ".txz", ".tar.lzma")
CLI_EXTS = (".rar", ".cbr", ".iso", ".cab", ".arj", ".lzh", ".lha", ".wim", ".dmg", ".z", ".cpio",
            ".rpm", ".deb", ".msi", ".vhd", ".xar", ".zst", ".tar.zst")
SUPPORTED_EXTS = ZIP_EXTS + TAR_EXTS + (".7z", ".gz", ".bz2", ".xz", ".lzma") + CLI_EXTS


def _sniff(path: str) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(262)
    except OSError as ex:
        raise ArchiveError(f"Cannot read file: {ex}") from ex
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:7] == b"Rar!\x1a\x07\x00" or head[:8] == b"Rar!\x1a\x07\x01\x00":
        return "cli"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    if head[:2] == b"\x1f\x8b" or head[:3] == b"BZh" or head[:6] == b"\xfd7zXZ\x00":
        try:
            with tarfile.open(path, "r:*") as t:
                t.next()
            return "tar"
        except Exception:  # noqa: BLE001
            return "single"
    return ""


def open_archive(path: str, password: Optional[str] = None) -> Archive:
    """Open and list an archive. Raises ArchiveError subclasses on failure."""
    if not os.path.isfile(path):
        raise ArchiveError("File not found")
    kind = _sniff(path)
    low = path.lower()
    arc: Archive
    if kind == "zip":
        arc = ZipArchive(path, password)
    elif kind == "tar":
        arc = TarArchive(path, password)
    elif kind == "7z":
        if py7zr is not None:
            arc = SevenZipArchive(path, password)
        else:
            arc = CliArchive(path, password)
    elif kind == "single" and os.path.splitext(low)[1] in SingleFileArchive.OPENERS:
        arc = SingleFileArchive(path, password)
    elif kind == "cli" or low.endswith(CLI_EXTS) or find_7zip():
        arc = CliArchive(path, password)
    else:
        raise UnsupportedFormat("This file isn't an archive Unboxed recognises.")
    arc.list()
    return arc


def smart_destination(archive: Archive, base_dir: Optional[str] = None) -> str:
    """
    Pick a sensible output folder:
      * archive has exactly one top-level folder -> extract into base_dir
        (it will create that folder itself, no double nesting)
      * otherwise -> base_dir/<archive name>/
    """
    base_dir = base_dir or os.path.dirname(os.path.abspath(archive.path))
    tops = {e.path.split("/", 1)[0] for e in archive.entries if e.path}
    single_folder = False
    if len(tops) == 1:
        top = next(iter(tops))
        single_folder = any(e.path.startswith(top + "/") or (e.is_dir and e.path.rstrip("/") == top)
                            for e in archive.entries)
        # never "merge" into a folder that already exists next to the archive
        if single_folder and os.path.exists(os.path.join(base_dir, top)):
            single_folder = False
    if single_folder or len(archive.entries) == 1 and isinstance(archive, SingleFileArchive):
        return base_dir
    return unique_path(os.path.join(base_dir, archive_stem(archive.path)))


# --------------------------------------------------------------------------- #
# Creating archives
# --------------------------------------------------------------------------- #
CREATE_FORMATS = ["zip", "tar.gz", "tar.xz"] + (["7z"] if py7zr else [])


def _walk_inputs(inputs: List[str]):
    """Yield (abs_path, arcname) for every file and dir."""
    for inp in inputs:
        inp = os.path.abspath(inp)
        base = os.path.dirname(inp)
        if os.path.isdir(inp):
            yield inp, os.path.relpath(inp, base).replace(os.sep, "/") + "/"
            for root, dirs, files in os.walk(inp):
                dirs.sort()
                for d in dirs:
                    p = os.path.join(root, d)
                    yield p, os.path.relpath(p, base).replace(os.sep, "/") + "/"
                for f in sorted(files):
                    p = os.path.join(root, f)
                    yield p, os.path.relpath(p, base).replace(os.sep, "/")
        else:
            yield inp, os.path.basename(inp)


def create_archive(out_path: str, inputs: List[str], fmt: str, opts: Options,
                   level: int = 6) -> str:
    items = list(_walk_inputs(inputs))
    total = sum(os.path.getsize(p) for p, a in items if not a.endswith("/")) or 1
    state = {"done": 0, "total": total}
    tmp = out_path + ".part"
    try:
        if fmt == "zip":
            if opts.password and pyzipper:
                zf = pyzipper.AESZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED,
                                         compresslevel=level, encryption=pyzipper.WZ_AES)
                zf.setpassword(opts.password.encode())
            else:
                if opts.password:
                    raise ArchiveError("Password-protected ZIPs need 'pyzipper' "
                                       "(included in the built app). Or choose 7z.")
                zf = zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level)
            with zf:
                for p, arc in items:
                    if arc.endswith("/"):
                        zf.writestr(zipfile.ZipInfo.from_file(p, arc), b"")
                        continue
                    zi = zipfile.ZipInfo.from_file(p, arc)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    with open(p, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                        _copy_stream(src, dst, opts, state, arc)
        elif fmt in ("tar.gz", "tar.xz"):
            mode = "w:gz" if fmt == "tar.gz" else "w:xz"
            kw = {"compresslevel": level} if fmt == "tar.gz" else {"preset": min(level, 9)}
            with tarfile.open(tmp, mode, **kw) as tf:
                for p, arc in items:
                    ti = tf.gettarinfo(p, arc.rstrip("/"))
                    if ti.isreg():
                        with open(p, "rb") as src:
                            reader = _ProgressReader(src, opts, state, arc)
                            tf.addfile(ti, reader)
                    elif ti.isdir():
                        tf.addfile(ti)
        elif fmt == "7z":
            if py7zr is None:
                raise UnsupportedFormat("7z creation needs py7zr")
            with py7zr.SevenZipFile(tmp, "w", password=opts.password or None,
                                    header_encryption=bool(opts.password)) as z:
                for p, arc in items:
                    if opts.cancel.is_set():
                        raise Cancelled("Cancelled")
                    if arc.endswith("/"):
                        z.write(p, arc.rstrip("/"))
                    else:
                        z.write(p, arc)
                        state["done"] += os.path.getsize(p)
                        if opts.progress:
                            opts.progress(state["done"], total, arc)
        else:
            raise UnsupportedFormat(fmt)
        if os.path.exists(out_path):
            os.remove(out_path)
        os.replace(tmp, out_path)
        return out_path
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class _ProgressReader:
    def __init__(self, f, opts, state, name):
        self.f, self.opts, self.state, self.name = f, opts, state, name

    def read(self, n=-1):
        if self.opts.cancel.is_set():
            raise Cancelled("Cancelled")
        b = self.f.read(n)
        self.state["done"] += len(b)
        if self.opts.progress:
            self.opts.progress(self.state["done"], self.state["total"], self.name)
        return b
