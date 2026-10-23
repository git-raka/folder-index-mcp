"""Text extraction. Office formats are read straight from their XML so the only
heavy dependency is pypdf."""

from __future__ import annotations

import html
import logging
import re
import zipfile
from pathlib import Path

from . import config

logging.getLogger("pypdf").setLevel(logging.ERROR)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    # broken PDFs can yield lone surrogates, which SQLite refuses to store
    text = text.encode("utf-8", "ignore").decode("utf-8")
    text = _WS.sub(" ", text)
    return _BLANKS.sub("\n\n", text).strip()[: config.MAX_CHARS]


def _xml_text(xml: str, para_tags: tuple[str, ...]) -> str:
    for tag in para_tags:
        xml = xml.replace(f"</{tag}>", f"</{tag}>\n")
    return html.unescape(_TAG.sub("", xml))


def _natural_key(name: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", name)]


def _pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return ""
    out, total = [], 0
    for page in reader.pages[: config.MAX_PDF_PAGES]:
        t = page.extract_text() or ""
        out.append(t)
        total += len(t)
        if total > config.MAX_CHARS:
            break
    return "\n".join(out)


def _ooxml(path: Path, ext: str) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        read = lambda n: z.read(n).decode("utf-8", errors="ignore")
        if ext in {".docx", ".docm"}:
            parts = [n for n in names if re.fullmatch(r"word/(document|header\d*|footer\d*|footnotes)\.xml", n)]
            return "\n".join(_xml_text(read(n), ("w:p",)) for n in sorted(parts))
        if ext in {".pptx", ".pptm"}:
            slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_natural_key)
            return "\n\n".join(_xml_text(read(n), ("a:p",)) for n in slides)
        if ext in {".xlsx", ".xlsm"}:
            out = []
            if "xl/workbook.xml" in names:
                out.append("Sheets: " + ", ".join(re.findall(r'<sheet [^>]*name="([^"]+)"', read("xl/workbook.xml"))))
            if "xl/sharedStrings.xml" in names:
                out.append(_xml_text(read("xl/sharedStrings.xml"), ("si",)))
            for n in sorted((n for n in names if n.startswith("xl/worksheets/sheet")), key=_natural_key)[:20]:
                inline = re.findall(r"<is>(.*?)</is>", read(n), re.S)
                if inline:
                    out.append(_xml_text("\n".join(inline), ()))
            return "\n".join(out)
        if ext in {".odt", ".ods", ".odp"} and "content.xml" in names:
            return _xml_text(read("content.xml"), ("text:p", "text:h"))
    return ""


def _archive_listing(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
    shown = names[:2000]
    more = f"\n... (+{len(names) - len(shown)} more entries)" if len(names) > len(shown) else ""
    return "Archive contents:\n" + "\n".join(shown) + more


def _looks_utf16(head: bytes) -> str | None:
    """'utf-16-le'/'utf-16-be' for BOM-less UTF-16 text (NULs on one side of each pair)."""
    if len(head) < 64:
        return None
    even, odd = head[0::2], head[1::2]
    if odd.count(0) > 0.6 * len(odd) and even.count(0) < 0.1 * len(even):
        return "utf-16-le"
    if even.count(0) > 0.6 * len(even) and odd.count(0) < 0.1 * len(odd):
        return "utf-16-be"
    return None


def decode_text(raw: bytes, truncated: bool = False) -> str:
    """Decode text of unknown encoding: BOMs first, then UTF-8 (tolerating a character cut by the
    read limit), else Windows-1252. Returns "" for binary data."""
    import codecs

    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    if enc := _looks_utf16(raw[:4096]):
        return raw.decode(enc, errors="replace")
    if b"\x00" in raw[:4096]:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if truncated and exc.start >= len(raw) - 3:  # the read limit cut a multi-byte character
            return raw[:exc.start].decode("utf-8")
    return raw.decode("cp1252", errors="replace")


def _plain(path: Path) -> str:
    with open(path, "rb") as f:
        raw = f.read(config.MAX_TEXT_BYTES)
    text = decode_text(raw, truncated=len(raw) >= config.MAX_TEXT_BYTES)
    if path.suffix.lower() in {".html", ".htm", ".xml"}:
        text = html.unescape(_TAG.sub(" ", text))
    return text


_UTF16_RUN = re.compile(rb"(?:[\x20-\x7e\xa0-\xff]\x00){6,}")
_ASCII_RUN = re.compile(rb"[A-Za-z0-9][\x20-\x7e]{7,}")


def _legacy_office(path: Path) -> str:
    """Best-effort strings from old binary .doc/.xls/.ppt (no parser dependency)."""
    with open(path, "rb") as f:
        raw = f.read(config.MAX_DOC_BYTES)
    runs = [m.decode("utf-16-le", errors="ignore") for m in _UTF16_RUN.findall(raw)]
    if sum(map(len, runs)) < 200:
        runs += [m.decode("latin-1") for m in _ASCII_RUN.findall(raw)]
    # keep runs that look like words, not binary noise
    return "\n".join(r for r in runs if sum(c.isalpha() or c == " " for c in r) > 0.7 * len(r))


def extract(path: Path, ext: str) -> str:
    """Return extracted text (possibly empty). Raises on unreadable files."""
    if ext == ".pdf":
        text = _pdf(path)
    elif ext in config.LEGACY_OFFICE_EXTS:
        text = _legacy_office(path)
    elif ext in config.DOC_EXTS:
        text = _ooxml(path, ext)
    elif ext in config.ARCHIVE_EXTS:
        text = _archive_listing(path)
    elif ext in config.OCR_IMAGE_EXTS:
        return ""  # no text layer; the OCR pass fills it in
    else:
        text = _plain(path)
    return _clean(text)
