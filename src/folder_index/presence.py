"""Which AI apps are using the index right now. Each running MCP server keeps a small
heartbeat file; the desktop app reads them. Files of dead servers become history."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config

TOUCH_EVERY = 10  # seconds between heartbeat writes


def _dir() -> Path:
    return config.data_dir() / "clients"


def _history_path() -> Path:
    return config.data_dir() / "clients-seen.json"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Heartbeat:
    """Owned by one MCP server process."""

    def __init__(self):
        self.path = _dir() / f"{os.getpid()}.json"
        self.data = {"pid": os.getpid(), "client": None, "client_version": None,
                     "started": time.time(), "last_seen": time.time(), "requests": 0}
        self._last_write = 0.0
        self._write()

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            os.replace(tmp, self.path)
            self._last_write = time.time()
        except OSError:
            pass

    def client(self, name: str | None, version: str | None) -> None:
        self.data["client"], self.data["client_version"] = name, version
        self._write()

    def touch(self, tool: str | None = None) -> None:
        self.data["last_seen"] = time.time()
        self.data["requests"] += 1
        if tool:
            self.data["last_tool"] = tool
        if time.time() - self._last_write >= TOUCH_EVERY or tool:
            self._write()

    def close(self) -> None:
        self.data["last_seen"] = time.time()
        _remember(self.data)
        self.path.unlink(missing_ok=True)


def _remember(data: dict) -> None:
    name = data.get("client")
    if not name:
        return
    try:
        hist = json.loads(_history_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        hist = {}
    prev = hist.get(name, {})
    if data.get("last_seen", 0) >= prev.get("last_seen", 0):
        hist[name] = {"last_seen": data.get("last_seen", 0), "version": data.get("client_version"),
                      "last_tool": data.get("last_tool", prev.get("last_tool"))}
        _history_path().parent.mkdir(parents=True, exist_ok=True)
        _history_path().write_text(json.dumps(hist, indent=1), encoding="utf-8")


def sessions() -> dict:
    """{"active": [...live servers...], "history": {client: {...}}}; prunes dead heartbeats."""
    active = []
    for p in sorted(_dir().glob("*.json")) if _dir().exists() else []:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if pid_alive(int(data.get("pid", 0))):
            active.append(data)
        else:
            _remember(data)
            p.unlink(missing_ok=True)
    try:
        hist = json.loads(_history_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        hist = {}
    return {"active": active, "history": hist}
