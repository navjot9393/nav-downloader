"""Tests for setup_jd.py (setup-jd): switching on JDownloader's local API in its settings files."""

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import setup_jd  # noqa: E402

API = setup_jd.REMOTE_API
GRABBER = setup_jd.GRABBER
WANTED = {"deprecatedapienabled": True, "deprecatedapilocalhostonly": True, "deprecatedapiport": 3128}


class SetupJDTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="navdl-jd-"))
        self.addCleanup(shutil.rmtree, self.root)

    def run_setup(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = setup_jd.main([*map(str, args)])
        return code, out.getvalue() + err.getvalue()

    def read(self, path):
        return json.loads(Path(path).read_text())

    def test_never_started_jdownloader_is_left_alone(self):
        # Creating cfg/ here would stop images like jlesage's adding their default settings.
        for extra in ([], ["JDownloader.jar"]):
            for name in extra:
                (self.root / name).write_text("")
            code, out = self.run_setup(self.root)
            self.assertEqual(code, 1)
            self.assertIn("Start it once", out)
            self.assertFalse((self.root / "cfg").exists())

    def test_jdownloader_folder_with_cfg(self):
        (self.root / "JDownloader.jar").write_text("")
        (self.root / "cfg").mkdir()
        self.assertEqual(self.run_setup(self.root)[0], 0)
        self.assertEqual(self.read(self.root / "cfg" / API), WANTED)
        self.assertEqual(self.read(self.root / "cfg" / GRABBER), {"autoconfirmdelay": 1000})

    def test_existing_settings_are_kept(self):
        cfg = self.root / "cfg"
        cfg.mkdir()
        (cfg / API).write_text(json.dumps({"deprecatedapienabled": False, "jdanywhereapienabled": False, "deprecatedapiport": 3128}))
        os.chmod(cfg / API, 0o600)
        (cfg / GRABBER).write_text(json.dumps({"autoconfirmdelay": 15000, "linkgrabberautostartenabled": True}))
        code, out = self.run_setup(self.root)  # finds cfg/ by itself
        self.assertEqual(code, 0, out)
        self.assertIn("set deprecatedapienabled, deprecatedapilocalhostonly", out)
        self.assertEqual(self.read(cfg / API), {**WANTED, "jdanywhereapienabled": False})
        self.assertEqual(self.read(cfg / GRABBER), {"autoconfirmdelay": 1000, "linkgrabberautostartenabled": True})
        self.assertEqual(stat.S_IMODE(os.stat(cfg / API).st_mode), 0o600)

    def test_the_cfg_folder_itself(self):
        (self.root / "org.jdownloader.settings.GeneralSettings.json").write_text("{}")
        self.assertEqual(self.run_setup(self.root)[0], 0)
        self.assertEqual(self.read(self.root / API), WANTED)
        self.assertFalse((self.root / "cfg").exists())

    def test_second_run_changes_nothing(self):
        (self.root / "cfg").mkdir()
        self.run_setup(self.root)
        before = (self.root / "cfg" / API).stat().st_mtime_ns
        code, out = self.run_setup(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("already set"), 2)
        self.assertEqual((self.root / "cfg" / API).stat().st_mtime_ns, before)

    def test_unknown_folder_is_left_alone(self):
        (self.root / "notes.txt").write_text("not JDownloader")
        code, out = self.run_setup(self.root)
        self.assertEqual(code, 1)
        self.assertIn("--here", out)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["notes.txt"])
        self.assertEqual(self.run_setup(self.root, "--here")[0], 0)
        self.assertEqual(self.read(self.root / API), WANTED)

    def test_broken_json_is_left_alone(self):
        cfg = self.root / "cfg"
        cfg.mkdir()
        (cfg / API).write_text("{not json")
        code, out = self.run_setup(self.root)
        self.assertEqual(code, 1)
        self.assertIn("isn't valid JSON", out)
        self.assertEqual((cfg / API).read_text(), "{not json")

    def test_no_autoconfirm(self):
        (self.root / "cfg").mkdir()
        self.assertEqual(self.run_setup(self.root, "--no-autoconfirm")[0], 0)
        self.assertTrue((self.root / "cfg" / API).exists())
        self.assertFalse((self.root / "cfg" / GRABBER).exists())

    def test_missing_folder(self):
        self.assertEqual(self.run_setup(self.root / "nope")[0], 1)


if __name__ == "__main__":
    unittest.main()
