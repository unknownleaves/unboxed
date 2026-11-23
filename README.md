<p align="center"><img src="assets/icon_128.png" width="96" alt="Unboxed icon"></p>
<h1 align="center">Unboxed</h1>
<p align="center">A fast, friendly archive extractor for Windows. Free, no nag screens.</p>

## Install

Open **PowerShell** (press Start, type *PowerShell*, press Enter) and paste:

```powershell
irm https://raw.githubusercontent.com/unknownleaves/unboxed/main/install.ps1 | iex
```

That's it: no admin rights needed. Unboxed appears in the Start Menu and on your desktop, and **Extract Here (Unboxed)** is added to the right-click menu of archives (on Windows 11 it's under *Show more options*).

- **Update:** Unboxed checks for updates by itself and shows an **Update now** button. You can also use *Help › Check for updates…* or run the install command again.
- **Uninstall:** Settings › Apps › Unboxed › Uninstall.

## Features

- **Smart Extract Here.** If the archive has one top folder, it's extracted as-is. If it's loose files, they go in a new folder. No mess on your desktop, no `folder/folder/` double nesting.
- **Opens everything:** ZIP, 7Z, **RAR**, TAR, GZ, BZ2, XZ, ISO, CAB, JAR and more (RAR/ISO/CAB use the built-in 7-Zip engine).
- **Creates** ZIP, 7Z, TAR.GZ and TAR.XZ, with optional AES-256 passwords.
- **Light and dark mode**, sharp on high-resolution screens.
- **Drag & drop** archives to open them; drop files or folders to compress them.
- **Instant search** inside archives, and **double-click any file to open it** without extracting everything.
- **Live progress** with speed, time left and a Cancel button that works.
- **Safe:** blocks archives that try to write files outside the folder you chose.

### Keyboard shortcuts

| Keys | Action |
|---|---|
| Ctrl+O | Open archive |
| Ctrl+Shift+E | Extract here |
| Ctrl+E | Extract to… |
| Ctrl+N | Create archive |
| Ctrl+F | Search |
| Ctrl+T | Test archive |
| Enter / double-click | Open file |
| Esc | Clear search / cancel |

## Publishing an update

1. Change the code.
2. Raise the number in [`version.py`](version.py), e.g. `1.1.0` → `1.2.0`.
3. Push to `main`.

GitHub Actions then builds the app on Windows, runs the full app self-test and an install/uninstall test, and publishes a release. Everyone's Unboxed shows **Update now** within a few hours, or right away from *Help › Check for updates…*. If a test fails, nothing is released; see the **Actions** tab for why.

## Run from source

```
pip install -r requirements.txt
python unboxed.py
python -m unittest discover tests      # engine tests
python unboxed.py --selftest           # drives the real window end to end
```

If something goes wrong, *Help › Open error log* shows what happened.

## Project layout

```
unboxed.py          the app window
unboxed_core.py     archive engine (no GUI code)
updater.py          checks GitHub Releases and installs updates
version.py          the current version - bump it to release
install.ps1         the one-line installer
assets/             app icon
.github/workflows/  build, test and release on Windows
```

RAR, ISO and CAB support uses [7-Zip](https://www.7-zip.org) by Igor Pavlov (GNU LGPL + unRAR restriction); its license is included in the app.
