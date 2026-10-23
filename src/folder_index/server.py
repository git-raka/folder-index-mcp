"""MCP server (stdio). Opens the index read-only and refreshes it in a detached
subprocess, so a long rebuild never blocks or corrupts the stdio channel."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import closing

from mcp.server.mcpserver import MCPServer

from . import indexer, query
from .config import Settings

AUTO_REFRESH_AFTER = 15 * 60  # seconds since the last update


def spawn_update(settings: Settings) -> bool:
    if indexer.is_updating(settings.db_path):
        return False
    log = open(settings.db_path.with_suffix(".log"), "a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [sys.executable, "-m", "folder_index.cli", "update", "--root", str(settings.root),
         "--db", str(settings.db_path)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags, close_fds=True,
    )
    return True


def build(settings: Settings, auto_refresh: bool = True) -> MCPServer:
    mcp = MCPServer(
        "folder-index",
        instructions=(
            f"Full-text index of the folder {settings.root}. Use these tools instead of listing "
            "directories or opening files to locate things: search_files for words in names or "
            "contents, find_files for name/type/date/size filters, read_file_text to read a "
            "document's extracted text without opening it. Paths are relative to the root."
        ),
    )
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    indexer.connect(settings.db_path).close()  # make sure the schema exists

    def db():
        return closing(indexer.connect(settings.db_path, readonly=True))

    @mcp.tool()
    def search_files(query_text: str, ext: str = "", folder: str = "", kind: str = "",
                     modified_after: str = "", limit: int = 10) -> str:
        """Full-text search over file names, paths and extracted contents (PDF, Word, Excel,
        PowerPoint, text, code, zip listings). Every word must match; words are prefix-matched.
        FTS5 syntax ("exact phrase", OR, NOT) also works.
        ext: comma list like "pdf,docx". folder: substring of the path. kind: document | text |
        archive | image | media | binary | other. modified_after: YYYY-MM-DD."""
        with db() as conn:
            return query.search(conn, query_text, ext, folder, kind, modified_after, limit)

    @mcp.tool()
    def find_files(name: str = "", ext: str = "", folder: str = "", kind: str = "",
                   modified_after: str = "", modified_before: str = "", min_size_mb: float = 0,
                   sort: str = "modified", limit: int = 20) -> str:
        """List files by metadata. name: substring or glob (e.g. "*invoice*2025*").
        sort: modified | oldest | size | name. Dates are YYYY-MM-DD. Also reports total count/size."""
        with db() as conn:
            return query.find(conn, name, ext, folder, kind, modified_after, modified_before,
                              min_size_mb, sort, limit)

    @mcp.tool()
    def read_file_text(path: str, offset: int = 0, length: int = 4000) -> str:
        """Return the already-extracted text of an indexed file (relative path as shown in
        results). Page through long files with offset."""
        with db() as conn:
            return query.read_text(conn, path, offset, length)

    @mcp.tool()
    def recent_files(limit: int = 15, ext: str = "") -> str:
        """Most recently modified/downloaded files."""
        with db() as conn:
            return query.find(conn, ext=ext, sort="modified", limit=limit)

    @mcp.tool()
    def find_duplicates(folder: str = "", limit: int = 20) -> str:
        """Groups of byte-identical documents, largest waste first."""
        with db() as conn:
            return query.duplicates(conn, folder, limit)

    @mcp.tool()
    def index_status() -> str:
        """Root folder, file counts, last update time and whether an update is running."""
        with db() as conn:
            return query.status(conn, settings.db_path)

    @mcp.tool()
    def refresh_index() -> str:
        """Start an incremental re-index in the background (only new/changed files are read)."""
        if spawn_update(settings):
            return "Re-index started in the background; results update as it progresses."
        return "A re-index is already running."

    if auto_refresh:
        with closing(indexer.connect(settings.db_path)) as conn:
            last = float(indexer.get_meta(conn, "last_update", "0") or 0)
        if time.time() - last > AUTO_REFRESH_AFTER:
            spawn_update(settings)
    return mcp


def serve(settings: Settings, auto_refresh: bool = True) -> None:
    build(settings, auto_refresh).run()
