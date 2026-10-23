import json
import zipfile
from pathlib import Path

import pytest

from folder_index import config, indexer
from folder_index.config import Settings
from folder_index.library import Library


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Keep the app config, indexes and heartbeats out of the real ~/.folder-index."""
    monkeypatch.setenv("FOLDER_INDEX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))


def _docx(path: Path, text: str) -> None:
    body = f'<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", body)


def build(tmp_path: Path, name: str = "root") -> Settings:
    root = tmp_path / name
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "notes.txt").write_text("Quarterly budget for the Kafka cluster", encoding="utf-8")
    _docx(root / "docs" / "contract.docx", "Service agreement with PT Example")
    (root / "id_rsa").write_text("PRIVATE KEY MATERIAL", encoding="utf-8")
    (root / ".ssh").mkdir()
    (root / ".ssh" / "config").write_text("Host secret-host", encoding="utf-8")
    repo = root / "some-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text("{}", encoding="utf-8")
    (repo / "README.md").write_text("Top level readme mentions zebra", encoding="utf-8")
    (repo / "src" / "app.js").write_text("const zebraCode = 1", encoding="utf-8")
    (root / "copy.txt").write_text("Quarterly budget for the Kafka cluster", encoding="utf-8")
    settings = Settings.load(root, str(tmp_path / f"{name}.db"))
    indexer.update(settings, workers=1, log=lambda m: None)
    return settings


def test_search_reads_contents(tmp_path):
    lib = Library([build(tmp_path)])
    assert "docs/contract.docx" in lib.search("service agreement")
    assert "docs/notes.txt" in lib.search("kafka budg")  # prefix match


def test_secrets_and_excluded_dirs(tmp_path):
    lib = Library([build(tmp_path)])
    assert "No files match" in lib.search("PRIVATE KEY MATERIAL")
    assert "secret-host" not in lib.search("secret host")
    assert "content is never indexed" in lib.read_text("id_rsa")


def test_code_tree_is_name_only_except_top_readme(tmp_path):
    hits = Library([build(tmp_path)]).search("zebra")
    assert "some-repo/README.md" in hits
    assert "app.js" not in hits


def test_incremental_update_and_duplicates(tmp_path):
    s = build(tmp_path)
    (s.root / "docs" / "notes.txt").unlink()
    stats = indexer.update(s, workers=1, log=lambda m: None)
    assert stats["removed"] == 1 and stats["changed"] == 0
    (s.root / "copy2.txt").write_text("Quarterly budget for the Kafka cluster", encoding="utf-8")
    indexer.update(s, workers=1, log=lambda m: None)
    assert "2 copies" in Library([s]).duplicates()


def test_update_leaves_no_stale_progress(tmp_path):
    lib = Library([build(tmp_path)])
    st = lib.folder_stats(lib.folders[0])
    assert st["progress"] is None and not st["updating"] and st["files"] > 0


def test_multiple_folders_are_merged_and_labelled(tmp_path):
    a, b = build(tmp_path, "Alpha"), build(tmp_path, "Beta")
    (b.root / "only-in-beta.txt").write_text("unique walrus invoice", encoding="utf-8")
    indexer.update(b, workers=1, log=lambda m: None)
    lib = Library([a, b])
    assert "Beta/only-in-beta.txt" in lib.search("walrus")
    both = lib.search("service agreement")
    assert "Alpha/docs/contract.docx" in both and "Beta/docs/contract.docx" in both
    assert "Alpha/" not in lib.search("service agreement", folder="Beta")  # label scopes the search
    assert "unique walrus" in lib.read_text("Beta/only-in-beta.txt")
    assert "unique walrus" in lib.read_text(str(b.root / "only-in-beta.txt"))  # absolute path works
    assert "4 copies" in lib.duplicates()  # notes/copy across both folders


def test_app_config_folders(tmp_path):
    d = tmp_path / "Docs"
    d.mkdir()
    config.add_root(d)
    config.add_root(d)  # no duplicates
    assert config.configured_roots() == [d.resolve()]
    config.set_root_enabled(d, False)
    assert config.configured_roots() == []
    assert config.remove_root(d) and config.load_app_config()["roots"] == []


def test_scanned_pdf_is_ocrd(tmp_path, monkeypatch):
    from pypdf import PdfWriter

    from folder_index import ocr

    s = build(tmp_path)
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)  # no text layer, like a scan
    with open(s.root / "docs" / "scan.pdf", "wb") as f:
        w.write(f)
    indexer.update(s, workers=1, log=lambda m: None)

    monkeypatch.setattr(ocr, "load_engine", lambda: type("Fake", (), {"name": "fake"})())
    monkeypatch.setattr(ocr, "ocr_file", lambda engine, path, ext: "Scanned invoice number 4711")
    stats = indexer.run_ocr(s, log=lambda m: None, in_process=True)
    assert stats["done"] == 1
    lib = Library([s])
    assert "docs/scan.pdf" in lib.search("invoice 4711")
    assert "[OCR]" in lib.read_text("docs/scan.pdf")
    assert indexer.run_ocr(s, log=lambda m: None, in_process=True)["done"] == 0  # queue is empty


def test_ocr_pause_stop_and_worker_count(tmp_path):
    s = build(tmp_path)
    assert indexer.auto_workers("background") == 1
    assert indexer.auto_workers("turbo") >= 1
    indexer.set_ocr_speed(s, "pause")
    conn = indexer.connect(s.db_path)
    assert indexer.ocr_paused(conn)
    conn.close()
    text = Library([s]).status_text()
    assert "paused" in text or "not installed" in text
    # a stop request makes a running pass exit before its next file
    indexer._stop_path(s.db_path).touch()
    with indexer.UpdateLock(s.db_path, ".ocr.lock"):
        assert indexer.is_ocr_running(s.db_path)
    assert not indexer.is_ocr_running(s.db_path)


def test_client_connect_and_disconnect(tmp_path, monkeypatch):
    from folder_index import clients

    cfg = tmp_path / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    fake = [{**c, "files": [cfg]} if c["id"] == "cursor" else {**c, "files": []} for c in clients.CLIENTS]
    monkeypatch.setattr(clients, "CLIENTS", fake)

    assert "set up" in clients.connect("cursor")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"] == {"command": "x"}  # other servers untouched
    assert data["mcpServers"]["folder-index"]["args"][-1] == "serve"
    assert cfg.with_name("mcp.json.bak-folder-index").exists()
    assert next(c for c in clients.detect() if c["id"] == "cursor")["connected"]
    clients.disconnect("cursor")
    assert "folder-index" not in json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]

    cfg.write_text("{ // comments are not JSON\n}", encoding="utf-8")
    assert "not plain JSON" in clients.connect("cursor")  # never clobbers what it cannot parse


def test_presence_heartbeat(tmp_path):
    from folder_index import presence

    beat = presence.Heartbeat()
    beat.client("claude-code", "2.1")
    beat.touch("search_files")
    live = presence.sessions()["active"]
    assert live and live[0]["client"] == "claude-code" and live[0]["last_tool"] == "search_files"
    beat.close()
    s = presence.sessions()
    assert not s["active"] and s["history"]["claude-code"]["last_tool"] == "search_files"


def test_lock_of_a_dead_process_is_ignored(tmp_path):
    s = build(tmp_path)
    lock = s.db_path.with_suffix(".lock")
    lock.write_text("999999", encoding="utf-8")  # no such process
    assert not indexer.is_updating(s.db_path)
    assert not indexer.update(s, workers=1, log=lambda m: None).get("skipped")
    assert not lock.exists()


def test_legacy_appdata_folder_is_migrated(tmp_path, monkeypatch):
    legacy = tmp_path / "appdata" / "folder-index"
    s = build(tmp_path)
    legacy.mkdir(parents=True)
    import shutil
    shutil.copy(s.db_path, legacy / "Old-12345678.db")
    (legacy / "config.json").write_text('{"roots": [{"path": "C:/x", "enabled": true}]}', encoding="utf-8")
    (legacy / "Old-12345678.lock").write_text("1", encoding="utf-8")
    monkeypatch.delenv("FOLDER_INDEX_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    copied = config.migrate_legacy_data()
    new = tmp_path / "user" / ".folder-index"
    assert sorted(copied) == ["Old-12345678.db", "config.json"]
    assert (new / "Old-12345678.db").stat().st_size > 0 and not (new / "Old-12345678.lock").exists()
    assert config.migrate_legacy_data() == []  # only once



# --- regressions from the independent QA pass ---------------------------------------------------

def test_missing_folder_keeps_its_index(tmp_path):
    s = build(tmp_path)
    before = Library([s]).folder_stats(Library([s]).folders[0])["files"]
    s.root.rename(tmp_path / "unplugged")
    stats = indexer.update(s, workers=1, log=lambda m: None)
    assert stats.get("missing") and stats.get("skipped")
    lib = Library([s])
    assert lib.folder_stats(lib.folders[0])["files"] == before
    assert "not found or offline" in lib.status_text()


def test_a_crashing_worker_only_fails_its_own_job():
    results = {}
    jobs = [(i, ("x = 1",)) for i in range(12)]
    jobs[5] = (5, ("import os; os._exit(3)",))  # stands in for a native crash
    indexer.run_isolated(exec, jobs, 2, lambda k, r, e: results.__setitem__(k, e))
    assert len(results) == 12
    assert results[5] and "crashed" in results[5]
    assert all(results[k] is None for k in results if k != 5)


def test_invalid_utf16_file_name_does_not_break_updates(tmp_path):
    s = build(tmp_path)
    try:
        (s.root / "bad\ud800name.txt").write_bytes(b"x")
    except (OSError, UnicodeEncodeError):
        pytest.skip("this filesystem refuses such names")
    stats = indexer.update(s, workers=1, log=lambda m: None)
    assert not stats.get("skipped")
    assert "contract.docx" in Library([s]).search("service agreement")


def test_paused_folder_is_skipped_by_ocr(tmp_path):
    s = build(tmp_path)
    indexer.set_ocr_paused(s, True)
    assert indexer.run_ocr(s, log=lambda m: None, in_process=True).get("paused")


def test_text_decoding():
    from folder_index.extract import decode_text

    cut = ("x" * 9 + "josé").encode("utf-8")[:-1]  # read limit cut the é in half
    assert decode_text(cut, truncated=True) == "x" * 9 + "jos"
    assert decode_text("Rapport financier café".encode("cp1252")) == "Rapport financier café"
    assert decode_text("hello world, UTF-16 note".encode("utf-16")) == "hello world, UTF-16 note"
    assert decode_text(("no bom " * 20).encode("utf-16-le")).startswith("no bom")
    assert decode_text(b"\x00\x01\x02binary\x00") == ""


def test_lock_edge_cases(tmp_path):
    lock = tmp_path / "x.lock"
    lock.write_text("", encoding="utf-8")  # created but pid not written yet
    assert indexer._lock_alive(lock)
    assert indexer.clamp_workers(100) == indexer.MAX_WORKERS and indexer.clamp_workers(-3) == 1
    assert indexer.clamp_workers("abc") == 1


def test_more_secret_files_are_name_only():
    for name in ("credentials.json", "token.json", "ConsoleHost_history.txt", "appsettings.Production.json",
                 "wp-config.php", ".npmrc"):
        assert config.is_secret(name.lower(), Path(name).suffix.lower()), name
    assert not config.is_secret("corporate secretary.docx", ".docx")


def test_remove_deletes_index_and_nested_folders_are_refused(tmp_path):
    s = build(tmp_path)
    assert indexer.delete_index(s) is None
    assert not s.db_path.exists()
    outer = tmp_path / "Outer"
    (outer / "inner").mkdir(parents=True)
    config.add_root(outer)
    with pytest.raises(ValueError, match="overlaps"):
        config.add_root(outer / "inner")


def test_bad_inputs_get_friendly_answers(tmp_path):
    s = build(tmp_path)
    (s.root / "report[1].txt").write_text("bracketed name", encoding="utf-8")
    indexer.update(s, workers=1, log=lambda m: None)
    lib = Library([s])
    assert "report[1].txt" in lib.find("report[1]")
    assert "Invalid argument" in lib.search("kafka", modified_after="31-12-2025")
    assert "docs/notes.txt" in lib.search("kafka\x00budget", limit="abc")
    not_yet = Settings.load(tmp_path / "root", str(tmp_path / "never.db"))
    assert "not in the index" in Library([not_yet]).read_text("docs/notes.txt")
