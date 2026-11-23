"""
Unboxed — a fast, friendly archive manager.

    python unboxed.py                      open the app
    python unboxed.py archive.zip          open an archive
    python unboxed.py --extract-here FILE  one-click smart extract (used by Explorer menu)
    python unboxed.py --compress PATH...   open the "Create archive" window
    python unboxed.py --selftest [FILE]    run the built-in GUI test (used by GitHub Actions)
    Unboxed.exe --uninstall [--quiet]      remove Unboxed (used by Settings > Apps)
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

import unboxed_core as core
import updater
from version import VERSION

APP_NAME = "Unboxed"
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
except Exception:  # pragma: no cover
    TkinterDnD = None
    DND_FILES = None


# --------------------------------------------------------------------------- #
# Platform helpers
# --------------------------------------------------------------------------- #
def resource(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def config_dir() -> str:
    if IS_WIN:
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        d = os.path.join(base, APP_NAME)
    elif IS_MAC:
        d = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
    else:
        d = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "unboxed")
    os.makedirs(d, exist_ok=True)
    return d


def log_path() -> str:
    return os.path.join(config_dir(), "unboxed.log")


def log_error(text: str) -> None:
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] v{VERSION}\n{text}\n")
    except OSError:
        pass


def os_open(path: str) -> None:
    try:
        if IS_WIN:
            os.startfile(path)  # type: ignore[attr-defined]
        elif IS_MAC:
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as ex:  # noqa: BLE001
        messagebox.showerror(APP_NAME, f"Couldn't open:\n{path}\n\n{ex}")


def reveal(path: str) -> None:
    if IS_WIN and os.path.exists(path):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif IS_MAC:
        subprocess.Popen(["open", "-R", path])
    else:
        os_open(path if os.path.isdir(path) else os.path.dirname(path))


def system_prefers_dark() -> bool:
    try:
        if IS_WIN:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        if IS_MAC:
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                               capture_output=True, text=True, timeout=2)
            return "dark" in r.stdout.lower()
    except Exception:  # noqa: BLE001
        pass
    return False


def win_prepare_process() -> None:
    """Crisp text on high-DPI screens + our own taskbar icon."""
    if not IS_WIN:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Unboxed.ArchiveManager.1")
    except Exception:  # noqa: BLE001
        pass


def win_titlebar(win: tk.Misc, dark: bool) -> None:
    if not IS_WIN:
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                break
    except Exception:  # noqa: BLE001
        pass


def app_command() -> str:
    """Command line that launches this app (frozen exe or script)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    py = sys.executable
    if IS_WIN and py.lower().endswith("python.exe"):
        pyw = py[:-10] + "pythonw.exe"
        if os.path.exists(pyw):
            py = pyw
    return f'"{py}" "{os.path.abspath(__file__)}"'


SHELL_EXTS = [".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".txz", ".iso", ".cab", ".jar"]


def win_integrate(enable: bool) -> None:
    import winreg
    cls = r"Software\Classes"
    cmd = app_command()
    icon = sys.executable if getattr(sys, "frozen", False) else resource(os.path.join("assets", "icon.ico"))

    def setkey(path, value=None, **named):
        k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE)
        if value is not None:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, value)
        for n, v in named.items():
            winreg.SetValueEx(k, n, 0, winreg.REG_SZ, v)
        winreg.CloseKey(k)

    def deltree(path):
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS)
        except OSError:
            return
        while True:
            try:
                sub = winreg.EnumKey(k, 0)
            except OSError:
                break
            deltree(path + "\\" + sub)
        winreg.CloseKey(k)
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass

    progid = "Unboxed.Archive"
    if enable:
        setkey(rf"{cls}\{progid}", "Archive")
        setkey(rf"{cls}\{progid}\DefaultIcon", f'"{icon}",0' if icon.endswith(".exe") else icon)
        setkey(rf"{cls}\{progid}\shell\open\command", f'{cmd} "%1"')
        setkey(rf"{cls}\Applications\Unboxed.exe\shell\open\command", f'{cmd} "%1"')
        for ext in SHELL_EXTS:
            k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"{cls}\{ext}\OpenWithProgids", 0, winreg.KEY_WRITE)
            winreg.SetValueEx(k, progid, 0, winreg.REG_SZ, "")
            winreg.CloseKey(k)
            base = rf"{cls}\SystemFileAssociations\{ext}\shell"
            setkey(rf"{base}\Unboxed.1Here", "Extract Here (Unboxed)", Icon=icon)
            setkey(rf"{base}\Unboxed.1Here\command", f'{cmd} --extract-here "%1"')
            setkey(rf"{base}\Unboxed.2Open", "Open with Unboxed", Icon=icon)
            setkey(rf"{base}\Unboxed.2Open\command", f'{cmd} "%1"')
        setkey(rf"{cls}\AllFilesystemObjects\shell\Unboxed.Compress", "Compress with Unboxed…", Icon=icon)
        setkey(rf"{cls}\AllFilesystemObjects\shell\Unboxed.Compress\command", f'{cmd} --compress "%1"')
    else:
        deltree(rf"{cls}\{progid}")
        deltree(rf"{cls}\Applications\Unboxed.exe")
        deltree(rf"{cls}\AllFilesystemObjects\shell\Unboxed.Compress")
        for ext in SHELL_EXTS:
            deltree(rf"{cls}\SystemFileAssociations\{ext}\shell\Unboxed.1Here")
            deltree(rf"{cls}\SystemFileAssociations\{ext}\shell\Unboxed.2Open")
            try:
                k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{cls}\{ext}\OpenWithProgids", 0, winreg.KEY_WRITE)
                winreg.DeleteValue(k, progid)
                winreg.CloseKey(k)
            except OSError:
                pass
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
class Settings(dict):
    DEFAULTS = {"theme": "system", "overwrite": "rename", "open_after": True,
                "recent": [], "last_dir": os.path.expanduser("~"), "geometry": "",
                "create_format": "zip", "create_level": "Normal",
                "auto_update": True, "last_update_check": 0, "skip_version": ""}

    def __init__(self):
        super().__init__(self.DEFAULTS)
        self.path = os.path.join(config_dir(), "settings.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.update(json.load(f))
        except Exception:  # noqa: BLE001
            pass

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self, f, indent=2)
        except OSError:
            pass

    def add_recent(self, path):
        r = [p for p in self["recent"] if p != path]
        self["recent"] = [path] + r[:7]
        self.save()


# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #
PALETTES = {
    "light": dict(bg="#F5F4FA", surface="#FFFFFF", surface2="#F0EEF8", border="#E3E0EF",
                  text="#1B1928", muted="#6E6A84", accent="#5B47F5", accent_hover="#4936E3",
                  accent_text="#FFFFFF", select="#E8E4FF", select_text="#1B1928",
                  hover="#F1EFFA", danger="#D93D42", ok="#23955C", drop="#F8F7FF"),
    "dark": dict(bg="#121118", surface="#1B1A23", surface2="#24232F", border="#2E2C3B",
                 text="#ECEAF6", muted="#9C98B2", accent="#7B6CFF", accent_hover="#8E81FF",
                 accent_text="#FFFFFF", select="#3A3366", select_text="#FFFFFF",
                 hover="#272634", danger="#FF6369", ok="#4CC38A", drop="#1F1E2A"),
}


class FlatButton(tk.Label):
    """A flat, hoverable button that themes properly on every OS."""

    def __init__(self, master, app, text, command, kind="ghost", parent_bg="surface", **kw):
        super().__init__(master, text=text, cursor="hand2", font=app.f_btn,
                         padx=int(14 * app.scale), pady=int(7 * app.scale), **kw)
        self.app, self.command, self.kind, self.parent_bg = app, command, kind, parent_bg
        self.enabled, self.hover = True, False
        self.bind("<Enter>", lambda e: self._hov(True))
        self.bind("<Leave>", lambda e: self._hov(False))
        self.bind("<ButtonRelease-1>", self._click)
        app.buttons.append(self)
        self.refresh()

    def _hov(self, v):
        self.hover = v
        self.refresh()

    def _click(self, _e):
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, v: bool):
        self.enabled = v
        self.refresh()

    def refresh(self):
        p = self.app.p
        if self.kind == "primary":
            bg = p["accent_hover"] if self.hover and self.enabled else p["accent"]
            fg = p["accent_text"]
            if not self.enabled:
                bg, fg = p["surface2"], p["muted"]
        else:
            bg = p["hover"] if self.hover and self.enabled else p[self.parent_bg]
            fg = p["text"] if self.enabled else p["muted"]
        self.configure(bg=bg, fg=fg, cursor="hand2" if self.enabled else "arrow")


# --------------------------------------------------------------------------- #
# Dialog base
# --------------------------------------------------------------------------- #
class Dialog(tk.Toplevel):
    def __init__(self, app, title):
        super().__init__(app.root)
        self.app, self.result = app, None
        self.title(title)
        self.configure(bg=app.p["surface"])
        self.transient(app.root)
        self.resizable(False, False)
        self.body = tk.Frame(self, bg=app.p["surface"], padx=int(22 * app.scale), pady=int(18 * app.scale))
        self.body.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda e: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def label(self, parent, text, muted=False, bold=False, **kw):
        p = self.app.p
        return tk.Label(parent, text=text, bg=p["surface"], fg=p["muted"] if muted else p["text"],
                        font=self.app.f_bold if bold else self.app.f_small if muted else self.app.f_body,
                        anchor="w", justify="left", **kw)

    def buttons(self, ok_text="OK"):
        bar = tk.Frame(self.body, bg=self.app.p["surface"])
        bar.pack(fill="x", pady=(int(18 * self.app.scale), 0))
        FlatButton(bar, self.app, ok_text, self.ok, kind="primary").pack(side="right")
        FlatButton(bar, self.app, "Cancel", self.cancel).pack(side="right", padx=8)
        self.bind("<Return>", lambda e: self.ok())

    def show(self):
        self.update_idletasks()
        r = self.app.root
        x = r.winfo_rootx() + (r.winfo_width() - self.winfo_reqwidth()) // 2
        y = r.winfo_rooty() + (r.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win_titlebar(self, self.app.dark)
        self.grab_set()
        self.focus_set()
        self.wait_window()
        return self.result

    def ok(self):
        self.destroy()

    def cancel(self):
        self.result = None
        self.destroy()


class PasswordDialog(Dialog):
    def __init__(self, app, archive_name, wrong=False):
        super().__init__(app, "Password required")
        self.label(self.body, "Wrong password — try again." if wrong else "This archive is encrypted.",
                   bold=True).pack(fill="x")
        self.label(self.body, f"Enter the password for {archive_name}", muted=True).pack(fill="x", pady=(2, 12))
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self.body, textvariable=self.var, show="•", width=40, font=app.f_body)
        self.entry.pack(fill="x", ipady=4)
        self.showvar = tk.BooleanVar()
        ttk.Checkbutton(self.body, text="Show password", variable=self.showvar,
                        command=lambda: self.entry.configure(show="" if self.showvar.get() else "•")
                        ).pack(anchor="w", pady=(8, 0))
        self.buttons("Unlock")
        self.after(50, self.entry.focus_set)

    def ok(self):
        self.result = self.var.get()
        self.destroy()


class ExtractDialog(Dialog):
    POLICIES = [("Keep both (rename new files)", "rename"),
                ("Replace existing files", "overwrite"),
                ("Skip existing files", "skip")]

    def __init__(self, app, dest, n_selected):
        super().__init__(app, "Extract")
        self.label(self.body, "Extract to", bold=True).pack(fill="x")
        row = tk.Frame(self.body, bg=app.p["surface"])
        row.pack(fill="x", pady=(6, 14))
        self.dest = tk.StringVar(value=dest)
        ttk.Entry(row, textvariable=self.dest, width=56, font=app.f_body).pack(side="left", fill="x",
                                                                              expand=True, ipady=4)
        FlatButton(row, app, "Browse…", self.browse).pack(side="left", padx=(8, 0))

        self.scope = tk.StringVar(value="selected" if n_selected else "all")
        if n_selected:
            self.label(self.body, "What to extract", bold=True).pack(fill="x")
            ttk.Radiobutton(self.body, text=f"Selected items ({n_selected})", value="selected",
                            variable=self.scope).pack(anchor="w", pady=(4, 0))
            ttk.Radiobutton(self.body, text="Everything", value="all",
                            variable=self.scope).pack(anchor="w", pady=(2, 14))

        self.label(self.body, "If a file already exists", bold=True).pack(fill="x")
        self.policy = ttk.Combobox(self.body, state="readonly", values=[p[0] for p in self.POLICIES],
                                   font=app.f_body)
        cur = app.settings["overwrite"]
        self.policy.current(next(i for i, p in enumerate(self.POLICIES) if p[1] == cur))
        self.policy.pack(fill="x", pady=(6, 14), ipady=2)

        self.open_after = tk.BooleanVar(value=app.settings["open_after"])
        ttk.Checkbutton(self.body, text="Open the folder when finished",
                        variable=self.open_after).pack(anchor="w")
        self.buttons("Extract")

    def browse(self):
        d = filedialog.askdirectory(parent=self, initialdir=os.path.dirname(self.dest.get()) or "~",
                                    title="Choose where to extract")
        if d:
            self.dest.set(os.path.normpath(d))

    def ok(self):
        dest = self.dest.get().strip()
        if not dest:
            return
        self.result = dict(dest=dest, scope=self.scope.get(),
                           overwrite=self.POLICIES[self.policy.current()][1],
                           open_after=self.open_after.get())
        self.app.settings["overwrite"] = self.result["overwrite"]
        self.app.settings["open_after"] = self.result["open_after"]
        self.app.settings.save()
        self.destroy()


class CreateDialog(Dialog):
    LEVELS = {"Fastest": 1, "Normal": 6, "Maximum": 9}

    def __init__(self, app, items):
        super().__init__(app, "Create archive")
        p = app.p
        self.items = list(items)
        self.label(self.body, "Files and folders", bold=True).pack(fill="x")
        lf = tk.Frame(self.body, bg=p["border"], padx=1, pady=1)
        lf.pack(fill="x", pady=(6, 6))
        self.lb = tk.Listbox(lf, height=6, width=64, relief="flat", bd=0, highlightthickness=0,
                             bg=p["surface2"], fg=p["text"], selectbackground=p["select"],
                             selectforeground=p["select_text"], font=app.f_body, activestyle="none")
        self.lb.pack(fill="both")
        bar = tk.Frame(self.body, bg=p["surface"])
        bar.pack(fill="x", pady=(0, 14))
        FlatButton(bar, app, "+ Files", self.add_files).pack(side="left")
        FlatButton(bar, app, "+ Folder", self.add_folder).pack(side="left", padx=6)
        FlatButton(bar, app, "Remove", self.remove).pack(side="left")

        grid = tk.Frame(self.body, bg=p["surface"])
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        self.label(grid, "Format").grid(row=0, column=0, sticky="w", pady=4)
        self.fmt = ttk.Combobox(grid, state="readonly", values=core.CREATE_FORMATS, width=12, font=app.f_body)
        fmt = app.settings["create_format"]
        self.fmt.set(fmt if fmt in core.CREATE_FORMATS else "zip")
        self.fmt.grid(row=0, column=1, sticky="w", padx=(12, 0), pady=4)
        self.fmt.bind("<<ComboboxSelected>>", lambda e: self.fix_ext())
        self.label(grid, "Compression").grid(row=1, column=0, sticky="w", pady=4)
        self.level = ttk.Combobox(grid, state="readonly", values=list(self.LEVELS), width=12, font=app.f_body)
        self.level.set(app.settings.get("create_level", "Normal"))
        self.level.grid(row=1, column=1, sticky="w", padx=(12, 0), pady=4)
        self.label(grid, "Password").grid(row=2, column=0, sticky="w", pady=4)
        self.pw = tk.StringVar()
        ttk.Entry(grid, textvariable=self.pw, show="•", width=28, font=app.f_body).grid(
            row=2, column=1, sticky="w", padx=(12, 0), pady=4)
        hint = "Optional. Uses AES-256 encryption." if (core.pyzipper or core.py7zr) else \
            "Optional. Requires the full build (py7zr / pyzipper)."
        self.label(grid, hint, muted=True).grid(row=3, column=1, sticky="w", padx=(12, 0))

        self.label(self.body, "Save as", bold=True).pack(fill="x", pady=(14, 0))
        row = tk.Frame(self.body, bg=p["surface"])
        row.pack(fill="x", pady=(6, 0))
        self.out = tk.StringVar()
        ttk.Entry(row, textvariable=self.out, width=56, font=app.f_body).pack(side="left", fill="x",
                                                                             expand=True, ipady=4)
        FlatButton(row, app, "Browse…", self.browse).pack(side="left", padx=(8, 0))
        self.buttons("Create")
        self.refresh_list()

    def refresh_list(self):
        self.lb.delete(0, "end")
        for it in self.items:
            self.lb.insert("end", ("[folder]  " if os.path.isdir(it) else "") + it)
        if self.items and not self.out.get():
            first = self.items[0]
            base = os.path.basename(first.rstrip("\\/")) if len(self.items) == 1 else "Archive"
            self.out.set(os.path.join(os.path.dirname(first.rstrip("\\/")), base + "." + self.fmt.get()))
        self.fix_ext()

    def fix_ext(self):
        out = self.out.get()
        if not out:
            return
        for f in sorted(core.CREATE_FORMATS + ["7z"], key=len, reverse=True):
            if out.lower().endswith("." + f):
                out = out[: -len(f) - 1]
                break
        self.out.set(out + "." + self.fmt.get())

    def add_files(self):
        fs = filedialog.askopenfilenames(parent=self, title="Add files")
        self.items += [os.path.normpath(f) for f in fs if f not in self.items]
        self.refresh_list()

    def add_folder(self):
        d = filedialog.askdirectory(parent=self, title="Add folder")
        if d and d not in self.items:
            self.items.append(os.path.normpath(d))
            self.refresh_list()

    def remove(self):
        for i in reversed(self.lb.curselection()):
            del self.items[i]
        self.refresh_list()

    def browse(self):
        f = filedialog.asksaveasfilename(parent=self, defaultextension="." + self.fmt.get(),
                                         initialfile=os.path.basename(self.out.get()),
                                         initialdir=os.path.dirname(self.out.get()) or "~")
        if f:
            self.out.set(os.path.normpath(f))
            self.fix_ext()

    def ok(self):
        if not self.items:
            messagebox.showinfo(APP_NAME, "Add at least one file or folder.", parent=self)
            return
        if not self.out.get().strip():
            return
        self.result = dict(items=self.items, fmt=self.fmt.get(), level=self.LEVELS[self.level.get()],
                           password=self.pw.get() or None, out=self.out.get().strip())
        self.app.settings["create_format"] = self.fmt.get()
        self.app.settings["create_level"] = self.level.get()
        self.app.settings.save()
        self.destroy()


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root: tk.Tk, check_updates: bool = True):
        self.root = root
        root.report_callback_exception = self._on_tk_error
        self.settings = Settings()
        self.archive: core.Archive | None = None
        self.meta: dict = {}
        self.busy = False
        self.q: queue.Queue = queue.Queue()
        self.cancel_ev = threading.Event()
        self.temp_dirs: list = []
        self.buttons: list = []
        self.sort_state = ("name", False)

        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.dark = self._want_dark()
        self.p = PALETTES["dark" if self.dark else "light"]
        self._fonts()

        root.title(APP_NAME)
        root.geometry(self.settings["geometry"] or f"{int(1040 * self.scale)}x{int(660 * self.scale)}")
        root.minsize(int(720 * self.scale / 1.2), int(460 * self.scale / 1.2))
        self._set_icon()
        self._build()
        self.apply_theme()
        self._bind_keys()
        self._setup_dnd()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        if IS_MAC:
            root.createcommand("::tk::mac::OpenDocument", lambda *paths: self.open_any(list(paths)))
        root.after(40, self._poll)
        root.after(150, lambda: win_titlebar(root, self.dark))
        self.update_info = None
        if check_updates and self.settings.get("auto_update", True) and \
                time.time() - float(self.settings.get("last_update_check") or 0) > 6 * 3600:
            root.after(2500, lambda: self.check_updates(manual=False))

    def _on_tk_error(self, exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        log_error(text)
        try:
            if self.busy:
                self._finish()
        except Exception:  # noqa: BLE001
            pass
        messagebox.showerror(APP_NAME, f"Something went wrong:\n\n{val}\n\n"
                                       f"Details were saved to:\n{log_path()}", parent=self.root)

    # ---- setup ------------------------------------------------------------ #
    def _want_dark(self):
        t = self.settings["theme"]
        return system_prefers_dark() if t == "system" else t == "dark"

    def _fonts(self):
        fam = "Segoe UI" if IS_WIN else (".AppleSystemUIFont" if IS_MAC else
                                         tkfont.nametofont("TkDefaultFont").actual("family"))
        base = 10 if IS_WIN else 13 if IS_MAC else 10
        self.f_body = (fam, base)
        self.f_small = (fam, base - 1)
        self.f_bold = (fam, base, "bold")
        self.f_btn = (fam, base)
        self.f_title = (fam, base + 3, "bold")
        self.f_hero = (fam, base + 7, "bold")
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            try:
                tkfont.nametofont(name).configure(family=fam, size=base)
            except tk.TclError:
                pass

    def _set_icon(self):
        try:
            if IS_WIN:
                self.root.iconbitmap(default=resource(os.path.join("assets", "icon.ico")))
            self._icon_imgs = [tk.PhotoImage(file=resource(os.path.join("assets", f"icon_{s}.png")))
                               for s in (256, 64, 32, 16)]
            self.root.iconphoto(True, *self._icon_imgs)
        except Exception:  # noqa: BLE001
            self._icon_imgs = []

    def _logo(self, size):
        try:
            return tk.PhotoImage(file=resource(os.path.join("assets", f"icon_{size}.png")))
        except Exception:  # noqa: BLE001
            return None

    def _make_glyphs(self):
        """Folder and file icons drawn in code (themed)."""
        s = max(16, int(16 * self.scale))
        u = s / 16.0
        p = self.p

        def rect(img, color, x0, y0, x1, y1):
            img.put(color, to=(int(x0 * u), int(y0 * u), max(int(x1 * u), int(x0 * u) + 1),
                               max(int(y1 * u), int(y0 * u) + 1)))

        folder = tk.PhotoImage(width=s, height=s)
        rect(folder, "#F2B53A", 1, 3, 7, 5)
        rect(folder, "#F7C548", 1, 5, 15, 14)
        rect(folder, "#E9A82B", 1, 5, 15, 6)
        f = tk.PhotoImage(width=s, height=s)
        rect(f, p["muted"], 3, 1, 13, 15)
        rect(f, p["surface"], 4, 2, 12, 14)
        rect(f, p["accent"], 5, 5, 11, 6)
        rect(f, p["accent"], 5, 8, 11, 9)
        rect(f, p["accent"], 5, 11, 9, 12)
        lock = tk.PhotoImage(width=s, height=s)
        rect(lock, p["muted"], 3, 1, 13, 15)
        rect(lock, p["surface"], 4, 2, 12, 14)
        rect(lock, p["danger"], 5, 7, 11, 12)
        rect(lock, p["danger"], 6, 4, 7, 7)
        rect(lock, p["danger"], 9, 4, 10, 7)
        rect(lock, p["danger"], 6, 4, 10, 5)
        self.img_folder, self.img_file, self.img_lock = folder, f, lock

    def _build(self):
        r = self.root
        sc = self.scale
        # toolbar
        self.toolbar = tk.Frame(r, height=int(56 * sc))
        self.toolbar.pack(side="top", fill="x")
        self.logo_small = self._logo(32)
        self.brand = tk.Label(self.toolbar, image=self.logo_small, text="  " + APP_NAME,
                              compound="left", font=self.f_title, padx=int(14 * sc))
        self.brand.pack(side="left", pady=int(10 * sc))

        self.btn_open = FlatButton(self.toolbar, self, "Open", self.open_dialog)
        self.btn_new = FlatButton(self.toolbar, self, "Create", self.create_dialog)
        self.btn_test = FlatButton(self.toolbar, self, "Test", self.test_archive)
        self.btn_to = FlatButton(self.toolbar, self, "Extract to…", self.extract_dialog)
        self.btn_here = FlatButton(self.toolbar, self, "Extract Here", self.extract_here, kind="primary")
        for b in (self.btn_open, self.btn_new, self.btn_test, self.btn_to):
            b.pack(side="left", padx=2)
        self.btn_here.pack(side="left", padx=(8, 0))

        self.theme_btn = FlatButton(self.toolbar, self, "◐", self.cycle_theme)
        self.theme_btn.pack(side="right", padx=(4, 12))
        self.search_wrap = tk.Frame(self.toolbar, padx=1, pady=1)
        self.search_wrap.pack(side="right", pady=int(12 * sc))
        self.search_var = tk.StringVar()
        self.search = tk.Entry(self.search_wrap, textvariable=self.search_var, width=26, relief="flat",
                               bd=0, font=self.f_body, highlightthickness=0)
        self.search.pack(ipady=int(5 * sc), padx=int(8 * sc))
        self._placeholder = True
        self.search.bind("<FocusIn>", self._search_focus)
        self.search.bind("<FocusOut>", self._search_blur)
        self.search_var.trace_add("write", lambda *a: self._on_search())
        self.sep1 = tk.Frame(r, height=1)
        self.sep1.pack(fill="x")

        # update banner (hidden until an update is found)
        self.banner = tk.Frame(r, padx=int(18 * sc), pady=int(8 * sc))
        self.banner_text = tk.Label(self.banner, font=self.f_body, anchor="w")
        self.banner_text.pack(side="left")
        self.banner_later = FlatButton(self.banner, self, "Later", self.dismiss_update, parent_bg="select")
        self.banner_later.pack(side="right")
        self.banner_go = FlatButton(self.banner, self, "Update now", self.install_update, kind="primary")
        self.banner_go.pack(side="right", padx=(0, 6))

        # info bar
        self.infobar = tk.Frame(r, padx=int(18 * sc), pady=int(10 * sc))
        self.info_title = tk.Label(self.infobar, font=self.f_bold, anchor="w")
        self.info_title.pack(side="left")
        self.info_sub = tk.Label(self.infobar, font=self.f_small, anchor="w")
        self.info_sub.pack(side="left", padx=(12, 0))
        self.btn_close = FlatButton(self.infobar, self, "✕  Close", self.close_archive, parent_bg="bg")
        self.btn_close.pack(side="right")

        # content area
        self.content = tk.Frame(r)
        self.content.pack(fill="both", expand=True)

        # welcome / drop zone
        self.welcome = tk.Frame(self.content)
        self.drop = tk.Canvas(self.welcome, highlightthickness=0, cursor="hand2")
        self.drop.pack(fill="both", expand=True, padx=int(28 * sc), pady=(int(24 * sc), int(8 * sc)))
        self.drop.bind("<Configure>", lambda e: self._draw_drop())
        self.drop.bind("<Button-1>", lambda e: self.open_dialog())
        self.drop_hot = False
        self.logo_big = self._logo(128 if sc > 1.2 else 64)
        self.recent_frame = tk.Frame(self.welcome)
        self.recent_frame.pack(fill="x", padx=int(28 * sc), pady=(0, int(20 * sc)))

        # tree view
        self.treewrap = tk.Frame(self.content)
        cols = ("size", "packed", "modified", "type")
        self.tree = ttk.Treeview(self.treewrap, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Name", anchor="w", command=lambda: self.sort_by("name"))
        self.tree.heading("size", text="Size", anchor="e", command=lambda: self.sort_by("size"))
        self.tree.heading("packed", text="Packed", anchor="e", command=lambda: self.sort_by("packed"))
        self.tree.heading("modified", text="Modified", anchor="w", command=lambda: self.sort_by("modified"))
        self.tree.heading("type", text="Type", anchor="w", command=lambda: self.sort_by("type"))
        self.tree.column("#0", width=int(380 * sc), stretch=True)
        self.tree.column("size", width=int(90 * sc), anchor="e", stretch=False)
        self.tree.column("packed", width=int(90 * sc), anchor="e", stretch=False)
        self.tree.column("modified", width=int(140 * sc), stretch=False)
        self.tree.column("type", width=int(110 * sc), stretch=False)
        vsb = ttk.Scrollbar(self.treewrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Return>", self._on_double)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())
        self.tree.bind("<Button-3>" if not IS_MAC else "<Button-2>", self._context_menu)
        if IS_MAC:
            self.tree.bind("<Control-Button-1>", self._context_menu)

        # progress panel
        self.prog = tk.Frame(r, padx=int(18 * sc), pady=int(10 * sc))
        self.prog_label = tk.Label(self.prog, font=self.f_body, anchor="w")
        self.prog_label.pack(fill="x")
        prow = tk.Frame(self.prog)
        prow.pack(fill="x", pady=(6, 0))
        self.prog_row = prow
        self.pbar = ttk.Progressbar(prow, style="Accent.Horizontal.TProgressbar", mode="determinate",
                                    maximum=1000)
        self.pbar.pack(side="left", fill="x", expand=True)
        self.prog_btn = FlatButton(prow, self, "Cancel", self.cancel_task, parent_bg="surface")
        self.prog_btn.pack(side="right", padx=(12, 0))
        self.prog_detail = tk.Label(self.prog, font=self.f_small, anchor="w")
        self.prog_detail.pack(fill="x", pady=(4, 0))

        # status bar
        self.sep2 = tk.Frame(r, height=1)
        self.sep2.pack(side="bottom", fill="x")
        self.status = tk.Frame(r, padx=int(14 * sc), pady=int(5 * sc))
        self.status.pack(side="bottom", fill="x", before=self.sep2)
        self.status_msg = tk.Label(self.status, font=self.f_small, anchor="w", text="Ready")
        self.status_msg.pack(side="left")
        self.status_link = tk.Label(self.status, font=self.f_small, cursor="hand2")
        self.status_link.pack(side="left", padx=(10, 0))
        self.status_link.bind("<Button-1>", lambda e: self._status_action and self._status_action())
        self._status_action = None
        self.status_ver = tk.Label(self.status, font=self.f_small, text=f"v{VERSION}")
        self.status_ver.pack(side="right")
        # packing order matters: bars first, then the expanding content area
        self.sep2.pack_forget()
        self.status.pack_forget()
        self.content.pack_forget()
        self.status.pack(side="bottom", fill="x")
        self.sep2.pack(side="bottom", fill="x")
        self.content.pack(fill="both", expand=True)

        self._build_menu()
        self.show_welcome()

    def _build_menu(self):
        m = tk.Menu(self.root)
        mod = "Command" if IS_MAC else "Ctrl"
        fm = tk.Menu(m, tearoff=0)
        fm.add_command(label="Open archive…", accelerator=f"{mod}+O", command=self.open_dialog)
        self.recent_menu = tk.Menu(fm, tearoff=0)
        fm.add_cascade(label="Open recent", menu=self.recent_menu)
        fm.add_command(label="Create archive…", accelerator=f"{mod}+N", command=self.create_dialog)
        fm.add_separator()
        fm.add_command(label="Close archive", accelerator=f"{mod}+W", command=self.close_archive)
        if not IS_MAC:
            fm.add_separator()
            fm.add_command(label="Exit", command=self.quit)
        m.add_cascade(label="File", menu=fm)

        am = tk.Menu(m, tearoff=0)
        am.add_command(label="Extract here (smart)", accelerator=f"{mod}+Shift+E", command=self.extract_here)
        am.add_command(label="Extract to…", accelerator=f"{mod}+E", command=self.extract_dialog)
        am.add_command(label="Open selected file", accelerator="Enter", command=self.open_selected)
        am.add_separator()
        am.add_command(label="Test archive integrity", accelerator=f"{mod}+T", command=self.test_archive)
        am.add_command(label="Select all", accelerator=f"{mod}+A", command=self.select_all)
        am.add_command(label="Expand all", command=lambda: self._expand(True))
        am.add_command(label="Collapse all", command=lambda: self._expand(False))
        m.add_cascade(label="Actions", menu=am)

        vm = tk.Menu(m, tearoff=0)
        self.theme_var = tk.StringVar(value=self.settings["theme"])
        for val, lab in (("system", "Match system"), ("light", "Light"), ("dark", "Dark")):
            vm.add_radiobutton(label=lab, value=val, variable=self.theme_var,
                               command=lambda: self.set_theme(self.theme_var.get()))
        m.add_cascade(label="Theme", menu=vm)

        hm = tk.Menu(m, tearoff=0)
        if IS_WIN:
            hm.add_command(label="Add Unboxed to Explorer right-click menu", command=lambda: self.integrate(True))
            hm.add_command(label="Remove from Explorer menu", command=lambda: self.integrate(False))
            hm.add_separator()
        hm.add_command(label="Check for updates…", command=lambda: self.check_updates(manual=True))
        self.auto_upd_var = tk.BooleanVar(value=self.settings.get("auto_update", True))
        hm.add_checkbutton(label="Check for updates automatically", variable=self.auto_upd_var,
                           command=self._toggle_auto_update)
        hm.add_command(label="Open error log", command=self.open_log)
        hm.add_separator()
        hm.add_command(label="Supported formats", command=self.show_formats)
        hm.add_command(label=f"About {APP_NAME}", command=self.about)
        m.add_cascade(label="Help", menu=hm)
        self.root.configure(menu=m)
        self.menus = [m, fm, am, vm, hm, self.recent_menu]
        self._refresh_recent()

    def _bind_keys(self):
        mod = "Command" if IS_MAC else "Control"
        b = self.root.bind
        b(f"<{mod}-o>", lambda e: self.open_dialog())
        b(f"<{mod}-n>", lambda e: self.create_dialog())
        b(f"<{mod}-e>", lambda e: self.extract_dialog())
        b(f"<{mod}-E>", lambda e: self.extract_here())
        b(f"<{mod}-t>", lambda e: self.test_archive())
        b(f"<{mod}-w>", lambda e: self.close_archive())
        b(f"<{mod}-f>", lambda e: self.search.focus_set())
        b(f"<{mod}-a>", lambda e: self.select_all() if self.root.focus_get() is not self.search else None)
        b("<Escape>", lambda e: self._escape())
        b("<F5>", lambda e: self.reload())

    def _setup_dnd(self):
        if TkinterDnD is None:
            return
        try:
            for w in (self.root, self.drop, self.tree):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)
                w.dnd_bind("<<DropEnter>>", lambda e: self._hot(True))
                w.dnd_bind("<<DropLeave>>", lambda e: self._hot(False))
        except Exception:  # noqa: BLE001
            pass

    # ---- theme ------------------------------------------------------------ #
    def set_theme(self, t):
        self.settings["theme"] = t
        self.settings.save()
        self.theme_var.set(t)
        self.dark = self._want_dark()
        self.p = PALETTES["dark" if self.dark else "light"]
        self.apply_theme()
        if self.archive:
            self.populate()

    def cycle_theme(self):
        self.set_theme("light" if self.dark else "dark")

    def apply_theme(self):
        p, sc = self.p, self.scale
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", background=p["surface"], foreground=p["text"], font=self.f_body,
                     bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                     troughcolor=p["surface2"], focuscolor=p["accent"], selectbackground=p["select"],
                     selectforeground=p["select_text"], fieldbackground=p["surface"],
                     insertcolor=p["text"])
        st.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                     foreground=p["text"], rowheight=int(26 * sc), borderwidth=0, font=self.f_body)
        st.map("Treeview", background=[("selected", p["select"])],
               foreground=[("selected", p["select_text"])])
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.configure("Treeview.Heading", background=p["surface2"], foreground=p["muted"],
                     relief="flat", borderwidth=0, font=self.f_small, padding=(int(8 * sc), int(6 * sc)))
        st.map("Treeview.Heading", background=[("active", p["hover"])], foreground=[("active", p["text"])])
        st.configure("Vertical.TScrollbar", background=p["surface2"], troughcolor=p["surface"],
                     bordercolor=p["surface"], arrowcolor=p["muted"], relief="flat",
                     lightcolor=p["surface2"], darkcolor=p["surface2"], gripcount=0)
        st.map("Vertical.TScrollbar", background=[("active", p["border"])])
        st.configure("Accent.Horizontal.TProgressbar", background=p["accent"], troughcolor=p["surface2"],
                     bordercolor=p["surface2"], lightcolor=p["accent"], darkcolor=p["accent"],
                     thickness=int(8 * sc))
        st.configure("TEntry", fieldbackground=p["surface2"], foreground=p["text"], padding=4,
                     bordercolor=p["border"], lightcolor=p["border"], insertcolor=p["text"])
        st.map("TEntry", bordercolor=[("focus", p["accent"])], lightcolor=[("focus", p["accent"])])
        st.configure("TCombobox", fieldbackground=p["surface2"], background=p["surface2"],
                     foreground=p["text"], arrowcolor=p["muted"], padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", p["surface2"])],
               foreground=[("readonly", p["text"])], selectbackground=[("readonly", p["surface2"])],
               selectforeground=[("readonly", p["text"])], bordercolor=[("focus", p["accent"])])
        for w in ("TCheckbutton", "TRadiobutton"):
            st.configure(w, background=p["surface"], foreground=p["text"], indicatorbackground=p["surface2"],
                         indicatorforeground=p["accent"], font=self.f_body, padding=2)
            st.map(w, background=[("active", p["surface"])],
                   indicatorcolor=[("selected", p["accent"]), ("!selected", p["surface2"])])
        self.root.option_add("*TCombobox*Listbox.background", p["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", p["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", p["select"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", p["select_text"])
        self.root.option_add("*Menu.background", p["surface"])
        self.root.option_add("*Menu.foreground", p["text"])

        self.root.configure(bg=p["bg"])
        for w, bg in ((self.toolbar, "surface"), (self.infobar, "bg"), (self.content, "bg"),
                      (self.welcome, "bg"), (self.recent_frame, "bg"), (self.treewrap, "surface"),
                      (self.prog, "surface"), (self.prog_row, "surface"), (self.status, "surface")):
            w.configure(bg=p[bg])
        self.brand.configure(bg=p["surface"], fg=p["text"])
        self.sep1.configure(bg=p["border"])
        self.sep2.configure(bg=p["border"])
        self.search_wrap.configure(bg=p["border"])
        self.search.configure(bg=p["surface2"], fg=p["muted"] if self._placeholder else p["text"],
                              insertbackground=p["text"])
        self.search_wrap.configure(bg=p["surface2"])
        if self._placeholder:
            self._set_placeholder()
        self.banner.configure(bg=p["select"])
        self.banner_text.configure(bg=p["select"], fg=p["select_text"])
        self.info_title.configure(bg=p["bg"], fg=p["text"])
        self.info_sub.configure(bg=p["bg"], fg=p["muted"])
        self.prog_label.configure(bg=p["surface"], fg=p["text"])
        self.prog_detail.configure(bg=p["surface"], fg=p["muted"])
        self.status_msg.configure(bg=p["surface"], fg=p["muted"])
        self.status_link.configure(bg=p["surface"], fg=p["accent"])
        self.status_ver.configure(bg=p["surface"], fg=p["muted"])
        self.drop.configure(bg=p["bg"])
        for m in getattr(self, "menus", []):
            if not IS_MAC:
                m.configure(bg=p["surface"], fg=p["text"], activebackground=p["select"],
                            activeforeground=p["select_text"], bd=0, relief="flat")
        for b in self.buttons:
            try:
                b.refresh()
            except tk.TclError:
                pass
        self._make_glyphs()
        self._draw_drop()
        self._refresh_recent()
        win_titlebar(self.root, self.dark)

    # ---- welcome / drop zone --------------------------------------------- #
    def _draw_drop(self):
        c, p, sc = self.drop, self.p, self.scale
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            return
        r = int(18 * sc)
        pad = 2
        col = p["accent"] if self.drop_hot else p["border"]
        fill = p["select"] if self.drop_hot else p["drop"]
        x0, y0, x1, y1 = pad, pad, w - pad, h - pad
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        c.create_polygon(pts, smooth=True, fill=fill, outline=col, width=max(2, int(2 * sc)),
                         dash=(8, 6) if not self.drop_hot else ())
        cy = h // 2
        if self.logo_big:
            c.create_image(w // 2, cy - int(56 * sc), image=self.logo_big)
        c.create_text(w // 2, cy + int(26 * sc), text="Drop an archive here" if TkinterDnD else
                      "Open an archive to get started", font=self.f_hero, fill=p["text"])
        c.create_text(w // 2, cy + int(56 * sc), text="or click to browse  ·  drop files & folders to compress"
                      if TkinterDnD else "click anywhere here to browse", font=self.f_body, fill=p["muted"])
        c.create_text(w // 2, cy + int(84 * sc), font=self.f_small, fill=p["muted"],
                      text="ZIP · 7Z · RAR · TAR · GZ · BZ2 · XZ · ISO · CAB · JAR and more")

    def _hot(self, v):
        self.drop_hot = v
        self._draw_drop()

    def _refresh_recent(self):
        if not hasattr(self, "recent_frame"):
            return
        p = self.p
        for w in self.recent_frame.winfo_children():
            w.destroy()
        recent = [r for r in self.settings["recent"] if os.path.exists(r)]
        if hasattr(self, "recent_menu"):
            self.recent_menu.delete(0, "end")
            for r in recent:
                self.recent_menu.add_command(label=r, command=lambda r=r: self.open_path(r))
            if not recent:
                self.recent_menu.add_command(label="(none)", state="disabled")
        if not recent:
            return
        tk.Label(self.recent_frame, text="RECENT", font=self.f_small, bg=p["bg"],
                 fg=p["muted"], anchor="w").pack(fill="x", pady=(0, 4))
        for r in recent[:5]:
            row = tk.Frame(self.recent_frame, bg=p["bg"])
            row.pack(fill="x")
            a = tk.Label(row, text=os.path.basename(r), font=self.f_body, bg=p["bg"], fg=p["accent"],
                         cursor="hand2", anchor="w")
            a.pack(side="left")
            tk.Label(row, text="  " + os.path.dirname(r), font=self.f_small, bg=p["bg"],
                     fg=p["muted"], anchor="w").pack(side="left")
            a.bind("<Button-1>", lambda e, r=r: self.open_path(r))

    def show_welcome(self):
        self.treewrap.pack_forget()
        self.infobar.pack_forget()
        self.welcome.pack(fill="both", expand=True)
        self.root.title(APP_NAME)
        self._update_buttons()

    def show_tree(self):
        self.welcome.pack_forget()
        self.infobar.pack(fill="x", before=self.content)
        self.treewrap.pack(fill="both", expand=True)
        self._update_buttons()

    # ---- search ----------------------------------------------------------- #
    def _set_placeholder(self):
        self._placeholder = True
        self.search_var.set("Search in archive…")
        self.search.configure(fg=self.p["muted"])

    def _search_focus(self, _e):
        if self._placeholder:
            self._placeholder = False
            self.search_var.set("")
            self.search.configure(fg=self.p["text"])
        self.search_wrap.configure(bg=self.p["accent"])

    def _search_blur(self, _e):
        self.search_wrap.configure(bg=self.p["surface2"])
        if not self.search_var.get():
            self._set_placeholder()

    def _search_text(self):
        return "" if self._placeholder else self.search_var.get().strip()

    def _on_search(self):
        if self.archive and not self._placeholder:
            if hasattr(self, "_search_job"):
                self.root.after_cancel(self._search_job)
            self._search_job = self.root.after(180, self.populate)

    def _escape(self):
        if self.busy:
            self.cancel_task()
        elif self._search_text():
            self.search_var.set("")
            self.tree.focus_set()

    # ---- buttons ---------------------------------------------------------- #
    def _update_buttons(self):
        has = self.archive is not None and not self.busy
        for b in (self.btn_here, self.btn_to, self.btn_test):
            b.set_enabled(has)
        self.btn_open.set_enabled(not self.busy)
        self.btn_new.set_enabled(not self.busy)
        if has and self.tree.selection():
            self.btn_to.configure(text=f"Extract {len(self.tree.selection())} to…")
        else:
            self.btn_to.configure(text="Extract to…")

    def set_status(self, msg, link_text=None, action=None, error=False):
        self.status_msg.configure(text=msg, fg=self.p["danger"] if error else self.p["muted"])
        self.status_link.configure(text=link_text or "")
        self._status_action = action

    # ---- tree ------------------------------------------------------------- #
    def populate(self):
        arc = self.archive
        if not arc:
            return
        t = self.tree
        t.delete(*t.get_children())
        self.meta = {}
        q = self._search_text().lower()
        dirs = {}

        def ensure_dir(path):
            if not path:
                return ""
            if path in dirs:
                return dirs[path]
            parent, _, name = path.rpartition("/")
            piid = ensure_dir(parent)
            iid = "d:" + path
            t.insert(piid, "end", iid=iid, text="  " + name, image=self.img_folder, open=bool(q))
            dirs[path] = iid
            self.meta[iid] = {"name": name.lower(), "size": 0, "packed": 0, "mtime": None,
                              "type": "", "dir": True, "path": path}
            return iid

        for e in arc.entries:
            path = e.path.rstrip("/")
            if not path:
                continue
            if q and q not in path.lower():
                continue
            if e.is_dir:
                iid = ensure_dir(path)
                if e.mtime:
                    self.meta[iid]["mtime"] = e.mtime
                continue
            parent = path.rpartition("/")[0]
            piid = ensure_dir(parent)
            iid = "f:" + path
            if t.exists(iid):
                continue
            ext = os.path.splitext(e.name)[1][1:].upper()
            typ = f"{ext} file" if ext else "File"
            t.insert(piid, "end", iid=iid, text="  " + e.name,
                     image=self.img_lock if e.encrypted else self.img_file)
            self.meta[iid] = {"name": e.name.lower(), "size": e.size, "packed": e.packed,
                              "mtime": e.mtime, "type": typ, "dir": False, "path": path}
            # roll sizes up
            node = parent
            while node:
                m = self.meta["d:" + node]
                m["size"] += e.size
                m["packed"] += e.packed
                node = node.rpartition("/")[0]

        for iid, m in self.meta.items():
            t.item(iid, values=(core.human_size(m["size"]) if (m["size"] or not m["dir"]) else "",
                                core.human_size(m["packed"]) if m["packed"] else "",
                                m["mtime"].strftime("%Y-%m-%d  %H:%M") if m["mtime"] else "",
                                "Folder" if m["dir"] else m["type"]))
        col, rev = self.sort_state
        self._sort("", col, rev)
        # if the archive is a single folder, open it for convenience
        tops = t.get_children("")
        if len(tops) == 1 and tops[0].startswith("d:"):
            t.item(tops[0], open=True)
        if q:
            n = sum(1 for k in self.meta if k.startswith("f:"))
            self.set_status(f"{n} match{'es' if n != 1 else ''} for “{q}”")

    def _sort(self, parent, col, rev):
        kids = list(self.tree.get_children(parent))

        def key(iid):
            m = self.meta[iid]
            v = m.get(col if col != "modified" else "mtime")
            if col == "modified":
                v = v.timestamp() if v else 0
            if v is None:
                v = ""
            return (not m["dir"]) if not rev else m["dir"], v

        kids.sort(key=key, reverse=rev)
        for i, k in enumerate(kids):
            self.tree.move(k, parent, i)
            if k.startswith("d:"):
                self._sort(k, col, rev)

    def sort_by(self, col):
        cur, rev = self.sort_state
        rev = (not rev) if cur == col else False
        self.sort_state = (col, rev)
        names = {"name": "#0", "size": "size", "packed": "packed", "modified": "modified", "type": "type"}
        labels = {"name": "Name", "size": "Size", "packed": "Packed", "modified": "Modified", "type": "Type"}
        for c, h in names.items():
            arrow = (" ▾" if rev else " ▴") if c == col else ""
            self.tree.heading(h, text=labels[c] + arrow)
        self._sort("", col, rev)

    def _expand(self, v):
        def walk(p):
            for k in self.tree.get_children(p):
                if k.startswith("d:"):
                    self.tree.item(k, open=v)
                    walk(k)
        walk("")

    def select_all(self):
        if not self.archive:
            return
        self.tree.selection_set(self.tree.get_children(""))

    def selected_members(self):
        return [self.meta[i]["path"] for i in self.tree.selection() if i in self.meta]

    def _on_double(self, e):
        iid = self.tree.focus()
        if iid.startswith("f:"):
            self.open_selected()
            return "break"
        return None

    def _context_menu(self, e):
        if not self.archive:
            return
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        p = self.p
        m = tk.Menu(self.root, tearoff=0)
        if not IS_MAC:
            m.configure(bg=p["surface"], fg=p["text"], activebackground=p["select"],
                        activeforeground=p["select_text"], bd=0)
        sel = self.tree.selection()
        if len(sel) == 1 and sel[0].startswith("f:"):
            m.add_command(label="Open", command=self.open_selected)
        if sel:
            m.add_command(label=f"Extract {len(sel)} item{'s' if len(sel) > 1 else ''} to…",
                          command=self.extract_dialog)
            m.add_command(label="Extract to Desktop", command=self.extract_selected_desktop)
            m.add_command(label="Copy path", command=lambda: self._copy(
                "\n".join(self.meta[i]["path"] for i in sel)))
            m.add_separator()
        m.add_command(label="Extract everything here", command=self.extract_here)
        m.add_command(label="Select all", command=self.select_all)
        m.tk_popup(e.x_root, e.y_root)

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("Copied to clipboard")

    # ---- background tasks ------------------------------------------------- #
    def run_task(self, title, work, done=None, fail=None, indeterminate=False):
        if self.busy:
            return
        self.busy = True
        self.cancel_ev = threading.Event()
        self._t0 = time.time()
        self._last_p = None
        self.prog_label.configure(text=title)
        self.prog_detail.configure(text="")
        self.prog.pack(side="bottom", fill="x", before=self.sep2)
        if indeterminate:
            self.pbar.configure(mode="indeterminate")
            self.pbar.start(12)
        else:
            self.pbar.stop()
            self.pbar.configure(mode="determinate", value=0)
        self.root.configure(cursor="watch")
        self._update_buttons()

        def prog(d, total, name):
            self._last_p = (d, total, name)

        def target():
            try:
                self.q.put(("done", work(self.cancel_ev, prog), done))
            except BaseException as ex:  # noqa: BLE001
                self.q.put(("err", ex, fail))

        threading.Thread(target=target, daemon=True).start()

    def _poll(self):
        try:
            p = self._last_p
            if self.busy and p and str(self.pbar.cget("mode")) == "determinate":
                d, total, name = p
                frac = min(1.0, d / total) if total else 0
                self.pbar.configure(value=int(frac * 1000))
                el = max(0.001, time.time() - self._t0)
                speed = d / el
                eta = (total - d) / speed if speed > 0 else 0
                txt = f"{frac * 100:.0f}%  ·  {core.human_size(d)} of {core.human_size(total)}  ·  " \
                      f"{core.human_size(speed)}/s"
                if eta > 1:
                    txt += f"  ·  {int(eta // 60)}:{int(eta % 60):02d} left"
                if name:
                    short = name if len(name) < 70 else "…" + name[-68:]
                    txt += f"\n{short}"
                self.prog_detail.configure(text=txt)
            while True:
                kind, val, cb = self.q.get_nowait()
                self._finish()
                if kind == "done":
                    if cb:
                        cb(val)
                else:
                    if isinstance(val, core.Cancelled):
                        self.set_status("Cancelled")
                    elif cb:
                        cb(val)
                    else:
                        self.error(val)
        except queue.Empty:
            pass
        self.root.after(60, self._poll)

    def _finish(self):
        self.busy = False
        self.pbar.stop()
        self.prog.pack_forget()
        self.root.configure(cursor="")
        self._update_buttons()

    def cancel_task(self):
        if self.busy:
            self.cancel_ev.set()
            self.prog_label.configure(text="Cancelling…")

    def error(self, ex):
        msg = str(ex) or ex.__class__.__name__
        self.set_status(msg.splitlines()[0][:120], error=True)
        messagebox.showerror(APP_NAME, msg, parent=self.root)

    # ---- opening ---------------------------------------------------------- #
    def open_dialog(self):
        if self.busy:
            return
        types = [("Archives", " ".join("*" + e for e in core.SUPPORTED_EXTS)), ("All files", "*.*")]
        f = filedialog.askopenfilename(parent=self.root, title="Open archive",
                                       initialdir=self.settings["last_dir"], filetypes=types)
        if f:
            self.open_path(os.path.normpath(f))

    def open_any(self, paths):
        """Handle drops / OS 'open document' events."""
        paths = [p for p in paths if p]
        if not paths:
            return
        if len(paths) == 1 and os.path.isfile(paths[0]):
            self.open_path(paths[0], fallback_create=True)
        else:
            self.create_dialog(paths)

    def open_path(self, path, password=None, fallback_create=False):
        if self.busy:
            return
        self.settings["last_dir"] = os.path.dirname(path)

        def work(cancel, prog):
            return core.open_archive(path, password)

        def done(arc):
            if self.archive:
                self.archive.close()
            self.archive = arc
            self.settings.add_recent(path)
            self._refresh_recent()
            self.root.title(f"{os.path.basename(path)} — {APP_NAME}")
            self._show_info()
            self.populate()
            self.show_tree()
            self.tree.focus_set()
            self.set_status("Double-click a file to open it  ·  right-click for more options")

        def fail(ex):
            if isinstance(ex, core.PasswordRequired):
                pw = PasswordDialog(self, os.path.basename(path), wrong=password is not None).show()
                if pw is not None:
                    self.open_path(path, pw)
            elif isinstance(ex, core.UnsupportedFormat) and fallback_create and "recognise" in str(ex):
                self.create_dialog([path])
            else:
                self.error(ex)

        self.run_task(f"Opening {os.path.basename(path)}…", work, done, fail, indeterminate=True)

    def _show_info(self):
        arc = self.archive
        files = [e for e in arc.entries if not e.is_dir]
        total = sum(e.size for e in files)
        disk = os.path.getsize(arc.path)
        folders = len({e.path.rstrip("/").rsplit("/", 1)[0] for e in arc.entries if "/" in e.path.rstrip("/")}
                      | {e.path.rstrip("/") for e in arc.entries if e.is_dir})
        parts = [arc.kind, f"{len(files):,} file{'s' if len(files) != 1 else ''}"]
        if folders:
            parts.append(f"{folders:,} folder{'s' if folders != 1 else ''}")
        parts.append(f"{core.human_size(total)} unpacked")
        if total > disk > 0:
            parts.append(f"{100 - disk * 100 / total:.0f}% smaller")
        if arc.encrypted:
            parts.append("🔒 encrypted" if not IS_WIN else "encrypted")
        self.info_title.configure(text=os.path.basename(arc.path))
        self.info_sub.configure(text="  ·  ".join(parts))

    def close_archive(self):
        if self.busy:
            return
        if self.archive:
            self.archive.close()
        self.archive = None
        self.tree.delete(*self.tree.get_children())
        self.show_welcome()
        self.set_status("Ready")

    def reload(self):
        if self.archive and not self.busy:
            self.open_path(self.archive.path, self.archive.password)

    def _on_drop(self, e):
        self._hot(False)
        paths = list(self.root.tk.splitlist(e.data))
        self.open_any([os.path.normpath(p) for p in paths])
        return getattr(e, "action", "copy")

    # ---- password handling ------------------------------------------------ #
    def _ensure_password(self, wrong=False) -> bool:
        arc = self.archive
        pw = PasswordDialog(self, os.path.basename(arc.path), wrong=wrong).show()
        if pw is None:
            return False
        if isinstance(arc, core.ZipArchive):
            arc.password = pw
            arc.zf.setpassword(pw.encode("utf-8"))
        elif isinstance(arc, core.SevenZipArchive):
            try:
                new = core.open_archive(arc.path, pw)
            except core.PasswordRequired:
                return self._ensure_password(wrong=True)
            arc.close()
            self.archive = new
        else:
            arc.password = pw
        return True

    # ---- extraction ------------------------------------------------------- #
    def extract_here(self):
        if not self.archive or self.busy:
            return
        dest = core.smart_destination(self.archive)
        self._extract(dest, None, self.settings["overwrite"], self.settings["open_after"])

    def extract_dialog(self):
        if not self.archive or self.busy:
            return
        sel = self.selected_members()
        default = core.smart_destination(self.archive)
        if sel:
            default = os.path.join(os.path.dirname(self.archive.path), core.archive_stem(self.archive.path))
        r = ExtractDialog(self, default, len(sel)).show()
        if r:
            self._extract(r["dest"], sel if r["scope"] == "selected" else None,
                          r["overwrite"], r["open_after"])

    def extract_selected_desktop(self):
        sel = self.selected_members()
        if not sel:
            return
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        self._extract(desk if os.path.isdir(desk) else os.path.expanduser("~"), sel,
                      self.settings["overwrite"], self.settings["open_after"])

    def _folder_to_show(self, dest, members):
        """The folder the user cares about after extracting."""
        if members is None and os.path.normcase(os.path.abspath(dest)) == \
                os.path.normcase(os.path.dirname(os.path.abspath(self.archive.path))):
            tops = {e.path.split("/", 1)[0] for e in self.archive.entries if e.path}
            if len(tops) == 1:
                return os.path.join(dest, tops.pop())
        return dest

    def _extract(self, dest, members, overwrite, open_after, wrong_pw=False):
        arc = self.archive
        need_pw = arc.encrypted and not arc.password
        if need_pw or wrong_pw:
            if not self._ensure_password(wrong=wrong_pw):
                return
            arc = self.archive

        def work(cancel, prog):
            os.makedirs(dest, exist_ok=True)
            return arc.extract(dest, members, core.Options(overwrite=overwrite, password=arc.password,
                                                           cancel=cancel, progress=prog))

        def done(written):
            show = self._folder_to_show(dest, members)
            n = len(written)
            self.set_status(f"Extracted {n:,} file{'s' if n != 1 else ''} to {show}",
                            "Show folder", lambda: os_open(show) if os.path.isdir(show) else reveal(show))
            if open_after and os.path.isdir(show):
                os_open(show)

        def fail(ex):
            if isinstance(ex, core.PasswordRequired):
                self._extract(dest, members, overwrite, open_after, wrong_pw=True)
            else:
                self.error(ex)

        label = "Extracting" if members is None else f"Extracting {len(members)} item(s)"
        self.run_task(f"{label} to {dest}", work, done, fail)

    def open_selected(self):
        sel = [i for i in self.tree.selection() if i.startswith("f:")]
        if not sel or self.busy:
            return
        path = self.meta[sel[0]]["path"]
        arc = self.archive
        if arc.encrypted and not arc.password and not self._ensure_password():
            return
        arc = self.archive
        tmp = tempfile.mkdtemp(prefix="unboxed-")
        self.temp_dirs.append(tmp)

        def work(cancel, prog):
            return arc.extract(tmp, [path], core.Options(overwrite="overwrite", password=arc.password,
                                                         cancel=cancel, progress=prog))

        def done(written):
            if written:
                os_open(written[0])
                self.set_status(f"Opened {os.path.basename(written[0])} (temporary copy)")

        def fail(ex):
            if isinstance(ex, core.PasswordRequired):
                if self._ensure_password(wrong=True):
                    self.open_selected()
            else:
                self.error(ex)

        self.run_task(f"Opening {os.path.basename(path)}…", work, done, fail)

    # ---- test ------------------------------------------------------------- #
    def test_archive(self, wrong=False):
        if not self.archive or self.busy:
            return
        arc = self.archive
        if (arc.encrypted and not arc.password) or wrong:
            if not self._ensure_password(wrong=wrong):
                return
            arc = self.archive

        def work(cancel, prog):
            return arc.test(core.Options(password=arc.password, cancel=cancel, progress=prog))

        def done(problems):
            if not problems:
                self.set_status("Test passed — no errors found")
                messagebox.showinfo(APP_NAME, f"{os.path.basename(arc.path)}\n\nNo errors found. "
                                              "Every file was read and verified.", parent=self.root)
            else:
                self.set_status(f"Test found {len(problems)} problem(s)", error=True)
                messagebox.showerror(APP_NAME, "Problems found:\n\n" + "\n".join(problems[:20]),
                                     parent=self.root)

        def fail(ex):
            if isinstance(ex, core.PasswordRequired):
                self.test_archive(wrong=True)
            else:
                self.error(ex)

        self.run_task("Testing archive…", work, done, fail)

    # ---- create ----------------------------------------------------------- #
    def create_dialog(self, items=None):
        if self.busy:
            return
        r = CreateDialog(self, items or []).show()
        if not r:
            return
        out = r["out"]
        if os.path.exists(out) and not messagebox.askyesno(
                APP_NAME, f"{os.path.basename(out)} already exists. Replace it?", parent=self.root):
            return

        def work(cancel, prog):
            return core.create_archive(out, r["items"], r["fmt"], core.Options(
                password=r["password"], cancel=cancel, progress=prog), level=r["level"])

        def done(path):
            self.set_status(f"Created {os.path.basename(path)} ({core.human_size(os.path.getsize(path))})",
                            "Show in folder", lambda: reveal(path))

        self.run_task(f"Creating {os.path.basename(out)}…", work, done)

    # ---- misc ------------------------------------------------------------- #
    def integrate(self, enable):
        try:
            win_integrate(enable)
        except Exception as ex:  # noqa: BLE001
            self.error(ex)
            return
        if enable:
            messagebox.showinfo(APP_NAME, "Done! Right-click any archive in File Explorer to see "
                                          "“Extract Here (Unboxed)” and “Open with Unboxed”.\n\n"
                                          "On Windows 11 they appear under “Show more options”.\n\n"
                                          "To make Unboxed the default for .zip files: right-click a "
                                          ".zip › Open with › Choose another app › Unboxed › Always.",
                                parent=self.root)
        else:
            messagebox.showinfo(APP_NAME, "Removed Unboxed from the Explorer menu.", parent=self.root)

    def show_formats(self):
        seven = core.find_7zip()
        messagebox.showinfo(APP_NAME,
                            "Open & extract:\n"
                            "  ZIP, JAR, APK, EPUB, Office files\n"
                            "  TAR, TAR.GZ/TGZ, TAR.BZ2, TAR.XZ, GZ, BZ2, XZ, LZMA\n"
                            f"  7Z {'✓' if core.py7zr or seven else '(needs py7zr or 7-Zip)'}\n"
                            f"  RAR, ISO, CAB, ARJ, LZH, WIM, DMG {'✓ via 7-Zip' if seven else '(needs 7-Zip)'}\n\n"
                            f"Create:\n  {', '.join(f.upper() for f in core.CREATE_FORMATS)}"
                            f"{'  (with AES-256 passwords)' if core.pyzipper or core.py7zr else ''}",
                            parent=self.root)

    def about(self):
        messagebox.showinfo(f"About {APP_NAME}",
                            f"{APP_NAME} {VERSION}\n\nA fast, friendly archive manager.\n\n"
                            f"Free · no nag screens · no trial.\n\ngithub.com/{updater.REPO}", parent=self.root)

    def open_log(self):
        if os.path.exists(log_path()):
            os_open(log_path())
        else:
            messagebox.showinfo(APP_NAME, "No errors have been logged.", parent=self.root)

    # ---- updates ---------------------------------------------------------- #
    def _toggle_auto_update(self):
        self.settings["auto_update"] = bool(self.auto_upd_var.get())
        self.settings.save()

    def check_updates(self, manual=False):
        if self.busy and manual:
            return
        result = {}

        def work():
            try:
                result["info"] = updater.latest_release()
            except Exception as ex:  # noqa: BLE001
                result["error"] = ex

        t = threading.Thread(target=work, daemon=True)

        def wait():
            if t.is_alive():
                self.root.after(150, wait)
                return
            self.settings["last_update_check"] = time.time()
            self.settings.save()
            if "error" in result:
                if manual:
                    self.set_status("")
                    messagebox.showwarning(APP_NAME, "Couldn't check for updates. Check your internet "
                                                     f"connection.\n\n{result['error']}", parent=self.root)
                return
            info = result.get("info")
            if info and updater.is_newer(info["version"]):
                if not manual and info["version"] == self.settings.get("skip_version"):
                    return
                self.update_info = info
                self.banner_text.configure(text=f"Unboxed {info['version']} is available "
                                                f"(you have {VERSION}).")
                self.banner_go.configure(text="Update now" if updater.can_self_update() else "Download")
                self.banner.pack(fill="x", after=self.sep1)
                if manual:
                    self.set_status(f"Update available: {info['version']}")
            elif manual:
                self.set_status("You're up to date")
                messagebox.showinfo(APP_NAME, f"You're up to date.\n\nUnboxed {VERSION} is the latest version.",
                                    parent=self.root)

        t.start()
        if manual:
            self.set_status("Checking for updates…")
        self.root.after(150, wait)

    def dismiss_update(self):
        if self.update_info:
            self.settings["skip_version"] = self.update_info["version"]
            self.settings.save()
        self.banner.pack_forget()

    def install_update(self):
        info = self.update_info
        if not info or self.busy:
            return
        if not updater.can_self_update() or not info.get("url"):
            os_open(info["page"])
            return
        work_dir = tempfile.mkdtemp(prefix="unboxed-update-")
        zip_path = os.path.join(work_dir, updater.ASSET)

        def work(cancel, prog):
            try:
                updater.download(info["url"], zip_path, prog, cancel)
            except InterruptedError as ex:
                raise core.Cancelled("Cancelled") from ex
            return updater.prepare(zip_path)

        def done(stage):
            self.banner.pack_forget()
            try:
                updater.apply_and_restart(stage, info["version"])
            except Exception as ex:  # noqa: BLE001
                self.error(ex)
                return
            self.quit(force=True)

        self.run_task(f"Downloading Unboxed {info['version']}…", work, done)

    def quit(self, force=False):
        if self.busy and not force and not messagebox.askyesno(APP_NAME, "A task is still running. Quit anyway?",
                                                 parent=self.root):
            return
        self.cancel_ev.set()
        try:
            if self.root.state() == "normal":
                self.settings["geometry"] = self.root.geometry().split("+")[0]
        except tk.TclError:
            pass
        self.settings.save()
        if self.archive:
            self.archive.close()
        for d in self.temp_dirs:
            shutil.rmtree(d, ignore_errors=True)
        self.root.destroy()


# --------------------------------------------------------------------------- #
# One-click extract (from the Explorer right-click menu)
# --------------------------------------------------------------------------- #
def quick_extract(root, path):
    app = App(root, check_updates=False)
    root.withdraw()
    win = tk.Toplevel(root)
    p = app.p
    win.title(f"Extracting — {APP_NAME}")
    win.configure(bg=p["surface"])
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", lambda: (app.cancel_ev.set(), root.destroy()))
    frm = tk.Frame(win, bg=p["surface"], padx=int(20 * app.scale), pady=int(16 * app.scale))
    frm.pack(fill="both", expand=True)
    lab = tk.Label(frm, text=f"Opening {os.path.basename(path)}…", bg=p["surface"], fg=p["text"],
                   font=app.f_bold, anchor="w", width=50)
    lab.pack(fill="x")
    bar = ttk.Progressbar(frm, style="Accent.Horizontal.TProgressbar", maximum=1000, mode="indeterminate")
    bar.pack(fill="x", pady=(10, 6))
    bar.start(12)
    det = tk.Label(frm, text="", bg=p["surface"], fg=p["muted"], font=app.f_small, anchor="w")
    det.pack(fill="x")
    cancel = threading.Event()
    state = {"p": None, "result": None}
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"+{(sw - win.winfo_reqwidth()) // 2}+{(sh - win.winfo_reqheight()) // 3}")
    win_titlebar(win, app.dark)

    def ask_pw(name, wrong):
        app.root.deiconify()
        app.root.geometry(f"1x1+{sw // 2}+{sh // 3}")
        pw = PasswordDialog(app, name, wrong).show()
        app.root.withdraw()
        return pw

    def worker(password=None):
        try:
            arc = core.open_archive(path, password)
            if arc.encrypted and not arc.password:
                state["result"] = ("pw", False)
                return
            dest = core.smart_destination(arc)
            state["dest"] = dest
            state["show"] = dest
            tops = {e.path.split("/", 1)[0] for e in arc.entries if e.path}
            if dest == os.path.dirname(os.path.abspath(path)) and len(tops) == 1:
                state["show"] = os.path.join(dest, tops.pop())
            os.makedirs(dest, exist_ok=True)
            arc.extract(dest, None, core.Options(overwrite=app.settings["overwrite"], password=password,
                                                 cancel=cancel,
                                                 progress=lambda d, t, n: state.__setitem__("p", (d, t))))
            arc.close()
            state["result"] = ("ok", None)
        except core.PasswordRequired:
            state["result"] = ("pw", password is not None)
        except core.Cancelled:
            state["result"] = ("cancel", None)
        except Exception as ex:  # noqa: BLE001
            state["result"] = ("err", ex)

    def start(pw=None):
        state["result"] = None
        threading.Thread(target=worker, args=(pw,), daemon=True).start()

    def tick():
        if state["p"]:
            d, t = state["p"]
            if str(bar.cget("mode")) != "determinate":
                bar.stop()
                bar.configure(mode="determinate")
                lab.configure(text=f"Extracting {os.path.basename(path)}")
            bar.configure(value=int(1000 * d / max(t, 1)))
            det.configure(text=f"{d * 100 // max(t, 1)}%  ·  {core.human_size(d)} of {core.human_size(t)}")
        res = state["result"]
        if res:
            kind, val = res
            if kind == "pw":
                pw = ask_pw(os.path.basename(path), val)
                if pw is None:
                    root.destroy()
                    return
                start(pw)
            elif kind == "ok":
                if app.settings["open_after"]:
                    os_open(state["show"])
                root.destroy()
                return
            elif kind == "err":
                messagebox.showerror(APP_NAME, str(val), parent=win)
                root.destroy()
                return
            else:
                root.destroy()
                return
        root.after(80, tick)

    FlatButton(frm, app, "Cancel", lambda: cancel.set()).pack(anchor="e", pady=(10, 0))
    start()
    root.after(80, tick)


# --------------------------------------------------------------------------- #
def uninstall(quiet: bool) -> None:
    r = None
    if not quiet:
        r = tk.Tk()
        r.withdraw()
        if not messagebox.askyesno(APP_NAME, "Remove Unboxed from this computer?\n\n"
                                             "Your files and archives are not affected."):
            r.destroy()
            return
    if IS_WIN:
        try:
            win_integrate(False)
        except Exception as ex:  # noqa: BLE001
            log_error(f"uninstall: {ex}")
        if getattr(sys, "frozen", False):
            updater.schedule_uninstall()
    shutil.rmtree(config_dir(), ignore_errors=True)
    if r is not None:
        messagebox.showinfo(APP_NAME, "Unboxed has been removed.")
        r.destroy()


def selftest(out_file):
    """Drive the real interface end to end. Exit code 0 = pass."""
    lines, failures = [], []

    def log(msg):
        lines.append(str(msg))
        try:
            print(msg, flush=True)
        except Exception:  # noqa: BLE001  (no console in the windowed build)
            pass

    def arg1(a):
        return str(a[1] if len(a) > 1 else a)[:80]

    tmp = tempfile.mkdtemp(prefix="unboxed-selftest-")
    os.environ["APPDATA"] = os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
    messagebox.showerror = lambda *a, **k: failures.append("error dialog: " + arg1(a))
    messagebox.showwarning = lambda *a, **k: log("warning dialog: " + arg1(a))
    messagebox.showinfo = lambda *a, **k: log("info dialog: " + arg1(a).replace("\n", " "))
    messagebox.askyesno = lambda *a, **k: True
    win_prepare_process()
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    app = App(root, check_updates=False)

    def tk_err(exc, val, tb):
        failures.append("".join(traceback.format_exception(exc, val, tb)))
    root.report_callback_exception = tk_err
    app.settings["open_after"] = False
    log(f"Unboxed {VERSION} selftest | python {sys.version.split()[0]} | Tk {tk.TkVersion} | "
        f"frozen={getattr(sys, 'frozen', False)} | dnd={TkinterDnD is not None} | "
        f"py7zr={core.py7zr is not None} | pyzipper={core.pyzipper is not None} | 7zip={core.find_7zip()}")

    import zipfile
    src = os.path.join(tmp, "src", "project")
    os.makedirs(os.path.join(src, "src", "lib"))
    for rel, data in (("readme.txt", b"hello" * 2000), ("src/main.py", b"print(1)"),
                      ("src/lib/util.py", b"x = 1"), ("src/lib/ünïcødé.txt", b"u")):
        with open(os.path.join(src, *rel.split("/")), "wb") as f:
            f.write(data)
    demo = os.path.join(tmp, "demo.zip")
    with zipfile.ZipFile(demo, "w", zipfile.ZIP_DEFLATED) as z:
        for root_dir, _, files in os.walk(src):
            for fn in files:
                p = os.path.join(root_dir, fn)
                z.write(p, os.path.relpath(p, os.path.dirname(src)))

    def check(cond, what):
        if not cond:
            raise AssertionError(what)
        log(f"  ok  {what}")

    def steps():
        app.open_path(demo)
        yield
        check(app.archive is not None, "open zip")
        check(app.tree.get_children("") == ("d:project",), "folder tree built")
        for col in ("size", "modified", "type", "packed", "name"):
            app.sort_by(col)
        check(True, "sorting")
        app._search_focus(None)
        app.search_var.set("util")
        app.populate()
        check([k for k in app.meta if k.startswith("f:")] == ["f:project/src/lib/util.py"], "search")
        app.search_var.set("")
        app._set_placeholder()
        app.populate()
        app.tree.selection_set(["d:project/src"])
        app._update_buttons()
        check(app.selected_members() == ["project/src"], "selection")
        app.extract_here()
        yield
        check(os.path.isfile(os.path.join(tmp, "project", "src", "lib", "util.py")), "smart extract here")
        app._extract(os.path.join(tmp, "picked"), app.selected_members(), "rename", False)
        yield
        check(os.listdir(os.path.join(tmp, "picked")) == ["src"], "extract selected folder")
        app.test_archive()
        yield
        check(not failures, "test integrity")
        for d in (PasswordDialog(app, "x.zip"), ExtractDialog(app, tmp, 2), CreateDialog(app, [src])):
            d.update()
            d.destroy()
        check(True, "dialogs open")
        app.banner_text.configure(text="Unboxed 9.9.9 is available")
        app.banner.pack(fill="x", after=app.sep1)
        root.update()
        app.banner.pack_forget()
        check(True, "update banner")
        app.cycle_theme()
        root.update()
        app.cycle_theme()
        root.update()
        check(True, "light/dark theme")
        for fmt in core.CREATE_FORMATS:
            out = os.path.join(tmp, f"made.{fmt}")
            core.create_archive(out, [src], fmt, core.Options())
            app.open_path(out)
            yield
            check(app.archive is not None and len(app.archive.entries) >= 4, f"create + open {fmt}")
        if core.pyzipper:
            out = os.path.join(tmp, "secret.zip")
            core.create_archive(out, [src], "zip", core.Options(password="pw123"))
            a = core.open_archive(out, "pw123")
            check(a.encrypted, "AES zip detected as encrypted")
            a.extract(os.path.join(tmp, "dec"), None, core.Options(password="pw123"))
            check(os.path.isfile(os.path.join(tmp, "dec", "project", "readme.txt")), "AES zip password")
            a.close()
        if core.py7zr:
            out = os.path.join(tmp, "secret.7z")
            core.create_archive(out, [src], "7z", core.Options(password="pw123"))
            try:
                core.open_archive(out).close()
                raise AssertionError("7z opened without password")
            except core.PasswordRequired:
                pass
            a = core.open_archive(out, "pw123")
            a.extract(os.path.join(tmp, "dec7"), None, core.Options(password="pw123"))
            check(os.path.isfile(os.path.join(tmp, "dec7", "project", "readme.txt")), "7z password")
            a.close()
        if core.find_7zip():
            a = core.CliArchive(demo)
            a.list()
            a.extract(os.path.join(tmp, "cli"), None, core.Options())
            check(os.path.isfile(os.path.join(tmp, "cli", "project", "readme.txt")), "7-Zip engine (RAR/ISO)")
        app.close_archive()
        check(updater.is_newer("v1.10.2", "1.9.9") and not updater.is_newer("1.0.0", "1.0.0"),
              "version compare")

    gen = steps()
    state = {"code": None}

    def finish(code):
        if state["code"] is not None:
            return
        state["code"] = code
        for f in failures:
            log("FAIL " + f)
        log("SELFTEST PASSED" if code == 0 else f"SELFTEST FAILED (code {code})")
        if out_file:
            with open(out_file, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        root.after(10, root.destroy)

    def tick():
        if state["code"] is not None:
            return
        if failures:
            finish(1)
            return
        if app.busy:
            root.after(50, tick)
            return
        try:
            next(gen)
        except StopIteration:
            finish(1 if failures else 0)
            return
        except Exception:  # noqa: BLE001
            failures.append(traceback.format_exc())
            finish(1)
            return
        root.after(50, tick)

    def timeout():
        failures.append("timed out")
        finish(2)

    root.after(300, tick)
    root.after(120000, timeout)
    root.mainloop()
    shutil.rmtree(tmp, ignore_errors=True)
    return state["code"] if state["code"] is not None else 3


def main():
    args = sys.argv[1:]
    if args and args[0] in ("--register", "--unregister"):  # used by install.ps1
        if IS_WIN:
            win_integrate(args[0] == "--register")
        return 0
    if args and args[0] == "--uninstall":
        uninstall("--quiet" in args)
        return 0
    if args and args[0] == "--selftest":
        return selftest(args[1] if len(args) > 1 else None)
    win_prepare_process()
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    if args and args[0] == "--extract-here" and len(args) > 1:
        quick_extract(root, args[1])
    else:
        flag = args[0] if args else ""
        app = App(root, check_updates=flag not in ("--updated", "--update-failed"))
        if flag == "--updated":
            app.set_status(f"Updated to Unboxed {VERSION}")
        elif flag == "--update-failed":
            root.after(300, lambda: messagebox.showerror(
                APP_NAME, "The update couldn't be installed. Run the install command from "
                          f"github.com/{updater.REPO} to reinstall.", parent=root))
        elif flag == "--compress":
            root.after(200, lambda: app.create_dialog([os.path.abspath(a) for a in args[1:]]))
        elif args:
            root.after(100, lambda: app.open_any([os.path.abspath(a) for a in args]))
    root.mainloop()
    return 0


def run():
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        text = traceback.format_exc()
        log_error(text)
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror(APP_NAME, f"Unboxed couldn't start.\n\n{text[-900:]}\n\n"
                                           f"Saved to {log_path()}")
            r.destroy()
        except Exception:  # noqa: BLE001
            pass
        code = 1
    sys.exit(code or 0)


if __name__ == "__main__":
    run()
