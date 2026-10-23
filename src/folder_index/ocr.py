"""OCR for scanned PDFs and images (optional extra: `pip install folder-index-mcp[ocr]`).

Engines, in order of preference:
- "windows": the OCR engine built into Windows 10/11 (via WinRT), no install needed
- "tesseract": a `tesseract` binary on PATH (any OS)
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config

MAX_SIDE = 4000  # px; keeps big scans fast and under engine limits
PDF_DPI = 200


class OcrUnavailable(RuntimeError):
    pass


def _render_pdf(path: Path):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        for i in range(min(len(pdf), config.MAX_OCR_PAGES)):
            page = pdf[i]
            yield page.render(scale=PDF_DPI / 72).to_pil()
            page.close()
    finally:
        pdf.close()


def _load_image(path: Path):
    from PIL import Image, ImageOps

    img = Image.open(path)
    img.seek(0)  # first frame of GIF/TIFF
    img = ImageOps.exif_transpose(img)
    return img


def _prepare(img):
    img = img.convert("RGB")
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    return img


class _WindowsEngine:
    name = "windows"

    def __init__(self):
        from winrt.windows.media.ocr import OcrEngine

        self.engine = OcrEngine.try_create_from_user_profile_languages()
        if self.engine is None:
            raise OcrUnavailable("no OCR language pack installed in Windows")
        self.loop = asyncio.new_event_loop()

    async def _recognize(self, png: bytes) -> str:
        from winrt.windows.graphics.imaging import BitmapDecoder, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(png)
        await writer.store_async()
        writer.detach_stream()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        if bitmap.bitmap_pixel_format != BitmapPixelFormat.BGRA8:
            bitmap = SoftwareBitmap.convert(bitmap, BitmapPixelFormat.BGRA8)
        result = await self.engine.recognize_async(bitmap)
        return "\n".join(line.text for line in result.lines)

    def image_to_text(self, img) -> str:
        buf = io.BytesIO()
        _prepare(img).save(buf, format="PNG")
        return self.loop.run_until_complete(self._recognize(buf.getvalue()))


class _TesseractEngine:
    name = "tesseract"

    def __init__(self):
        self.exe = shutil.which("tesseract")
        if not self.exe:
            raise OcrUnavailable("tesseract not found on PATH")
        out = subprocess.run([self.exe, "--list-langs"], capture_output=True, text=True).stdout
        have = set(out.split())
        langs = [lang for lang in ("eng", "ind") if lang in have]
        self.langs = "+".join(langs) or "eng"

    def image_to_text(self, img) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            png = os.path.join(tmp, "page.png")
            _prepare(img).save(png)
            res = subprocess.run([self.exe, png, "stdout", "-l", self.langs],
                                 capture_output=True, text=True, encoding="utf-8", errors="ignore")
            return res.stdout


def load_engine():
    """Return the best available engine, or raise OcrUnavailable with the reason."""
    try:
        import PIL  # noqa: F401
        import pypdfium2  # noqa: F401
    except ImportError as exc:
        raise OcrUnavailable("install the extra: pip install 'folder-index-mcp[ocr]'") from exc
    reasons = []
    for cls in ((_WindowsEngine, _TesseractEngine) if os.name == "nt" else (_TesseractEngine,)):
        try:
            return cls()
        except (ImportError, OcrUnavailable, OSError) as exc:
            reasons.append(f"{cls.name}: {exc}")
    raise OcrUnavailable("; ".join(reasons))


def ocr_file(engine, path: Path, ext: str) -> str:
    if ext == ".pdf":
        pages = [engine.image_to_text(img) for img in _render_pdf(path)]
        return "\n\n".join(p for p in pages if p.strip())
    return engine.image_to_text(_load_image(path))
