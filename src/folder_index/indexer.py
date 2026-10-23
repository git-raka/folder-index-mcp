"""Walk a folder, extract text, and keep an incremental SQLite FTS5 index."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from . import config
from .config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,      -- relative to root, forward slashes
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    folder TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha1 TEXT,
    status TEXT NOT NULL,           -- content | metadata | secret | error
    chars INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
CREATE INDEX IF NOT EXISTS files_ext ON files(ext);
CREATE INDEX IF NOT EXISTS files_sha1 ON files(sha1);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    name, path, content, tokenize = "unicode61 remove_diacritics 2"
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(db_path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


@dataclass
class Entry:
    rel: str
    name: str
    ext: str
    size: int
    mtime: float
    status: str  # what we intend: content | metadata | secret


def _plan(name: str, ext: str, size: int, code_root: Path | None, parent: Path) -> str:
    if config.is_secret(name, ext):
        return "secret"
    kind = config.kind_of(ext)
    if kind == "document":
        return "content" if size <= config.MAX_DOC_BYTES else "metadata"
    if code_root is not None:
        # only the project's top-level README describes it; nested ones are noise
        is_top_readme = parent == code_root and name.lower().startswith("readme") and kind == "text"
        return "content" if is_top_readme else "metadata"
    if kind == "archive" and ext in config.ARCHIVE_EXTS:
        return "content" if size <= config.MAX_DOC_BYTES else "metadata"
    if kind == "text":
        return "content"
    return "metadata"


def scan(settings: Settings, skip: set[Path] = frozenset()) -> list[Entry]:
    """Walk the root without following links; prune excluded/ignored directories."""
    root = settings.root
    out: list[Entry] = []
    stack: list[tuple[Path, Path | None]] = [(root, None)]  # (dir, root of enclosing code tree)
    while stack:
        d, code_root = stack.pop()
        try:
            with os.scandir(d) as it:
                items = list(it)
        except OSError:
            continue
        if d != root and code_root is None and any(
                e.name in config.PROJECT_MARKERS for e in items if e.is_file(follow_symlinks=False)):
            code_root = d
        for e in items:
            p = Path(e.path)
            rel = p.relative_to(root).as_posix()
            try:
                if e.is_dir(follow_symlinks=False):
                    if (e.name in config.EXCLUDED_DIRS or p in skip or settings.ignored(rel)):
                        continue
                    stack.append((p, code_root))
                elif e.is_file(follow_symlinks=False):
                    if settings.ignored(rel):
                        continue
                    st = e.stat(follow_symlinks=False)
                    ext = os.path.splitext(e.name)[1].lower()
                    out.append(Entry(rel, e.name, ext, st.st_size, st.st_mtime,
                                     _plan(e.name, ext, st.st_size, code_root, d)))
            except OSError:
                continue
    return out


def _extract_job(root: str, rel: str, ext: str) -> tuple[str, str, str | None, str | None]:
    """Runs in a worker process: returns (rel, text, sha1, error)."""
    from .extract import extract

    path = Path(root) / rel
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return rel, extract(path, ext), h.hexdigest(), None
    except Exception as exc:  # corrupt / locked / password-protected files
        return rel, "", None, f"{type(exc).__name__}: {exc}"[:300]


class UpdateLock:
    """Cross-process guard so the MCP server and the CLI never update at once."""

    STALE_SECONDS = 3 * 3600

    def __init__(self, db_path: Path):
        self.path = db_path.with_suffix(".lock")
        self.held = False

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if time.time() - self.path.stat().st_mtime > self.STALE_SECONDS:
                self.path.unlink()
        except FileNotFoundError:
            pass
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.held = True
        except FileExistsError:
            self.held = False
        return self.held

    def __exit__(self, *exc) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)


def is_updating(db_path: Path) -> bool:
    lock = db_path.with_suffix(".lock")
    return lock.exists() and time.time() - lock.stat().st_mtime < UpdateLock.STALE_SECONDS


def update(settings: Settings, workers: int | None = None, log=print) -> dict:
    with UpdateLock(settings.db_path) as got:
        if not got:
            log("Another update is already running; skipped.")
            return {"skipped": True}
        return _update(settings, workers, log)


def _update(settings: Settings, workers: int | None, log) -> dict:
    t0 = time.time()
    conn = connect(settings.db_path)
    set_meta(conn, "root", settings.root)
    conn.commit()
    # never index the index itself, nor this tool's own checkout when it lives under root
    skip = {settings.db_path.parent, Path(__file__).resolve().parents[2]}
    entries = scan(settings, skip)
    log(f"Scanned {len(entries):,} files in {time.time() - t0:.1f}s")

    existing = {r["path"]: r for r in conn.execute("SELECT id, path, size, mtime, status FROM files")}
    seen, todo_meta, todo_content = set(), [], []
    for e in entries:
        seen.add(e.rel)
        old = existing.get(e.rel)
        if old and old["size"] == e.size and abs(old["mtime"] - e.mtime) < 1e-3 and \
                (old["status"] == e.status or (old["status"] == "error" and e.status == "content")):
            continue
        (todo_content if e.status == "content" else todo_meta).append(e)

    gone = [existing[p]["id"] for p in existing.keys() - seen]
    for i in range(0, len(gone), 500):
        ids = gone[i:i + 500]
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM fts WHERE rowid IN ({marks})", ids)
        conn.execute(f"DELETE FROM files WHERE id IN ({marks})", ids)

    def write(e: Entry, status: str, text: str = "", sha1: str | None = None, error: str | None = None):
        folder = e.rel.rsplit("/", 1)[0] if "/" in e.rel else ""
        cur = conn.execute(
            """INSERT INTO files(path, name, ext, folder, kind, size, mtime, sha1, status, chars, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,
                 sha1=excluded.sha1, status=excluded.status, chars=excluded.chars,
                 error=excluded.error, kind=excluded.kind
               RETURNING id""",
            (e.rel, e.name, e.ext, folder, config.kind_of(e.ext), e.size, e.mtime, sha1, status, len(text), error),
        )
        rowid = cur.fetchone()[0]
        conn.execute("DELETE FROM fts WHERE rowid=?", (rowid,))
        conn.execute("INSERT INTO fts(rowid, name, path, content) VALUES (?,?,?,?)",
                     (rowid, e.name, e.rel.replace("/", " / "), text))

    for i, e in enumerate(todo_meta, 1):
        write(e, e.status)
        if i % 2000 == 0:
            conn.commit()
    conn.commit()
    log(f"Metadata: {len(todo_meta):,} new/changed, {len(gone):,} removed")

    errors = 0
    if todo_content:
        n_workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
        log(f"Extracting text from {len(todo_content):,} files with {n_workers} workers...")
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_extract_job, str(settings.root), e.rel, e.ext): e for e in todo_content}
            for i, fut in enumerate(as_completed(futures), 1):
                e = futures[fut]
                try:
                    _, text, sha1, err = fut.result()
                    write(e, "error" if err else "content", text, sha1, err)
                except Exception as exc:  # a crashed worker or an unstorable text: keep going
                    err = f"{type(exc).__name__}: {exc}"[:300]
                    write(e, "error", "", None, err)
                if err:
                    errors += 1
                if i % 100 == 0:
                    conn.commit()
                    if i % 500 == 0:
                        log(f"  {i:,}/{len(todo_content):,}")
        conn.commit()

    set_meta(conn, "last_update", time.time())
    if todo_content or gone:
        conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
    conn.commit()
    conn.close()
    stats = {"files": len(entries), "changed": len(todo_meta) + len(todo_content),
             "removed": len(gone), "errors": errors, "seconds": round(time.time() - t0, 1)}
    log(f"Done: {stats}")
    return stats


if __name__ == "__main__":  # pragma: no cover
    sys.exit("Use the `folder-index` command.")
