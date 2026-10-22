#!/usr/bin/env python3
"""Switch on the JDownloader local API that Nav Downloader talks to.

Run it while JDownloader is stopped (JDownloader rewrites its settings when it
exits, which would undo the change), then start JDownloader:

    setup-jd /path/to/jdownloader/config [--here] [--no-autoconfirm]

The path can be JDownloader's own folder (it holds cfg/, as in the
jlesage/jdownloader-2 image's /config) or the cfg folder itself (the
org.jdownloader.*.json files, as in jaymoulin/jdownloader). It won't create
cfg/ in a JDownloader that has never started: images like jlesage's only put
their default settings (download folder and so on) in place when cfg/ is
missing. Start JDownloader once first, or use --here to write into the given
folder as it is.

What it sets, keeping every other setting:
  org.jdownloader.api.RemoteAPIConfig.json
    deprecatedapienabled: true         the local API Nav Downloader uses
    deprecatedapilocalhostonly: true   it has no login, so localhost only
    deprecatedapiport: 3128
  ...linkgrabber.addlinksdialog.LinkgrabberSettings.json (skip with --no-autoconfirm)
    autoconfirmdelay: 1000             "Start right away" links wait 1 s after
                                       their link check instead of 15 s

Run as root, it keeps the owner of files it changes, and gives files it creates
to PUID:PGID (default: the owner of the folder), so JDownloader can still write them.
"""

import argparse
import json
import os
import sys
from pathlib import Path

REMOTE_API = "org.jdownloader.api.RemoteAPIConfig.json"
GRABBER = "org.jdownloader.gui.views.linkgrabber.addlinksdialog.LinkgrabberSettings.json"
API_SETTINGS = {"deprecatedapienabled": True, "deprecatedapilocalhostonly": True, "deprecatedapiport": 3128}
GRABBER_SETTINGS = {"autoconfirmdelay": 1000}


class SetupError(Exception):
    pass


def find_cfg(root, here=False):
    """The folder holding JDownloader's org.jdownloader.*.json settings."""
    if not root.is_dir():
        raise SetupError(f"{root} is not a folder.")
    if here or root.name == "cfg" or any(root.glob("org.jdownloader.*.json")):
        return root
    if (root / "cfg").is_dir():
        return root / "cfg"
    entries = [p for p in root.iterdir() if not p.name.startswith(".")]
    if not entries or (root / "JDownloader.jar").exists():
        raise SetupError(
            f"JDownloader hasn't created its settings in {root} yet. Start it once, stop it, "
            "then run this again.")
    raise SetupError(
        f"Can't tell where JDownloader keeps its settings in {root}. Pass the folder that holds "
        "the org.jdownloader.*.json files (usually .../cfg), or add --here to use this folder as it is.")


def owner_for_new(folder):
    st = folder.stat()
    uid = int(os.environ.get("PUID", st.st_uid))
    gid = int(os.environ.get("PGID", st.st_gid))
    return uid, gid


def give(path, uid, gid):
    if os.geteuid() == 0:
        os.chown(path, uid, gid)


def merge(path, wanted):
    """Set `wanted` keys in a JDownloader JSON settings file. Returns the keys it changed."""
    try:
        current = json.loads(path.read_text())
    except FileNotFoundError:
        current, existed = {}, False
    except ValueError:
        raise SetupError(f"{path} isn't valid JSON, so it was left alone. Fix or delete it, then run this again.")
    else:
        existed = True
    if not isinstance(current, dict):
        raise SetupError(f"{path} doesn't hold JDownloader settings, so it was left alone.")
    changed = [k for k, v in wanted.items() if current.get(k) != v]
    if not changed:
        return []
    current.update(wanted)
    if existed:
        st = path.stat()
        uid, gid, mode = st.st_uid, st.st_gid, st.st_mode & 0o777
    else:
        (uid, gid), mode = owner_for_new(path.parent), 0o664
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(current, indent=2))
    os.chmod(tmp, mode)
    give(tmp, uid, gid)
    os.replace(tmp, path)
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Switch on JDownloader's local API for Nav Downloader.")
    parser.add_argument("folder", help="JDownloader's config folder, or its cfg folder")
    parser.add_argument("--here", action="store_true", help="use the folder as the cfg folder as it is")
    parser.add_argument("--no-autoconfirm", action="store_true",
                        help="leave JDownloader's 15 s wait before auto-starting links alone")
    args = parser.parse_args(argv)

    try:
        cfg = find_cfg(Path(args.folder), args.here)
        jobs = [(REMOTE_API, API_SETTINGS)]
        if not args.no_autoconfirm:
            jobs.append((GRABBER, GRABBER_SETTINGS))
        for name, wanted in jobs:
            changed = merge(cfg / name, wanted)
            print(f"{name}: " + (f"set {', '.join(changed)}" if changed else "already set"))
    except (SetupError, OSError) as e:
        print(f"setup-jd: {e}", file=sys.stderr)
        return 1
    print("Done. Now start JDownloader. If it was running, stop it and run this again first: "
          "it rewrites its settings when it exits.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
