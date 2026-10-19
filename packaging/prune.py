#!/usr/bin/env python3
"""Trims the PyInstaller bundle (the directory given) for an AppImage.

PyInstaller copies in every library the build machine resolves. Some of
those must come from the machine the AppImage runs on instead: the C++
runtime (the graphics drivers load the system's, which is newer, and an old
bundled one loaded first makes them fail), and the X11, Wayland, font and
D-Bus libraries every desktop has and that talk to services of its own
version. Qt's GTK theme plugin goes too (with the GTK it pulls in, which
would load the system's GTK modules), as do the Qt plugins a tray app never
uses. Then every bundled library that nothing left needs is dropped.
"""

import fnmatch
import os
import shutil
import subprocess
import sys

FROM_THE_SYSTEM = [
    "libstdc++.so.*", "libgcc_s.so.*",
    "libX11.so.*", "libX11-xcb.so.*", "libxcb.so.*",
    "libwayland-client.so.*", "libwayland-cursor.so.*", "libwayland-egl.so.*",
    "libEGL.so.*", "libGL.so.*", "libGLX.so.*", "libOpenGL.so.*", "libGLdispatch.so.*", "libdrm.so.*",
    "libfontconfig.so.*", "libfreetype.so.*", "libharfbuzz.so.*",
    "libdbus-1.so.*", "libexpat.so.*", "libz.so.*",
]

UNUSED = [
    "PyQt6/Qt6/plugins/platformthemes/libqgtk3.so",
    "PyQt6/Qt6/plugins/platforms/libqeglfs.so",
    "PyQt6/Qt6/plugins/platforms/libqlinuxfb.so",
    "PyQt6/Qt6/plugins/platforms/libqminimal.so",
    "PyQt6/Qt6/plugins/platforms/libqminimalegl.so",
    "PyQt6/Qt6/plugins/platforms/libqvnc.so",
    "PyQt6/Qt6/plugins/platforms/libqvkkhrdisplay.so",
    "PyQt6/Qt6/plugins/egldeviceintegrations",
    "PyQt6/Qt6/plugins/generic",
    "PyQt6/Qt6/plugins/imageformats/libqpdf.so",
    "PyQt6/Qt6/lib/libQt6Pdf.so.6",
    "PyQt6/Qt6/translations",
]

# Loaded by PyInstaller's bootloader with dlopen, so nothing lists it.
KEEP = ["libpython3*.so*"]


def needed(path):
    out = subprocess.run(["readelf", "-d", path], capture_output=True, text=True).stdout
    return {line.split("[", 1)[1].rstrip("]") for line in out.splitlines() if "(NEEDED)" in line}


def is_elf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def main(bundle):
    internal = os.path.join(bundle, "_internal")
    for rel in UNUSED:
        path = os.path.join(internal, rel)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.remove(path)
    for name in os.listdir(internal):
        if any(fnmatch.fnmatch(name, pat) for pat in FROM_THE_SYSTEM):
            print(f"prune: {name} (the system's)")
            os.remove(os.path.join(internal, name))

    # The loose libraries (directly in _internal) stay only while something
    # needs them; everything else (the program, Python's modules, Qt and its
    # plugins) is what needs them.
    loose = {n for n in os.listdir(internal) if ".so" in n and os.path.isfile(os.path.join(internal, n))}
    roots = set()
    for root, _, files in os.walk(bundle):
        for name in files:
            path = os.path.join(root, name)
            if (root == internal and name in loose) or not is_elf(path):
                continue
            roots |= needed(path)
    keep = {n for n in loose if n in roots or any(fnmatch.fnmatch(n, pat) for pat in KEEP)}
    todo = list(keep)
    while todo:
        for dep in needed(os.path.join(internal, todo.pop())):
            if dep in loose and dep not in keep:
                keep.add(dep)
                todo.append(dep)
    for name in sorted(loose - keep):
        print(f"prune: {name} (unused)")
        os.remove(os.path.join(internal, name))


if __name__ == "__main__":
    main(sys.argv[1])
