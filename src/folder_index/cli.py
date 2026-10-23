"""Command line: `folder-index [--root DIR] update|serve|search|find|status|ocr|...`.

Without --root, every folder added with `folder-index add` (or the desktop app) is used."""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys

from . import config, indexer
from .config import Settings
from .library import Library


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="folder-index", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.environ.get("FOLDER_INDEX_ROOT"),
                    help="index just this folder (env FOLDER_INDEX_ROOT; default: all added folders)")
    ap.add_argument("--db", default=os.environ.get("FOLDER_INDEX_DB"),
                    help="index file for --root (default: ~/.folder-index/<name>-<hash>.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("folders", help="list the indexed folders")
    ad = sub.add_parser("add", help="add a folder to index")
    ad.add_argument("path")
    rm = sub.add_parser("remove", help="stop indexing a folder (its index file is deleted)")
    rm.add_argument("path")

    up = sub.add_parser("update", help="build or incrementally refresh the index")
    up.add_argument("--workers", type=int, default=None)
    up.add_argument("--no-ocr", action="store_true", help="don't start the background OCR pass")
    oc = sub.add_parser("ocr", help="OCR scanned PDFs and images now (foreground)")
    oc.add_argument("--limit", type=int, default=None)
    oc.add_argument("--mode", choices=["background", "turbo"], default="background",
                    help="background: 1 low-priority worker; turbo: all cores but one, capped by free RAM")
    oc.add_argument("--workers", type=int, default=0, help="override the worker count")
    oc.add_argument("--takeover", action="store_true", help="stop a running OCR pass and continue its queue")
    ocs = sub.add_parser("ocr-speed", help="switch the background OCR pass: background | turbo | pause")
    ocs.add_argument("mode", choices=["background", "turbo", "pause"])
    ocs.add_argument("--workers", type=int, default=0)

    sv = sub.add_parser("serve", help="run the MCP server on stdio")
    sv.add_argument("--no-auto-refresh", action="store_true")
    se = sub.add_parser("search", help="full-text search")
    se.add_argument("query")
    se.add_argument("--ext", default="")
    se.add_argument("--limit", type=int, default=10)
    fi = sub.add_parser("find", help="metadata search")
    fi.add_argument("name", nargs="?", default="")
    fi.add_argument("--ext", default="")
    fi.add_argument("--sort", default="modified")
    fi.add_argument("--limit", type=int, default=20)
    sub.add_parser("status", help="show index statistics")
    sub.add_parser("dupes", help="list identical documents")

    cn = sub.add_parser("connect", help="register the MCP server with an AI app")
    cn.add_argument("client", nargs="?", help="claude-code | claude-desktop | gemini | cursor | vscode ...")
    cn.add_argument("--remove", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> None:
    multiprocessing.freeze_support()
    config.migrate_legacy_data()
    config.cleanup_after_migration()
    a = _parser().parse_args(argv)
    if hasattr(sys.stdout, "reconfigure") and a.cmd != "serve":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    log = lambda m: print(m, flush=True)  # noqa: E731

    if a.cmd == "folders":
        roots = config.load_app_config()["roots"]
        print("\n".join(f"{'on ' if r.get('enabled', True) else 'off'}  {r['path']}" for r in roots)
              or "No folders added yet: folder-index add <folder>")
        return
    if a.cmd == "add":
        p = config.add_root(a.path)
        print(f"Added {p}. Build its index with: folder-index --root \"{p}\" update")
        return
    if a.cmd == "remove":
        s = Settings.load(a.path)
        problem = indexer.delete_index(s)
        if problem and "next start" not in problem:
            sys.exit(f"Not removed: {problem}.")
        removed = config.remove_root(a.path)
        print((f"Removed {s.root}." if removed else f"{s.root} was not in the list.")
              + (f" Note: {problem}." if problem else ""))
        return
    if a.cmd == "connect":
        from . import clients

        if not a.client:
            for c in clients.detect():
                print(f"{c['id']:<15} {c['name']:<16} {'installed' if c['installed'] else 'not found':<10} "
                      f"{'connected' if c['connected'] else ''}")
            return
        print(clients.disconnect(a.client) if a.remove else clients.connect(a.client))
        return

    lib = Library.single(a.root, a.db) if a.root else Library.from_app_config()
    if not lib.folders and a.cmd != "serve":
        sys.exit("No folders to index. Add one with `folder-index add <folder>` or pass --root.")

    if a.cmd == "serve":
        from .server import serve
        serve(lib, auto_refresh=not a.no_auto_refresh)
    elif a.cmd == "update":
        for f in lib.folders:
            log(f"== {f.root}")
            stats = indexer.update(f.settings, a.workers, log=log)
            if not a.no_ocr and not stats.get("skipped"):
                indexer.spawn_ocr_if_pending(f.settings, log=log)
    elif a.cmd == "ocr":
        for f in lib.folders:  # each pass re-checks its folder's pause flag
            indexer.run_ocr(f.settings, log=log, limit=a.limit, mode=a.mode,
                            workers=a.workers or None, takeover=a.takeover)
    elif a.cmd == "ocr-speed":
        print(lib.ocr_speed(a.mode, a.workers))
    elif a.cmd == "search":
        print(lib.search(a.query, ext=a.ext, limit=a.limit))
    elif a.cmd == "find":
        print(lib.find(a.name, ext=a.ext, sort=a.sort, limit=a.limit))
    elif a.cmd == "dupes":
        print(lib.duplicates())
    else:
        print(lib.status_text())


if __name__ == "__main__":
    main()
