import zipfile
from contextlib import closing
from pathlib import Path

from folder_index import indexer, query
from folder_index.config import Settings


def _docx(path: Path, text: str) -> None:
    body = f'<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", body)


def build(tmp_path: Path) -> Settings:
    root = tmp_path / "root"
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
    settings = Settings.load(root, str(tmp_path / "index.db"))
    indexer.update(settings, workers=1, log=lambda m: None)
    return settings


def test_search_reads_contents(tmp_path):
    s = build(tmp_path)
    with closing(indexer.connect(s.db_path, readonly=True)) as conn:
        assert "docs/contract.docx" in query.search(conn, "service agreement")
        assert "docs/notes.txt" in query.search(conn, "kafka budg")  # prefix match


def test_secrets_and_excluded_dirs(tmp_path):
    s = build(tmp_path)
    with closing(indexer.connect(s.db_path, readonly=True)) as conn:
        assert "No files match" in query.search(conn, "PRIVATE KEY MATERIAL")
        assert "secret-host" not in query.search(conn, "secret host")
        assert "content is never indexed" in query.read_text(conn, "id_rsa")


def test_code_tree_is_name_only_except_top_readme(tmp_path):
    s = build(tmp_path)
    with closing(indexer.connect(s.db_path, readonly=True)) as conn:
        hits = query.search(conn, "zebra")
        assert "some-repo/README.md" in hits
        assert "app.js" not in hits


def test_incremental_update_and_duplicates(tmp_path):
    s = build(tmp_path)
    (s.root / "docs" / "notes.txt").unlink()
    stats = indexer.update(s, workers=1, log=lambda m: None)
    assert stats["removed"] == 1 and stats["changed"] == 0
    (s.root / "copy2.txt").write_text("Quarterly budget for the Kafka cluster", encoding="utf-8")
    indexer.update(s, workers=1, log=lambda m: None)
    with closing(indexer.connect(s.db_path, readonly=True)) as conn:
        assert "2 copies" in query.duplicates(conn)


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
    stats = indexer.run_ocr(s, log=lambda m: None)
    assert stats["done"] == 1
    with closing(indexer.connect(s.db_path, readonly=True)) as conn:
        assert "docs/scan.pdf" in query.search(conn, "invoice 4711")
        assert "[OCR]" in query.read_text(conn, "docs/scan.pdf")
    assert indexer.run_ocr(s, log=lambda m: None)["done"] == 0  # nothing left in the queue


def test_ocr_pause_stop_and_worker_count(tmp_path):
    s = build(tmp_path)
    assert indexer.auto_workers("background") == 1
    assert indexer.auto_workers("turbo") >= 1

    indexer.set_ocr_speed(s, "pause")
    with closing(indexer.connect(s.db_path, readonly=True)) as conn:
        assert indexer.ocr_paused(conn)
        assert "paused" in query.status(conn, s.db_path) or "nothing queued" in query.status(conn, s.db_path)
    assert indexer.spawn_ocr_if_pending(s, log=lambda m: None) >= 0  # paused: never spawns

    # a stop request makes a running pass exit before its next file
    indexer._stop_path(s.db_path).touch()
    with indexer.UpdateLock(s.db_path, ".ocr.lock"):
        assert indexer.is_ocr_running(s.db_path)
    assert not indexer.is_ocr_running(s.db_path)
    indexer._stop_path(s.db_path).unlink()
