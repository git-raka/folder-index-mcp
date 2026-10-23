"""MCP server (stdio). Opens the indexes read-only and refreshes them in detached
subprocesses, so a long rebuild never blocks or corrupts the stdio channel."""

from __future__ import annotations

import atexit

from mcp.server.mcpserver import MCPServer

from . import presence
from .library import Library


def build(lib: Library, auto_refresh: bool = True) -> MCPServer:
    beat = presence.Heartbeat()
    atexit.register(beat.close)

    async def track_client(ctx, call_next):
        """Record which AI app connected (from `initialize`) and when it was last active."""
        if ctx.method == "initialize" and isinstance(ctx.params, dict):
            info = ctx.params.get("clientInfo") or {}
            beat.client(info.get("name"), info.get("version"))
        name = ctx.params.get("name") if ctx.method == "tools/call" and isinstance(ctx.params, dict) else None
        beat.touch(name)
        return await call_next(ctx)

    folders = ", ".join(str(f.root) for f in lib.folders) or "(none yet)"
    labels = ("Paths are shown as <folder label>/<relative path>; labels: "
              + ", ".join(f"{f.label} = {f.root}" for f in lib.folders) + ".") if lib.multi \
        else "Paths are relative to the indexed folder."
    mcp = MCPServer(
        "folder-index",
        instructions=(
            f"Full-text index of these folders: {folders}. Use these tools instead of listing "
            "directories or opening files to locate things: search_files for words in names or "
            "contents, find_files for name/type/date/size filters, read_file_text to read a "
            f"document's extracted text without opening it. {labels}"
        ),
        middleware=[track_client],
    )

    @mcp.tool()
    def search_files(query_text: str, ext: str = "", folder: str = "", kind: str = "",
                     modified_after: str = "", limit: int = 10) -> str:
        """Full-text search over file names, paths and extracted contents (PDF, Word, Excel,
        PowerPoint, text, code, zip listings, OCR'd scans and images). Every word must match;
        words are prefix-matched. FTS5 syntax ("exact phrase", OR, NOT) also works.
        ext: comma list like "pdf,docx". folder: substring of the path, or "<label>/..." to search
        one indexed folder. kind: document | text | archive | image | media | binary | other.
        modified_after: YYYY-MM-DD."""
        return lib.search(query_text, ext, folder, kind, modified_after, limit)

    @mcp.tool()
    def find_files(name: str = "", ext: str = "", folder: str = "", kind: str = "",
                   modified_after: str = "", modified_before: str = "", min_size_mb: float = 0,
                   sort: str = "modified", limit: int = 20) -> str:
        """List files by metadata. name: substring or glob (e.g. "*invoice*2025*").
        sort: modified | oldest | size | name. Dates are YYYY-MM-DD. Also reports total count/size."""
        return lib.find(name, ext, folder, kind, modified_after, modified_before, min_size_mb, sort, limit)

    @mcp.tool()
    def read_file_text(path: str, offset: int = 0, length: int = 4000) -> str:
        """Return the already-extracted text of an indexed file (path as shown in results, or an
        absolute path). Page through long files with offset."""
        return lib.read_text(path, offset, length)

    @mcp.tool()
    def recent_files(limit: int = 15, ext: str = "") -> str:
        """Most recently modified/downloaded files."""
        return lib.find(ext=ext, sort="modified", limit=limit)

    @mcp.tool()
    def find_duplicates(folder: str = "", limit: int = 20) -> str:
        """Groups of byte-identical documents (also across indexed folders), largest waste first."""
        return lib.duplicates(folder, limit)

    @mcp.tool()
    def index_status() -> str:
        """Indexed folders, file counts, last update, and update/OCR progress."""
        return lib.status_text()

    @mcp.tool()
    def ocr_speed(mode: str = "turbo", workers: int = 0) -> str:
        """Control the OCR pass over scanned PDFs and images.
        mode: "turbo" = parallel workers on all CPU cores but one (capped by free RAM), normal
        priority, fastest; "background" = one low-priority worker (default after updates);
        "pause" = stop OCR until resumed. workers: override the worker count (0 = auto).
        A running pass hands its remaining queue over without redoing finished files."""
        return lib.ocr_speed(mode, workers)

    @mcp.tool()
    def refresh_index() -> str:
        """Start an incremental re-index of every folder in the background (only new/changed
        files are read). Scanned PDFs and images found along the way are OCR'd afterwards."""
        started = lib.refresh()
        if started:
            return "Re-index started in the background for: " + ", ".join(started)
        return "A re-index is already running." if lib.folders else "No folders are indexed yet."

    if auto_refresh:
        lib.refresh(only_stale=True)
    return mcp


def serve(lib: Library, auto_refresh: bool = True) -> None:
    build(lib, auto_refresh).run()
