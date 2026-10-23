"""Read-side queries against one index database. They return rows; `library` merges rows
from several indexed folders and formats them as compact text or UI data."""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import indexer


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _ts(value: str) -> float:
    return datetime.strptime(value.strip(), "%Y-%m-%d").timestamp()


def _exts(ext: str) -> list[str]:
    return [("." + e.strip().lower().lstrip(".")) for e in ext.split(",") if e.strip()]


def filters(ext: str = "", folder: str = "", kind: str = "", modified_after: str = "",
            modified_before: str = "", min_size_mb: float = 0) -> tuple[str, list]:
    where, args = [], []
    if exts := _exts(ext):
        where.append(f"f.ext IN ({','.join('?' * len(exts))})")
        args += exts
    if folder:
        where.append("(f.path LIKE ? ESCAPE '\\')")
        f = folder.replace("\\", "/").strip("/").replace("%", r"\%").replace("_", r"\_")
        args.append(f"%{f}%")
    if kind:
        where.append("f.kind = ?")
        args.append(kind.lower())
    if modified_after:
        where.append("f.mtime >= ?")
        args.append(_ts(modified_after))
    if modified_before:
        where.append("f.mtime < ?")
        args.append(_ts(modified_before))
    if min_size_mb:
        where.append("f.size >= ?")
        args.append(int(min_size_mb * 1024 * 1024))
    return (" AND " + " AND ".join(where)) if where else "", args


_TERM = re.compile(r"[^\s\"'()*:^+]+", re.U)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_query(q: str) -> str:
    """Drop control characters (NUL breaks FTS5) and cap the length."""
    return _CONTROL.sub(" ", str(q))[:2000]


def term_count(q: str) -> int:
    return len(_TERM.findall(q))


def _fts_query(q: str) -> str:
    """Turn free text into an FTS5 query: every word must match, prefix-matched."""
    terms = _TERM.findall(q)
    return " ".join(f'"{t}"*' for t in terms if t.upper() not in {"AND", "OR", "NOT", "NEAR"})


def search_rows(conn: sqlite3.Connection, query: str, ext: str = "", folder: str = "", kind: str = "",
                modified_after: str = "", limit: int = 10, relaxed: bool = False,
                marks: tuple[str, str] = ("[", "]")) -> list[sqlite3.Row]:
    """Ranked matches (lower `score` is better). relaxed: any word may match instead of all.
    marks: what the snippet puts around matched words."""
    where, args = filters(ext, folder, kind, modified_after)
    sql = f"""SELECT f.*, snippet(fts, 2, ?, ?, '…', 14) AS snip, bm25(fts, 8.0, 3.0, 1.0) AS score
              FROM fts JOIN files f ON f.id = fts.rowid
              WHERE fts MATCH ? {where}
              ORDER BY score LIMIT ?"""
    raw = query.strip()
    if not relaxed and any(tok in raw for tok in ('"', " OR ", " NOT ", "NEAR(")):
        try:
            rows = conn.execute(sql, [*marks, raw, *args, limit]).fetchall()
            if rows:
                return rows
        except sqlite3.OperationalError:
            pass
    fq = _fts_query(raw)
    if not fq:
        return []
    if relaxed:
        fq = fq.replace('* "', '* OR "')
    return conn.execute(sql, [*marks, fq, *args, limit]).fetchall()


def find_rows(conn: sqlite3.Connection, name: str = "", ext: str = "", folder: str = "", kind: str = "",
              modified_after: str = "", modified_before: str = "", min_size_mb: float = 0,
              sort: str = "modified", limit: int = 20) -> tuple[list[sqlite3.Row], int, int]:
    """Rows plus the total count and size of everything matching."""
    where, args = filters(ext, folder, kind, modified_after, modified_before, min_size_mb)
    if name:
        pattern = clean_query(name).replace("\\", "/")
        if not any(c in pattern for c in "*?"):
            pattern = "*" + pattern.replace("[", "[[]") + "*"  # plain text: '[' is literal
        where += " AND lower(f.name) GLOB ?"
        args.append(pattern.lower())
    order = SORTS.get(sort, SORTS["modified"])[0]
    total = conn.execute(f"SELECT count(*), sum(size) FROM files f WHERE 1=1 {where}", args).fetchone()
    rows = conn.execute(f"SELECT f.* FROM files f WHERE 1=1 {where} ORDER BY {order} LIMIT ?",
                        [*args, limit]).fetchall()
    return rows, total[0], total[1] or 0


# sort name -> (SQL order, python key for merging rows from several folders)
SORTS = {
    "modified": ("f.mtime DESC", lambda r: -r["mtime"]),
    "oldest": ("f.mtime ASC", lambda r: r["mtime"]),
    "size": ("f.size DESC", lambda r: -r["size"]),
    "name": ("f.name COLLATE NOCASE", lambda r: r["name"].lower()),
}


def get_row(conn: sqlite3.Connection, rel: str) -> sqlite3.Row | None:
    return conn.execute("SELECT f.*, fts.content FROM files f JOIN fts ON fts.rowid = f.id "
                        "WHERE f.path = ? COLLATE NOCASE", (rel,)).fetchone()


def hashed_rows(conn: sqlite3.Connection, folder: str = "") -> list[sqlite3.Row]:
    where, args = filters(folder=folder)
    return conn.execute(f"SELECT sha1, size, path FROM files f WHERE sha1 IS NOT NULL {where}", args).fetchall()


def stats(conn: sqlite3.Connection, db_path: Path) -> dict:
    """Everything the status text and the desktop app show for one indexed folder."""
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    by_status = dict(conn.execute("SELECT status, count(*) FROM files GROUP BY status").fetchall())
    by_kind = dict(conn.execute("SELECT kind, count(*) FROM files GROUP BY kind").fetchall())
    total_size = conn.execute("SELECT sum(size) FROM files").fetchone()[0] or 0
    ocr_pending = conn.execute(f"SELECT count(*) FROM files WHERE {indexer.OCR_WHERE}").fetchone()[0]
    ocr_states = dict(conn.execute("SELECT CASE WHEN ocr LIKE 'failed%' THEN 'failed' ELSE ocr END, count(*) "
                                   "FROM files WHERE ocr IS NOT NULL GROUP BY 1").fetchall())
    return {
        "root": meta.get("root", ""),
        "db_path": str(db_path),
        "db_size": db_path.stat().st_size if db_path.exists() else 0,
        "last_update": float(meta.get("last_update") or 0),
        "updating": indexer.is_updating(db_path),
        "progress": indexer.load_json(meta.get("progress")),
        "files": sum(by_status.values()),
        "total_size": total_size,
        "by_status": by_status,
        "by_kind": by_kind,
        "ocr": {
            "pending": ocr_pending,
            "done": ocr_states.get("done", 0),
            "empty": ocr_states.get("empty", 0),
            "failed": ocr_states.get("failed", 0),
            "running": indexer.is_ocr_running(db_path),
            "paused": meta.get("ocr_paused") == "1",
            "mode": meta.get("ocr_mode", ""),
            "progress": indexer.load_json(meta.get("ocr_progress")),
            "installed": indexer.ocr_installed(),
        },
    }


def staleness(last_update: float) -> str:
    if not last_update:
        return " The index has never been built."
    age_h = (time.time() - last_update) / 3600
    return f" (Index last updated {age_h:.1f} h ago; call refresh_index if the file is newer.)" if age_h > 1 else ""
