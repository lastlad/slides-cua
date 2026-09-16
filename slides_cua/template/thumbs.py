"""Render per-slide PNGs from the template's PDF export.

Rendering .pptx directly needs LibreOffice, which is a heavy and often-absent system
dependency. Google's `File > Download > PDF` gives the same pixels, and pypdfium2 is a
self-contained wheel — so the PDF is the thumbnail source and the .pptx is the
structure source.
"""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium

DEFAULT_WIDTH = 1000


class ThumbnailMismatch(RuntimeError):
    """The PDF and .pptx exports disagree on slide count."""


def render(pdf_path: Path, out_dir: Path, expected: int | None = None, width: int = DEFAULT_WIDTH) -> list[Path]:
    """Render every PDF page to out_dir/NNN.png and return the paths in slide order."""
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        pages = len(document)
        if expected is not None and pages != expected:
            # Silently tolerating this would offset every label after the gap, which is
            # far worse than refusing to ingest.
            raise ThumbnailMismatch(
                f"{pdf_path.name} has {pages} pages but the .pptx has {expected} slides. "
                "Re-download both exports from the same version of the template."
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for index in range(pages):
            page = document[index]
            scale = width / page.get_width() if page.get_width() else 1.0
            image = page.render(scale=scale).to_pil()
            path = out_dir / f"{index + 1:03d}.png"
            image.save(path)
            paths.append(path)
        return paths
    finally:
        document.close()
