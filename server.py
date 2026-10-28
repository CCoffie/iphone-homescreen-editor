# /// script
# requires-python = ">=3.11"
# dependencies = ["pymobiledevice3>=11.45"]
# ///
"""Drag-and-drop home screen editor for a USB-connected iPhone.

  uv run server.py                          start the editor, then open http://localhost:8765
  uv run server.py --port 9000              use another port
  uv run server.py --restore backups/X.plist   put a saved layout back on the phone
"""
import argparse
import asyncio
import hashlib
import json
import plistlib
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from pymobiledevice3.exceptions import NoDeviceConnectedError
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.springboard import SpringBoardServicesService


ROOT = Path(__file__).parent
ICONS = ROOT / "icons"
BACKUPS = ROOT / "backups"
PORT = 8765
FOLDER_PAGE_SIZE = 9
WIDGET_SLOTS = {"small": 4, "medium": 8, "large": 16, "extraLarge": 32}
NO_DEVICE = "No iPhone found. Connect it by USB, unlock it, tap Trust if asked, then click Reload."
device_lock = threading.Lock()


def on_phone(fn):
    """Run an async fn(springboard, lockdown) against the phone, one at a time."""
    async def run():
        lockdown = await create_using_usbmux()
        return await fn(SpringBoardServicesService(lockdown), lockdown)
    with device_lock:
        return asyncio.run(run())


def is_widget(item):
    return "elements" in item


def is_folder(item):
    return "iconLists" in item and not is_widget(item)


def flat_apps(state):
    for page in state:
        for item in page:
            if is_folder(item):
                yield from (a for lst in item["iconLists"] for a in lst)
            elif not is_widget(item):
                yield item


def version(state):
    """Fingerprint of the arrangement only, so icon/app updates don't count as changes."""
    def sig(item):
        if is_folder(item):
            return [item["displayName"], [a["displayIdentifier"] for l in item["iconLists"] for a in l]]
        return item["displayIdentifier"]
    return hashlib.sha1(json.dumps([[sig(i) for i in p] for p in state]).encode()).hexdigest()


def clean(name):
    return name.strip("‎ ").strip()


def to_model(state):
    apps = {a["displayIdentifier"]: {"name": clean(a["displayName"])} for a in flat_apps(state)}
    pages, n = [], 0
    for page in state:
        out = []
        for item in page:
            if is_widget(item):
                names = [apps.get(e.get("containerBundleIdentifier"), {}).get("name")
                         or e.get("containerBundleIdentifier", "?").split(".")[-1] for e in item["elements"]]
                out.append({"t": "widget", "id": item["displayIdentifier"],
                            "size": item.get("gridSize", "small"), "label": " · ".join(names)})
            elif is_folder(item):
                n += 1
                out.append({"t": "folder", "id": f"f{n}", "name": item["displayName"],
                            "orig": item["displayName"],
                            "apps": [a["displayIdentifier"] for l in item["iconLists"] for a in l]})
            else:
                out.append({"t": "app", "id": item["displayIdentifier"]})
        pages.append(out)
    return {"apps": apps, "pages": pages, "version": version(state)}


def from_model(state, model):
    items = {a["displayIdentifier"]: a for a in flat_apps(state)}
    widgets = {w["displayIdentifier"]: w for p in state for w in p if is_widget(w)}
    orig_folders = {f["displayName"]: f for p in state for f in p if is_folder(f)}
    seen, errors, pages = set(), [], []

    def take(ident, pool):
        if ident not in pool:
            errors.append(f"unknown item {ident}")
            return None
        if ident in seen:
            errors.append(f"{ident} appears twice")
        seen.add(ident)
        return pool[ident]

    for i, page in enumerate(model["pages"]):
        out, slots = [], 0
        for item in page:
            if item["t"] == "app":
                out.append(take(item["id"], items)); slots += 1
            elif item["t"] == "widget":
                w = take(item["id"], widgets)
                out.append(w); slots += WIDGET_SLOTS.get(w and w.get("gridSize"), 4)
            elif item["t"] == "folder":
                name = item["name"].strip() or "Folder"
                contents = [take(a, items) for a in item["apps"]]
                if not contents:
                    continue
                folder = dict(orig_folders.get(item.get("orig"), {}))
                folder.update(displayName=name, listType="folder",
                              iconLists=[contents[j:j + FOLDER_PAGE_SIZE]
                                         for j in range(0, len(contents), FOLDER_PAGE_SIZE)])
                out.append(folder); slots += 1
        if i == 0 and len(out) > 4:
            errors.append(f"Dock has {len(out)} items (max 4)")
        elif i > 0 and slots > 24:
            errors.append(f"Page {i} is over capacity ({slots}/24 slots)")
        if i == 0 or out:
            pages.append(out)
    missing = (set(items) | set(widgets)) - seen
    if missing:
        errors.append(f"missing from layout: {', '.join(sorted(missing))}")
    return pages, errors


def prefetch_icons(state):
    ICONS.mkdir(exist_ok=True)
    ids = [a["displayIdentifier"] for a in flat_apps(state)
           if not (ICONS / f"{a['displayIdentifier']}.png").exists()]
    if not ids:
        return

    async def fetch(sb, _):
        for ident in ids:
            try:
                (ICONS / f"{ident}.png").write_bytes(await sb.get_icon_pngdata(ident))
            except Exception as e:  # noqa: BLE001 - a missing icon just falls back to a letter
                print(f"  no icon for {ident}: {e}")
    print(f"Fetching {len(ids)} icons from the phone...")
    on_phone(fetch)


def get_state():
    async def fn(sb, lockdown):
        return await sb.get_icon_state(), getattr(lockdown, "display_name", None) or "iPhone"
    return on_phone(fn)


def apply(payload):
    async def fn(sb, _):
        state = await sb.get_icon_state()
        if version(state) != payload.get("version"):
            return 409, {"error": "The phone's layout changed since this page loaded. Reload and try again."}
        new, errors = from_model(state, payload["model"])
        if errors:
            return 400, {"error": "; ".join(errors)}
        BACKUPS.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (BACKUPS / f"{stamp}.plist").write_bytes(plistlib.dumps(state))
        await sb.set_icon_state(new)
        check = await sb.get_icon_state()
        before = sorted(a["displayIdentifier"] for a in flat_apps(state))
        after = sorted(a["displayIdentifier"] for a in flat_apps(check))
        if before != after:
            return 500, {"error": f"Phone reports a different app set after applying. Backup: backups/{stamp}.plist"}
        return 200, {"ok": True, "backup": f"backups/{stamp}.plist"}
    return on_phone(fn)


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if ctype == "image/png":
            self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/layout":
                state, name = get_state()
                prefetch_icons(state)
                self.send(200, {**to_model(state), "device": name})
            elif self.path.startswith("/icon/"):
                f = ICONS / f"{Path(unquote(self.path[6:])).name}.png"
                self.send(200, f.read_bytes(), "image/png") if f.exists() else self.send(404, {})
            else:
                self.send(404, {"error": "not found"})
        except NoDeviceConnectedError:
            self.send(503, {"error": NO_DEVICE})
        except Exception as e:  # noqa: BLE001
            self.send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if self.path != "/api/apply":
            return self.send(404, {"error": "not found"})
        try:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send(*apply(payload))
        except NoDeviceConnectedError:
            self.send(503, {"error": NO_DEVICE})
        except Exception as e:  # noqa: BLE001
            self.send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        if not self.path.startswith("/icon/"):
            super().log_message(fmt, *args)


def restore(path):
    saved = plistlib.loads(Path(path).read_bytes())

    async def fn(sb, _):
        current = await sb.get_icon_state()
        BACKUPS.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (BACKUPS / f"{stamp}.plist").write_bytes(plistlib.dumps(current))
        await sb.set_icon_state(saved)
        return stamp
    stamp = on_phone(fn)
    print(f"Restored {path}. The layout it replaced was saved to backups/{stamp}.plist")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Drag-and-drop home screen editor for a USB-connected iPhone.")
    parser.add_argument("--port", type=int, default=PORT, help=f"port to serve on (default {PORT})")
    parser.add_argument("--restore", metavar="PLIST", help="put a layout from backups/ back on the phone and exit")
    args = parser.parse_args()
    if args.restore:
        try:
            restore(args.restore)
        except NoDeviceConnectedError:
            raise SystemExit(NO_DEVICE.replace(", then click Reload", ""))
    else:
        print(f"Open http://localhost:{args.port}")
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
