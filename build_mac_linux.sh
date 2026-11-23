#!/usr/bin/env bash
# Builds Unboxed on macOS (Unboxed.app) or Linux (binary + menu entry).
# macOS: needs Python from python.org (includes Tk).  Linux: sudo apt install python3-tk python3-venv
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt pyinstaller pillow

ARGS=(--noconfirm --clean --windowed --name Unboxed --icon assets/icon.png
      --add-data "assets:assets" --collect-all tkinterdnd2 --collect-all py7zr --hidden-import pyzipper)
if command -v 7zz >/dev/null || command -v 7z >/dev/null; then
  mkdir -p bin; cp "$(command -v 7zz || command -v 7z)" bin/7zz
  ARGS+=(--add-binary "bin/7zz:bin")
fi
pyinstaller "${ARGS[@]}" unboxed.py

if [[ "$(uname)" == "Darwin" ]]; then
  echo "Built dist/Unboxed.app - drag it into your Applications folder."
  open dist
else
  mkdir -p ~/.local/share/applications ~/.local/share/icons/hicolor/256x256/apps ~/.local/bin
  cp -r dist/Unboxed ~/.local/share/unboxed-app 2>/dev/null || { rm -rf ~/.local/share/unboxed-app; cp -r dist/Unboxed ~/.local/share/unboxed-app; }
  ln -sf ~/.local/share/unboxed-app/Unboxed ~/.local/bin/unboxed
  cp assets/icon.png ~/.local/share/icons/hicolor/256x256/apps/unboxed.png
  cat > ~/.local/share/applications/unboxed.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Unboxed
Comment=Open and extract archives
Exec=$HOME/.local/share/unboxed-app/Unboxed %F
Icon=unboxed
Terminal=false
Categories=Utility;Archiving;Compression;
MimeType=application/zip;application/x-7z-compressed;application/vnd.rar;application/x-rar;application/x-tar;application/gzip;application/x-compressed-tar;application/x-bzip2;application/x-xz;application/x-iso9660-image;
EOF
  update-desktop-database ~/.local/share/applications 2>/dev/null || true
  echo "Installed! Find 'Unboxed' in your app menu, or run: unboxed"
fi
