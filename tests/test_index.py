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
