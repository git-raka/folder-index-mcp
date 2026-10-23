"""Command line: `folder-index update|serve|search|find|status`."""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import closing

from . import indexer, query
from .config import Settings


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="folder-index", description=__doc__)
    ap.add_argument("--root", default=os.environ.get("FOLDER_INDEX_ROOT", os.getcwd()),
                    help="folder to index (env FOLDER_INDEX_ROOT, default: current dir)")
    ap.add_argument("--db", default=os.environ.get("FOLDER_INDEX_DB"),
                    help="index file (default: %%LOCALAPPDATA%%/folder-index/<name>-<hash>.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

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

    a = ap.parse_args(argv)
    settings = Settings.load(a.root, a.db)

    if a.cmd == "serve":
        from .server import serve
        serve(settings, auto_refresh=not a.no_auto_refresh)
        return
    log = lambda m: print(m, flush=True)  # noqa: E731
    if a.cmd == "update":
        stats = indexer.update(settings, a.workers, log=log)
        if not a.no_ocr and not stats.get("skipped"):
            indexer.spawn_ocr_if_pending(settings, log=log)
        return
    if a.cmd == "ocr":
        indexer.run_ocr(settings, log=log, limit=a.limit, mode=a.mode,
                        workers=a.workers or None, takeover=a.takeover)
        return
    if a.cmd == "ocr-speed":
        print(indexer.set_ocr_speed(settings, a.mode, a.workers))
        return

    if not settings.db_path.exists():
        sys.exit(f"No index at {settings.db_path}. Run `folder-index --root \"{settings.root}\" update` first.")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with closing(indexer.connect(settings.db_path, readonly=True)) as conn:
        if a.cmd == "search":
            print(query.search(conn, a.query, ext=a.ext, limit=a.limit))
        elif a.cmd == "find":
            print(query.find(conn, a.name, ext=a.ext, sort=a.sort, limit=a.limit))
        elif a.cmd == "dupes":
            print(query.duplicates(conn))
        else:
            print(query.status(conn, settings.db_path))


if __name__ == "__main__":
    main()
