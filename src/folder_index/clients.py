"""Register the MCP server with AI apps on this machine (one click in the desktop app).

Each app keeps MCP servers in a JSON config file. We only ever add, update or remove our own
"folder-index" entry, back the file up first, and refuse to touch files we cannot parse."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import indexer

NAME = "folder-index"
HOME = Path.home()
APPDATA = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
MAC_SUPPORT = HOME / "Library" / "Application Support"

# id, display name, candidate config files, key holding the servers, entry style, restart hint
CLIENTS = [
    {"id": "claude-code", "name": "Claude Code", "files": [HOME / ".claude.json"], "key": "mcpServers",
     "style": "claude-code", "hint": "Start a new Claude Code session to load it."},
    {"id": "claude-desktop", "name": "Claude Desktop",
     "files": [APPDATA / "Claude" / "claude_desktop_config.json",
               *[Path(p) for p in glob.glob(str(LOCALAPPDATA / "Claude-Data" / "*" / "claude_desktop_config.json"))],
               MAC_SUPPORT / "Claude" / "claude_desktop_config.json",
               HOME / ".config" / "Claude" / "claude_desktop_config.json"],
     "key": "mcpServers", "style": "std", "hint": "Quit and reopen Claude Desktop."},
    {"id": "gemini", "name": "Gemini CLI", "files": [HOME / ".gemini" / "settings.json"], "key": "mcpServers",
     "style": "std", "hint": "Start a new `gemini` session."},
    {"id": "cursor", "name": "Cursor", "files": [HOME / ".cursor" / "mcp.json"], "key": "mcpServers",
     "style": "std", "hint": "Reload the Cursor window."},
    {"id": "vscode", "name": "VS Code", "files": [APPDATA / "Code" / "User" / "mcp.json",
                                                  MAC_SUPPORT / "Code" / "User" / "mcp.json",
                                                  HOME / ".config" / "Code" / "User" / "mcp.json"],
     "key": "servers", "style": "vscode", "hint": "Reload VS Code, then enable it in Copilot Chat's tools."},
    {"id": "vscode-insiders", "name": "VS Code Insiders",
     "files": [APPDATA / "Code - Insiders" / "User" / "mcp.json",
               MAC_SUPPORT / "Code - Insiders" / "User" / "mcp.json",
               HOME / ".config" / "Code - Insiders" / "User" / "mcp.json"],
     "key": "servers", "style": "vscode", "hint": "Reload VS Code Insiders."},
    {"id": "windsurf", "name": "Windsurf", "files": [HOME / ".codeium" / "windsurf" / "mcp_config.json"],
     "key": "mcpServers", "style": "std", "hint": "Refresh MCP servers in Windsurf."},
    {"id": "chatgpt", "name": "ChatGPT", "files": [], "key": None, "style": None,
     "hint": "ChatGPT only connects to remote MCP servers over HTTPS, not to programs on this PC. "
             "Exposing your file index to the internet is not supported on purpose."},
]


def server_command() -> list[str]:
    return [*indexer.cli_command(), "serve"]


def _entry(style: str) -> dict:
    cmd = server_command()
    e = {"command": cmd[0], "args": cmd[1:]}
    if style == "vscode":
        e = {"type": "stdio", **e}
    return e


def _pick_file(c: dict) -> Path | None:
    """The config file in use: an existing one, else the first whose app folder exists."""
    for f in c["files"]:
        if f.exists():
            return f
    for f in c["files"]:
        if f.parent.exists():
            return f
    return None


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if text.strip() else {}


def _save(path: Path, data: dict) -> None:
    if path.exists():
        backup = path.with_name(path.name + ".bak-folder-index")
        if not backup.exists():
            shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp-folder-index")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _claude_cli() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    bundled = sorted(glob.glob(str(LOCALAPPDATA / "Claude-Data" / "*" / "claude-code" / "*" / "*" / "claude.exe")),
                     key=os.path.getmtime)
    return bundled[-1] if bundled else None


def _claude_code_state(data: dict) -> tuple[bool, str]:
    if NAME in (data.get("mcpServers") or {}):
        return True, "all projects"
    local = [p for p, v in (data.get("projects") or {}).items() if NAME in (v.get("mcpServers") or {})]
    if local:
        return True, "only in " + ", ".join(local)
    return False, ""


def detect() -> list[dict]:
    out = []
    for c in CLIENTS:
        item = {"id": c["id"], "name": c["name"], "hint": c["hint"], "supported": c["style"] is not None,
                "installed": False, "connected": False, "scope": "", "config": "", "error": ""}
        if not item["supported"]:
            out.append(item)
            continue
        path = _pick_file(c)
        if c["id"] == "claude-code":
            item["installed"] = bool(path and path.exists()) or bool(_claude_cli())
        else:
            item["installed"] = path is not None
        if path:
            item["config"] = str(path)
            try:
                data = _load(path)
                if c["style"] == "claude-code":
                    item["connected"], item["scope"] = _claude_code_state(data)
                else:
                    item["connected"] = NAME in (data.get(c["key"]) or {})
            except (OSError, ValueError) as exc:
                item["error"] = f"cannot read config: {exc}"
        out.append(item)
    return out


def _client(cid: str) -> dict:
    for c in CLIENTS:
        if c["id"] == cid:
            return c
    raise ValueError(f"unknown app {cid!r}; one of: " + ", ".join(c["id"] for c in CLIENTS))


def connect(cid: str) -> str:
    c = _client(cid)
    if c["style"] is None:
        return f"{c['name']}: not supported. {c['hint']}"
    if c["style"] == "claude-code":
        cli = _claude_cli()
        if cli:
            subprocess.run([cli, "mcp", "remove", NAME, "-s", "user"], capture_output=True)
            res = subprocess.run([cli, "mcp", "add", NAME, "--scope", "user", "--", *server_command()],
                                 capture_output=True, text=True)
            if res.returncode != 0:
                return f"Claude Code: failed: {(res.stderr or res.stdout).strip()}"
            return f"Claude Code: set up for all projects. {c['hint']}"
    path = _pick_file(c)
    if path is None:
        return f"{c['name']}: not found on this computer."
    try:
        data = _load(path)
    except ValueError:
        return f"{c['name']}: {path} is not plain JSON (comments?); add the server by hand."
    data.setdefault(c["key"], {})[NAME] = _entry("std" if c["style"] == "claude-code" else c["style"])
    path.parent.mkdir(parents=True, exist_ok=True)
    _save(path, data)
    return f"{c['name']}: set up in {path}. {c['hint']}"


def disconnect(cid: str) -> str:
    c = _client(cid)
    if c["style"] is None:
        return f"{c['name']}: nothing to remove."
    if c["style"] == "claude-code" and (cli := _claude_cli()):
        subprocess.run([cli, "mcp", "remove", NAME, "-s", "user"], capture_output=True)
        return "Claude Code: removed from all projects (per-project entries stay)."
    path = _pick_file(c)
    if not path or not path.exists():
        return f"{c['name']}: not connected."
    try:
        data = _load(path)
    except ValueError:
        return f"{c['name']}: {path} is not plain JSON; remove the entry by hand."
    if NAME in (data.get(c["key"]) or {}):
        del data[c["key"]][NAME]
        _save(path, data)
    return f"{c['name']}: removed."
