#!/usr/bin/env bash
# Builds out/Chatter-<version>-x86_64.AppImage: the tray app (src/chatter.py)
# frozen with PyInstaller, and the adw-gtk3 theme (libadwaita's look for GTK 3,
# which Chrome draws its window frame with). Chrome for Testing is not in it:
# Chatter downloads that at its first start.
#
#   VERSION  the app version (default: __version__ in src/chatter.py)
#   PYTHON   the Python to build with (default: python3)
#
# It needs git, curl, readelf and npm (for Dart Sass, which adw-gtk3 is
# written for).
#
# Chrome for Testing is built for x86_64 Linux only, so the AppImage is too.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

VERSION=${VERSION:-$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/chatter.py)}
PYTHON=${PYTHON:-python3}
BUILD=build
APPDIR=$BUILD/AppDir
OUT=out
ADW_GTK3_VERSION=v6.5
SASS_VERSION=1.105.2
APPIMAGETOOL_URL=https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
UPDATE_INFO="gh-releases-zsync|tahuffman1s|chatter4teams|latest|Chatter-*-x86_64.AppImage.zsync"

mkdir -p "$BUILD/cache" "$OUT"
rm -rf "$APPDIR"

echo "Chatter $VERSION"

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

# -- adw-gtk3, built from its source (only GTK 3's part is needed)
adw=$BUILD/adw-gtk3-$ADW_GTK3_VERSION
if [ ! -d "$adw/install/share/themes/adw-gtk3" ]; then
  rm -rf "$adw"
  git clone -q --depth 1 --branch "$ADW_GTK3_VERSION" https://github.com/lassekongo83/adw-gtk3 "$adw/src"
  npm install --silent --prefix "$adw/sass" "sass@$SASS_VERSION"
  "$BUILD/venv/bin/pip" install -q meson ninja
  (
    export PATH="$PWD/$adw/sass/node_modules/.bin:$PWD/$BUILD/venv/bin:$PATH"
    meson setup --prefix "$PWD/$adw/install" "$adw/build" "$adw/src" >/dev/null
    ninja -C "$adw/build" install >/dev/null
  )
fi
for theme in adw-gtk3 adw-gtk3-dark; do
  mkdir -p "$APPDIR/usr/share/themes/$theme"
  cp -a "$adw/install/share/themes/$theme/index.theme" "$adw/install/share/themes/$theme/gtk-3.0" \
    "$APPDIR/usr/share/themes/$theme/"
done
install -Dm644 "$adw/src/LICENSE" "$APPDIR/usr/share/licenses/adw-gtk3/LICENSE"

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
