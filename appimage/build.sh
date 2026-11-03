#!/usr/bin/env bash
# Build imagesearcher-x86_64.AppImage: a portable CPython (python-build-standalone) + PySide6 from pip (its
# wheels carry Qt, WebP and the built-in browser) + the app, packed by the official appimagetool.
# Runs on any glibc Linux (CI: ubuntu); on NixOS run it inside `steam-run`.
# Usage: appimage/build.sh            -> imagesearcher-x86_64.AppImage in the repo root
set -euo pipefail
cd "$(dirname "$0")/.."
BUILD=build/appimage
APPDIR=$BUILD/AppDir
PY_SERIES=${PY_SERIES:-3.12}
mkdir -p "$BUILD"

# 1. portable Python: the newest python-build-standalone release for PY_SERIES (ponytail: newest at build time,
#    pin PY_URL to a fixed asset if builds must be reproducible)
if [ -z "${PY_URL:-}" ]; then
  PY_URL=$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest |
    grep -o "https://[^\"]*cpython-${PY_SERIES}\.[0-9]*+[0-9]*-x86_64-unknown-linux-gnu-install_only\.tar\.gz" | head -1)
fi
echo "python: $PY_URL"
[ -f "$BUILD/python.tar.gz" ] || curl -fL -o "$BUILD/python.tar.gz" "$PY_URL"

# 2. appimagetool (official, AppImage/appimagetool)
TOOL=$BUILD/appimagetool
[ -x "$TOOL" ] || { curl -fL -o "$TOOL" https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage; chmod +x "$TOOL"; }

# 3. the AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/app"
tar -xzf "$BUILD/python.tar.gz" -C "$APPDIR/usr"      # -> usr/python
PY="$APPDIR/usr/python/bin/python3"
"$PY" -m pip install --no-cache-dir --disable-pip-version-check --no-warn-script-location ${PYSIDE:-PySide6}
# drop what the app never loads (QML, headers, dev tools): about a third of PySide6's size
SITE=$("$PY" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
rm -rf "$SITE"/PySide6/{qml,include,typesystems,glue,scripts,examples,doc} \
       "$SITE"/PySide6/{designer,assistant,linguist,lupdate,lrelease,qmlls,qmllint,qmlformat,qmltyperegistrar,uic,rcc} 2>/dev/null || true
find "$APPDIR/usr/python" -name "__pycache__" -prune -exec rm -rf {} +
cp -r imagesearcher.py imagesearcher_app README.md "$APPDIR/usr/app/"
rm -rf "$APPDIR/usr/app/imagesearcher_app/__pycache__"

cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/sh
# The app inside the AppImage; settings, history and downloads go to the usual places in your home folder.
HERE="$(dirname "$(readlink -f "$0")")"
export PYTHONNOUSERSITE=1
exec "$HERE/usr/python/bin/python3" "$HERE/usr/app/imagesearcher.py" "$@"
RUN
chmod +x "$APPDIR/AppRun"
cat > "$APPDIR/imagesearcher.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=Image Searcher
Comment=Boorus, Pixiv, Pawchive, X and imageboard browser and downloader
Exec=imagesearcher
Icon=imagesearcher
Categories=Graphics;Network;
DESK
# the icon, drawn with the bundled Qt (no binary file in the repo)
QT_QPA_PLATFORM=offscreen "$PY" - "$APPDIR/imagesearcher.png" <<'ICON'
import sys
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
app = QGuiApplication([])
img = QImage(256, 256, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
p.setBrush(QColor("#3b8eea"))
p.setPen(Qt.NoPen)
p.drawRoundedRect(QRectF(8, 8, 240, 240), 48, 48)
p.setPen(QColor("white"))
p.setFont(QFont("Sans", 110, QFont.Bold))
p.drawText(QRectF(0, 0, 256, 256), Qt.AlignCenter, "iS")
p.end()
img.save(sys.argv[1])
ICON
ln -sf imagesearcher.png "$APPDIR/.DirIcon"

# 4. pack
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream "$APPDIR" imagesearcher-x86_64.AppImage
ls -la imagesearcher-x86_64.AppImage
