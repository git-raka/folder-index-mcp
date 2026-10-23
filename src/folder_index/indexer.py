"""Walk a folder, extract text, and keep an incremental SQLite FTS5 index."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
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
    error TEXT,
    ocr TEXT                        -- NULL = not tried | done | empty | failed: ...
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
        cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
        if "ocr" not in cols:  # index built by 0.1.0
            conn.execute("ALTER TABLE files ADD COLUMN ocr TEXT")
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
    if ext in config.OCR_IMAGE_EXTS:
        # images get content status so the OCR pass picks them up
        ok = config.MIN_OCR_IMAGE_BYTES <= size <= config.MAX_OCR_IMAGE_BYTES
        return "content" if ok else "metadata"
    if kind == "archive" and ext in config.ARCHIVE_EXTS:
        return "content" if size <= config.MAX_DOC_BYTES else "metadata"
    if kind == "text":
        return "content"
    return "metadata"


class RootUnavailable(OSError):
    """The indexed folder is missing or unreadable (unplugged drive, offline share, deleted)."""


def _encodable(name: str) -> bool:
    try:
        name.encode("utf-8")
        return True
    except UnicodeEncodeError:  # lone surrogate: an invalid UTF-16 name on Windows
        return False


def scan(settings: Settings, skip: set[Path] = frozenset()) -> list[Entry]:
    """Walk the root without following links; prune excluded/ignored directories.
    Raises RootUnavailable when the root itself cannot be listed, so callers never mistake an
    offline folder for an empty one."""
    root = settings.root
    out: list[Entry] = []
    stack: list[tuple[Path, Path | None]] = [(root, None)]  # (dir, root of enclosing code tree)
    while stack:
        d, code_root = stack.pop()
        try:
            with os.scandir(d) as it:
                items = [e for e in it if _encodable(e.name)]
        except OSError as exc:
            if d == root:
                raise RootUnavailable(f"cannot read {root}: {exc}") from exc
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
    """Cross-process guard so two updates (or two OCR passes) never run at once."""

    STALE_SECONDS = 3 * 3600

    def __init__(self, db_path: Path, suffix: str = ".lock"):
        self.path = db_path.with_suffix(suffix)
        self.held = False

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not _lock_alive(self.path):
            try:
                self.path.unlink()  # left behind by a killed process
            except FileNotFoundError:
                pass
            except OSError:
                return False  # someone else is touching it right now; treat as taken
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.held = True
        except OSError:  # exists, or briefly locked by a reader on Windows
            self.held = False
        return self.held

    def touch(self) -> None:
        """Long jobs call this so their lock never looks stale."""
        if self.held:
            try:
                os.utime(self.path)
            except OSError:
                pass

    def __exit__(self, *exc) -> None:
        if not self.held:
            return
        for _ in range(40):  # a reader may hold the file open for a moment (WinError 32)
            try:
                self.path.unlink(missing_ok=True)
                return
            except OSError:
                time.sleep(0.05)


def _lock_alive(lock: Path) -> bool:
    """A lock counts only while the process that wrote it is alive (and it isn't ancient).
    Background passes can be killed with their host, e.g. when an MCP client exits."""
    from .presence import pid_alive

    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False  # no lock
    if age > UpdateLock.STALE_SECONDS:
        return False
    try:
        pid = int(lock.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        pid = 0
    if not pid:  # just created and not written yet, or briefly unreadable: trust a fresh lock
        return age < 30
    return pid == os.getpid() or pid_alive(pid)


def _running(db_path: Path, suffix: str) -> bool:
    return _lock_alive(db_path.with_suffix(suffix))


def is_updating(db_path: Path) -> bool:
    return _running(db_path, ".lock")


def is_ocr_running(db_path: Path) -> bool:
    return _running(db_path, ".ocr.lock")


def load_json(value: str | None):
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


def cli_command() -> list[str]:
    """How to invoke the CLI: the module from source, or the console exe next to a frozen app."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("folder-index.exe" if os.name == "nt" else "folder-index")
        return [str(exe)]
    return [sys.executable, "-m", "folder_index.cli"]


def spawn(settings: Settings, *args: str, low_priority: bool = True) -> None:
    """Run `folder-index --root <root> <args>` detached, logging next to the index."""
    spawn_cli(["--root", str(settings.root), "--db", str(settings.db_path), *args],
              settings.db_path.with_suffix(".log"), low_priority)


def spawn_cli(args: list[str], log_path: Path, low_priority: bool = True) -> None:
    """Run `folder-index <args>` detached from this process, output appended to log_path."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "a", encoding="utf-8")
    cmd = [*cli_command(), *args]
    kw = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True)
    if os.name != "nt":
        subprocess.Popen(cmd, start_new_session=True, **kw)
        return
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    if low_priority:
        flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
    try:  # outlive the MCP host, which may kill its whole job when the session ends
        subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, **kw)


MAX_WORKERS = 32  # ProcessPoolExecutor refuses more than 61 on Windows


def clamp_workers(n) -> int:
    try:
        return max(1, min(int(n), MAX_WORKERS))
    except (TypeError, ValueError):
        return 1


def run_isolated(fn, jobs, n_workers: int, on_result, should_stop=lambda: False,
                 initializer=None, initargs=()) -> bool:
    """Run fn(*args) for every (key, args) job in worker processes, at most 2*n in flight.
    on_result(key, result, error) is called in this process. If a worker dies (native crash,
    out of memory), the jobs that were in flight are re-run one at a time in fresh processes,
    so only the file that really crashes is reported. Returns True if stopped early."""
    pending = list(reversed(jobs))
    suspects: list = []
    stopped = False
    while (pending or suspects) and not stopped:
        if suspects:
            key, args = suspects.pop()
            try:
                with ProcessPoolExecutor(1, initializer=initializer, initargs=initargs) as solo:
                    result = solo.submit(fn, *args).result()
                on_result(key, result, None)
            except BrokenProcessPool:
                on_result(key, None, "worker process crashed on this file")
            except Exception as exc:
                on_result(key, None, f"{type(exc).__name__}: {exc}"[:300])
            stopped = should_stop()
            continue
        with ProcessPoolExecutor(n_workers, initializer=initializer, initargs=initargs) as pool:
            inflight: dict = {}
            broken = False
            while pending or inflight:
                while pending and not stopped and len(inflight) < 2 * n_workers:
                    key, args = pending.pop()
                    inflight[pool.submit(fn, *args)] = (key, args)
                if not inflight:
                    break
                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    item = inflight.pop(fut)
                    try:
                        result = fut.result()
                    except BrokenProcessPool:
                        broken = True
                        suspects.append(item)
                        continue
                    except Exception as exc:
                        on_result(item[0], None, f"{type(exc).__name__}: {exc}"[:300])
                        continue
                    on_result(item[0], result, None)
                if broken:
                    suspects.extend(inflight.values())
                    break
                if not stopped and should_stop():
                    stopped = True  # stop submitting; finish and keep what is in flight
    return stopped


def update(settings: Settings, workers: int | None = None, log=print) -> dict:
    lock = UpdateLock(settings.db_path)
    with lock as got:
        if not got:
            log("Another update is already running; skipped.")
            return {"skipped": True}
        if not settings.root.is_dir():
            log(f"Folder not found or offline: {settings.root}. Its index is kept as it is.")
            return {"skipped": True, "missing": True}
        try:
            return _update(settings, workers, log, lock)
        except RootUnavailable as exc:
            log(f"{exc}. Its index is kept as it is.")
            return {"skipped": True, "missing": True}
        finally:
            try:
                conn = connect(settings.db_path)
                set_meta(conn, "progress", "")
                conn.commit()
                conn.close()
            except sqlite3.Error:
                pass


def _update(settings: Settings, workers: int | None, log, lock: UpdateLock) -> dict:
    t0 = time.time()
    conn = connect(settings.db_path)
    set_meta(conn, "root", settings.root)
    conn.commit()
    # never index the index itself, nor this tool's own checkout when it lives under root
    skip = {settings.db_path.parent, Path(__file__).resolve().parents[2]}
    def progress(phase: str, done: int = 0, total: int = 0) -> None:
        set_meta(conn, "progress", json.dumps({"phase": phase, "done": done, "total": total, "started": t0}))
        conn.commit()
        lock.touch()

    progress("scanning")
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
                 error=excluded.error, kind=excluded.kind, ocr=NULL
               RETURNING id""",
            (e.rel, e.name, e.ext, folder, config.kind_of(e.ext), e.size, e.mtime, sha1, status, len(text), error),
        )
        rowid = cur.fetchone()[0]
        conn.execute("DELETE FROM fts WHERE rowid=?", (rowid,))
        conn.execute("INSERT INTO fts(rowid, name, path, content) VALUES (?,?,?,?)",
                     (rowid, e.name, e.rel.replace("/", " / "), text))

    skipped = 0
    for i, e in enumerate(todo_meta, 1):
        try:
            write(e, e.status)
        except (sqlite3.Error, UnicodeError) as exc:
            skipped += 1
            log(f"  skipped {e.rel!r}: {exc}")
        if i % 2000 == 0:
            progress("listing", i, len(todo_meta))
    conn.commit()
    log(f"Metadata: {len(todo_meta):,} new/changed, {len(gone):,} removed")

    errors = 0
    if todo_content:
        n_workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
        log(f"Extracting text from {len(todo_content):,} files with {n_workers} workers...")
        done = 0

        def on_result(e: Entry, result, error) -> None:
            nonlocal done, errors
            done += 1
            text, sha1, err = ("", None, error) if result is None else result[1:]
            try:
                write(e, "error" if err else "content", text, sha1, err)
            except (sqlite3.Error, UnicodeError) as exc:  # an unstorable text: keep going
                err = f"{type(exc).__name__}: {exc}"[:300]
                write(e, "error", "", None, err)
            errors += bool(err)
            if done % 100 == 0:
                progress("reading", done, len(todo_content))
                if done % 500 == 0:
                    log(f"  {done:,}/{len(todo_content):,}")

        run_isolated(_extract_job, [(e, (str(settings.root), e.rel, e.ext)) for e in todo_content],
                     n_workers, on_result)
        conn.commit()

    set_meta(conn, "last_update", time.time())
    set_meta(conn, "progress", "")
    if todo_content or gone:
        conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
    conn.commit()
    conn.close()
    stats = {"files": len(entries), "changed": len(todo_meta) + len(todo_content),
             "removed": len(gone), "errors": errors + skipped, "seconds": round(time.time() - t0, 1)}
    log(f"Done: {stats}")
    return stats


# --- OCR pass -------------------------------------------------------------

OCR_WHERE = (f"status = 'content' AND ocr IS NULL AND "
             f"((ext = '.pdf' AND chars < {config.OCR_MIN_CHARS}) OR kind = 'image')")


def ocr_installed() -> bool:
    import importlib.util

    return all(importlib.util.find_spec(m) for m in ("pypdfium2", "PIL"))


OCR_MODES = ("background", "turbo")
WORKER_RAM_MB = 350  # rough peak per OCR worker: a 200-dpi page render plus the engine


def _free_ram_mb() -> int | None:
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEMORYSTATUSEX(dwLength=ctypes.sizeof(MEMORYSTATUSEX))
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullAvailPhys // (1024 * 1024)
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


def auto_workers(mode: str) -> int:
    """background: 1 worker. turbo: every core but one, capped by free RAM."""
    if mode != "turbo":
        return 1
    cpu = max(1, (os.cpu_count() or 2) - 1)
    ram = _free_ram_mb()
    by_ram = max(1, ram // WORKER_RAM_MB) if ram else cpu
    return clamp_workers(min(cpu, by_ram))


def _stop_path(db_path: Path) -> Path:
    return db_path.with_suffix(".ocr.stop")


def ocr_paused(conn: sqlite3.Connection) -> bool:
    return get_meta(conn, "ocr_paused", "0") == "1"


def set_ocr_paused(settings: Settings, paused: bool) -> None:
    conn = connect(settings.db_path)
    set_meta(conn, "ocr_paused", "1" if paused else "0")
    conn.commit()
    conn.close()


def stop_ocr(settings: Settings, wait: float = 0) -> bool:
    """Ask a running OCR pass to stop after its current file(s). Returns True once nothing runs."""
    if not is_ocr_running(settings.db_path):
        return True
    _stop_path(settings.db_path).touch()
    deadline = time.time() + wait
    while time.time() < deadline and is_ocr_running(settings.db_path):
        time.sleep(0.5)
    return not is_ocr_running(settings.db_path)


def set_ocr_speed(settings: Settings, mode: str, workers: int = 0) -> str:
    """Switch the OCR pass to background / turbo, or pause it. Never blocks."""
    if mode == "pause":
        set_ocr_paused(settings, True)
        stopped = stop_ocr(settings, wait=0)
        return "OCR paused." if stopped else "OCR will pause after the file(s) in progress."
    if mode not in OCR_MODES:
        return f"Unknown mode {mode!r}; use background, turbo or pause."
    if not ocr_installed():
        return "OCR is not installed: pip install 'folder-index-mcp[ocr]'."
    set_ocr_paused(settings, False)
    n = clamp_workers(workers) if workers else auto_workers(mode)
    args = ["ocr", "--mode", mode, "--workers", str(n)]
    running = is_ocr_running(settings.db_path)
    if running:
        args.append("--takeover")
    spawn(settings, *args, low_priority=mode != "turbo")
    how = "switching after the file(s) in progress" if running else "starting now"
    return f"OCR {mode} with {n} worker{'s' if n > 1 else ''}: {how}."


def spawn_ocr_if_pending(settings: Settings, log=print) -> int:
    """Start a background OCR pass when scans/images are waiting. Returns the queue size."""
    if not ocr_installed() or is_ocr_running(settings.db_path):
        return 0
    conn = connect(settings.db_path)
    paused = ocr_paused(conn)
    pending = conn.execute(f"SELECT count(*) FROM files WHERE {OCR_WHERE}").fetchone()[0]
    conn.close()
    if pending and not paused:
        spawn(settings, "ocr")
        log(f"OCR started in the background for {pending:,} scanned PDFs/images.")
    return pending


def _lower_priority() -> None:
    try:
        if os.name == "nt":
            import ctypes

            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:
        pass


def _ocr_one(engine, root: str, rel: str, ext: str, size: int, mtime: float) -> tuple[str, str] | None:
    """OCR one file. Returns (text, state), or None when the file changed since indexing."""
    from . import ocr
    from .extract import _clean

    path = Path(root) / rel
    try:
        st = path.stat()
        if st.st_size != size or abs(st.st_mtime - mtime) > 1e-3:
            return None  # the next update re-queues it
        text = _clean(ocr.ocr_file(engine, path, ext))
        return text, ("done" if text else "empty")
    except Exception as exc:  # unreadable scan, odd image format, ...
        return "", f"failed: {type(exc).__name__}: {exc}"[:300]


_worker_engine = None


def _ocr_worker_init(low_priority: bool) -> None:
    global _worker_engine
    from . import ocr

    if low_priority:
        _lower_priority()
    _worker_engine = ocr.load_engine()


def _ocr_worker_job(root: str, rel: str, ext: str, size: int, mtime: float):
    return _ocr_one(_worker_engine, root, rel, ext, size, mtime)


def run_ocr(settings: Settings, log=print, limit: int | None = None, mode: str = "background",
            workers: int | None = None, takeover: bool = False, in_process: bool = False) -> dict:
    """Work through the OCR queue. takeover: stop a running pass first, then continue its queue.
    Each file is OCR'd in a worker process so a crash in a native library costs one file, not the
    pass; in_process=True runs in this process instead (tests)."""
    if takeover:
        log("Asking the running OCR pass to stop...")
        if not stop_ocr(settings, wait=600):
            log("The running OCR pass did not stop; giving up.")
            return {"skipped": True}
    _stop_path(settings.db_path).unlink(missing_ok=True)
    if not settings.db_path.exists():
        return {"skipped": True}
    lock = UpdateLock(settings.db_path, ".ocr.lock")
    with lock as got:
        if not got:
            log("An OCR pass is already running; skipped.")
            return {"skipped": True}
        conn = connect(settings.db_path)
        if ocr_paused(conn):
            conn.close()
            log("OCR is paused for this folder; skipped.")
            return {"skipped": True, "paused": True}
        from . import ocr

        try:
            engine = ocr.load_engine()
        except ocr.OcrUnavailable as exc:
            conn.close()
            log(f"OCR unavailable: {exc}")
            return {"unavailable": str(exc)}
        n = clamp_workers(workers) if workers else auto_workers(mode)
        low = mode != "turbo"
        if low:
            _lower_priority()
        t0 = time.time()
        rows = conn.execute(f"SELECT id, path, ext, size, mtime FROM files WHERE {OCR_WHERE} "
                            "ORDER BY mtime DESC").fetchall()[:limit]
        by_id = {r["id"]: r for r in rows}
        counts = {"done": 0, "empty": 0, "failed": 0}

        def progress() -> None:
            set_meta(conn, "ocr_progress", json.dumps({"done": sum(counts.values()), "total": len(rows),
                                                       "started": t0, "workers": n, "mode": mode}))
            conn.commit()
            lock.touch()

        set_meta(conn, "ocr_mode", f"{mode} x{n}")
        progress()
        log(f"OCR ({engine.name}, {mode}, {n} worker{'s' if n > 1 else ''}): "
            f"{len(rows):,} files queued, newest first")
        stop_file = _stop_path(settings.db_path)
        last_check = [0.0, False]

        def should_stop() -> bool:
            """A stop request (mode switch) or the folder being paused, checked every second."""
            if stop_file.exists():
                return True
            if time.time() - last_check[0] > 1:
                last_check[:] = [time.time(), ocr_paused(conn)]
            return last_check[1]

        def save(row_id: int, result, error) -> None:
            r = by_id[row_id]
            if error:
                result = ("", f"failed: {error}"[:300])
            if result is None:
                return  # the file changed since indexing; the next update re-queues it
            current = conn.execute("SELECT path, mtime FROM files WHERE id = ?", (row_id,)).fetchone()
            if not current or current["path"] != r["path"] or abs(current["mtime"] - r["mtime"]) > 1e-3:
                return  # the row was replaced or the file re-indexed meanwhile
            text, state = result
            if text:
                old = conn.execute("SELECT content FROM fts WHERE rowid = ?", (row_id,)).fetchone()
                prefix = old[0] + "\n\n" if old and old[0] else ""
                merged = prefix + "[OCR]\n" + text
                conn.execute("UPDATE fts SET content = ? WHERE rowid = ?", (merged, row_id))
                conn.execute("UPDATE files SET chars = ?, ocr = ? WHERE id = ?", (len(merged), state, row_id))
            else:
                conn.execute("UPDATE files SET ocr = ? WHERE id = ?", (state, row_id))
            counts[state.split(":")[0]] += 1
            progress()
            total = sum(counts.values())
            if total % 100 == 0:
                log(f"  OCR {total:,}/{len(rows):,} ({total / (time.time() - t0):.1f} files/s)")

        def job_args(r):
            return str(settings.root), r["path"], r["ext"], r["size"], r["mtime"]

        if in_process:
            stopped = False
            for r in rows:
                if should_stop():
                    stopped = True
                    break
                save(r["id"], _ocr_one(engine, *job_args(r)), None)
        else:
            del engine  # each worker process loads its own
            stopped = run_isolated(_ocr_worker_job, [(r["id"], job_args(r)) for r in rows], n, save,
                                   should_stop, initializer=_ocr_worker_init, initargs=(low,))
        set_meta(conn, "ocr_mode", "")
        set_meta(conn, "ocr_progress", "")
        conn.commit()
        conn.close()
        stop_file.unlink(missing_ok=True)
        stats = {**counts, "stopped": stopped, "workers": n, "seconds": round(time.time() - t0, 1)}
        log(f"OCR {'stopped' if stopped else 'done'}: {stats}")
        return stats


def delete_index(settings: Settings) -> str | None:
    """Delete a folder's index files. Returns None on success, else why it could not (yet)."""
    if is_updating(settings.db_path):
        return "an update of this folder is running; try again when it finishes"
    if not stop_ocr(settings, wait=15):
        return "the OCR pass of this folder did not stop; try again in a moment"
    leftovers = []
    for suffix in (".db", ".db-wal", ".db-shm", ".log", ".lock", ".ocr.lock", ".ocr.stop"):
        path = settings.db_path.with_suffix(suffix)
        for attempt in range(20):
            try:
                path.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.1)
        else:
            leftovers.append(str(path))
    if leftovers:
        config.remember_pending_delete(leftovers)
        return "some index files are still open; they will be deleted on the next start"
    return None

if __name__ == "__main__":  # pragma: no cover
    sys.exit("Use the `folder-index` command.")
