"""Several indexed folders behind one API. Each folder keeps its own index database;
queries run against every folder and the results are merged."""

from __future__ import annotations

import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import config, indexer, query
from .config import Settings

AUTO_REFRESH_AFTER = 15 * 60  # seconds since a folder's last update


@dataclass
class Folder:
    label: str
    settings: Settings

    @property
    def root(self) -> Path:
        return self.settings.root


class Library:
    def __init__(self, settings: list[Settings], from_config: bool = False):
        self.from_config = from_config
        self.folders: list[Folder] = []
        used: dict[str, int] = {}
        for s in settings:
            base = s.root.name or str(s.root).rstrip("\\/:") or "root"
            used[base] = used.get(base, 0) + 1
            label = base if used[base] == 1 else f"{base}~{used[base]}"
            self.folders.append(Folder(label, s))

    @classmethod
    def from_app_config(cls) -> "Library":
        return cls([Settings.load(p) for p in config.configured_roots()], from_config=True)

    @classmethod
    def single(cls, root: str | Path, db: str | None = None) -> "Library":
        return cls([Settings.load(root, db)])

    @property
    def multi(self) -> bool:
        return len(self.folders) > 1

    # --- helpers -------------------------------------------------------------

    def _open(self, f: Folder):
        return closing(indexer.connect(f.settings.db_path, readonly=True))

    def _indexed(self) -> list[Folder]:
        return [f for f in self.folders if f.settings.db_path.exists()]

    def display(self, f: Folder, rel: str) -> str:
        return f"{f.label}/{rel}" if self.multi else rel

    def _scope(self, folder: str) -> list[tuple[Folder, str]]:
        """Route a folder filter: "Label/sub" targets one indexed folder, anything else all."""
        wanted = folder.replace("\\", "/").strip("/")
        for f in self.folders:
            low = wanted.lower()
            if self.multi and (low == f.label.lower() or low.startswith(f.label.lower() + "/")):
                return [(f, wanted[len(f.label) + 1:])] if f in self._indexed() else []
        return [(f, wanted) for f in self._indexed()]

    def resolve(self, path: str) -> tuple[Folder, str] | None:
        """Map a displayed, relative or absolute path back to (folder, relative path)."""
        p = path.replace("\\", "/").strip().strip('"')
        for f in self.folders:
            root = f.root.as_posix().lower().rstrip("/")
            if p.lower().startswith(root + "/"):
                return f, p[len(root) + 1:]
        if self.multi:
            for f in self.folders:
                if p.lower().startswith(f.label.lower() + "/"):
                    return f, p[len(f.label) + 1:]
        for f in self._indexed():
            with self._open(f) as conn:
                if query.get_row(conn, p):
                    return f, p
        return None

    def _line(self, i: int, f: Folder, r, snippet: str | None = None) -> str:
        tag = {"secret": " [secret: name only]", "error": " [unreadable]"}.get(r["status"], "")
        out = f"{i}. {self.display(f, r['path'])}  ({query.human(r['size'])}, {query.date(r['mtime'])}){tag}"
        if snippet:
            out += "\n   " + " ".join(snippet.split())
        return out

    def _staleness(self) -> str:
        lasts = [self.folder_stats(f)["last_update"] for f in self._indexed()]
        return query.staleness(min(lasts)) if lasts else " Nothing is indexed yet; add a folder first."

    # --- queries (text for agents and the CLI) -------------------------------

    def search_hits(self, text: str, ext: str = "", folder: str = "", kind: str = "",
                    modified_after: str = "", limit: int = 10,
                    marks: tuple[str, str] = ("[", "]")) -> tuple[list[tuple[Folder, object]], bool]:
        limit = max(1, min(int(limit), 50))
        scope = self._scope(folder)
        for relaxed in (False, True):
            if relaxed and query.term_count(text) < 2:
                break
            hits = []
            for f, sub in scope:
                with self._open(f) as conn:
                    hits += [(f, r) for r in query.search_rows(conn, text, ext, sub, kind, modified_after,
                                                               limit, relaxed, marks)]
            if hits:
                hits.sort(key=lambda h: h[1]["score"])
                return hits[:limit], relaxed
        return [], False

    def search(self, text: str, ext: str = "", folder: str = "", kind: str = "",
               modified_after: str = "", limit: int = 10) -> str:
        hits, relaxed = self.search_hits(text, ext, folder, kind, modified_after, limit)
        if not hits:
            return f"No files match {text!r}." + self._staleness()
        mode = " (partial matches: not every word was found)" if relaxed else ""
        body = "\n".join(self._line(i, f, r, r["snip"] if r["chars"] else None)
                         for i, (f, r) in enumerate(hits, 1))
        return f"{len(hits)} result(s) for {text!r}{mode}:\n{body}"

    def find(self, name: str = "", ext: str = "", folder: str = "", kind: str = "",
             modified_after: str = "", modified_before: str = "", min_size_mb: float = 0,
             sort: str = "modified", limit: int = 20) -> str:
        limit = max(1, min(int(limit), 200))
        sort = sort if sort in query.SORTS else "modified"
        hits, count, size = [], 0, 0
        for f, sub in self._scope(folder):
            with self._open(f) as conn:
                rows, c, s = query.find_rows(conn, name, ext, sub, kind, modified_after, modified_before,
                                             min_size_mb, sort, limit)
            hits += [(f, r) for r in rows]
            count += c
            size += s
        if not hits:
            return "No files match." + self._staleness()
        key = query.SORTS[sort][1]
        hits = sorted(hits, key=lambda h: key(h[1]))[:limit]
        head = f"{count:,} file(s), {query.human(size)} total; showing {len(hits)} by {sort}:"
        return head + "\n" + "\n".join(self._line(i, f, r) for i, (f, r) in enumerate(hits, 1))

    def read_text(self, path: str, offset: int = 0, length: int = 4000) -> str:
        found = self.resolve(path)
        row = None
        if found:
            f, rel = found
            with self._open(f) as conn:
                row = query.get_row(conn, rel)
        if not row:
            return f"{path!r} is not in the index. Use search_files or find_files to get the exact path."
        head = (f"{self.display(f, row['path'])}  ({query.human(row['size'])}, {query.date(row['mtime'])}, "
                f"{row['chars']:,} chars indexed)")
        if row["status"] == "secret":
            return head + "\nThis file is treated as a secret; its content is never indexed."
        if row["status"] == "error":
            return head + f"\nText could not be extracted: {row['error']}"
        if not row["chars"]:
            return head + "\nNo text is indexed for this file type (name/metadata only)."
        offset = max(0, int(offset))
        length = max(200, min(int(length), 20000))
        chunk = row["content"][offset: offset + length]
        more = row["chars"] - (offset + len(chunk))
        tail = f"\n[... {more:,} more chars; call again with offset={offset + len(chunk)}]" if more > 0 else ""
        return f"{head}\n---\n{chunk}{tail}"

    def duplicates(self, folder: str = "", limit: int = 20) -> str:
        groups: dict[str, list] = {}
        for f, sub in self._scope(folder):
            with self._open(f) as conn:
                for r in query.hashed_rows(conn, sub):
                    groups.setdefault(r["sha1"], []).append((r["size"], self.display(f, r["path"])))
        dupes = sorted((g for g in groups.values() if len(g) > 1),
                       key=lambda g: g[0][0] * (len(g) - 1), reverse=True)[:max(1, min(int(limit), 100))]
        if not dupes:
            return "No identical documents found (only content-indexed files are hashed)."
        wasted = sum(g[0][0] * (len(g) - 1) for g in dupes)
        out = [f"{len(dupes)} group(s) of identical files, {query.human(wasted)} reclaimable in these groups:"]
        for i, g in enumerate(dupes, 1):
            paths = "\n      ".join(p for _, p in g)
            out.append(f"{i}. {len(g)} copies x {query.human(g[0][0])}\n      {paths}")
        return "\n".join(out)

    # --- status --------------------------------------------------------------

    def folder_stats(self, f: Folder) -> dict:
        if not f.settings.db_path.exists():
            return {"root": str(f.root), "label": f.label, "never": True, "last_update": 0,
                    "updating": indexer.is_updating(f.settings.db_path), "files": 0}
        with self._open(f) as conn:
            st = query.stats(conn, f.settings.db_path)
        st["label"] = f.label
        st["root"] = str(f.root)
        return st

    def status_text(self) -> str:
        if not self.folders:
            return "No folders are indexed yet. Add one with `folder-index add <folder>` or the desktop app."
        blocks = []
        for f in self.folders:
            st = self.folder_stats(f)
            if st.get("never"):
                blocks.append(f"[{f.label}] {f.root}\n  Not indexed yet"
                              + (" (indexing now)" if st["updating"] else ""))
                continue
            last = st["last_update"]
            o = st["ocr"]
            if o["running"]:
                ocr_state = f"running: {o['mode'] or 'starting'}"
            else:
                ocr_state = "paused" if o["paused"] else "idle"
            ocr_line = (f"OCR ({ocr_state}): {o['pending']:,} pending, done {o['done']:,}, "
                        f"empty {o['empty']:,}, failed {o['failed']:,}")
            if not o["installed"] and not o["pending"]:
                ocr_line = "OCR: not installed"
            blocks.append("\n".join([
                f"[{f.label}] {f.root}",
                f"  Index: {st['db_path']} ({query.human(st['db_size'])})",
                f"  Last update: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}" if last
                else "  Last update: never",
                f"  Update running now: {'yes' if st['updating'] else 'no'}",
                f"  {ocr_line}",
                f"  Files: {st['files']:,} — " + ", ".join(f"{k} {v:,}" for k, v in sorted(st["by_status"].items())),
                "  By kind: " + ", ".join(f"{k} {v:,}" for k, v in
                                          sorted(st["by_kind"].items(), key=lambda kv: -kv[1])),
            ]))
        return "\n".join(blocks)

    # --- actions -------------------------------------------------------------

    def refresh(self, only_stale: bool = False) -> list[str]:
        """Start a background update (followed by OCR) for each folder. Returns the labels started."""
        started = []
        for f in self.folders:
            if indexer.is_updating(f.settings.db_path):
                continue
            if only_stale and f.settings.db_path.exists():
                with self._open(f) as conn:
                    last = float(indexer.get_meta(conn, "last_update", "0") or 0)
                if time.time() - last < AUTO_REFRESH_AFTER:
                    continue
            indexer.spawn(f.settings, "update")
            started.append(f.label)
        return started

    def ocr_speed(self, mode: str, workers: int = 0) -> str:
        mode = mode.lower().strip()
        if mode not in ("pause", *indexer.OCR_MODES):
            return f"Unknown mode {mode!r}; use background, turbo or pause."
        if not self.folders:
            return "No folders are indexed yet."
        if mode == "turbo" and self.multi and self.from_config:
            # one turbo pass walks the folders in turn, so cores are not oversubscribed
            for f in self.folders:
                indexer.set_ocr_paused(f.settings, False)
            n = workers or indexer.auto_workers("turbo")
            args = ["ocr", "--mode", "turbo", "--takeover", "--workers", str(n)]
            indexer.spawn_cli(args, config.data_dir() / "app.log", low_priority=False)
            return f"OCR turbo with {n} workers: working through {len(self.folders)} folders in turn."
        return "\n".join(f"[{f.label}] " + indexer.set_ocr_speed(f.settings, mode, workers)
                         for f in self.folders if f.settings.db_path.exists())
