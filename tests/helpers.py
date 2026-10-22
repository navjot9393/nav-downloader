"""Test helpers: run the real server.py and dev/mock_jd.py as subprocesses and talk HTTP to them."""

import http.cookiejar
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE_RE = re.compile(r"(setup|reset) code: ([A-Z2-9]{4}-[A-Z2-9]{4})")
GENERAL = "org.jdownloader.settings.GeneralSettings"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url, proc, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"process exited with {proc.returncode} before {url} came up")
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.05)
    raise RuntimeError(f"{url} didn't come up")


class MockJD:
    """dev/mock_jd.py in its own process."""

    def __init__(self):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen([sys.executable, "-I", str(ROOT / "dev" / "mock_jd.py"), str(self.port)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(self.url + "/downloadcontroller/getCurrentState", self.proc)

    def call(self, path, *params):
        query = "&".join(urllib.parse.quote(json.dumps(p), safe="") for p in params)
        with urllib.request.urlopen(f"{self.url}/{path}" + (f"?{query}" if query else ""), timeout=5) as r:
            return json.loads(r.read())["data"]

    def stop(self):
        self.proc.terminate()
        self.proc.wait(5)


class Server:
    """server.py in its own process, with a fresh data folder. Its log is kept so
    tests can read the setup and reset codes it prints."""

    def __init__(self, jd_url, **env):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.data = tempfile.mkdtemp(prefix="navdl-test-")
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("JDWEB_", "JD_API"))}
        clean.update(JDWEB_PORT=str(self.port), JDWEB_BIND="127.0.0.1", JD_API=jd_url, JDWEB_DATA=self.data, **env)
        self.log = []
        self.proc = subprocess.Popen([sys.executable, "-I", "-u", str(ROOT / "server.py")], env=clean,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._read, daemon=True).start()
        try:
            wait_for(self.base + "/healthz", self.proc)
        except RuntimeError:
            self.stop()
            raise

    def _read(self):
        for line in self.proc.stdout:
            self.log.append(line)

    def codes(self, kind=None):
        return [code for k, code in CODE_RE.findall("".join(self.log)) if kind in (None, k)]

    def wait_code(self, kind, count=1, timeout=5):
        """The newest code of `kind` once at least `count` have been printed."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.codes(kind)
            if len(found) >= count:
                return found[-1]
            time.sleep(0.05)
        raise AssertionError(f"no {kind} code #{count} in the log:\n{''.join(self.log)}")

    def client(self, ip=None):
        return Client(self.base, ip)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(5)
        self.proc.stdout.close()
        shutil.rmtree(self.data, ignore_errors=True)


class Client:
    """A browser stand-in: keeps the session cookie, sends the X-JDWeb header,
    and can pose as a client behind a proxy (X-Forwarded-For)."""

    def __init__(self, base, ip=None):
        self.base = base
        self.ip = ip
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.last_headers = None

    def request(self, path, body=None, headers=None):
        h = {"X-JDWeb": "1", "Content-Type": "application/json", **(headers or {})}
        if self.ip:
            h["X-Forwarded-For"] = self.ip
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with self.opener.open(req, timeout=20) as r:
                self.last_headers = r.headers
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                self.last_headers = e.headers
                return e.code, json.loads(e.read() or b"{}")

    def get(self, path, **kw):
        return self.request(path, **kw)

    def post(self, path, body=None, **kw):
        return self.request(path, body if body is not None else {}, **kw)
