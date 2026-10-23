"""Desktop app: a window showing indexing/OCR progress and connected AI apps, plus a tray icon.

Closing the window hides it to the tray; "Quit" in the tray menu exits. Indexing itself always
runs in detached `folder-index` processes, so it keeps going whether or not the app is open."""

from __future__ import annotations

import multiprocessing
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, clients, config, indexer, presence, query
from .config import Settings
from .library import Library

APP_NAME = "Folder Index"
SINGLE_INSTANCE_PORT = 47615
REFRESH_EVERY = 15 * 60
HIT_MARKS = ("\x02", "\x03")  # snippet highlight markers the UI turns into <mark>


def _icon_image(size: int = 64):
    """The app icon, drawn at runtime: a rounded tile with a magnifier over a page."""
    from PIL import Image, ImageDraw

    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    top, bottom = (99, 102, 241), (168, 85, 247)
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / s
        gd.line([(0, y), (s, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=s // 4.5, fill=255)
    img.paste(grad, (0, 0), mask)
    w = s // 14
    d.rounded_rectangle([s * 0.22, s * 0.18, s * 0.62, s * 0.70], radius=s // 18, outline="white", width=w)
    for i, y in enumerate((0.31, 0.42, 0.53)):
        d.line([(s * 0.31, s * y), (s * (0.53 if i < 2 else 0.44), s * y)], fill="white", width=w // 2)
    cx, cy, r = s * 0.62, s * 0.62, s * 0.15
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(124, 92, 246, 255), outline="white", width=w)
    d.line([(cx + r * 0.7, cy + r * 0.7), (s * 0.84, s * 0.84)], fill="white", width=int(w * 1.4))
    return img.resize((size, size), Image.LANCZOS)


def write_icon_file(path: Path) -> Path:
    """Save a multi-size .ico (used by the window and the packaged exe)."""
    img = _icon_image(256)
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path


def _open_path(path: str) -> None:
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 - opening the user's own file in its default app
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _reveal(path: str) -> None:
    if os.name == "nt":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


# --- autostart (Windows: HKCU Run key) ---------------------------------------

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" -m folder_index.app --minimized'


def get_autostart() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    if os.name != "nt":
        return False
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
    return get_autostart()


# --- the API the web UI calls ------------------------------------------------

class Api:
    def __init__(self):
        self._window = None
        self._clients_cache: tuple[float, list] = (0, [])

    @staticmethod
    def _lib() -> Library:
        return Library.from_app_config()

    def get_state(self) -> dict:
        lib = self._lib()
        cfg = {Path(r["path"]): r.get("enabled", True) for r in config.load_app_config()["roots"]}
        folders = []
        for f in lib.folders:
            st = lib.folder_stats(f)
            st["enabled"] = True
            folders.append(st)
        for path, enabled in cfg.items():  # disabled folders are listed too, greyed out
            if not enabled:
                folders.append({"root": str(path), "label": path.name, "enabled": False, "never": True,
                                "files": 0, "last_update": 0, "updating": False})
        return {
            "folders": folders,
            "sessions": presence.sessions(),
            "now": time.time(),
        }

    def get_meta(self) -> dict:
        return {
            "version": __version__,
            "autostart": get_autostart(),
            "autostart_supported": os.name == "nt",
            "ocr_installed": indexer.ocr_installed(),
            "cores": os.cpu_count() or 1,
            "free_ram_mb": indexer._free_ram_mb(),
            "turbo_workers": indexer.auto_workers("turbo"),
            "data_dir": str(config.data_dir()),
            "server_command": " ".join(f'"{c}"' if " " in c else c for c in clients.server_command()),
        }

    def get_clients(self, force: bool = False) -> list:
        at, cached = self._clients_cache
        if force or time.time() - at > 10:
            self._clients_cache = (time.time(), clients.detect())
        return self._clients_cache[1]

    # folders
    def add_folder(self) -> dict:
        import webview

        picked = self._window.create_file_dialog(webview.FileDialog.FOLDER) if self._window else None
        if not picked:
            return {"ok": False}
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
        try:
            p = config.add_root(path)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        indexer.spawn(Settings.load(p), "update")
        return {"ok": True, "path": str(p)}

    def remove_folder(self, path: str) -> dict:
        s = Settings.load(path)
        problem = indexer.delete_index(s)
        if problem and "next start" not in problem:
            return {"ok": False, "error": f"Not removed: {problem}."}
        config.remove_root(path)
        return {"ok": True, "note": problem}

    def set_folder_enabled(self, path: str, enabled: bool) -> dict:
        config.set_root_enabled(path, enabled)
        if not enabled:
            indexer.stop_ocr(Settings.load(path), wait=0)
        return {"ok": True}

    def update_folder(self, path: str) -> dict:
        s = Settings.load(path)
        if indexer.is_updating(s.db_path):
            return {"ok": False, "error": "already updating"}
        indexer.spawn(s, "update")
        return {"ok": True}

    def update_all(self) -> dict:
        return {"ok": True, "started": self._lib().refresh()}

    def ocr_speed(self, mode: str) -> dict:
        return {"ok": True, "message": self._lib().ocr_speed(mode)}

    def open_folder(self, path: str) -> None:
        _open_path(path)

    # search
    def search(self, text: str, limit: int = 30) -> list:
        if not text.strip():
            return []
        lib = self._lib()
        hits, relaxed = lib.search_hits(text, limit=limit, marks=HIT_MARKS)
        return [{
            "display": lib.display(f, r["path"]) if lib.multi else r["path"],
            "folder": f.label,
            "abs": str(f.root / r["path"]),
            "name": r["name"],
            "ext": r["ext"].lstrip("."),
            "size": query.human(r["size"]),
            "date": query.date(r["mtime"]),
            "snippet": r["snip"] if r["chars"] else "",
            "ocr": bool(r["ocr"]) and r["ocr"] == "done",
            "partial": relaxed,
        } for f, r in hits]

    def open_file(self, path: str) -> None:
        if os.path.exists(path):
            _open_path(path)

    def reveal_file(self, path: str) -> None:
        if os.path.exists(path):
            _reveal(path)

    # AI apps
    def connect_client(self, cid: str) -> dict:
        msg = clients.connect(cid)
        return {"message": msg, "clients": self.get_clients(force=True)}

    def disconnect_client(self, cid: str) -> dict:
        msg = clients.disconnect(cid)
        return {"message": msg, "clients": self.get_clients(force=True)}

    # settings
    def set_autostart(self, enabled: bool) -> bool:
        return set_autostart(bool(enabled))

    def open_data_dir(self) -> None:
        _open_path(str(config.data_dir()))

    def copy_text(self, text: str) -> bool:
        if os.name == "nt":
            subprocess.run(["clip"], input=text.encode("utf-16-le"), check=False)
            return True
        return False


# --- single instance -----------------------------------------------------------

def _claim_single_instance(on_show) -> socket.socket | None:
    """Bind a localhost port; a second launch pokes it and exits instead of opening a second app."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        try:
            with socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=2) as c:
                c.sendall(b"show")
        except OSError:
            pass
        return None
    srv.listen(2)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    if conn.recv(16) == b"show":
                        on_show()
            except OSError:
                return

    threading.Thread(target=loop, daemon=True).start()
    return srv


def _ui_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent))
    for p in (base / "folder_index" / "ui" / "index.html", Path(__file__).parent / "ui" / "index.html"):
        if p.exists():
            return p
    raise FileNotFoundError("ui/index.html")


def _log_to_file() -> None:
    """A windowed exe has no console; keep its output (and any crash) in app.log."""
    if sys.stdout is None or sys.stderr is None or getattr(sys, "frozen", False):
        try:
            config.data_dir().mkdir(parents=True, exist_ok=True)
            log = open(config.data_dir() / "app.log", "a", encoding="utf-8", buffering=1)
            log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} Folder Index {__version__} starting\n")
            sys.stdout = sys.stderr = log
        except OSError:
            pass


def main() -> None:
    multiprocessing.freeze_support()
    config.migrate_legacy_data()
    config.cleanup_after_migration()
    _log_to_file()
    import logging

    import webview

    logging.basicConfig(stream=sys.stderr, level=logging.INFO)

    minimized = "--minimized" in sys.argv
    api = Api()
    state = {"quitting": False}
    window_box: dict = {}

    def show():
        w = window_box.get("w")
        if w:
            w.show()
            w.restore()

    lock = _claim_single_instance(show)
    if lock is None:
        return  # the running app was asked to show itself

    width, height = 1180, 800
    try:  # fit small and portrait screens
        scr = webview.screens[0]
        width, height = min(width, scr.width - 40), min(height, scr.height - 80)
    except Exception:
        pass
    window = webview.create_window(
        APP_NAME, url=str(_ui_path()), js_api=api, width=width, height=height, min_size=(420, 520),
        hidden=minimized, background_color="#0b0d14", text_select=True,
    )
    api._window = window
    window_box["w"] = window

    def on_closing():
        if state["quitting"]:
            return True
        window.hide()
        return False  # keep running in the tray

    window.events.closing += on_closing

    icon = _start_tray(api, show, lambda: _quit(state, window, icon_box), state)
    icon_box = {"icon": icon}

    def housekeeping():
        """Keep indexes fresh while the app runs (the MCP server does the same per session)."""
        while not state["quitting"]:
            try:
                Library.from_app_config().refresh(only_stale=True)
            except Exception:
                pass
            for _ in range(REFRESH_EVERY):
                if state["quitting"]:
                    return
                time.sleep(1)

    threading.Thread(target=housekeeping, daemon=True).start()
    icon_path = config.data_dir() / "folder-index.ico"
    try:
        config.data_dir().mkdir(parents=True, exist_ok=True)
        write_icon_file(icon_path)
    except OSError:
        icon_path = None
    webview.start(icon=str(icon_path) if icon_path else None, private_mode=True)
    state["quitting"] = True
    if icon:
        icon.stop()


def _quit(state, window, icon_box) -> None:
    state["quitting"] = True
    icon = icon_box.get("icon")
    if icon:
        icon.stop()
    window.destroy()


def _start_tray(api: Api, show, quit_app, state):
    try:
        import pystray
    except ImportError:
        return None
    item = pystray.MenuItem
    menu = pystray.Menu(
        item(f"Open {APP_NAME}", lambda: show(), default=True),
        pystray.Menu.SEPARATOR,
        item("Update all folders", lambda: api.update_all()),
        item("OCR", pystray.Menu(
            item("Turbo (all cores)", lambda: api.ocr_speed("turbo")),
            item("Background", lambda: api.ocr_speed("background")),
            item("Pause", lambda: api.ocr_speed("pause")),
        )),
        pystray.Menu.SEPARATOR,
        item("Quit", lambda: quit_app()),
    )
    icon = pystray.Icon("folder-index", _icon_image(64), APP_NAME, menu)
    icon.run_detached()

    def tooltip():
        while not state["quitting"]:
            try:
                busy = []
                for f in api.get_state()["folders"]:
                    if f.get("updating"):
                        busy.append(f"{f['label']}: indexing")
                    elif (f.get("ocr") or {}).get("running"):
                        p = f["ocr"].get("progress") or {}
                        pct = int(100 * p["done"] / p["total"]) if p.get("total") else 0
                        busy.append(f"{f['label']}: OCR {pct}%")
                icon.title = (APP_NAME + " — " + "; ".join(busy))[:120] if busy else f"{APP_NAME} — up to date"
            except Exception:
                pass
            time.sleep(3)

    threading.Thread(target=tooltip, daemon=True).start()
    return icon


if __name__ == "__main__":
    main()
