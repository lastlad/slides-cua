"""Template ingestion: a .pptx export in, a `template.json` catalog out."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import extract as _extract
from . import label as _label
from . import thumbs as _thumbs
from .schema import Field, Layout, Slide, Template, Visuals

__all__ = ["Field", "Layout", "Slide", "Template", "Visuals", "ingest", "load", "TEMPLATE_JSON"]

TEMPLATE_JSON = "template.json"
THUMBS_DIR = "thumbs"


def load(templates_root: Path, name: str) -> Template:
    path = templates_root / name / TEMPLATE_JSON
    if not path.exists():
        raise FileNotFoundError(
            f"No ingested template named {name!r} at {path}. Run `template ingest` first."
        )
    return Template.read(path)


def ingest(
    pptx_path: Path,
    out_dir: Path,
    *,
    name: str,
    pdf_path: Path | None = None,
    client: Any = None,
    model: str = "",
    reconcile: bool = True,
    on_event: Callable[[str], None] = lambda _: None,
) -> Template:
    """Extract structure, render thumbnails, then label. Writes `template.json`."""
    on_event(f"reading {pptx_path.name}")
    template = _extract.extract(pptx_path, name)
    on_event(f"  {len(template.slides)} slides, {len(template.layouts)} theme layouts")

    thumbnails: dict[str, Path] = {}
    if pdf_path is not None:
        on_event(f"rendering thumbnails from {pdf_path.name}")
        paths = _thumbs.render(pdf_path, out_dir / THUMBS_DIR, expected=len(template.slides))
        for slide, path in zip(template.slides, paths):
            slide.thumbnail = f"{THUMBS_DIR}/{path.name}"
            thumbnails[slide.id] = path
    else:
        on_event("no PDF supplied; labeling from structure alone (roles will be rougher)")

    if client is not None:
        _label.label(
            template,
            thumbnails,
            client=client,
            model=model,
            reconcile=reconcile,
            on_event=on_event,
        )
    else:
        on_event("skipping the labeling pass (no API client)")

    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / TEMPLATE_JSON
    template.write(destination)
    on_event(f"wrote {destination}")
    return template
