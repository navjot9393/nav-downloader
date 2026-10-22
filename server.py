#!/usr/bin/env python3
"""Nav Downloader: a small web UI for a headless JDownloader 2.

It talks to JDownloader's local API (RemoteAPIConfig: deprecatedapienabled
= true, deprecatedapilocalhostonly = true, port 3128; setup_jd.py sets these).
That API has no login of its own, so it must stay bound to localhost. This
server is the only part that is exposed: it serves the page, owns the login,
and forwards a fixed set of actions to JDownloader, never arbitrary API calls.

Standard library only. Configured through the environment:
  JDWEB_PORT           port to listen on                         (default 30500)
  JDWEB_BIND           address to listen on                      (default 0.0.0.0)
  JD_API               JDownloader local API base URL            (default http://127.0.0.1:3128)
  JDWEB_DATA           folder for the password hash + session key (default /data)
  JDWEB_PASSWORD       sets the password at every start (no setup page, no change
                       from the page); JDWEB_PASSWORD_FILE reads it from a file
  JDWEB_FOLDERS        "Label=/path,Label=/path": folders offered under "Save to",
                       as JDownloader sees them, mounted at the same paths here
  JDWEB_TRUST_PROXY    1 behind a reverse proxy: use X-Forwarded-For/-Proto
  JDWEB_IMAGE          image name shown in setup instructions (set by the Dockerfile)

Without a password, the server prints a one-time setup code to its log; the
page asks for it, so only someone who can read the log can claim the install.
"Forgot password?" works the same way with a reset code.
"""

import hashlib
import hmac
import http.client
import http.cookies
import http.server
import json
import os
import secrets
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("JDWEB_PORT", "30500"))
BIND = os.environ.get("JDWEB_BIND", "0.0.0.0")
JD_API = os.environ.get("JD_API", "http://127.0.0.1:3128").rstrip("/")
DATA = Path(os.environ.get("JDWEB_DATA", "/data"))
STATIC = Path(__file__).resolve().parent / "static"
TRUST_PROXY = os.environ.get("JDWEB_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")
IMAGE = os.environ.get("JDWEB_IMAGE", "nav-downloader")
FOLDERS_ENV = os.environ.get("JDWEB_FOLDERS", "").strip()

AUTH_FILE = DATA / "auth.json"
KEY_FILE = DATA / "session.key"
# Optional, written by a helper outside the container: the folders JDownloader
# can save into, as JDownloader sees them. JDWEB_FOLDERS takes precedence.
FOLDERS_FILE = DATA / "folders.json"
COOKIE = "jdweb_session"
SESSION_SECONDS = 30 * 86400
MIN_PASSWORD = 8
MAX_BODY = 1 << 20  # 1 MiB: room for a few thousand pasted links
GENERAL = "org.jdownloader.settings.GeneralSettings"
MAX_DOWNLOADS = 20  # JDownloader's own settings screen allows 1-20 at a time
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread
CODE_LENGTH = 8         # 32^8: about 40 bits
CODE_TRIES = 5
RESET_CODE_SECONDS = 15 * 60
CODE_REISSUE_SECONDS = 60
FOLDER_DEPTH = 3        # sub-folder levels listed under each JDWEB_FOLDERS entry
FOLDER_MAX = 20000      # per entry, keeps the list small
FOLDER_RESCAN_SECONDS = 60

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


# ---------------------------------------------------------------- password + sessions

def _scrypt(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1,
                          maxmem=64 * 1024 * 1024, dklen=32)


def load_auth():
    try:
        return json.loads(AUTH_FILE.read_text())
    except FileNotFoundError:
        return None


def write_auth(password, first_time):
    salt = secrets.token_bytes(16)
    record = json.dumps({"salt": salt.hex(), "hash": _scrypt(password, salt).hex()})
    if first_time:
        # O_EXCL: two people racing the setup page can't both win.
        fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(record)
    else:
        tmp = AUTH_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(record)
        os.replace(tmp, AUTH_FILE)


def check_password(password):
    auth = load_auth()
    if not auth:
        return False
    digest = _scrypt(password, bytes.fromhex(auth["salt"]))
    return hmac.compare_digest(digest.hex(), auth["hash"])


def session_key():
    try:
        return bytes.fromhex(KEY_FILE.read_text().strip())
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        try:
            fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key.hex())
            return key
        except FileExistsError:
            return bytes.fromhex(KEY_FILE.read_text().strip())


def _sign(expires):
    # Bound to the password hash, so changing the password signs everyone out.
    auth = load_auth() or {}
    msg = f"{expires}.{auth.get('hash', '')}".encode()
    return hmac.new(session_key(), msg, hashlib.sha256).hexdigest()


def new_session():
    expires = int(time.time()) + SESSION_SECONDS
    return f"{expires}.{_sign(expires)}"


def valid_session(token):
    try:
        expires_s, sig = token.split(".", 1)
        expires = int(expires_s)
    except (AttributeError, ValueError):
        return False
    return expires > time.time() and load_auth() is not None and hmac.compare_digest(sig, _sign(expires))


class LoginThrottle:
    """After 5 wrong passwords from one address, wait 30 s, then 60 s, ... up to 15 min."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fails = {}

    def wait_seconds(self, ip):
        with self._lock:
            _, until = self._fails.get(ip, (0, 0))
            return max(0, int(until - time.time()))

    def failed(self, ip):
        with self._lock:
            count, _ = self._fails.get(ip, (0, 0))
            count += 1
            until = time.time() + min(900, 30 * 2 ** (count - 5)) if count >= 5 else 0
            self._fails[ip] = (count, until)

    def succeeded(self, ip):
        with self._lock:
            self._fails.pop(ip, None)


throttle = LoginThrottle()


def env_password():
    """The password from JDWEB_PASSWORD or JDWEB_PASSWORD_FILE, or None."""
    value = os.environ.get("JDWEB_PASSWORD")
    path = os.environ.get("JDWEB_PASSWORD_FILE")
    if not value and path:
        value = Path(path).read_text().rstrip("\r\n")
    return value or None


PASSWORD_MANAGED = False  # True when JDWEB_PASSWORD sets the password (main)


class OneTimeCode:
    """The setup or reset code. It goes to the log only, never to the page, so
    using it proves access to the server. One at a time, a few tries each."""

    def __init__(self):
        self._lock = threading.Lock()
        self._code = None
        self._issued = 0.0
        self._expires = 0.0
        self._tries = 0

    def issue(self, purpose):
        """Print a new code. Returns 0, or the seconds to wait when one was printed moments ago."""
        with self._lock:
            now = time.time()
            if self._code and now < self._expires and now - self._issued < CODE_REISSUE_SECONDS:
                return int(CODE_REISSUE_SECONDS - (now - self._issued)) + 1
            raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            self._code, self._issued, self._tries = raw, now, CODE_TRIES
            # The setup code lasts until a password is set; a reset code 15 minutes.
            self._expires = float("inf") if purpose == "setup" else now + RESET_CODE_SECONDS
        shown = f"{raw[:4]}-{raw[4:]}"
        if purpose == "setup":
            lines = [f"Nav Downloader setup code: {shown}",
                     "Open the page and enter this code to create your password."]
        else:
            lines = [f"Nav Downloader password reset code: {shown} (valid {RESET_CODE_SECONDS // 60} minutes)",
                     'Enter it on the sign-in page under "Forgot password?".']
        rule = "=" * max(len(line) for line in lines)
        print("\n".join([rule, *lines, rule]), flush=True)
        return 0

    def check(self, attempt):
        """True once for the right code. Wrong tries use the code up."""
        cleaned = "".join(c for c in str(attempt or "").upper() if c.isalnum())
        with self._lock:
            if not self._code or time.time() >= self._expires:
                return False
            if hmac.compare_digest(cleaned.encode(), self._code.encode()):
                self._code = None
                return True
            self._tries -= 1
            if self._tries <= 0:
                self._code = None
            return False


one_time_code = OneTimeCode()


# ---------------------------------------------------------------- JDownloader API

class JDError(Exception):
    pass


class JDUnreachable(JDError):
    def __init__(self, message, kind="other"):
        super().__init__(message)
        self.kind = kind  # "refused", "timeout" or "other", for the setup help on the page


class BadRequest(Exception):
    pass


def jd(path, *params, form=False, timeout=15):
    """Call the local API. Each parameter is sent JSON-encoded, joined with '&'
    (the API's own format); `form` sends them as a POST body for long link lists."""
    encoded = "&".join(urllib.parse.quote(json.dumps(p), safe="") for p in params)
    url, data, headers = f"{JD_API}/{path}", None, {}
    if params and form:
        data = encoded.encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif params:
        url += "?" + encoded
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise JDError(f"JDownloader refused {path} (HTTP {e.code})") from e
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        reason = getattr(e, "reason", e)
        kind = ("refused" if isinstance(reason, ConnectionRefusedError)
                else "timeout" if isinstance(reason, TimeoutError) else "other")
        raise JDUnreachable("Can't reach JDownloader. It may be starting up.", kind) from e
    try:
        return json.loads(body).get("data")
    except (ValueError, AttributeError) as e:
        raise JDError(f"Unexpected reply from JDownloader for {path}") from e


def jd_status():
    """For the "Connect to JDownloader" help: can we reach the API, and if not, how it fails."""
    status = {"api": JD_API, "image": IMAGE}
    try:
        status["state"] = jd("downloadcontroller/getCurrentState", timeout=4)
    except JDUnreachable as e:
        return {**status, "reachable": False, "error": e.kind}
    except JDError as e:
        return {**status, "reachable": False, "error": "http", "detail": str(e)}
    return {**status, "reachable": True}


PACKAGE_QUERY = {
    "bytesLoaded": True, "bytesTotal": True, "childCount": True, "enabled": True, "eta": True,
    "finished": True, "hosts": True, "running": True, "saveTo": True, "speed": True,
    "status": True, "maxResults": -1, "startAt": 0,
}
LINK_QUERY = {
    "bytesLoaded": True, "bytesTotal": True, "enabled": True, "eta": True, "extractionStatus": True,
    "finished": True, "host": True, "running": True, "skipped": True, "speed": True,
    "status": True, "maxResults": -1, "startAt": 0,
}
GRABBER_PACKAGE_QUERY = {
    "availableOfflineCount": True, "availableOnlineCount": True, "availableTempUnknownCount": True,
    "availableUnknownCount": True, "bytesTotal": True, "childCount": True, "enabled": True,
    "hosts": True, "saveTo": True, "status": True, "maxResults": -1, "startAt": 0,
}
GRABBER_LINK_QUERY = {
    "availability": True, "bytesTotal": True, "enabled": True, "host": True, "status": True,
    "maxResults": -1, "startAt": 0,
}


def _nest(packages, links):
    by_package = {}
    for link in links or []:
        by_package.setdefault(link.get("packageUUID"), []).append(link)
    packages = packages or []
    for package in packages:
        package["links"] = by_package.get(package.get("uuid"), [])
    return packages


def get_state():
    return {
        "controller": jd("downloadcontroller/getCurrentState"),
        "speed": jd("downloadcontroller/getSpeedInBps") or 0,
        "limit": {
            "enabled": bool(jd("config/get", GENERAL, None, "DownloadSpeedLimitEnabled")),
            "bytes": jd("config/get", GENERAL, None, "DownloadSpeedLimit") or 0,
        },
        # Files downloading at once; JDownloader queues the rest.
        "maxDownloads": jd("config/get", GENERAL, None, "MaxSimultaneDownloads") or 0,
        "collecting": bool(jd("linkgrabberv2/isCollecting")),
        "downloads": _nest(jd("downloadsV2/queryPackages", PACKAGE_QUERY),
                           jd("downloadsV2/queryLinks", LINK_QUERY)),
        "grabber": _nest(jd("linkgrabberv2/queryPackages", GRABBER_PACKAGE_QUERY),
                         jd("linkgrabberv2/queryLinks", GRABBER_LINK_QUERY)),
        "captchas": len(jd("captcha/list") or []),
    }


def parse_folders_env(value):
    """JDWEB_FOLDERS: "Label=/path,Label=/path" (or bare paths) -> [(label, path)]."""
    roots = []
    for part in value.split(","):
        label, sep, path = part.strip().partition("=")
        if not sep:
            label, path = "", label
        path = path.strip().rstrip("/")
        if not path:
            continue
        if not path.startswith("/"):
            print(f"JDWEB_FOLDERS: ignoring {path!r}, use the full path as JDownloader sees it", flush=True)
            continue
        roots.append((label.strip() or os.path.basename(path), path))
    return roots


FOLDER_ROOTS = parse_folders_env(FOLDERS_ENV)
_scan = {"at": 0.0, "data": None}
_scan_lock = threading.Lock()


def sub_folders(root):
    """Sub-folders of `root`, FOLDER_DEPTH levels deep, as paths relative to it."""
    found = []
    for current, dirs, _ in os.walk(root):
        rel = os.path.relpath(current, root)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = sorted((d for d in dirs if not d.startswith((".", "@", "$"))), key=str.lower)
        if level >= FOLDER_DEPTH:
            dirs[:] = []
        for d in dirs:
            found.append(d if rel == "." else f"{rel}/{d}")
            if len(found) >= FOLDER_MAX:
                return found
    return sorted(found, key=str.lower)


def scan_folders():
    """The JDWEB_FOLDERS list with sub-folders, rescanned at most once a minute.
    These folders are mounted here at the same paths JDownloader uses."""
    with _scan_lock:
        if _scan["data"] is not None and time.time() - _scan["at"] < FOLDER_RESCAN_SECONDS:
            return _scan["data"]
        try:
            jd_default = str(jd("config/get", GENERAL, None, "DefaultDownloadFolder", timeout=4) or "").rstrip("/")
        except JDError:
            jd_default = ""
        roots = [{"label": label, "path": path, "host": "", "default": path == jd_default,
                  "dirs": sub_folders(path) if os.path.isdir(path) else []}
                 for label, path in FOLDER_ROOTS]
        roots.sort(key=lambda r: not r["default"])  # stable: otherwise in the order given
        default = jd_default if any(r["default"] for r in roots) else roots[0]["path"]
        _scan.update(at=time.time(), data={"generated": int(time.time()), "default": default, "roots": roots})
        return _scan["data"]


_folders_cache = {"mtime": None, "data": None}


def load_folders():
    if FOLDER_ROOTS:
        return scan_folders()
    try:
        mtime = FOLDERS_FILE.stat().st_mtime
    except FileNotFoundError:
        return None
    if mtime != _folders_cache["mtime"]:
        try:
            _folders_cache["data"] = json.loads(FOLDERS_FILE.read_text())
        except ValueError:
            return _folders_cache["data"]
        _folders_cache["mtime"] = mtime
    return _folders_cache["data"]


def resolve_folder(path, new_name=""):
    """Turn a picked folder (+ optional new sub-folder name) into a download
    destination. Only folders from the published list are accepted; None means
    JDownloader's own default."""
    path = str(path or "").strip().rstrip("/")
    name = str(new_name or "").strip()
    if not path and not name:
        return None
    folders = load_folders() or {}
    if not path:
        path = str(folders.get("default") or "").rstrip("/")
    for root in folders.get("roots", []):
        base = root["path"].rstrip("/")
        if path == base or (path.startswith(base + "/") and path[len(base) + 1:] in set(root.get("dirs", []))):
            break
    else:
        raise BadRequest("Pick a folder from the list.")
    if name:
        if name in (".", "..") or name.startswith(".") or any(c in name for c in '/\\\0') or len(name) > 200:
            raise BadRequest("A new folder name can't contain / or start with a dot.")
        path = f"{path}/{name}"
    return path


def _ids(body, key):
    value = body.get(key) or []
    if not isinstance(value, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in value):
        raise BadRequest(f"{key} must be a list of ids")
    return value


def do_action(body):
    action = body.get("action")
    links, packages = _ids(body, "linkIds"), _ids(body, "packageIds")

    if action == "start":
        return jd("downloadcontroller/start")
    if action == "stop":
        return jd("downloadcontroller/stop")
    if action == "pause":
        return jd("downloadcontroller/pause", bool(body.get("value")))

    if action == "add":
        text = str(body.get("links") or "").strip()
        if not text:
            raise BadRequest("Paste at least one link.")
        query = {"links": text, "autostart": bool(body.get("autostart")), "deepDecrypt": False}
        for key in ("packageName", "extractPassword"):
            value = str(body.get(key) or "").strip()
            if value:
                query[key] = value
        folder = resolve_folder(body.get("folder"), body.get("newFolder"))
        if folder:
            query["destinationFolder"] = folder
        return jd("linkgrabberv2/addLinks", query, form=True)

    if action == "grabber_download":
        if not links and not packages:
            packages = [p["uuid"] for p in jd("linkgrabberv2/queryPackages", {"maxResults": -1, "startAt": 0}) or []]
        jd("linkgrabberv2/moveToDownloadlist", links, packages)
        if jd("downloadcontroller/getCurrentState") in ("IDLE", "STOPPED_STATE"):
            jd("downloadcontroller/start")
        return True
    if action == "grabber_remove":
        return jd("linkgrabberv2/removeLinks", links, packages)
    if action == "grabber_clear":
        return jd("linkgrabberv2/clearList")
    if action == "grabber_folder":
        folder = resolve_folder(body.get("folder"), body.get("newFolder"))
        if not packages or not folder:
            raise BadRequest("Pick a package and a folder.")
        # setDownloadDirectory uses the path as given, while adding links puts each
        # package in its own sub-folder (packagizer). Keep moves consistent with that.
        names = {p["uuid"]: p.get("name") or "" for p in
                 jd("linkgrabberv2/queryPackages", {"packageUUIDs": packages, "maxResults": -1, "startAt": 0}) or []}
        for uuid in packages:
            name = names.get(uuid, "").replace("/", "_").strip(". ")
            jd("linkgrabberv2/setDownloadDirectory", f"{folder}/{name}" if name else folder, [uuid])
        return True

    if action == "dl_enable":
        return jd("downloadsV2/setEnabled", bool(body.get("enabled")), links, packages)
    if action == "dl_force":
        return jd("downloadsV2/forceDownload", links, packages)
    if action == "dl_reset":
        return jd("downloadsV2/resetLinks", links, packages)
    if action == "dl_remove":
        if not links and not packages:
            raise BadRequest("Nothing selected.")
        if body.get("deleteFiles"):
            return jd("downloadsV2/cleanup", links, packages, "DELETE_ALL",
                      "REMOVE_LINKS_AND_DELETE_FILES", "SELECTED")
        return jd("downloadsV2/removeLinks", links, packages)
    if action == "dl_clear_finished":
        return jd("downloadsV2/cleanup", [], [], "DELETE_FINISHED", "REMOVE_LINKS_ONLY", "ALL")

    if action == "speed_limit":
        enabled = bool(body.get("enabled"))
        if enabled:
            try:
                limit = int(body.get("bytes"))
            except (TypeError, ValueError):
                raise BadRequest("Speed limit must be a number.")
            if not 1024 <= limit <= 10 * 1024**3:
                raise BadRequest("Speed limit must be between 1 KB/s and 10 GB/s.")
            jd("config/set", GENERAL, None, "DownloadSpeedLimit", limit)
        jd("config/set", GENERAL, None, "DownloadSpeedLimitEnabled", enabled)
        return True

    if action == "max_downloads":
        value = body.get("value")
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_DOWNLOADS:
            raise BadRequest(f"Pick between 1 and {MAX_DOWNLOADS} downloads at a time.")
        return jd("config/set", GENERAL, None, "MaxSimultaneDownloads", value)

    raise BadRequest("Unknown action.")


# ---------------------------------------------------------------- HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "NavDownloader"
    sys_version = ""

    def log_message(self, fmt, *args):
        # The page polls /api/state every 2 s; logging each one would bury the useful lines.
        if not self.path.startswith(("/api/state", "/healthz")):
            sys.stdout.write("%s %s\n" % (self._client_ip(), fmt % args))
            sys.stdout.flush()

    # --- helpers

    def _client_ip(self):
        # Behind a trusted reverse proxy every request comes from the proxy; the
        # address it appended last is the real client (earlier ones can be forged).
        if TRUST_PROXY:
            forwarded = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            if forwarded:
                return forwarded
        return self.client_address[0]

    def _send(self, code, body, content_type, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj, extra=()):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self._send(code, body, "application/json", (("Cache-Control", "no-store"),) + tuple(extra))

    def _cookie(self, value, max_age):
        https = TRUST_PROXY and self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        secure = "; Secure" if https else ""
        return ("Set-Cookie", f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict{secure}")

    def _authenticated(self):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return False
        morsel = jar.get(COOKIE)
        return bool(morsel) and valid_session(morsel.value)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise BadRequest("Bad request.")
        if length > MAX_BODY:
            raise BadRequest("Too much text in one go.")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise BadRequest("Bad request.")
        if not isinstance(data, dict):
            raise BadRequest("Bad request.")
        return data

    # --- routes

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self._send(200, b"ok\n", "text/plain; charset=utf-8")
        if path == "/api/session":
            return self._json(200, {"authenticated": self._authenticated(), "setup": load_auth() is None,
                                    "passwordManaged": PASSWORD_MANAGED})
        if path == "/api/jd-status":
            if not self._authenticated():
                return self._json(401, {"error": "Please sign in."})
            return self._json(200, jd_status())
        if path == "/api/folders":
            if not self._authenticated():
                return self._json(401, {"error": "Please sign in."})
            return self._json(200, load_folders() or {"roots": [], "default": None})
        if path == "/api/state":
            if not self._authenticated():
                return self._json(401, {"error": "Please sign in."})
            try:
                return self._json(200, get_state())
            except JDUnreachable as e:
                return self._json(503, {"error": str(e), "unreachable": True})
            except JDError as e:
                return self._json(502, {"error": str(e)})
        return self._static(path)

    def _static(self, path):
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if target.parent != STATIC or not target.is_file() or target.suffix not in STATIC_TYPES:
            return self._send(404, b"Not found\n", "text/plain; charset=utf-8")
        self._send(200, target.read_bytes(), STATIC_TYPES[target.suffix], (("Cache-Control", "no-cache"),))

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        # A custom header can't be sent cross-site without a CORS preflight, which
        # this server never answers: together with SameSite=Strict that stops CSRF.
        if self.headers.get("X-JDWeb") != "1":
            return self._json(403, {"error": "Forbidden."})
        ip = self._client_ip()
        try:
            body = self._body()

            if path == "/api/setup":
                if load_auth() is not None:
                    return self._json(409, {"error": "A password is already set. Please sign in."})
                password = self._new_password(body)
                self._check_code(body, ip, "That setup code is wrong or used up. Use the latest one from the log, or get a new one.")
                try:
                    write_auth(password, first_time=True)
                except FileExistsError:
                    return self._json(409, {"error": "A password is already set. Please sign in."})
                return self._json(200, {"ok": True}, (self._cookie(new_session(), SESSION_SECONDS),))

            if path == "/api/reset/request":
                self._password_not_managed()
                setup = load_auth() is None
                wait = one_time_code.issue("setup" if setup else "reset")
                return self._json(200, {"ok": True, "setup": setup, "waitSeconds": wait})

            if path == "/api/reset/confirm":
                self._password_not_managed()
                if load_auth() is None:
                    raise BadRequest("No password is set yet. Create one instead.")
                password = self._new_password(body)
                self._check_code(body, ip, "That code is wrong or has expired. Ask for a new one.")
                write_auth(password, first_time=False)
                throttle.succeeded(ip)
                print("Password was reset with a code from the log.", flush=True)
                return self._json(200, {"ok": True}, (self._cookie(new_session(), SESSION_SECONDS),))

            if path == "/api/login":
                wait = throttle.wait_seconds(ip)
                if wait:
                    return self._json(429, {"error": f"Too many attempts. Try again in {wait} s."})
                if not check_password(str(body.get("password") or "")):
                    throttle.failed(ip)
                    time.sleep(0.5)
                    return self._json(401, {"error": "Wrong password."})
                throttle.succeeded(ip)
                return self._json(200, {"ok": True}, (self._cookie(new_session(), SESSION_SECONDS),))

            if path == "/api/logout":
                return self._json(200, {"ok": True}, (self._cookie("", 0),))

            if not self._authenticated():
                return self._json(401, {"error": "Please sign in."})

            if path == "/api/password":
                self._password_not_managed()
                if not check_password(str(body.get("current") or "")):
                    throttle.failed(ip)
                    return self._json(400, {"error": "Current password is wrong."})
                new = str(body.get("new") or "")
                if len(new) < MIN_PASSWORD:
                    raise BadRequest(f"Use at least {MIN_PASSWORD} characters.")
                write_auth(new, first_time=False)
                return self._json(200, {"ok": True}, (self._cookie(new_session(), SESSION_SECONDS),))

            if path == "/api/do":
                return self._json(200, {"ok": True, "result": do_action(body)})

            return self._json(404, {"error": "Not found."})
        except Throttled as e:
            return self._json(429, {"error": f"Too many attempts. Try again in {e.wait} s."})
        except BadRequest as e:
            return self._json(400, {"error": str(e)})
        except JDUnreachable as e:
            return self._json(503, {"error": str(e), "unreachable": True})
        except JDError as e:
            return self._json(502, {"error": str(e)})

    # --- setup / reset helpers

    @staticmethod
    def _new_password(body):
        password = str(body.get("password") or "")
        if len(password) < MIN_PASSWORD:
            raise BadRequest(f"Use at least {MIN_PASSWORD} characters.")
        return password

    @staticmethod
    def _check_code(body, ip, message):
        wait = throttle.wait_seconds(ip)
        if wait:
            raise Throttled(wait)
        if not one_time_code.check(body.get("code")):
            throttle.failed(ip)
            raise BadRequest(message)

    @staticmethod
    def _password_not_managed():
        if PASSWORD_MANAGED:
            raise BadRequest("The password is set with JDWEB_PASSWORD. Change it there and restart the container.")


class Throttled(Exception):
    def __init__(self, wait):
        super().__init__(wait)
        self.wait = wait


def main():
    global PASSWORD_MANAGED
    DATA.mkdir(parents=True, exist_ok=True)
    session_key()
    try:
        password = env_password()
    except OSError as e:
        sys.exit(f"Can't read JDWEB_PASSWORD_FILE: {e}")
    if password is not None:
        if len(password) < MIN_PASSWORD:
            sys.exit(f"JDWEB_PASSWORD must be at least {MIN_PASSWORD} characters.")
        PASSWORD_MANAGED = True
        if not check_password(password):
            write_auth(password, first_time=False)
            print("Password set from JDWEB_PASSWORD.", flush=True)
    # As PID 1 in a container, SIGTERM is ignored unless handled: exit promptly on docker stop.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = http.server.ThreadingHTTPServer((BIND, PORT), Handler)
    server.daemon_threads = True
    print(f"Nav Downloader listening on {BIND}:{PORT}, JDownloader API {JD_API}"
          + (", behind a reverse proxy" if TRUST_PROXY else ""), flush=True)
    if load_auth() is None:
        one_time_code.issue("setup")  # last, so it's easy to spot in the log
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
