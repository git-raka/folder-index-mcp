"""Index policy: what gets skipped, what gets metadata only, what gets full content."""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

# Directories never walked at all (dependency caches, VCS internals, secret stores).
EXCLUDED_DIRS = {
    "node_modules", ".git", ".hg", ".svn", "__pycache__", ".venv", "venv",
    "site-packages", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".gradle", ".next", ".nuxt", ".cache", ".idea", ".vs",
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store",
}

# Files listed by name only; their contents are never read.
SECRET_EXTS = {
    ".pem", ".key", ".crt", ".cer", ".der", ".csr", ".p12", ".pfx", ".jks",
    ".keystore", ".truststore", ".kdbx", ".ovpn", ".ppk", ".gpg", ".asc",
}
SECRET_NAMES = {
    ".env", ".bash_history", ".zsh_history", ".python_history", ".netrc",
    ".pgpass", ".git-credentials", "credentials", "id_rsa", "id_ed25519",
    "id_ecdsa", "id_dsa", "known_hosts", "authorized_keys",
}
SECRET_NAME_GLOBS = ["id_rsa*", "id_ed25519*", ".env.*", "client_secret*", "*secrets.*",
                     "secret.*", "*.secret", "*.tfstate", "*service-account*.json"]

# A directory holding one of these is treated as a source-code tree: its files
# are indexed by name only (except READMEs), so code does not drown documents.
PROJECT_MARKERS = {
    "package.json", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "Cargo.toml", "pyproject.toml", "setup.py", "composer.json",
    "Gemfile", "CMakeLists.txt", "gradlew", "mvnw", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", ".gitignore", "Makefile", "build.xml", "go.env",
    # licence/notice files mark an unpacked distribution (SDKs, Apache tarballs)
    "LICENSE", "LICENSE.txt", "LICENSE.md", "LICENCE", "NOTICE", "NOTICE.txt", "COPYING",
}

LEGACY_OFFICE_EXTS = {".doc", ".xls", ".ppt"}
DOC_EXTS = {".pdf", ".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm",
            ".odt", ".ods", ".odp"} | LEGACY_OFFICE_EXTS
ARCHIVE_EXTS = {".zip"}  # archives whose member list is indexed
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".xml",
    ".html", ".htm", ".sql", ".log", ".sh", ".ps1", ".bat", ".cmd", ".py",
    ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".cs", ".php", ".rb",
    ".rs", ".c", ".h", ".cpp", ".css", ".scss", ".vue", ".svelte", ".tf",
    ".hcl", ".cypher", ".graphql", ".srt", ".vtt", ".eml", ".rtf", ".tex",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic", ".tif", ".tiff"}
MEDIA_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav", ".m4a", ".flac", ".webm"}

MAX_DOC_BYTES = 80 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_CHARS = 60_000
MAX_PDF_PAGES = 60

# OCR (optional extra): PDFs with less text than this are treated as scans
OCR_MIN_CHARS = 100
MAX_OCR_PAGES = 30
OCR_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"}
MIN_OCR_IMAGE_BYTES = 30 * 1024      # skip icons and thumbnails
MAX_OCR_IMAGE_BYTES = 25 * 1024 * 1024

IGNORE_FILE = ".folderindexignore"


def kind_of(ext: str) -> str:
    if ext in DOC_EXTS:
        return "document"
    if ext in ARCHIVE_EXTS or ext in {".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".jar", ".war"}:
        return "archive"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in MEDIA_EXTS:
        return "media"
    if ext in TEXT_EXTS:
        return "text"
    if ext in {".exe", ".msi", ".dll", ".apk", ".dmg", ".iso", ".deb", ".rpm"}:
        return "binary"
    return "other"


def is_secret(name: str, ext: str) -> bool:
    low = name.lower()
    if ext in SECRET_EXTS or low in SECRET_NAMES:
        return True
    return any(fnmatch.fnmatch(low, g) for g in SECRET_NAME_GLOBS)


def default_db_path(root: Path) -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".local", "share")
    digest = hashlib.sha1(str(root).lower().encode()).hexdigest()[:8]
    return Path(base) / "folder-index" / f"{root.name or 'root'}-{digest}.db"


@dataclass
class Settings:
    root: Path
    db_path: Path
    ignore_globs: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, root: str | os.PathLike, db_path: str | None = None) -> "Settings":
        r = Path(root).expanduser().resolve()
        globs: list[str] = []
        ign = r / IGNORE_FILE
        if ign.is_file():
            for line in ign.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    globs.append(line.replace("\\", "/").strip("/"))
        return cls(root=r, db_path=Path(db_path) if db_path else default_db_path(r), ignore_globs=globs)

    def ignored(self, rel_posix: str) -> bool:
        name = rel_posix.rsplit("/", 1)[-1]
        for g in self.ignore_globs:
            if fnmatch.fnmatch(rel_posix, g) or fnmatch.fnmatch(name, g):
                return True
        return False


# --- app-wide config: the list of indexed folders --------------------------

def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(Path.home(), ".local", "share")
    return Path(base) / "folder-index"


def config_path() -> Path:
    return data_dir() / "config.json"


def load_app_config() -> dict:
    import json

    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        cfg = {}
    cfg.setdefault("roots", [])
    return cfg


def save_app_config(cfg: dict) -> None:
    import json

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def configured_roots(include_disabled: bool = False) -> list[Path]:
    return [Path(r["path"]) for r in load_app_config()["roots"]
            if include_disabled or r.get("enabled", True)]


def add_root(path: str | os.PathLike) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"not a folder: {p}")
    cfg = load_app_config()
    if not any(Path(r["path"]) == p for r in cfg["roots"]):
        cfg["roots"].append({"path": str(p), "enabled": True})
        save_app_config(cfg)
    return p


def remove_root(path: str | os.PathLike) -> bool:
    p = Path(path).expanduser().resolve()
    cfg = load_app_config()
    keep = [r for r in cfg["roots"] if Path(r["path"]) != p]
    changed = len(keep) != len(cfg["roots"])
    cfg["roots"] = keep
    save_app_config(cfg)
    return changed


def set_root_enabled(path: str | os.PathLike, enabled: bool) -> None:
    p = Path(path).expanduser().resolve()
    cfg = load_app_config()
    for r in cfg["roots"]:
        if Path(r["path"]) == p:
            r["enabled"] = enabled
    save_app_config(cfg)
