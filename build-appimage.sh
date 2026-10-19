#!/usr/bin/env bash
# Builds out/Chatter-<version>-x86_64.AppImage: the tray app (src/chatter.py)
# frozen with PyInstaller, and Chrome for Testing beside it.
#
#   VERSION         the app version (default: __version__ in src/chatter.py)
#   CHROME_VERSION  a Chrome for Testing version, e.g. 141.0.7390.54
#                   (default: the current one of CHROME_CHANNEL)
#   CHROME_CHANNEL  Stable (default), Beta, Dev or Canary
#   PYTHON          the Python to build with (default: python3)
#
# Chrome for Testing is built for x86_64 Linux only, so the AppImage is too.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

VERSION=${VERSION:-$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/chatter.py)}
CHROME_CHANNEL=${CHROME_CHANNEL:-Stable}
PYTHON=${PYTHON:-python3}
BUILD=build
APPDIR=$BUILD/AppDir
OUT=out
CFT=https://googlechromelabs.github.io/chrome-for-testing
APPIMAGETOOL_URL=https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
UPDATE_INFO="gh-releases-zsync|tahuffman1s|chatter4teams|latest|Chatter-*-x86_64.AppImage.zsync"

mkdir -p "$BUILD/cache" "$OUT"
rm -rf "$APPDIR"

# -- Chrome for Testing
if [ -z "${CHROME_VERSION:-}" ]; then
  CHROME_VERSION=$(curl -fsSL "$CFT/last-known-good-versions.json" |
    "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["channels"][sys.argv[1]]["version"])' "$CHROME_CHANNEL")
fi
echo "Chatter $VERSION with Chrome for Testing $CHROME_VERSION"
zip=$BUILD/cache/chrome-linux64-$CHROME_VERSION.zip
if [ ! -s "$zip" ]; then
  curl -fL --retry 3 -o "$zip.part" \
    "https://storage.googleapis.com/chrome-for-testing-public/$CHROME_VERSION/linux64/chrome-linux64.zip"
  mv "$zip.part" "$zip"
fi
rm -rf "$BUILD/chrome-linux64"
unzip -q "$zip" -d "$BUILD"
mkdir -p "$APPDIR/opt"
mv "$BUILD/chrome-linux64" "$APPDIR/opt/chrome"
echo "$CHROME_VERSION" > "$APPDIR/opt/chrome/CHROME_VERSION"

# -- the tray app, frozen with its Python and Qt
if [ ! -x "$BUILD/venv/bin/python" ]; then
  "$PYTHON" -m venv "$BUILD/venv"
fi
"$BUILD/venv/bin/pip" install -q -r requirements.txt
sed "s/^__version__ = .*/__version__ = \"$VERSION\"/" src/chatter.py > "$BUILD/chatter.py"
"$BUILD/venv/bin/pyinstaller" --noconfirm --clean --log-level WARN --onedir --name chatter \
  --distpath "$BUILD/dist" --workpath "$BUILD/pyinstaller" --specpath "$BUILD" "$BUILD/chatter.py"
mkdir -p "$APPDIR/usr/lib"
cp -a "$BUILD/dist/chatter" "$APPDIR/usr/lib/chatter"
"$BUILD/venv/bin/python" packaging/prune.py "$APPDIR/usr/lib/chatter"

# -- the AppDir
install -m755 packaging/AppRun "$APPDIR/AppRun"
install -m644 packaging/chatter.desktop "$APPDIR/chatter.desktop"
install -m644 assets/chatter.png "$APPDIR/chatter.png"
ln -s chatter.png "$APPDIR/.DirIcon"
install -Dm644 packaging/chatter.desktop "$APPDIR/usr/share/applications/chatter.desktop"
install -Dm644 assets/chatter.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/chatter.png"
install -Dm644 assets/chatter.svg "$APPDIR/usr/share/icons/hicolor/scalable/apps/chatter.svg"

# -- the AppImage (appimagetool extracts itself, so no FUSE is needed here)
tool=$BUILD/cache/appimagetool-x86_64.AppImage
if [ ! -x "$tool" ]; then
  curl -fL --retry 3 -o "$tool.part" "$APPIMAGETOOL_URL"
  chmod +x "$tool.part"
  mv "$tool.part" "$tool"
fi
image=$OUT/Chatter-$VERSION-x86_64.AppImage
rm -f "$image" "$image.zsync"
ARCH=x86_64 VERSION=$VERSION "$tool" --appimage-extract-and-run --no-appstream -u "$UPDATE_INFO" "$APPDIR" "$image"
# (appimagetool writes the .zsync beside where it was run)
if [ -f "$(basename "$image").zsync" ]; then
  mv "$(basename "$image").zsync" "$OUT/"
fi
echo "Built $image"
