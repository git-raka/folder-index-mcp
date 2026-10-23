"""Read-side queries. Every function returns compact plain text, sized for an LLM context."""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import indexer


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _ts(date: str) -> float:
    return datetime.strptime(date.strip(), "%Y-%m-%d").timestamp()


def _exts(ext: str) -> list[str]:
    return [("." + e.strip().lower().lstrip(".")) for e in ext.split(",") if e.strip()]


def _filters(ext: str = "", folder: str = "", kind: str = "", modified_after: str = "",
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


def _line(i: int, r: sqlite3.Row, snippet: str | None = None) -> str:
    tag = {"secret": " [secret: name only]", "error": " [unreadable]"}.get(r["status"], "")
    out = f"{i}. {r['path']}  ({_human(r['size'])}, {_date(r['mtime'])}){tag}"
    if snippet:
        out += "\n   " + " ".join(snippet.split())
    return out


_TERM = re.compile(r"[^\s\"'()*:^+]+", re.U)


def _fts_query(q: str) -> str:
    """Turn free text into an FTS5 query: every word must match, prefix-matched."""
    terms = _TERM.findall(q)
    return " ".join(f'"{t}"*' for t in terms if t.upper() not in {"AND", "OR", "NOT", "NEAR"})


def search(conn: sqlite3.Connection, query: str, ext: str = "", folder: str = "", kind: str = "",
           modified_after: str = "", limit: int = 10) -> str:
    where, args = _filters(ext, folder, kind, modified_after)
    limit = max(1, min(int(limit), 50))
    sql = f"""SELECT f.*, snippet(fts, 2, '[', ']', '…', 14) AS snip
              FROM fts JOIN files f ON f.id = fts.rowid
              WHERE fts MATCH ? {where}
              ORDER BY bm25(fts, 8.0, 3.0, 1.0) LIMIT ?"""
    rows, mode = [], ""
    raw = query.strip()
    if any(tok in raw for tok in ('"', " OR ", " NOT ", "NEAR(")):
        try:
            rows = conn.execute(sql, [raw, *args, limit]).fetchall()
        except sqlite3.OperationalError:
            rows = []
    if not rows and (fq := _fts_query(raw)):
        rows = conn.execute(sql, [fq, *args, limit]).fetchall()
        if not rows and len(_TERM.findall(raw)) > 1:
            # relax AND -> OR so partial matches still surface
            rows = conn.execute(sql, [fq.replace('* "', '* OR "'), *args, limit]).fetchall()
            mode = " (partial matches: not every word was found)"
    if not rows:
        return f"No files match {query!r}." + _staleness(conn)
    body = "\n".join(_line(i, r, r["snip"] if r["chars"] else None) for i, r in enumerate(rows, 1))
    return f"{len(rows)} result(s) for {query!r}{mode}:\n{body}"


def find(conn: sqlite3.Connection, name: str = "", ext: str = "", folder: str = "", kind: str = "",
         modified_after: str = "", modified_before: str = "", min_size_mb: float = 0,
         sort: str = "modified", limit: int = 20) -> str:
    where, args = _filters(ext, folder, kind, modified_after, modified_before, min_size_mb)
    if name:
        pattern = name.replace("\\", "/")
        if not any(c in pattern for c in "*?"):
            pattern = f"*{pattern}*"
        where += " AND lower(f.name) GLOB ?"
        args.append(pattern.lower())
    order = {"modified": "f.mtime DESC", "oldest": "f.mtime ASC", "size": "f.size DESC",
             "name": "f.name COLLATE NOCASE"}.get(sort, "f.mtime DESC")
    limit = max(1, min(int(limit), 200))
    total = conn.execute(f"SELECT count(*), sum(size) FROM files f WHERE 1=1 {where}", args).fetchone()
    rows = conn.execute(f"SELECT f.* FROM files f WHERE 1=1 {where} ORDER BY {order} LIMIT ?",
                        [*args, limit]).fetchall()
    if not rows:
        return "No files match." + _staleness(conn)
    head = f"{total[0]:,} file(s), {_human(total[1] or 0)} total; showing {len(rows)} by {sort}:"
    return head + "\n" + "\n".join(_line(i, r) for i, r in enumerate(rows, 1))


def read_text(conn: sqlite3.Connection, path: str, offset: int = 0, length: int = 4000) -> str:
    p = path.replace("\\", "/").strip()
    root = indexer.get_meta(conn, "root", "")
    if root and p.lower().startswith(Path(root).as_posix().lower() + "/"):
        p = p[len(Path(root).as_posix()) + 1:]
    row = conn.execute("SELECT f.*, fts.content FROM files f JOIN fts ON fts.rowid = f.id "
                       "WHERE f.path = ? COLLATE NOCASE", (p,)).fetchone()
    if not row:
        return f"{path!r} is not in the index. Use search_files or find_files to get the exact path."
    head = f"{row['path']}  ({_human(row['size'])}, {_date(row['mtime'])}, {row['chars']:,} chars indexed)"
    if row["status"] == "secret":
        return head + "\nThis file is treated as a secret; its content is never indexed."
    if row["status"] == "error":
        return head + f"\nText could not be extracted: {row['error']}"
    if not row["chars"]:
        return head + "\nNo text is indexed for this file type (name/metadata only)."
    length = max(200, min(int(length), 20000))
    chunk = row["content"][offset: offset + length]
    more = row["chars"] - (offset + len(chunk))
    tail = f"\n[... {more:,} more chars; call again with offset={offset + len(chunk)}]" if more > 0 else ""
    return f"{head}\n---\n{chunk}{tail}"


def duplicates(conn: sqlite3.Connection, folder: str = "", limit: int = 20) -> str:
    where, args = _filters(folder=folder)
    rows = conn.execute(
        f"""SELECT sha1, size, count(*) AS n, group_concat(path, '\n      ') AS paths
            FROM files f WHERE sha1 IS NOT NULL {where}
            GROUP BY sha1 HAVING n > 1 ORDER BY size * (n - 1) DESC LIMIT ?""",
        [*args, max(1, min(int(limit), 100))]).fetchall()
    if not rows:
        return "No identical documents found (only content-indexed files are hashed)."
    wasted = sum(r["size"] * (r["n"] - 1) for r in rows)
    out = [f"{len(rows)} group(s) of identical files, {_human(wasted)} reclaimable in these groups:"]
    for i, r in enumerate(rows, 1):
        out.append(f"{i}. {r['n']} copies x {_human(r['size'])}\n      {r['paths']}")
    return "\n".join(out)


def status(conn: sqlite3.Connection, db_path: Path) -> str:
    last = float(indexer.get_meta(conn, "last_update", "0") or 0)
    rows = conn.execute("SELECT status, count(*) n, sum(size) s FROM files GROUP BY status").fetchall()
    kinds = conn.execute("SELECT kind, count(*) n FROM files GROUP BY kind ORDER BY n DESC").fetchall()
    total = sum(r["n"] for r in rows)
    lines = [
        f"Root: {indexer.get_meta(conn, 'root', '?')}",
        f"Index: {db_path} ({_human(db_path.stat().st_size) if db_path.exists() else '?'})",
        f"Last update: {datetime.fromtimestamp(last):%Y-%m-%d %H:%M}" if last else "Last update: never",
        f"Update running now: {'yes' if indexer.is_updating(db_path) else 'no'}",
        _ocr_line(conn, db_path),
        f"Files: {total:,} — " + ", ".join(f"{r['status']} {r['n']:,}" for r in rows),
        "By kind: " + ", ".join(f"{r['kind']} {r['n']:,}" for r in kinds),
    ]
    return "\n".join(lines)


def _ocr_line(conn: sqlite3.Connection, db_path: Path) -> str:
    pending = conn.execute(f"SELECT count(*) FROM files WHERE {indexer.OCR_WHERE}").fetchone()[0]
    rows = dict(conn.execute("SELECT CASE WHEN ocr LIKE 'failed%' THEN 'failed' ELSE ocr END, count(*) "
                             "FROM files WHERE ocr IS NOT NULL GROUP BY 1").fetchall())
    if not pending and not rows:
        return "OCR: nothing queued" + ("" if indexer.ocr_installed() else " (OCR extra not installed)")
    if indexer.is_ocr_running(db_path):
        running = f"running: {indexer.get_meta(conn, 'ocr_mode', '') or 'starting'}"
    else:
        running = "paused" if indexer.ocr_paused(conn) else "idle"
    done = ", ".join(f"{k} {v:,}" for k, v in sorted(rows.items()))
    return f"OCR ({running}): {pending:,} pending" + (f", {done}" if done else "")


def _staleness(conn: sqlite3.Connection) -> str:
    last = float(indexer.get_meta(conn, "last_update", "0") or 0)
    if not last:
        return " The index has never been built."
    age_h = (time.time() - last) / 3600
    return f" (Index last updated {age_h:.1f} h ago; call refresh_index if the file is newer.)" if age_h > 1 else ""
