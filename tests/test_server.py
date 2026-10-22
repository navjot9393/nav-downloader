"""End-to-end tests: the real server.py against the fake JDownloader in dev/mock_jd.py.

    python3 -m unittest discover -s tests -v
"""

import os
import shutil
import tempfile
import time
import unittest

from helpers import GENERAL, MockJD, Server, free_port

PASSWORD = "correct horse battery"


class ServerTest(unittest.TestCase):
    jd = None

    @classmethod
    def setUpClass(cls):
        cls.jd = MockJD()

    @classmethod
    def tearDownClass(cls):
        cls.jd.stop()

    def start(self, **env):
        server = Server(self.jd.url, **env)
        self.addCleanup(server.stop)
        return server

    def signed_in(self, server, client=None):
        """Create the password with the setup code from the log; returns a signed-in client."""
        client = client or server.client()
        code = server.wait_code("setup")
        status, body = client.post("/api/setup", {"code": code, "password": PASSWORD})
        self.assertEqual(status, 200, body)
        return client


class FirstVisitTests(ServerTest):
    def test_setup_needs_the_code_from_the_log(self):
        server = self.start()
        c = server.client()
        self.assertEqual(c.get("/api/session")[1], {"authenticated": False, "setup": True, "passwordManaged": False})
        code = server.wait_code("setup")

        status, body = c.post("/api/setup", {"password": PASSWORD})
        self.assertEqual(status, 400)
        self.assertIn("setup code", body["error"])
        self.assertEqual(c.post("/api/setup", {"code": "2222-2222", "password": PASSWORD})[0], 400)
        # A too-short password is refused before the code is checked, so the code still works after.
        self.assertIn("at least 8", c.post("/api/setup", {"code": code, "password": "short"})[1]["error"])

        typed = code.replace("-", "").lower()  # people type it any old way
        self.assertEqual(c.post("/api/setup", {"code": typed, "password": PASSWORD})[0], 200)
        self.assertTrue(c.get("/api/session")[1]["authenticated"])
        self.assertEqual(c.get("/api/state")[0], 200)
        self.assertEqual(server.client().post("/api/setup", {"code": code, "password": "someone else!"})[0], 409)

    def test_a_new_setup_code_at_most_once_a_minute(self):
        server = self.start()
        server.wait_code("setup")
        status, body = server.client().post("/api/reset/request")
        self.assertEqual(status, 200)
        self.assertTrue(body["setup"])
        self.assertGreater(body["waitSeconds"], 0)  # the startup code is seconds old
        time.sleep(0.3)
        self.assertEqual(len(server.codes("setup")), 1)

    def test_five_wrong_tries_use_the_code_up(self):
        server = self.start(JDWEB_TRUST_PROXY="1")  # a new address per try, so the login throttle stays out of it
        code = server.wait_code("setup")
        for i in range(5):
            self.assertEqual(server.client(f"10.0.0.{i}").post("/api/setup", {"code": "2222-2222", "password": PASSWORD})[0], 400)
        self.assertEqual(server.client("10.0.1.1").post("/api/setup", {"code": code, "password": PASSWORD})[0], 400)
        # A used-up code can be replaced straight away.
        self.assertEqual(server.client("10.0.1.1").post("/api/reset/request")[1]["waitSeconds"], 0)
        fresh = server.wait_code("setup", count=2)
        self.assertNotEqual(fresh, code)
        self.assertEqual(server.client("10.0.1.1").post("/api/setup", {"code": fresh, "password": PASSWORD})[0], 200)


class ResetTests(ServerTest):
    def test_forgot_password(self):
        server = self.start()
        old_session = self.signed_in(server)
        c = server.client()
        status, body = c.post("/api/reset/request")
        self.assertEqual((status, body["setup"], body["waitSeconds"]), (200, False, 0))
        code = server.wait_code("reset")
        self.assertIn("valid 15 minutes", "".join(server.log))

        self.assertEqual(c.post("/api/reset/confirm", {"code": "2222-2222", "password": "a new password"})[0], 400)
        self.assertEqual(c.post("/api/reset/confirm", {"code": code, "password": "a new password"})[0], 200)
        self.assertTrue(c.get("/api/session")[1]["authenticated"])

        self.assertEqual(old_session.get("/api/state")[0], 401)  # everyone else is signed out
        self.assertEqual(server.client().post("/api/login", {"password": PASSWORD})[0], 401)
        self.assertEqual(server.client().post("/api/login", {"password": "a new password"})[0], 200)
        self.assertEqual(c.post("/api/reset/confirm", {"code": code, "password": "third password"})[0], 400)  # used

    def test_reset_codes_at_most_once_a_minute(self):
        server = self.start()
        self.signed_in(server)
        self.assertEqual(server.client().post("/api/reset/request")[1]["waitSeconds"], 0)
        server.wait_code("reset")
        self.assertGreater(server.client().post("/api/reset/request")[1]["waitSeconds"], 0)
        time.sleep(0.3)
        self.assertEqual(len(server.codes("reset")), 1)

    def test_reset_before_any_password(self):
        server = self.start()
        status, body = server.client().post("/api/reset/confirm", {"code": "2222-2222", "password": PASSWORD})
        self.assertEqual(status, 400)
        self.assertIn("No password", body["error"])


class EnvPasswordTests(ServerTest):
    def test_password_from_env(self):
        server = self.start(JDWEB_PASSWORD="from the environment")
        c = server.client()
        self.assertEqual(c.get("/api/session")[1], {"authenticated": False, "setup": False, "passwordManaged": True})
        self.assertEqual(server.codes(), [])
        self.assertEqual(c.post("/api/login", {"password": "from the environment"})[0], 200)
        self.assertIn("JDWEB_PASSWORD", c.post("/api/password", {"current": "from the environment", "new": "something else"})[1]["error"])
        self.assertIn("JDWEB_PASSWORD", c.post("/api/reset/request")[1]["error"])
        self.assertEqual(server.codes(), [])

    def test_password_file(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        path = os.path.join(folder, "password")
        with open(path, "w") as f:
            f.write("from a secret file\n")
        server = self.start(JDWEB_PASSWORD_FILE=path)
        self.assertEqual(server.client().post("/api/login", {"password": "from a secret file"})[0], 200)

    def test_too_short_env_password_stops_the_server(self):
        with self.assertRaisesRegex(RuntimeError, "exited"):
            self.start(JDWEB_PASSWORD="short")


class ProxyTests(ServerTest):
    def wrong_logins(self, server, ip):
        for _ in range(5):
            self.assertEqual(server.client(ip).post("/api/login", {"password": "wrong password"})[0], 401)

    def test_forwarded_for_ignored_without_trust(self):
        server = self.start(JDWEB_PASSWORD=PASSWORD)
        self.wrong_logins(server, "10.0.0.1")
        # Not trusted: all of these are the same address, so everyone is throttled.
        self.assertEqual(server.client("10.0.0.2").post("/api/login", {"password": PASSWORD})[0], 429)

    def test_trusted_proxy_throttles_per_client(self):
        server = self.start(JDWEB_PASSWORD=PASSWORD, JDWEB_TRUST_PROXY="1")
        self.wrong_logins(server, "10.0.0.1")
        self.assertEqual(server.client("10.0.0.2").post("/api/login", {"password": PASSWORD})[0], 200)
        self.assertEqual(server.client("10.0.0.1").post("/api/login", {"password": PASSWORD})[0], 429)
        # Only the last X-Forwarded-For entry counts: the proxy appends it, earlier ones can be forged.
        self.assertEqual(server.client("9.9.9.9, 10.0.0.1").post("/api/login", {"password": PASSWORD})[0], 429)

    def test_secure_cookie_only_behind_trusted_https_proxy(self):
        https = {"X-Forwarded-Proto": "https"}
        plain = self.start(JDWEB_PASSWORD=PASSWORD)
        c = plain.client()
        c.post("/api/login", {"password": PASSWORD}, headers=https)
        self.assertNotIn("Secure", c.last_headers["Set-Cookie"])
        proxied = self.start(JDWEB_PASSWORD=PASSWORD, JDWEB_TRUST_PROXY="1")
        c = proxied.client()
        c.post("/api/login", {"password": PASSWORD}, headers=https)
        self.assertIn("Secure", c.last_headers["Set-Cookie"])


class ActionTests(ServerTest):
    def setUp(self):
        self.folders = tempfile.mkdtemp(prefix="navdl-folders-")
        self.addCleanup(shutil.rmtree, self.folders)
        for rel in ("dl/Old Package", "movies/Classics/Noir/Deep/Deeper", "movies/.hidden", "movies/4K"):
            os.makedirs(os.path.join(self.folders, rel))
        self.dl, self.movies = f"{self.folders}/dl", f"{self.folders}/movies"
        self.jd.call("config/set", GENERAL, None, "DefaultDownloadFolder", self.dl)
        self.addCleanup(self.jd.call, "config/set", GENERAL, None, "DefaultDownloadFolder", "/output")
        self.server = self.start(JDWEB_PASSWORD=PASSWORD, JDWEB_IMAGE="someone/nav-downloader",
                                 JDWEB_FOLDERS=f"Movies={self.movies}, Downloads={self.dl}/")
        self.c = self.server.client()
        self.assertEqual(self.c.post("/api/login", {"password": PASSWORD})[0], 200)

    def test_max_downloads(self):
        self.addCleanup(self.c.post, "/api/do", {"action": "max_downloads", "value": 3})
        self.assertEqual(self.c.post("/api/do", {"action": "max_downloads", "value": 5})[0], 200)
        self.assertEqual(self.c.get("/api/state")[1]["maxDownloads"], 5)
        for bad in (0, 21, "5", True, 2.5, None):
            self.assertEqual(self.c.post("/api/do", {"action": "max_downloads", "value": bad})[0], 400, bad)

    def test_folders_from_env(self):
        status, folders = self.c.get("/api/folders")
        self.assertEqual(status, 200)
        roots = folders["roots"]
        # JDownloader's default folder first, then in the order given.
        self.assertEqual([(r["label"], r["path"], r["default"]) for r in roots],
                         [("Downloads", self.dl, True), ("Movies", self.movies, False)])
        self.assertEqual(folders["default"], self.dl)
        self.assertEqual(roots[1]["dirs"], ["4K", "Classics", "Classics/Noir", "Classics/Noir/Deep"])  # 3 levels, no dot-folders

    def test_add_into_a_listed_folder(self):
        status, _ = self.c.post("/api/do", {"action": "add", "links": "https://example.org/a/film.mkv", "autostart": False,
                                            "folder": f"{self.movies}/Classics", "newFolder": "Season 1"})
        self.assertEqual(status, 200)
        saved = [p["saveTo"] for p in self.jd.call("linkgrabberv2/queryPackages", {}) if p["name"] == "film.mkv"]
        self.assertEqual(saved, [f"{self.movies}/Classics/Season 1/film.mkv"])
        for folder, new in ((f"{self.folders}/elsewhere", ""), (self.movies, "a/b"), (self.movies, ".hidden")):
            self.assertEqual(self.c.post("/api/do", {"action": "add", "links": "https://example.org/x",
                                                     "folder": folder, "newFolder": new})[0], 400, (folder, new))

    def test_jd_status(self):
        status, body = self.c.get("/api/jd-status")
        self.assertEqual((status, body["reachable"], body["image"]), (200, True, "someone/nav-downloader"))


class UnreachableTests(unittest.TestCase):
    def test_connect_help_details(self):
        server = Server(f"http://127.0.0.1:{free_port()}", JDWEB_PASSWORD=PASSWORD)  # nothing listens there
        self.addCleanup(server.stop)
        c = server.client()
        c.post("/api/login", {"password": PASSWORD})
        status, body = c.get("/api/state")
        self.assertEqual((status, body.get("unreachable")), (503, True))
        status, body = c.get("/api/jd-status")
        self.assertEqual((status, body["reachable"], body["error"]), (200, False, "refused"))
        self.assertEqual(server.client().get("/api/jd-status")[0], 401)


if __name__ == "__main__":
    unittest.main()
