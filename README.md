# Chatter

Microsoft Teams for Linux, as one AppImage for any distribution: the Teams
web app in its own Chrome window, with a tray icon that does what the
Windows app's does. Chatter runs Teams in Chrome for Testing, which it
downloads at its first start and keeps up to date, so nothing else needs
installing, and Chrome extensions work in it.

Microsoft publishes no Linux Teams client. Wrappers built on other browser
engines can't answer the Microsoft passkey prompt (no password-manager
extension, no "use a phone" QR code), so signing in fails there. Chatter's
window is real Chrome: install your password manager's extension (Proton
Pass, Bitwarden, 1Password, ...) and it answers the prompt, or use Chrome's
own phone sign-in.

Chatter is not affiliated with or endorsed by Microsoft.

## What it does

- **Tray icon**: shows your presence (the dot) and the unread count (the
  badge); the tooltip says the same.
  - Left click opens or raises the window.
  - Middle click minimizes it.
  - The menu sets your status (Available, Busy, Do not disturb, Be right
    back, Away, Appear offline), opens the extension pages, and quits.
- **Keeps running in the tray**: closing the window leaves Teams running,
  so chat and call notifications still arrive, and the next click brings
  the window back.
- **Start at login** (tray menu): starts in the tray with no window, as on
  Windows.
- **Presence follows the whole computer**: Teams on the web only sees
  activity inside its own page, so it sets you Away while you work in other
  apps. While the desktop reports you active, Chatter passes a mouse nudge
  into the page every 20 s. It never does this while the Teams window has
  focus, and never touches a status you set yourself. Away still comes by
  itself when you really leave.
  - KDE Plasma and other desktops report activity through
    `org.freedesktop.ScreenSaver`; GNOME through Mutter's idle monitor.
- Calls, screen sharing and notifications work as they do in Chrome. Chrome
  hands notifications to your desktop. Notifications, camera and microphone
  are allowed for Teams up front.

## Install

1. Download `Chatter-<version>-x86_64.AppImage` from
   [Releases](https://github.com/tahuffman1s/chatter4teams/releases).
2. Make it executable and run it:

   ```sh
   chmod +x Chatter-*-x86_64.AppImage
   ./Chatter-*-x86_64.AppImage
   ```

On first start, Chatter downloads Chrome for Testing (about 170 MB) from
Google into `~/.local/share/chatter`, showing its progress. Then it adds
itself to your app menu. It updates that entry
each time it starts, so it follows the AppImage if you move or rename it.
Turn the entry off with **Show in the app menu** in the tray menu. Turn
**Start at login** on there too.

Updates:

- **Chrome**: Chatter checks for a newer Chrome for Testing Stable at start
  and every 6 hours, and downloads it in the background. The new version
  takes over at the next start; the tray menu offers **Restart** to switch
  right away. The old version is deleted then. Set
  `CHATTER_CHROME_VERSION` to pin one version instead.
- **Chatter**: the AppImage carries update information, so
  [AppImageUpdate](https://github.com/AppImageCommunity/AppImageUpdate) (or
  AppImageLauncher, Gear Lever, ...) can update it in place, downloading
  only what changed.

To remove it, run `./Chatter-*.AppImage --uninstall`, then delete the
AppImage. That removes the menu and autostart entries. Your sign-in and
extensions stay in `~/.local/share/chatter`, with Chrome for Testing; delete
that folder too if you want them gone.

### Requirements

- x86_64 Linux. Chrome for Testing is only built for x86_64.
- The libraries Chrome needs (NSS, GTK 3, ALSA, ...). Any desktop that runs
  a browser has them.
- Internet access to `googlechromelabs.github.io` and
  `storage.googleapis.com`, for the Chrome downloads. Chatter uses the
  system's CA certificates, so a company TLS proxy works if its CA is
  installed.
- glibc 2.35 or newer: Ubuntu 22.04, Debian 12, Fedora 36, openSUSE Leap
  15.6 / Tumbleweed, Arch, or newer.
- A tray:
  - KDE Plasma, Cinnamon, Xfce, MATE, Budgie, LXQt and most other desktops
    show one.
  - GNOME needs the
    [AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/).
    Ubuntu's GNOME has it already.
  - Without a tray, Chatter still works: the app menu entry brings a closed
    window back.
- FUSE, which almost every desktop has. Where it's missing, run the image
  with `--appimage-extract-and-run`.
- NixOS has no `/usr/lib`, so run AppImages through `appimage-run`, which
  provides the libraries they expect, for Chatter and for the Chrome it
  starts: `appimage-run Chatter-*.AppImage`. To run them directly as
  `./Chatter-*.AppImage` (and from the app menu), set:

  ```nix
  programs.appimage = {
    enable = true;
    binfmt = true;
  };
  ```

## Extensions

The tray menu's **Extensions** submenu offers:

- **Get extensions (Chrome Web Store)**: opens the Web Store in a normal
  Chrome window of Chatter's profile. **Add to Chrome** installs into
  Chatter.
- **Manage extensions**: opens `chrome://extensions`, to turn extensions on
  or off, remove them, or pin them.
- **Unpacked extensions folder**: opens `~/.config/chatter/extensions`.
  Put each unpacked extension there in its own directory, the one that holds
  its `manifest.json`. Chatter loads them at its next start.

Chrome for Testing is used because it still loads unpacked extensions from
the command line, which regular Chrome no longer allows.

## The Chrome sandbox

Chrome for Testing has no setuid sandbox helper: it is unpacked in your home
directory, not installed by root. It uses unprivileged user namespaces
instead. Some
systems turn those off, or restrict them: Ubuntu 23.10 and later restrict
them through AppArmor. On those systems, Chatter starts Chrome with
`--no-sandbox` and says so on stderr. That is what other Chromium-based
AppImages do too.

To keep the sandbox on Ubuntu 23.10 and later, allow user namespaces for
Chatter's Chrome with an AppArmor profile, then tell Chatter to keep the
sandbox. Create `/etc/apparmor.d/chatter`:

```
abi <abi/4.0>,
include <tunables/global>

profile chatter-chrome @{HOME}/.local/share/chatter/chrome-for-testing/*/chrome-linux64/chrome flags=(unconfined) {
  userns,
  include if exists <local/chatter-chrome>
}
```

Then run `sudo apparmor_parser -r /etc/apparmor.d/chatter`, and start
Chatter with `CHATTER_NO_SANDBOX=0`. You can set that variable in the
`Exec=` line of `~/.local/share/applications/chatter.desktop`, for example
`Exec=env CHATTER_NO_SANDBOX=0 /path/to/Chatter.AppImage`. Chatter rewrites
that file at each start, so either set the variable in your session's
environment instead, or turn the menu entry off and keep your own.

## Settings and files

| What | Where |
| --- | --- |
| Chrome profile: sign-in, extensions, settings | `~/.local/share/chatter/chrome` |
| Chrome for Testing | `~/.local/share/chatter/chrome-for-testing/<version>` |
| Unpacked extensions | `~/.config/chatter/extensions/<name>/` |
| Chatter's own settings | `~/.config/chatter/settings.json` |
| App menu entry | `~/.local/share/applications/chatter.desktop` |
| Login autostart | `~/.config/autostart/chatter.desktop` |

Environment variables:

| Variable | Effect |
| --- | --- |
| `CHATTER_NO_SANDBOX` | `1` runs Chrome without its sandbox; `0` keeps the sandbox always |
| `CHATTER_CHROME_FLAGS` | extra Chrome flags, e.g. `--ozone-platform=wayland` |
| `CHATTER_CHROME_VERSION` | a Chrome for Testing version to use and keep, e.g. `155.0.8059.84` ([versions](https://googlechromelabs.github.io/chrome-for-testing/)) |
| `CHATTER_CHROME` | a different Chrome binary; no Chrome for Testing is downloaded |
| `CHATTER_URL` | a different Teams URL; the default is `https://teams.microsoft.com/v2/` |
| `CHATTER_PROFILE` | a different Chrome profile directory |

Setting your status from the tray clicks through Teams' own menus by their
English labels. With Teams in another language, it falls back to Teams'
`/busy`-style commands.

## How it works

`src/chatter.py` is a PyQt6 tray app that starts Chrome and drives it over
the DevTools pipe (`--remote-debugging-pipe`):

- It injects a small hook into the Teams page. The hook reports the unread
  badge Teams sets with `navigator.setAppBadge` (which Chrome drops on
  Linux), the `(3)` count in the title, and the presence shown on your
  avatar.
- Chrome starts with no window. A hidden page target keeps it alive: that
  target is blank while the Teams window is open, and Teams itself while
  the window is closed.
- Each window open is a second `chrome --app=URL` launch into the same
  profile, which Chrome's process singleton hands to the running instance.

## Building

```sh
./build-appimage.sh          # → out/Chatter-<version>-x86_64.AppImage (+ .zsync)
```

The script:

1. Freezes the tray app with its Python and Qt using PyInstaller, then
   trims libraries the host should provide (`packaging/prune.py`).
2. Packs it with appimagetool.

It needs Python 3.10 or newer, `curl` and `readelf`. Build on the
oldest system you want to support; the release workflow uses Ubuntu 22.04.

Releases: push a tag `v<version>`, e.g. `v0.2.0`.
`.github/workflows/appimage.yml` builds the AppImage with that version and
attaches it and its `.zsync` file to a GitHub release. Or run the workflow
by hand (Actions → AppImage → Run workflow) with a version such as `0.2.0`:
it makes the release and its tag `v0.2.0` on the commit it builds. Pushes to `main` and
pull requests build it as a workflow artifact.

Running from source, without packaging: `pip install PyQt6`, then
`python3 src/chatter.py`. It downloads Chrome for Testing the same way, unless
`CHATTER_CHROME` names a Chrome to use.

## License

Chatter is under the [MIT license](LICENSE). The AppImage also contains
Python, Qt and PyQt6, which keep their own licenses: PyQt6 is GPLv3, so the
AppImage as a whole is distributed under the GPLv3's terms. Chrome for
Testing is downloaded from Google by each user and is not part of Chatter.
