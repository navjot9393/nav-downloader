#!/usr/bin/env python3
"""Fake JDownloader local API for trying the page without a real JDownloader.

Speaks the same wire format as the real API (each parameter JSON-encoded,
joined with '&', in the query string or a form body) and simulates a few
packages downloading. Only the calls server.py makes are implemented.

    python3 dev/mock_jd.py [port]      (default 3129)
"""

import http.server
import itertools
import json
import sys
import threading
import time
import urllib.parse

lock = threading.Lock()
ids = itertools.count(1700000000000)
GB = 1024**3
MB = 1024**2

controller = {"state": "RUNNING", "paused": False}
config = {"DownloadSpeedLimitEnabled": False, "DownloadSpeedLimit": 5 * MB, "MaxSimultaneDownloads": 3,
          "AutoConfirmDelay": 1000, "DefaultDownloadFolder": "/output"}
CHECK_SECONDS = 2  # how long the fake link check takes
collecting_until = [0.0]
last_tick = [time.time()]


def link(name, total, loaded=0, host="example-host.com", speed=8 * MB, enabled=True, finished=False, status=None):
    return {"uuid": next(ids), "name": name, "bytesTotal": total, "bytesLoaded": loaded, "host": host,
            "enabled": enabled, "finished": finished, "running": False, "speed": 0, "eta": 0,
            "status": status, "_rate": speed}


def package(name, links):
    return {"uuid": next(ids), "name": name, "saveTo": "/output/" + name, "links": links}


downloads = [
    package("Big.Buck.Bunny.2160p.HDR", [link("Big.Buck.Bunny.2160p.part1.rar", 4 * GB, int(3.1 * GB)),
                                         link("Big.Buck.Bunny.2160p.part2.rar", 4 * GB, int(1.2 * GB)),
                                         link("Big.Buck.Bunny.2160p.part3.rar", int(1.5 * GB))]),
    package("Ubuntu 26.04 Desktop ISO", [link("ubuntu-26.04-desktop-amd64.iso", int(6.2 * GB), int(0.8 * GB), host="releases.ubuntu.com", speed=11 * MB)]),
    package("Podcast – Season 3", [link(f"episode-{i:02d}.mp3", 80 * MB + i * MB, 80 * MB + i * MB, finished=True, status="Finished") for i in range(1, 6)]),
    package("Old.Archive.Collection", [link("collection.zip", 2 * GB, 2 * GB, finished=True, status="Extraction OK")]),
    package("Broken.Link.Pack", [link("missing-file.bin", 0, 0, status="File not found")]),
    package("Paused.Package.Example", [link("paused-file.mkv", 3 * GB, int(0.4 * GB), enabled=False)]),
]
grabber = [
    package("Sintel.1080p", [link("sintel-1080p.mkv", int(1.1 * GB), host="download.blender.org"),
                             link("sintel-subs.zip", 2 * MB, host="download.blender.org")]),
    package("Mixed availability", [link("still-online.pdf", 12 * MB), link("gone.pdf", 0)]),
]
grabber[0]["links"][0]["availability"] = "ONLINE"
grabber[0]["links"][1]["availability"] = "ONLINE"
grabber[1]["links"][0]["availability"] = "ONLINE"
grabber[1]["links"][1]["availability"] = "OFFLINE"


def tick():
    now = time.time()
    dt, last_tick[0] = now - last_tick[0], now
    # Like JDownloader: "autostart" links wait in the grabber until they're checked,
    # then AutoConfirmDelay longer, before moving to the download list and starting.
    for p in list(grabber):
        for l in p["links"]:
            if l.get("availability") == "UNKNOWN" and now >= p.get("_checked_at", 0):
                l["availability"] = "ONLINE"
        if p.get("_confirm_at") and now >= p["_confirm_at"]:
            grabber.remove(p)
            downloads.append(p)
            controller.update(state="RUNNING", paused=False)
    active = controller["state"] == "RUNNING" and not controller["paused"]
    limit = config["DownloadSpeedLimit"] if config["DownloadSpeedLimitEnabled"] else None
    slots = config["MaxSimultaneDownloads"]
    for p in downloads:
        for l in p["links"]:
            l["running"], l["speed"], l["eta"] = False, 0, 0
            if not active or not l["enabled"] or l["finished"] or not l["bytesTotal"] or l["status"]:
                continue
            if slots == 0:
                continue
            slots -= 1
            rate = min(l["_rate"], limit / config["MaxSimultaneDownloads"]) if limit else l["_rate"]
            l["bytesLoaded"] = min(l["bytesTotal"], l["bytesLoaded"] + int(rate * dt))
            l["running"], l["speed"] = True, int(rate)
            l["eta"] = int((l["bytesTotal"] - l["bytesLoaded"]) / rate) if rate else 0
            if l["bytesLoaded"] >= l["bytesTotal"]:
                l.update(finished=True, running=False, speed=0, status="Finished")


def package_view(p, grab=False):
    ls = p["links"]
    v = {"uuid": p["uuid"], "name": p["name"], "saveTo": p["saveTo"], "childCount": len(ls),
         "bytesTotal": sum(l["bytesTotal"] for l in ls), "hosts": sorted({l["host"] for l in ls}),
         "enabled": any(l["enabled"] for l in ls)}
    if grab:
        v["onlineCount"] = sum(l.get("availability") == "ONLINE" for l in ls)
        v["offlineCount"] = sum(l.get("availability") == "OFFLINE" for l in ls)
        v["unknownCount"] = sum(l.get("availability") not in ("ONLINE", "OFFLINE") for l in ls)
        return v
    v.update(bytesLoaded=sum(l["bytesLoaded"] for l in ls), speed=sum(l["speed"] for l in ls),
             running=any(l["running"] for l in ls), finished=all(l["finished"] for l in ls))
    v["eta"] = max((l["eta"] for l in ls), default=0)
    done = [l["status"] for l in ls if l["finished"]]
    if v["finished"]:
        v["status"] = done[0] if done else "Finished"
    elif any(l["status"] and not l["finished"] for l in ls):
        v["status"] = next(l["status"] for l in ls if l["status"] and not l["finished"])
    return v


def link_view(p, l, grab=False):
    v = {k: l[k] for k in ("uuid", "name", "bytesTotal", "host", "enabled")}
    v["packageUUID"] = p["uuid"]
    if grab:
        v["availability"] = l.get("availability", "UNKNOWN")
        return v
    v.update({k: l[k] for k in ("bytesLoaded", "finished", "running", "speed", "eta")})
    if l["status"]:
        v["status"] = l["status"]
    return v


def select(pkgs, link_ids, package_ids):
    for p in pkgs:
        for l in p["links"]:
            if p["uuid"] in package_ids or l["uuid"] in link_ids:
                yield p, l


def prune(pkgs):
    pkgs[:] = [p for p in pkgs if p["links"]]


def handle(path, params):
    tick()
    if path == "downloadcontroller/getCurrentState":
        return "PAUSE" if controller["paused"] and controller["state"] == "RUNNING" else controller["state"]
    if path == "downloadcontroller/getSpeedInBps":
        return sum(l["speed"] for p in downloads for l in p["links"])
    if path == "downloadcontroller/start":
        controller.update(state="RUNNING", paused=False); return True
    if path == "downloadcontroller/stop":
        controller.update(state="STOPPED_STATE", paused=False); return True
    if path == "downloadcontroller/pause":
        controller["paused"] = bool(params[0]); return True
    if path == "config/get":
        return config.get(params[2])
    if path == "config/set":
        config[params[2]] = params[3]; return True
    if path == "captcha/list":
        return []
    if path == "linkgrabberv2/isCollecting":
        return time.time() < collecting_until[0]
    if path == "downloadsV2/queryPackages":
        return [package_view(p) for p in downloads]
    if path == "downloadsV2/queryLinks":
        return [link_view(p, l) for p in downloads for l in p["links"]]
    if path == "linkgrabberv2/queryPackages":
        return [package_view(p, True) for p in grabber]
    if path == "linkgrabberv2/queryLinks":
        return [link_view(p, l, True) for p in grabber for l in p["links"]]
    if path == "linkgrabberv2/addLinks":
        q = params[0]
        urls = [u for u in q["links"].split() if "://" in u]
        name = q.get("packageName") or (urls[0].rstrip("/").rsplit("/", 1)[-1] if urls else "New package")
        new = package(name, [])
        if q.get("destinationFolder"):
            new["saveTo"] = q["destinationFolder"].rstrip("/") + "/" + name
        for u in urls:
            l = link(u.rstrip("/").rsplit("/", 1)[-1] or u, 500 * MB, host=urllib.parse.urlsplit(u).hostname or "")
            l["availability"] = "UNKNOWN"
            new["links"].append(l)
        now = time.time()
        collecting_until[0] = new["_checked_at"] = now + CHECK_SECONDS
        if q.get("autostart"):
            new["_confirm_at"] = now + CHECK_SECONDS + config["AutoConfirmDelay"] / 1000
        grabber.append(new)
        return {"id": next(ids)}
    if path == "linkgrabberv2/moveToDownloadlist":
        link_ids, package_ids = set(params[0]), set(params[1])
        for p in list(grabber):
            moving = [l for l in p["links"] if p["uuid"] in package_ids or l["uuid"] in link_ids]
            if moving:
                p["links"] = [l for l in p["links"] if l not in moving]
                moved = package(p["name"], moving)
                moved["saveTo"] = p["saveTo"]
                downloads.append(moved)
        prune(grabber); return True
    if path == "linkgrabberv2/setDownloadDirectory":
        for p in grabber:
            if p["uuid"] in set(params[1]):
                p["saveTo"] = params[0].rstrip("/")  # like JDownloader: exactly as given
        return True
    if path in ("linkgrabberv2/removeLinks", "downloadsV2/removeLinks"):
        pkgs = grabber if path.startswith("link") else downloads
        link_ids, package_ids = set(params[0]), set(params[1])
        for p in pkgs:
            p["links"] = [l for l in p["links"] if not (p["uuid"] in package_ids or l["uuid"] in link_ids)]
        prune(pkgs); return True
    if path == "linkgrabberv2/clearList":
        grabber.clear(); return True
    if path == "downloadsV2/setEnabled":
        for _, l in select(downloads, set(params[1]), set(params[2])):
            l["enabled"] = bool(params[0])
        return True
    if path == "downloadsV2/forceDownload":
        controller.update(state="RUNNING", paused=False); return True
    if path == "downloadsV2/resetLinks":
        for _, l in select(downloads, set(params[0]), set(params[1])):
            l.update(bytesLoaded=0, finished=False, status=None)
        return True
    if path == "downloadsV2/cleanup":
        link_ids, package_ids, action, mode, selection = params
        for p in downloads:
            if action == "DELETE_FINISHED" and selection == "ALL":
                p["links"] = [l for l in p["links"] if not l["finished"]]
            elif selection == "SELECTED":
                p["links"] = [l for l in p["links"] if not (p["uuid"] in package_ids or l["uuid"] in link_ids)]
        prune(downloads); return True
    raise KeyError(path)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _params(self, raw):
        return [json.loads(urllib.parse.unquote(p)) for p in raw.split("&")] if raw else []

    def _reply(self, path, raw):
        try:
            with lock:
                data = handle(path.strip("/"), self._params(raw))
            body, code = json.dumps({"data": data}).encode(), 200
        except KeyError:
            body, code = b'{"type":"API_COMMAND_NOT_FOUND"}', 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, raw = self.path.partition("?")
        self._reply(path, raw)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        self._reply(self.path.partition("?")[0], raw)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3129
    print(f"mock JDownloader API on 127.0.0.1:{port}", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
