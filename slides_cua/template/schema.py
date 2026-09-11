"""Data model for an ingested Google Slides template.

`template.json` is the contract between the three stages: `extract` fills in the
structural half, `label` fills in the semantic half, and `planner` reads both. Keep
serialization here so the stages never hand-roll a dict shape.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Controlled vocabulary. The planner matches content sections against these, so a
# freeform role would silently never be selected. label.py constrains the model to
# this list; unknown values fall back to UNKNOWN_ROLE.
ROLES = (
    "title",
    "agenda",
    "section_divider",
    "content_bullets",
    "two_column",
    "comparison",
    "quote",
    "stat_highlight",
    "chart",
    "table",
    "timeline",
    "process",
    "team",
    "image_full",
    "image_text",
    "closing",
    "qa",
    "contact",
    "appendix",
)

UNKNOWN_ROLE = "content_bullets"

# Field kinds, derived from the pptx placeholder type where there is one and from
# geometry otherwise.
FIELD_KINDS = ("title", "subtitle", "body", "caption", "number", "image", "table", "chart")

# Kinds the agent can actually fill by pasting text. Images, charts and tables are left
# to the template; the planner is never offered them.
TEXT_KINDS = ("title", "subtitle", "body", "caption", "number")


@dataclass
class Field:
    """One editable region on a slide.

    `anchor` is (left, top, width, height) as fractions of the slide canvas. The CUA
    agent multiplies it by the on-screen canvas rect to get a click point, which is
    the whole reason this file exists.
    """

    key: str
    kind: str
    anchor: tuple[float, float, float, float]
    max_chars: int = 0
    lines: int = 1
    sample: str = ""
    placeholder_idx: int | None = None
    # Human meaning, supplied by label.py ("second milestone caption"). The key stays
    # deterministic so labeling can never drift a field away from its anchor.
    label: str = ""
    # Repeated template chrome (a footer, a confidentiality notice, a logo caption).
    # Hidden from the planner and never filled.
    boilerplate: bool = False

    def center(self) -> tuple[float, float]:
        left, top, width, height = self.anchor
        return (left + width / 2, top + height / 2)


@dataclass
class Visuals:
    images: int = 0
    charts: int = 0
    tables: int = 0
    decorative_shapes: int = 0

    def any(self) -> bool:
        return bool(self.images or self.charts or self.tables)


@dataclass
class Layout:
    """A theme layout from Slide > Edit theme, cross-linked from slides by id."""

    id: str
    name: str
    placeholders: list[str] = field(default_factory=list)


@dataclass
class Slide:
    id: str
    index: int
    layout_id: str
    layout_name: str
    fields: list[Field] = field(default_factory=list)
    visuals: Visuals = field(default_factory=Visuals)
    notes: str = ""
    thumbnail: str = ""
    # Filled by label.py.
    role: str = ""
    purpose: str = ""
    keywords: list[str] = field(default_factory=list)
    repeatable: bool = False
    capacity: dict[str, int] = field(default_factory=dict)

    def field_keys(self) -> list[str]:
        return [f.key for f in self.fields if not f.boilerplate]

    def content_fields(self) -> list["Field"]:
        """Fields worth showing the planner: no chrome, no unfillable visuals."""
        return [f for f in self.fields if not f.boilerplate and f.kind in TEXT_KINDS]

    def get_field(self, key: str) -> Field | None:
        for f in self.fields:
            if f.key == key:
                return f
        return None


@dataclass
class Template:
    name: str
    slide_size: tuple[int, int]
    source_pptx: str = ""
    theme: dict[str, Any] = field(default_factory=dict)
    layouts: list[Layout] = field(default_factory=list)
    slides: list[Slide] = field(default_factory=list)

    @property
    def aspect(self) -> float:
        width, height = self.slide_size
        return width / height if height else 16 / 9

    def get_slide(self, slide_id: str) -> Slide | None:
        for slide in self.slides:
            if slide.id == slide_id:
                return slide
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": {"pptx": self.source_pptx, "slide_size": list(self.slide_size)},
            "theme": self.theme,
            "layouts": [asdict(layout) for layout in self.layouts],
            "slides": [_slide_to_dict(slide) for slide in self.slides],
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> "Template":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Template":
        source = data.get("source") or {}
        size = source.get("slide_size") or [9144000, 5143500]
        return cls(
            name=data["name"],
            slide_size=(int(size[0]), int(size[1])),
            source_pptx=source.get("pptx", ""),
            theme=data.get("theme") or {},
            layouts=[Layout(**layout) for layout in data.get("layouts", [])],
            slides=[_slide_from_dict(slide) for slide in data.get("slides", [])],
        )


def _slide_to_dict(slide: Slide) -> dict[str, Any]:
    data = asdict(slide)
    data["fields"] = [{**asdict(f), "anchor": list(f.anchor)} for f in slide.fields]
    return data


def _slide_from_dict(data: dict[str, Any]) -> Slide:
    fields = []
    for raw in data.get("fields", []):
        raw = dict(raw)
        raw["anchor"] = tuple(raw["anchor"])
        fields.append(Field(**raw))
    visuals = Visuals(**(data.get("visuals") or {}))
    known = {k: v for k, v in data.items() if k not in {"fields", "visuals"}}
    return Slide(fields=fields, visuals=visuals, **known)
