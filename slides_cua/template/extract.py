"""Pull structural facts out of a .pptx export. No LLM, fully deterministic.

Google's `File > Download > Microsoft PowerPoint` export keeps layout names, placeholder
types, geometry, and speaker notes, which is everything needed to describe *what is
editable on a slide and where it sits*. The semantic half (what the slide is for) is
label.py's job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

from .schema import Field, Layout, Slide, Template, Visuals

# Placeholders Slides manages on its own; editing them through the agent is noise.
SKIP_PLACEHOLDERS = {
    PP_PLACEHOLDER.SLIDE_NUMBER,
    PP_PLACEHOLDER.DATE,
    PP_PLACEHOLDER.FOOTER,
    PP_PLACEHOLDER.HEADER,
}

TITLE_PLACEHOLDERS = {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE}

# A text box narrower/shorter than this fraction of the canvas is treated as a caption
# rather than a body: too small to hold bullets, and the planner should not send
# paragraphs to it.
CAPTION_AREA = 0.06

# Shapes whose tops fall within this fraction of each other count as the same visual
# row for reading-order purposes.
ROW_BAND = 0.04

# A field is template chrome when the same text sits at the same spot on at least this
# share of the deck. Both guards matter: the share catches footers in a long template,
# the minimum stops a 3-slide template calling its only heading boilerplate.
BOILERPLATE_SHARE = 0.5
BOILERPLATE_MIN_SLIDES = 5


def extract(pptx_path: Path, name: str) -> Template:
    """Read a .pptx into a Template with the structural half populated."""
    presentation = Presentation(str(pptx_path))
    width = int(presentation.slide_width or 9144000)
    height = int(presentation.slide_height or 5143500)

    layouts, layout_ids = _extract_layouts(presentation)

    slides: list[Slide] = []
    for index, slide in enumerate(presentation.slides, start=1):
        layout = slide.slide_layout
        layout_name = layout.name or f"Layout {index}"
        slides.append(
            Slide(
                id=f"s{index:02d}",
                index=index,
                layout_id=layout_ids.get(id(layout), ""),
                layout_name=layout_name,
                fields=_extract_fields(slide, width, height),
                visuals=_count_visuals(slide),
                notes=_extract_notes(slide),
            )
        )

    _flag_boilerplate(slides)

    return Template(
        name=name,
        slide_size=(width, height),
        source_pptx=pptx_path.name,
        theme=_extract_theme(presentation),
        layouts=layouts,
        slides=slides,
    )


def _flag_boilerplate(slides: list[Slide]) -> None:
    """Mark repeated template chrome so it never reaches the planner.

    A real template puts the same footer on every slide ("Confidential", a page
    number, a logo caption). Left alone, that is one wasted field per slide in the
    planner's context and one more thing the agent might overwrite. Identity is the
    same text at the same spot, which decoration shares and real content never does.
    """
    if len(slides) < BOILERPLATE_MIN_SLIDES:
        return

    counts: dict[tuple[str, int, int], int] = {}
    for slide in slides:
        # Once per slide, so a slide repeating a word internally cannot inflate the count.
        for signature in {_signature(f) for f in slide.fields if f.sample.strip()}:
            counts[signature] = counts.get(signature, 0) + 1

    threshold = max(BOILERPLATE_MIN_SLIDES, int(len(slides) * BOILERPLATE_SHARE))
    repeated = {signature for signature, count in counts.items() if count >= threshold}
    for slide in slides:
        for field in slide.fields:
            if field.sample.strip() and _signature(field) in repeated:
                field.boilerplate = True


def _signature(field: Field) -> tuple[str, int, int]:
    left, top, _, _ = field.anchor
    return (field.sample.strip().lower(), round(left * 100), round(top * 100))


def _extract_layouts(presentation: Any) -> tuple[list[Layout], dict[int, str]]:
    """Catalog every theme layout and return a map from layout object to its id."""
    layouts: list[Layout] = []
    ids: dict[int, str] = {}
    counter = 0
    for master in presentation.slide_masters:
        for layout in master.slide_layouts:
            counter += 1
            layout_id = f"l{counter:02d}"
            ids[id(layout)] = layout_id
            placeholders = []
            for shape in layout.placeholders:
                try:
                    kind = shape.placeholder_format.type
                except (AttributeError, ValueError):
                    continue
                if kind in SKIP_PLACEHOLDERS:
                    continue
                placeholders.append(str(kind).split(".")[-1].split(" ")[0])
            layouts.append(Layout(id=layout_id, name=layout.name or layout_id, placeholders=placeholders))
    return layouts, ids


def _walk(shapes: Any) -> Iterator[Any]:
    """Yield shapes, descending into groups so nested text boxes are not lost."""
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _walk(shape.shapes)
        else:
            yield shape


def _geometry(shape: Any, width: int, height: int) -> tuple[float, float, float, float] | None:
    """Normalize EMU geometry to fractions of the canvas."""
    try:
        left, top = shape.left, shape.top
        shape_width, shape_height = shape.width, shape.height
    except (AttributeError, ValueError):
        return None
    if None in (left, top, shape_width, shape_height):
        return None
    if shape_width <= 0 or shape_height <= 0:
        return None
    # Clamp to the canvas. Designers routinely let a shape bleed off the slide edge,
    # and an anchor outside [0, 1] would send the agent clicking into the Slides
    # chrome instead of the text box.
    x0 = min(max(left / width, 0.0), 1.0)
    y0 = min(max(top / height, 0.0), 1.0)
    x1 = min(max((left + shape_width) / width, 0.0), 1.0)
    y1 = min(max((top + shape_height) / height, 0.0), 1.0)
    if x1 <= x0 or y1 <= y0:
        return None
    return (round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4))


def _placeholder_type(shape: Any) -> Any:
    try:
        return shape.placeholder_format.type
    except (AttributeError, ValueError):
        return None


def _placeholder_idx(shape: Any) -> int | None:
    try:
        return int(shape.placeholder_format.idx)
    except (AttributeError, ValueError, TypeError):
        return None


def _extract_fields(slide: Any, width: int, height: int) -> list[Field]:
    """Every editable region on the slide, in reading order, with stable keys."""
    candidates: list[tuple[tuple[float, float, float, float], str, Any]] = []

    for shape in _walk(slide.shapes):
        placeholder = _placeholder_type(shape)
        if placeholder in SKIP_PLACEHOLDERS:
            continue

        anchor = _geometry(shape, width, height)
        if anchor is None:
            continue

        kind = _classify(shape, placeholder, anchor)
        if kind is None:
            continue
        candidates.append((anchor, kind, shape))

    # Reading order: top-to-bottom in visual rows, then left-to-right inside a row.
    # Rounding the top into bands keeps a row of four timeline captions together even
    # when the designer nudged them a few thousand EMU apart.
    candidates.sort(key=lambda item: (round(item[0][1] / ROW_BAND), item[0][0]))

    fields: list[Field] = []
    counters: dict[str, int] = {}
    for anchor, kind, shape in candidates:
        key = _make_key(kind, counters)
        text = _shape_text(shape)
        lines = [line for line in text.splitlines() if line.strip()]
        fields.append(
            Field(
                key=key,
                kind=kind,
                anchor=anchor,
                max_chars=max((len(line) for line in lines), default=0),
                lines=max(len(lines), 1),
                sample=text.strip()[:300],
                placeholder_idx=_placeholder_idx(shape),
            )
        )
    return fields


def _classify(shape: Any, placeholder: Any, anchor: tuple[float, float, float, float]) -> str | None:
    """Map a shape to a field kind, or None if it is not editable content."""
    if getattr(shape, "has_chart", False):
        return "chart"
    if getattr(shape, "has_table", False):
        return "table"
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE or placeholder == PP_PLACEHOLDER.PICTURE:
        return "image"
    if placeholder in TITLE_PLACEHOLDERS:
        return "title"
    if placeholder == PP_PLACEHOLDER.SUBTITLE:
        return "subtitle"

    if not getattr(shape, "has_text_frame", False):
        return None
    text = _shape_text(shape).strip()
    if not text and placeholder is None:
        # An empty non-placeholder shape is decoration, not a field.
        return None

    if text.isdigit() and len(text) <= 3:
        return "number"
    _, _, width, height = anchor
    if width * height < CAPTION_AREA:
        return "caption"
    return "body"


def _make_key(kind: str, counters: dict[str, int]) -> str:
    """Deterministic key: the first title is `title`, the second is `title_2`."""
    counters[kind] = counters.get(kind, 0) + 1
    count = counters[kind]
    return kind if count == 1 else f"{kind}_{count}"


def _shape_text(shape: Any) -> str:
    if not getattr(shape, "has_text_frame", False):
        return ""
    try:
        return shape.text_frame.text or ""
    except (AttributeError, ValueError):
        return ""


def _count_visuals(slide: Any) -> Visuals:
    visuals = Visuals()
    for shape in _walk(slide.shapes):
        if getattr(shape, "has_chart", False):
            visuals.charts += 1
        elif getattr(shape, "has_table", False):
            visuals.tables += 1
        elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE or _placeholder_type(shape) == PP_PLACEHOLDER.PICTURE:
            # Empty picture placeholders count too: a grid of eight of them is the whole
            # point of a photo-wall slide, and counting only filled ones would hide that.
            visuals.images += 1
        elif not _shape_text(shape).strip() and _placeholder_type(shape) is None:
            visuals.decorative_shapes += 1
    return visuals


def _extract_notes(slide: Any) -> str:
    """Speaker notes. Template authors write 'use this slide for X' here, which is the
    single strongest signal available to the labeling pass."""
    if not getattr(slide, "has_notes_slide", False):
        return ""
    try:
        return (slide.notes_slide.notes_text_frame.text or "").strip()[:2000]
    except (AttributeError, ValueError):
        return ""


def _extract_theme(presentation: Any) -> dict[str, Any]:
    """Fonts and the accent palette, read straight off the theme part's XML.

    python-pptx has no theme API, so this digs into the related part. Every failure
    here is non-fatal: the theme block is context for labeling, not load-bearing.
    """
    theme: dict[str, Any] = {}
    try:
        master = presentation.slide_masters[0]
        part = master.part.part_related_by(
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"
        )
        element = etree.fromstring(part.blob)
    except Exception:  # noqa: BLE001 - theme is optional metadata
        return theme

    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    fonts = {}
    for role, path in (("major", "a:majorFont"), ("minor", "a:minorFont")):
        node = element.find(f".//a:fontScheme/{path}/a:latin", ns)
        if node is not None and node.get("typeface"):
            fonts[role] = node.get("typeface")
    if fonts:
        theme["fonts"] = fonts

    colors = {}
    scheme = element.find(".//a:clrScheme", ns)
    if scheme is not None:
        for child in scheme:
            name = child.tag.split("}")[-1]
            srgb = child.find("a:srgbClr", ns)
            if srgb is not None and srgb.get("val"):
                colors[name] = "#" + srgb.get("val")
    if colors:
        theme["colors"] = colors
    return theme
