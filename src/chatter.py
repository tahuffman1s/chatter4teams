#!/usr/bin/env python3
"""Chatter: Microsoft Teams in its own Chrome window, with the tray icon the
Windows app has.

Teams is the web app in an app-mode Chrome window with a profile of its own
(Chrome for Testing, downloaded at the first start and kept up to date with
Google's Stable releases). Chrome's extensions work
there, so a password manager such as Proton Pass can answer the Microsoft
passkey prompt, and Chrome's own "use a phone" QR code is there too. This
owns that Chrome and drives it over the DevTools pipe:

- the tray icon shows your presence (the dot) and the unread count (the
  badge the page sets through navigator.setAppBadge, which Chrome would
  otherwise drop on Linux); the tooltip says the same;
- left click opens or raises the window, middle click minimizes it; the
  menu opens it, sets your status (through Teams' /available, /busy, ...
  commands), opens the Chrome Web Store or the extensions page, or quits;
- closing the window keeps Teams running: the page carries on in a hidden
  target, so chat and call notifications still arrive, and the next click
  brings the window back;
- --background (the login autostart) starts it that way, with no window;
- presence follows the whole computer, as on Windows: Teams on the web only
  sees activity inside its page and sets you Away while you work in other
  apps, so while the desktop reports you active (org.freedesktop.ScreenSaver
  on KDE and others, Mutter's idle monitor on GNOME) a mouse nudge goes into
  the page every 20 s, unless the window has focus. Away comes by itself
  when you really leave, and Available again when you are back. A status
  you set yourself is never touched.

Chrome starts with --no-startup-window and a hidden target (the "anchor",
about:blank while the window is open, Teams while it is closed) keeps it
alive without windows and without Chrome's own background-mode tray icon.
A second `chrome --app=URL` launch into the same profile asks the running
instance to open the app window (Chrome's process singleton), which is how
the window is opened and reopened.

Extensions: install them from the Chrome Web Store (tray menu > Extensions),
or put unpacked ones, one directory each, in ~/.config/chatter/extensions;
those load at the next start.

Environment: CHATTER_ICON (set by the AppImage), and the overrides
CHATTER_CHROME (a Chrome to use instead), CHATTER_CHROME_VERSION (a Chrome
for Testing version to pin), CHATTER_URL, CHATTER_PROFILE,
CHATTER_CHROME_FLAGS and CHATTER_NO_SANDBOX (1 to run Chrome without its
sandbox, 0 to keep it).
"""

import argparse
import ctypes
import fcntl
import functools
import json
import os
import queue
import select
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile

__version__ = "0.1.2"

APP_NAME = "Chatter"
APP_ID = "chatter"

URL = os.environ.get("CHATTER_URL", "https://teams.microsoft.com/v2/")
ORIGIN = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(URL))
ICON = os.environ.get("CHATTER_ICON", "")
WEB_STORE = "https://chromewebstore.google.com/"
EXTENSIONS_PAGE = "chrome://extensions/"

DATA_HOME = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
PROFILE = os.environ.get("CHATTER_PROFILE") or os.path.join(DATA_HOME, APP_ID, "chrome")
CONFIG_DIR = os.path.join(CONFIG_HOME, APP_ID)
EXTENSIONS_DIR = os.path.join(CONFIG_DIR, "extensions")
HELPER_DIR = os.path.join(DATA_HOME, APP_ID, "helper-extension")
SETTINGS = os.path.join(CONFIG_DIR, "settings.json")

# Chrome for Testing, one directory per version: <version>/chrome-linux64/.
CFT_DIR = os.path.join(DATA_HOME, APP_ID, "chrome-for-testing")
CFT_LATEST = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json"
CFT_ZIP = "https://storage.googleapis.com/chrome-for-testing-public/{}/linux64/chrome-linux64.zip"

# As Chrome names app windows (chrome-<host>__-<profile dir>), so the desktop
# matches the window to the menu entry: panel launcher, icon, grouping.
WM_CLASS = "chrome-teams.microsoft.com__-Default"
MENU_ENTRY = os.path.join(DATA_HOME, "applications", f"{APP_ID}.desktop")
# (in Chatter's own data, not ~/.local/share/icons, which is the icon
# theme's and can be read-only, as when home-manager links a theme there)
MENU_ICON = os.path.join(DATA_HOME, APP_ID, f"{APP_ID}.png")
AUTOSTART_ENTRY = os.path.join(CONFIG_HOME, "autostart", f"{APP_ID}.desktop")

ACTIVITY_INTERVAL_MS = 20_000  # how often to pass the computer's activity on
ACTIVE_WITHIN_MS = 60_000  # ...when the last input was this recent
UPDATE_CHECK_MS = 6 * 3600_000  # how often to look for a newer Chrome

# Tray menu entries: the label, and the /command of last resort.
STATUSES = [
    ("Available", "/available"),
    ("Busy", "/busy"),
    ("Do not disturb", "/dnd"),
    ("Be right back", "/brb"),
    ("Away", "/away"),
    ("Appear offline", "/offline"),
]

# Runs in the Teams page (and again in every new document). Reports through
# the chatter binding: the badge Teams sets (Chrome drops it on Linux), the
# "(3)" prefix of the title as a fallback, and the presence shown on your
# own avatar, found by its accessible label since the DOM changes often.
PAGE_HOOK = r"""
(() => {
  if (window.__chatterHooked) return;
  window.__chatterHooked = true;
  const send = (o) => { try { window.chatter(JSON.stringify(o)); } catch (e) {} };
  const badge = (n) => { send({badge: Number(n) || 0}); return Promise.resolve(); };
  try {
    Object.defineProperty(navigator, "setAppBadge", {value: badge, configurable: true});
    Object.defineProperty(navigator, "clearAppBadge", {value: () => badge(0), configurable: true});
  } catch (e) {}
  let lastTitle = null, lastPresence = null;
  const PRESENCE = /\b(Available|Busy|Do not disturb|Be right back|Away|Appear offline|Offline|In a call|In a meeting|Presenting|Out of office|Focusing)\b/i;
  const scan = () => {
    if (document.title !== lastTitle) {
      lastTitle = document.title;
      const m = /^\((\d+)\+?\)/.exec(document.title);
      send({title: document.title, titleBadge: m ? Number(m[1]) : 0});
    }
    let presence = null;
    const labelled = document.querySelectorAll('[aria-label*="status" i], [aria-label*="presence" i], [title*="status" i], [data-tid*="presence" i]');
    for (const el of labelled) {
      const text = (el.getAttribute("aria-label") || "") + " " + (el.getAttribute("title") || "");
      const m = PRESENCE.exec(text);
      if (m) { presence = m[1]; break; }
    }
    if (presence !== lastPresence) { lastPresence = presence; send({presence}); }
  };
  setInterval(scan, 2000);
  setTimeout(scan, 500);
})();
"""


# Sets the status the way a hand does: opens the profile menu from the own
# avatar (the element whose label carries the presence, as the hook finds
# it), clicks the current status in it, then the wanted one in the submenu
# ("Away" is "Appear away" there). Resolves to "ok" or to what went wrong.
SET_STATUS_JS = r"""
(async (wanted, strict) => {
  const PRES = /\b(Available|Busy|Do not disturb|Be right back|Appear away|Away|Appear offline|Offline)\b/i;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // (a page without a window lays nothing out, so there "visible" is moot)
  const visible = strict ? (el) => !!(el.offsetParent || el.getClientRects().length) : () => true;
  const text = (el) => ((el.getAttribute("aria-label") || "") + " " + (el.innerText || el.textContent || "")).replace(/\s+/g, " ").trim();
  const find = (sel, pred) => Array.from(document.querySelectorAll(sel)).filter((el) => visible(el) && pred(el));
  const waitFor = async (fn, ms = 3000) => { const end = Date.now() + ms; while (Date.now() < end) { const r = fn(); if (r) return r; await sleep(100); } return null; };
  const click = (el) => { el.scrollIntoView && el.scrollIntoView({block: "nearest"}); el.click(); };
  const items = (pred, exclude) => {
    for (const sel of ['[role^="menuitem"], [role="option"]', 'button, [role="button"]']) {
      const r = find(sel, (el) => !exclude.some((x) => x === el || x.contains(el)) && pred(el));
      if (r.length) return r;
    }
    return [];
  };
  let avatar = find('[aria-label*="status" i], [aria-label*="presence" i]', (el) => PRES.test(el.getAttribute("aria-label") || ""))[0];
  if (!avatar) return "no avatar with a status label found";
  avatar = avatar.closest("button, [role=button]") || avatar;
  click(avatar);
  const current = await waitFor(() => items((el) => PRES.test(text(el)) && !/status message|reset status/i.test(text(el)), [avatar])[0]);
  if (!current) return "the profile menu has no status entry";
  click(current);
  const re = new RegExp("^(Appear )?" + wanted.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\b", "i");
  const option = await waitFor(() => items((el) => re.test(text(el)), [avatar, current])[0]);
  if (!option) return "the status menu has no \"" + wanted + "\" entry";
  click(option);
  return "ok";
})
"""


# Chrome creates a hidden target only while it has some other DevTools
# target (a page, a worker, ...), and with no window open there may be none:
# its own extensions' workers run on the first start, not later. So this
# extension, always loaded, has a worker that wakes at every browser start.
HELPER_FILES = {
    "manifest.json": json.dumps({
        "manifest_version": 3,
        "name": APP_NAME,
        "version": "1",
        "description": f"Part of {APP_NAME}: lets it keep Teams running while its window is closed.",
        "background": {"service_worker": "worker.js"},
    }, indent=2),
    "worker.js": "chrome.runtime.onStartup.addListener(() => {});\n",
}


# A mouse move raised in the page (bubbles from body to document and window).
ACTIVITY_JS = """
(() => { const o = {bubbles: true, cancelable: true, clientX: 1, clientY: 1, view: window};
  (document.body || document).dispatchEvent(new MouseEvent("mousemove", o)); })()
"""


def log(message):
    print(f"{APP_ID}: {message}", file=sys.stderr, flush=True)


def frozen():
    return getattr(sys, "frozen", False)


# -- Chrome for Testing: downloaded at the first start into CFT_DIR, and a
# newer Stable fetched in the background, taking over at the next start.

def version_key(version):
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def cft_chrome(version):
    return os.path.join(CFT_DIR, version, "chrome-linux64", "chrome")


def installed_versions():
    """The complete installs, oldest first (an install is unpacked beside
    its directory and renamed into place only when it is whole)."""
    try:
        names = os.listdir(CFT_DIR)
    except OSError:
        return []
    found = [n for n in names if version_key(n) and os.access(cft_chrome(n), os.X_OK)]
    return sorted(found, key=version_key)


def installed_chrome():
    """The Chrome to run, if there is one yet: CHATTER_CHROME, else the
    pinned or the newest Chrome for Testing that is installed."""
    if os.environ.get("CHATTER_CHROME"):
        return os.environ["CHATTER_CHROME"]
    pinned = os.environ.get("CHATTER_CHROME_VERSION")
    versions = installed_versions()
    if pinned:
        return cft_chrome(pinned) if pinned in versions else None
    return cft_chrome(versions[-1]) if versions else None


def managed_version(chrome):
    """The Chrome for Testing version `chrome` is, if it is one of ours."""
    rel = os.path.relpath(os.path.realpath(chrome), os.path.realpath(CFT_DIR))
    version = rel.split(os.sep, 1)[0]
    return version if not rel.startswith("..") and version_key(version) else None


@functools.lru_cache(maxsize=None)
def ssl_context():
    """The system's certificates (so a company's own CA works too). The
    frozen app's OpenSSL looks where the build machine keeps them, which
    on another distribution is the wrong place."""
    context = ssl.create_default_context()
    for path in (os.environ.get("SSL_CERT_FILE"),
                 "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch, Gentoo
                 "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
                 "/etc/ssl/ca-bundle.pem",  # openSUSE
                 "/etc/ssl/cert.pem"):  # Alpine, Void
        if path and os.path.isfile(path):
            context.load_verify_locations(cafile=path)
            break
    return context


def http_get(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__}"})
    return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())


def wanted_chrome_version():
    """The pinned version, or the current Stable."""
    pinned = os.environ.get("CHATTER_CHROME_VERSION")
    if pinned:
        return pinned
    with http_get(CFT_LATEST) as response:
        return json.load(response)["channels"]["Stable"]["version"]


class Cancelled(Exception):
    pass


def install_chrome(version, progress=None, cancelled=lambda: False):
    """Download and unpack Chrome for Testing `version`; returns its chrome.
    progress(done, total) gets the bytes so far (total 0 if unknown), then
    (None, None) while unpacking."""
    os.makedirs(CFT_DIR, exist_ok=True)
    target = os.path.join(CFT_DIR, version)
    archive = target + ".zip.part"
    unpacked = target + ".part"
    try:
        with http_get(CFT_ZIP.format(version), timeout=60) as response, open(archive, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(1 << 20):
                if cancelled():
                    raise Cancelled()
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if progress:
            progress(None, None)
        shutil.rmtree(unpacked, ignore_errors=True)
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                path = z.extract(info, unpacked)
                mode = stat.S_IMODE(info.external_attr >> 16)
                if mode and not info.is_dir():
                    os.chmod(path, mode)  # (zipfile drops the executable bits)
        shutil.rmtree(target, ignore_errors=True)
        os.rename(unpacked, target)
    finally:
        remove_file(archive)
        shutil.rmtree(unpacked, ignore_errors=True)
    return cft_chrome(version)


def remove_other_chromes(keep):
    """Every Chrome for Testing but `keep` (the one in use), and what an
    interrupted download left."""
    try:
        names = os.listdir(CFT_DIR)
    except OSError:
        return
    for name in names:
        if name == keep:
            continue
        path = os.path.join(CFT_DIR, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            remove_file(path)


def update_chrome(current, notify):
    """Fetch a Stable newer than `current` for the next start; notify
    ("chrome_update", version) when it is there."""
    try:
        version = wanted_chrome_version()
        if version_key(version) > version_key(current) and version not in installed_versions():
            install_chrome(version)
            notify("chrome_update", version)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        notify("error", f"looking for a newer Chrome for Testing: {e}")


CHROME = installed_chrome()


def use_chrome(path):
    global CHROME
    CHROME = path
    sandbox_args.cache_clear()
    chrome_args.cache_clear()


def host_env():
    """The environment for programs of the system's (Chrome, xdg-open): not
    the library and plugin paths of the frozen app's own Python and Qt."""
    env = dict(os.environ)
    if frozen():
        # PyInstaller puts its bundle first on the library path and keeps
        # the original in LD_LIBRARY_PATH_ORIG.
        if "LD_LIBRARY_PATH_ORIG" in env:
            env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
        else:
            env.pop("LD_LIBRARY_PATH", None)
        for name in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "PYTHONHOME",
                     "PYTHONPATH"):
            env.pop(name, None)
    return env


def read_sysctl(name):
    try:
        with open(f"/proc/sys/{name}") as f:
            return f.read().strip()
    except OSError:
        return None


@functools.lru_cache(maxsize=None)
def sandbox_args():
    """--no-sandbox where Chrome's sandbox can't work. Chrome for Testing,
    unpacked in the home directory, has no setuid helper, so Chrome needs
    unprivileged user namespaces, which some distributions turn off or
    restrict (Ubuntu 23.10 and later, through AppArmor)."""
    forced = os.environ.get("CHATTER_NO_SANDBOX")
    if forced is not None:
        return ["--no-sandbox"] if forced not in ("", "0") else []
    if not managed_version(CHROME):
        return []  # a Chrome of the system's has its own setuid sandbox
    reason = None
    if read_sysctl("kernel/apparmor_restrict_unprivileged_userns") == "1":
        reason = "AppArmor restricts unprivileged user namespaces (kernel.apparmor_restrict_unprivileged_userns=1)"
    elif read_sysctl("kernel/unprivileged_userns_clone") == "0":
        reason = "unprivileged user namespaces are off (kernel.unprivileged_userns_clone=0)"
    elif read_sysctl("user/max_user_namespaces") == "0":
        reason = "user namespaces are off (user.max_user_namespaces=0)"
    if not reason:
        return []
    log(f"running Chrome without its sandbox: {reason}. See the README to keep the sandbox.")
    return ["--no-sandbox"]


def unpacked_extensions():
    """The unpacked extensions in EXTENSIONS_DIR: each directory with a manifest."""
    try:
        names = sorted(os.listdir(EXTENSIONS_DIR))
    except OSError:
        return []
    return [path for path in (os.path.join(EXTENSIONS_DIR, n) for n in names)
            if os.path.isfile(os.path.join(path, "manifest.json"))]


@functools.lru_cache(maxsize=None)
def chrome_args():
    # --disable-infobars: no "Chrome for Testing is only for automated
    # testing" bar (nor the --no-sandbox one) across the top of Teams.
    args = [f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check", "--disable-infobars",
            *sandbox_args()]
    args.append("--load-extension=" + ",".join([HELPER_DIR, *unpacked_extensions()]))
    return [*args, *os.environ.get("CHATTER_CHROME_FLAGS", "").split()]


def spawn(args):
    return subprocess.Popen(args, env=host_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def open_window():
    """Ask the running Chrome for the app window (process singleton), or
    start one with it."""
    return spawn([CHROME, *chrome_args(), f"--app={URL}"])


def stop_orphaned_chrome():
    """A Chrome that an earlier Chatter started and left running (it was
    killed before it could close it) still holds the profile: a new Chrome
    would hand over to it and exit. It is known by the profile's lock (its
    host and pid) and by its command line: this profile, over a DevTools
    pipe, which only Chatter gives it. (This runs holding Chatter's own
    lock, so no other Chatter owns it.)"""
    try:
        host, _, pid = os.readlink(os.path.join(PROFILE, "SingletonLock")).rpartition("-")
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            # (Chrome rewrites its process title: by then this is its
            # arguments joined with spaces, not separated with NULs)
            cmdline = b" " + f.read().replace(b"\0", b" ") + b" "
    except (OSError, ValueError):
        return
    if host != socket.gethostname() or not pid.isdigit():
        return
    if f" --user-data-dir={PROFILE} ".encode() not in cmdline or b" --remote-debugging-pipe " not in cmdline:
        return
    log(f"stopping the Chrome an earlier {APP_NAME} left running (pid {pid})")
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(int(pid), sig)
        except ProcessLookupError:
            return
        end = time.monotonic() + 10
        while time.monotonic() < end and os.path.exists(f"/proc/{pid}"):
            time.sleep(0.2)
        if not os.path.exists(f"/proc/{pid}"):
            return


def prepare_profile():
    """Before Chrome starts: the helper extension in place, no Chrome left
    over on the profile, and no "running in the background" tray icon of
    Chrome's own (the one here is the one that matters): background mode
    off in Local State."""
    os.makedirs(HELPER_DIR, exist_ok=True)
    for name, text in HELPER_FILES.items():
        write_file(os.path.join(HELPER_DIR, name), text)
    stop_orphaned_chrome()
    os.makedirs(PROFILE, exist_ok=True)
    path = os.path.join(PROFILE, "Local State")
    try:
        with open(path) as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError):
        return
    if not isinstance(state, dict) or state.get("background_mode", {}).get("enabled") is False:
        return
    state.setdefault("background_mode", {})["enabled"] = False
    with open(path + ".chatter", "w") as f:
        json.dump(state, f)
    os.replace(path + ".chatter", path)


libc = ctypes.CDLL(None, use_errno=True)
PR_SET_PDEATHSIG = 1


class DevTools:
    """Chrome over --remote-debugging-pipe (fds 3 and 4), one caller at a
    time. Events that arrive while waiting for a reply are queued."""

    def __init__(self, args):
        to_child_r, self.to_child = os.pipe()
        self.from_child, from_child_w = os.pipe()
        hi_r = fcntl.fcntl(to_child_r, fcntl.F_DUPFD_CLOEXEC, 10)
        hi_w = fcntl.fcntl(from_child_w, fcntl.F_DUPFD_CLOEXEC, 10)
        os.close(to_child_r)
        os.close(from_child_w)

        def child_fds():
            os.dup2(hi_r, 3)
            os.dup2(hi_w, 4)
            # Chrome goes when Chatter does, however Chatter ends, rather
            # than holding the profile with nothing to drive it.
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)

        self.proc = subprocess.Popen(
            [CHROME, *args], preexec_fn=child_fds, pass_fds=(3, 4), env=host_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(hi_r)
        os.close(hi_w)
        self.buf = b""
        self.next_id = 0
        self.events = []

    def _read(self, timeout):
        """Read until a reply or an event is complete; returns False on timeout."""
        ready, _, _ = select.select([self.from_child], [], [], timeout)
        if not ready:
            return False
        chunk = os.read(self.from_child, 65536)
        if not chunk:
            raise EOFError("Chrome closed the pipe")
        self.buf += chunk
        return True

    def _messages(self):
        while b"\0" in self.buf:
            raw, self.buf = self.buf.split(b"\0", 1)
            yield json.loads(raw)

    def call(self, method, params=None, session=None, timeout=15):
        self.next_id += 1
        msg_id = self.next_id
        msg = {"id": msg_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        os.write(self.to_child, json.dumps(msg).encode() + b"\0")
        deadline = time.monotonic() + timeout
        while True:
            for m in self._messages():
                if m.get("id") == msg_id:
                    if "error" in m:
                        raise RuntimeError(f"{method}: {m['error'].get('message', m['error'])}")
                    return m.get("result", {})
                if "method" in m:
                    self.events.append(m)
            if not self._read(max(0, deadline - time.monotonic())):
                raise TimeoutError(method)

    def poll_events(self, timeout):
        """Collect events for up to `timeout` seconds; returns them."""
        if self._read(timeout):
            self.events.extend(m for m in self._messages() if "method" in m)
        events, self.events = self.events, []
        return events


class Controller(threading.Thread):
    """Owns Chrome: window and hidden-page targets, the page hook, and the
    commands from the tray. Reports through `notify(kind, value)`, called on
    this thread; the tray marshals it to the GUI thread."""

    def __init__(self, notify, background):
        super().__init__(daemon=True)
        self.notify = notify
        self.background = background
        self.commands = queue.Queue()
        self.quitting = False
        self.chrome = None
        self.sessions = {}  # targetId -> sessionId
        self.window_target = None
        self.hidden_target = None  # the anchor
        self.hidden_active = False  # ...showing Teams (window closed)
        self.pending_hidden = None  # targetId of the anchor being created
        self.nudge = 1

    # -- commands from the tray (any thread)
    def send(self, command, *args):
        self.commands.put((command, args))

    # -- Chrome
    def create_hidden(self):
        if self.hidden_target or self.pending_hidden:
            return
        # Chrome allows hidden targets only a moment after it has started.
        deadline = time.monotonic() + 15
        while True:
            try:
                self.pending_hidden = self.chrome.call("Target.createTarget",
                                                       {"url": "about:blank", "hidden": True})["targetId"]
                return
            except RuntimeError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.25)

    def hidden_show(self, teams):
        """Point the anchor at Teams (window closed) or back at nothing."""
        session = self.sessions.get(self.hidden_target)
        if not session or self.hidden_active == teams:
            return
        self.hidden_active = teams
        self.chrome.call("Page.navigate", {"url": URL if teams else "about:blank"}, session=session)

    def grant_permissions(self):
        """Notifications, camera and microphone allowed for Teams up front,
        so it never asks (the page without a window could not). Chrome hands
        its notifications to the desktop's notification service."""
        for name in ("notifications", "camera", "microphone"):
            try:
                self.chrome.call("Browser.setPermission", {"permission": {"name": name}, "setting": "granted",
                                                           "origin": ORIGIN})
            except RuntimeError as e:
                self.notify("error", f"allow {name}: {e}")

    def attach(self, target_id):
        c = self.chrome
        session = c.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        self.sessions[target_id] = session
        c.call("Runtime.enable", session=session)
        c.call("Page.enable", session=session)
        c.call("Runtime.addBinding", {"name": "chatter"}, session=session)
        c.call("Page.addScriptToEvaluateOnNewDocument", {"source": PAGE_HOOK}, session=session)
        c.call("Runtime.evaluate", {"expression": PAGE_HOOK}, session=session)

    def has_window(self, target_id):
        try:
            self.chrome.call("Browser.getWindowForTarget", {"targetId": target_id})
            return True
        except RuntimeError:
            return False

    def on_target_info(self, info):
        """A page target appeared or changed. New targets start at
        about:blank and get their URL later, so this runs for both
        Target.targetCreated and Target.targetInfoChanged."""
        target_id = info["targetId"]
        if target_id in self.sessions:
            return
        # (a hidden target reports type "other", so it is known by id)
        if target_id != self.pending_hidden and not (info["type"] == "page" and info["url"].startswith(ORIGIN)):
            return
        self.attach(target_id)
        if target_id == self.pending_hidden or not self.has_window(target_id):
            self.hidden_target = target_id
            self.pending_hidden = None
            if self.background and not self.window_target:
                self.hidden_show(True)
            return
        self.window_target = target_id
        self.notify("window", True)
        # The window takes over from the hidden page: one Teams, one set of
        # notifications.
        self.hidden_show(False)

    def on_target_destroyed(self, target_id):
        self.sessions.pop(target_id, None)
        if target_id == self.hidden_target:
            self.hidden_target = None
            self.hidden_active = False
        if target_id == self.window_target:
            self.window_target = None
            self.notify("window", False)
            if not self.quitting:
                # Closed with the X: keep Teams running in the tray.
                self.hidden_show(True)

    def on_binding(self, payload):
        try:
            data = json.loads(payload)
        except ValueError:
            return
        if "badge" in data:
            self.notify("badge", data["badge"])
        if "titleBadge" in data:
            self.notify("title_badge", data["titleBadge"])
        if "presence" in data:
            self.notify("presence", data["presence"])

    def handle_event(self, event):
        method, params = event["method"], event.get("params", {})
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            self.on_target_info(params["targetInfo"])
        elif method == "Target.targetDestroyed":
            self.on_target_destroyed(params["targetId"])
        elif method == "Runtime.bindingCalled" and params.get("name") == "chatter":
            self.on_binding(params.get("payload", ""))

    def page_focused(self, session):
        return bool(self.chrome.call("Runtime.evaluate", {"expression": "document.hasFocus()", "returnByValue": True},
                                     session=session).get("result", {}).get("value"))

    def window_session(self):
        return self.sessions.get(self.window_target) if self.window_target else None

    def wait_until(self, pred, timeout):
        """Keep handling events until pred() holds (or the time is up)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            for event in self.chrome.poll_events(0.25):
                self.handle_event(event)
        return pred()

    def set_status_js(self, session, label, strict):
        return self.chrome.call("Runtime.evaluate", {
            "expression": f"({SET_STATUS_JS})({json.dumps(label)}, {json.dumps(strict)})",
            "awaitPromise": True, "returnByValue": True}, session=session, timeout=20).get("result", {}).get("value")

    def set_window_state(self, state):
        win = self.chrome.call("Browser.getWindowForTarget", {"targetId": self.window_target})
        self.chrome.call("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": {"windowState": state}})

    def handle_command(self, command, args):
        c = self.chrome
        if command == "open":
            if self.window_target:
                try:
                    self.set_window_state("normal")
                    c.call("Page.bringToFront", session=self.window_session())
                except RuntimeError:
                    pass
            else:
                open_window()
        elif command == "minimize":
            if self.window_target:
                self.set_window_state("minimized")
        elif command == "browse":
            # A normal Chrome window in the same profile, for the Web Store
            # and the extensions page (the app window has no address bar).
            c.call("Target.createTarget", {"url": args[0], "newWindow": True})
        elif command == "status":
            label, slash = args
            session = self.window_session()
            if session:
                c.call("Page.bringToFront", session=session)
                result = self.set_status_js(session, label, True)
            elif self.hidden_active and self.sessions.get(self.hidden_target):
                result = self.set_status_js(self.sessions[self.hidden_target], label, False)
            else:
                result = "Teams is not running"
            if result == "ok":
                return
            if not self.window_target:
                # The page without a window would not do it: open the window
                # (it stays open) and do it there.
                self.notify("error", f"set status in the background: {result}; opening the window")
                open_window()
                if self.wait_until(lambda: self.window_session() is not None, 15):
                    time.sleep(4)  # Teams rendering its top bar
                    session = self.window_session()
                    result = self.set_status_js(session, label, True)
                    if result == "ok":
                        return
            session = self.window_session()
            if not session:
                return
            self.notify("error", f"set status through the menu: {result}; trying {slash}")
            # Of last resort: the slash command in the search box (Ctrl+E).
            c.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "e", "code": "KeyE", "modifiers": 2,
                                              "windowsVirtualKeyCode": 69}, session=session)
            c.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "e", "code": "KeyE", "modifiers": 2,
                                              "windowsVirtualKeyCode": 69}, session=session)
            time.sleep(0.4)
            c.call("Input.insertText", {"text": slash}, session=session)
            time.sleep(0.4)
            for kind in ("keyDown", "keyUp"):
                c.call("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                  "windowsVirtualKeyCode": 13}, session=session)
        elif command == "activity":
            # The computer is in use: let the page see it too, unless the
            # window has focus (then it sees the real thing). A page without
            # a window gets no input events, so there the move is raised in
            # the page itself.
            if self.window_target:
                session = self.window_session()
                if self.page_focused(session):
                    return
                self.nudge = 3 - self.nudge  # 1, 2, 1, ...
                c.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": self.nudge, "y": 1}, session=session)
            elif self.hidden_active and self.sessions.get(self.hidden_target):
                session = self.sessions[self.hidden_target]
            else:
                return
            c.call("Runtime.evaluate", {"expression": ACTIVITY_JS}, session=session)
        elif command == "quit":
            self.quitting = True
            try:
                c.call("Browser.close", timeout=5)
            except (RuntimeError, TimeoutError, EOFError):
                pass

    def run(self):
        try:
            prepare_profile()
            self.chrome = DevTools([*chrome_args(), "--remote-debugging-pipe", "--no-startup-window"])
            self.chrome.call("Target.setDiscoverTargets", {"discover": True})
            self.grant_permissions()
            # The anchor first: it is what keeps Chrome alive with no window.
            self.create_hidden()
            if not self.background:
                open_window()
            self.notify("running", True)
            while True:
                try:
                    while True:
                        command, args = self.commands.get_nowait()
                        try:
                            self.handle_command(command, args)
                        except (RuntimeError, TimeoutError) as e:
                            self.notify("error", f"{command}: {e}")
                except queue.Empty:
                    pass
                for event in self.chrome.poll_events(0.25):
                    try:
                        self.handle_event(event)
                    except (RuntimeError, TimeoutError) as e:
                        self.notify("error", str(e))
                if self.chrome.proc.poll() is not None:
                    raise EOFError("Chrome exited")
        except (EOFError, OSError, RuntimeError, TimeoutError) as e:
            self.notify("running", False)
            if not self.quitting:
                self.notify("failed", f"Chrome stopped: {e}")
        finally:
            if self.chrome and self.chrome.proc.poll() is None:
                if not self.quitting:
                    self.chrome.proc.terminate()  # it failed to start up
                try:
                    self.chrome.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.chrome.proc.kill()
            self.notify("exited", None)


# The desktop's idle time, in milliseconds: KDE (and others) answer
# org.freedesktop.ScreenSaver, on Wayland too; GNOME's ScreenSaver doesn't,
# its Mutter idle monitor does.
IDLE_SOURCES = [
    ("org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver", "GetSessionIdleTime"),
    ("org.gnome.Mutter.IdleMonitor", "/org/gnome/Mutter/IdleMonitor/Core", "org.gnome.Mutter.IdleMonitor",
     "GetIdletime"),
]


def session_idle_ms():
    """Milliseconds since the last input, from the desktop, or None if it
    won't say."""
    from PyQt6.QtDBus import QDBus, QDBusConnection, QDBusMessage

    bus = QDBusConnection.sessionBus()
    for source in IDLE_SOURCES:
        reply = bus.call(QDBusMessage.createMethodCall(*source), QDBus.CallMode.Block, 1000)
        if reply.type() == QDBusMessage.MessageType.ErrorMessage or not reply.arguments():
            continue
        try:
            return int(reply.arguments()[0])
        except (TypeError, ValueError):
            continue
    return None


def single_instance():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or os.path.dirname(PROFILE)
    os.makedirs(runtime, exist_ok=True)
    lock = open(os.path.join(runtime, f"{APP_ID}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return lock


def presence_color(presence):
    p = (presence or "").lower()
    if p in ("available",):
        return "#6bb700"
    if p in ("busy", "in a call", "in a meeting", "presenting", "do not disturb", "focusing"):
        return "#c4314b"
    if p in ("away", "be right back"):
        return "#ffaa44"
    if p in ("offline", "appear offline", "out of office"):
        return "#8a8886"
    return None


# -- desktop integration: the app menu entry and the login autostart, both
# pointing at this AppImage wherever it is now (refreshed at every start).

def load_settings():
    try:
        with open(SETTINGS) as f:
            settings = json.load(f)
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    settings = {**load_settings(), **changes}
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS + ".tmp", "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(SETTINGS + ".tmp", SETTINGS)


def launch_command():
    """How to start this again: the AppImage, the frozen binary, or the script."""
    if os.environ.get("APPIMAGE"):
        return [os.environ["APPIMAGE"]]
    if frozen():
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def desktop_exec(args):
    """An Exec= line: each argument quoted as the Desktop Entry spec says."""
    def quote(arg):
        if not any(ch in arg for ch in " \t\n\"'\\><~|&;$*?#()`"):
            return arg.replace("%", "%%")
        escaped = "".join("\\" + ch if ch in '"`$\\' else ch for ch in arg)
        return '"' + escaped.replace("%", "%%") + '"'

    return " ".join(quote(a) for a in args)


def write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        f.write(text)
    os.replace(path + ".tmp", path)


def remove_file(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def install_icon():
    if not ICON or not os.path.exists(ICON):
        return "internet-chat"
    os.makedirs(os.path.dirname(MENU_ICON), exist_ok=True)
    shutil.copyfile(ICON, MENU_ICON)
    return MENU_ICON


def menu_entry_text(icon):
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "GenericName=Team chat and calls",
        "Comment=Microsoft Teams in its own window, with a tray icon",
        f"Exec={desktop_exec(launch_command())}",
        f"Icon={icon}",
        "Terminal=false",
        "StartupNotify=true",
        f"StartupWMClass={WM_CLASS}",
        "Categories=Network;Chat;InstantMessaging;",
        "Keywords=Teams;Microsoft;Chat;Meeting;Call;",
        "",
    ])


def autostart_text(icon):
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME} (background)",
        f"Exec={desktop_exec([*launch_command(), '--background'])}",
        f"Icon={icon}",
        "Terminal=false",
        "NoDisplay=true",
        "X-GNOME-Autostart-enabled=true",
        "X-KDE-autostart-phase=2",
        "",
    ])


def set_menu_entry(on):
    save_settings(menu_entry=on)
    if on:
        write_file(MENU_ENTRY, menu_entry_text(install_icon()))
    else:
        remove_file(MENU_ENTRY)
        if not os.path.exists(AUTOSTART_ENTRY):
            remove_file(MENU_ICON)


def set_autostart(on):
    if on:
        write_file(AUTOSTART_ENTRY, autostart_text(install_icon()))
    else:
        remove_file(AUTOSTART_ENTRY)
        if not os.path.exists(MENU_ENTRY):
            remove_file(MENU_ICON)


def refresh_desktop_entries():
    """Keep the entries pointing at this copy: an AppImage gets moved and
    renamed when it is updated. The menu entry is there unless turned off."""
    try:
        if load_settings().get("menu_entry", bool(os.environ.get("APPIMAGE"))):
            set_menu_entry(True)
        if os.path.exists(AUTOSTART_ENTRY):
            set_autostart(True)
    except OSError as e:
        log(f"could not update the desktop entries: {e}")


def main():
    parser = argparse.ArgumentParser(prog=APP_ID, description=f"{APP_NAME}: Microsoft Teams with a tray icon.")
    parser.add_argument("--background", action="store_true", help="start in the tray, with no window")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the app menu entry and the login autostart, then exit")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    opts, _ = parser.parse_known_args()

    if opts.uninstall:
        set_menu_entry(False)
        set_autostart(False)
        print(f"Removed the {APP_NAME} menu entry and autostart. Your sign-in and extensions are in {PROFILE}.")
        return 0

    background = opts.background
    lock = single_instance()
    if lock is None:
        # Already running: just ask it for the window (unless it is still
        # downloading Chrome).
        if not background and CHROME:
            open_window()
        return 0

    refresh_desktop_entries()

    try:
        from PyQt6.QtCore import QObject, QRect, Qt, QTimer, pyqtSignal
        from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap
        from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QProgressDialog, QSystemTrayIcon
    except ImportError as e:
        # Qt takes OpenGL, X11, Wayland and font libraries from the system.
        log(f"Qt could not start: {e}")
        if os.path.exists("/etc/NIXOS"):
            log("On NixOS, run AppImages through appimage-run, which provides the libraries they expect: "
                "`appimage-run Chatter-*.AppImage`. To run them directly, set "
                "programs.appimage = { enable = true; binfmt = true; }; in your configuration.")
        else:
            log("Install the missing library from your distribution (libGL.so.1 is in libgl1, mesa-libGL or "
                "libglvnd).")
        return 1

    class Bridge(QObject):
        event = pyqtSignal(str, object)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName(APP_ID)

    base = QPixmap(ICON) if ICON and os.path.exists(ICON) else QIcon.fromTheme("internet-chat").pixmap(64, 64)
    state = {"badge": 0, "title_badge": 0, "presence": None, "window": False, "running": False,
             "chrome_update": None}
    restart = [False]
    download = {}

    def draw_icon():
        size = 64
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if state["running"]:
            p.drawPixmap(QRect(0, 0, size, size), base)
        else:
            p.setOpacity(0.4)
            p.drawPixmap(QRect(0, 0, size, size), base)
            p.setOpacity(1)
        color = presence_color(state["presence"]) if state["running"] else None
        if color:
            d = 22
            p.setPen(QPen(QColor("white"), 3))
            p.setBrush(QColor(color))
            p.drawEllipse(size - d - 1, size - d - 1, d, d)
            if (state["presence"] or "").lower() == "do not disturb":
                p.setPen(QPen(QColor("white"), 4))
                p.drawLine(size - d + 4, size - d // 2 - 1, size - 6, size - d // 2 - 1)
            elif (state["presence"] or "").lower() in ("away", "be right back"):
                p.setPen(QPen(QColor("white"), 3))
                cx, cy = size - d // 2 - 1, size - d // 2 - 1
                p.drawLine(cx, cy - 6, cx, cy)
                p.drawLine(cx, cy, cx + 5, cy + 3)
        count = state["badge"] or state["title_badge"]
        if count and state["running"]:
            text = "99+" if count > 99 else str(count)
            w = 24 if len(text) == 1 else 30 if len(text) == 2 else 38
            h = 24
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(QColor("#d13438"))
            p.drawRoundedRect(size - w - 1, 1, w, h, h / 2, h / 2)
            p.setPen(QColor("white"))
            f = QFont()
            f.setBold(True)
            f.setPixelSize(16)
            p.setFont(f)
            p.drawText(QRect(size - w - 1, 1, w, h), Qt.AlignmentFlag.AlignCenter, text)
        p.end()
        return QIcon(pm)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        log("the desktop shows no tray icons (on GNOME, install the AppIndicator extension); "
            "a closed window comes back from the app menu")
    tray = QSystemTrayIcon()
    controller = Controller(lambda kind, value: bridge.event.emit(kind, value), background)
    bridge = Bridge()

    def refresh():
        tray.setIcon(draw_icon())
        count = state["badge"] or state["title_badge"]
        parts = [APP_NAME]
        if not CHROME:
            parts.append("downloading Chrome")
        elif not state["running"]:
            parts.append("not running")
        else:
            if state["presence"]:
                parts.append(state["presence"])
            parts.append(f"{count} unread" if count else "no unread messages")
        tray.setToolTip(" — ".join(parts))
        title.setText(" — ".join(parts[:2]))
        open_action.setText(f"Open {APP_NAME}" if state["running"] else f"Start {APP_NAME}")
        open_action.setEnabled(bool(CHROME))
        restart_action.setVisible(bool(state["chrome_update"]))
        if state["chrome_update"]:
            restart_action.setText(f"Restart to use Chrome {state['chrome_update']}")
        status_menu.setEnabled(state["running"])
        store_action.setEnabled(state["running"])
        manage_action.setEnabled(state["running"])

    def emit(kind, value):
        bridge.event.emit(kind, value)

    def chrome_ready(path):
        use_chrome(path)
        controller.start()
        version = managed_version(path)
        if version:
            remove_other_chromes(version)
            if not os.environ.get("CHATTER_CHROME_VERSION"):
                check_for_update()
                update_timer.start(UPDATE_CHECK_MS)
        refresh()

    def check_for_update():
        version = managed_version(CHROME)
        threading.Thread(target=update_chrome, args=(version, emit), daemon=True).start()

    def first_download():
        """No Chrome yet: download Chrome for Testing, with a progress window."""
        dialog = QProgressDialog(f"Downloading Chrome for Testing for {APP_NAME} (first start only)…", "Cancel", 0, 0)
        dialog.setWindowTitle(APP_NAME)
        dialog.setWindowIcon(QIcon(base))
        dialog.setMinimumWidth(460)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        cancel = threading.Event()
        dialog.canceled.connect(cancel.set)
        download["dialog"] = dialog

        def work():
            try:
                version = wanted_chrome_version()
                emit("download_progress", (version, 0, 0))
                path = install_chrome(version, lambda done, total: emit("download_progress", (version, done, total)),
                                      cancel.is_set)
                emit("download_done", path)
            except Cancelled:
                emit("download_failed", None)
            except Exception as e:
                emit("download_failed", str(e))

        threading.Thread(target=work, daemon=True).start()
        dialog.show()

    def on_download(kind, value):
        dialog = download["dialog"]
        if kind == "download_progress":
            version, done, total = value
            if done is None:
                dialog.setRange(0, 0)
                dialog.setLabelText(f"Unpacking Chrome for Testing {version}…")
            else:
                mb = f"{done / 1e6:.0f} of {total / 1e6:.0f} MB" if total else f"{done / 1e6:.0f} MB"
                dialog.setLabelText(f"Downloading Chrome for Testing {version} for {APP_NAME} "
                                    f"(first start only): {mb}")
                if total:
                    dialog.setRange(0, total // 1024)
                    dialog.setValue(done // 1024)
            return
        # (hide, not close: closing a progress dialog counts as Cancel)
        dialog.hide()
        dialog.deleteLater()
        if kind == "download_done":
            chrome_ready(value)
        elif value is None:
            app.quit()  # cancelled
        else:
            log(f"downloading Chrome for Testing: {value}")
            box = QMessageBox(QMessageBox.Icon.Critical, APP_NAME,
                              f"{APP_NAME} could not download Chrome for Testing, which it runs Teams in.\n\n{value}",
                              QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Close)
            if box.exec() == QMessageBox.StandardButton.Retry:
                first_download()
            else:
                app.quit()

    def on_event(kind, value):
        if kind.startswith("download_"):
            on_download(kind, value)
            return
        if kind == "chrome_update":
            tray.showMessage(APP_NAME, f"Chrome for Testing {value} is ready. It takes over when {APP_NAME} "
                             "next starts, or now with Restart in the tray menu.",
                             QSystemTrayIcon.MessageIcon.Information, 10_000)
        if kind == "failed":
            tray.showMessage(APP_NAME, f"{value}. Click the tray icon to start again.",
                             QSystemTrayIcon.MessageIcon.Warning, 10_000)
            kind = "error"
        if kind in state:
            state[kind] = value
        elif kind == "error":
            log(value)
        elif kind == "exited":
            if controller.quitting:
                app.quit()
                return
            state.update(running=False, window=False, badge=0, title_badge=0, presence=None)
        refresh()

    bridge.event.connect(on_event)

    def start():
        nonlocal controller
        if not CHROME:
            return  # still downloading it
        if state["running"]:
            controller.send("open")
            return
        controller = Controller(lambda kind, value: bridge.event.emit(kind, value), False)
        controller.start()

    def activated(reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            start()
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            controller.send("minimize")

    def open_extensions_folder():
        os.makedirs(EXTENSIONS_DIR, exist_ok=True)
        try:
            spawn(["xdg-open", EXTENSIONS_DIR])
        except OSError as e:
            log(f"open {EXTENSIONS_DIR}: {e}")

    def toggled(setter):
        def apply(on):
            try:
                setter(on)
            except OSError as e:
                log(str(e))
        return apply

    menu = QMenu()
    title = QAction(APP_NAME, menu)
    title.setEnabled(False)
    menu.addAction(title)
    menu.addSeparator()
    open_action = QAction(QIcon.fromTheme("window-new"), f"Open {APP_NAME}", menu)
    open_action.triggered.connect(start)
    menu.addAction(open_action)
    status_menu = menu.addMenu(QIcon.fromTheme("user-status-pending"), "Set status")
    for label, command in STATUSES:
        act = QAction(label, status_menu)
        act.triggered.connect(lambda _checked=False, lbl=label, cmd=command: controller.send("status", lbl, cmd))
        status_menu.addAction(act)

    extensions_menu = menu.addMenu(QIcon.fromTheme("preferences-plugin"), "Extensions")
    store_action = QAction(QIcon.fromTheme("internet-web-browser"), "Get extensions (Chrome Web Store)…",
                           extensions_menu)
    store_action.triggered.connect(lambda: controller.send("browse", WEB_STORE))
    extensions_menu.addAction(store_action)
    manage_action = QAction(QIcon.fromTheme("configure"), "Manage extensions…", extensions_menu)
    manage_action.triggered.connect(lambda: controller.send("browse", EXTENSIONS_PAGE))
    extensions_menu.addAction(manage_action)
    extensions_menu.addSeparator()
    folder_action = QAction(QIcon.fromTheme("folder"), "Unpacked extensions folder (loaded at start)…",
                            extensions_menu)
    folder_action.triggered.connect(open_extensions_folder)
    extensions_menu.addAction(folder_action)

    menu.addSeparator()
    menu_entry_action = QAction("Show in the app menu", menu, checkable=True)
    menu_entry_action.setChecked(os.path.exists(MENU_ENTRY))
    menu_entry_action.toggled.connect(toggled(set_menu_entry))
    menu.addAction(menu_entry_action)
    autostart_action = QAction("Start at login", menu, checkable=True)
    autostart_action.setChecked(os.path.exists(AUTOSTART_ENTRY))
    autostart_action.toggled.connect(toggled(set_autostart))
    menu.addAction(autostart_action)
    menu.addSeparator()
    restart_action = QAction(QIcon.fromTheme("view-refresh"), "Restart", menu)

    def restart_now():
        restart[0] = True
        quit_all()

    restart_action.triggered.connect(restart_now)
    menu.addAction(restart_action)
    quit_action = QAction(QIcon.fromTheme("application-exit"), f"Quit {APP_NAME}", menu)

    def quit_all():
        if state["running"]:
            controller.quitting = True
            controller.send("quit")
        else:
            app.quit()

    quit_action.triggered.connect(quit_all)
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(activated)
    idle_warned = [False]

    def pass_activity_on():
        if not state["running"]:
            return
        idle = session_idle_ms()
        if idle is None:
            if not idle_warned[0]:
                idle_warned[0] = True
                log("the desktop does not report idle time; presence follows the Teams page only")
            return
        if idle < ACTIVE_WITHIN_MS:
            controller.send("activity")

    activity_timer = QTimer()
    activity_timer.timeout.connect(pass_activity_on)
    activity_timer.start(ACTIVITY_INTERVAL_MS)

    update_timer = QTimer()
    update_timer.timeout.connect(check_for_update)

    refresh()
    tray.show()
    if CHROME:
        chrome_ready(CHROME)
    else:
        first_download()
    status = app.exec()
    if restart[0]:
        lock.close()
        command = launch_command()
        os.execv(command[0], command)
    return status


if __name__ == "__main__":
    sys.exit(main())
