"""Assign a purpose to every template slide with one vision pass.

extract.py knows a slide has a title and four small caption boxes in a row. Only a model
looking at the rendered pixels knows that is a *timeline*. This module closes that gap
and produces the `role` / `purpose` / `keywords` / `capacity` fields the planner matches on.

Slides go up in chunks rather than one at a time so the model can see repetition across
neighbours — that is what makes `repeatable` reliable.
"""

from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .schema import ROLES, UNKNOWN_ROLE, Slide, Template

CHUNK_SIZE = 8

SYSTEM = f"""You are cataloging a Google Slides template so an automated agent can pick the
right slide for a piece of content later.

For each slide you are given its rendered image plus the structural facts pulled from the
file: layout name, every editable field with its key, kind, position (as fractions of the
canvas), sample text, and the designer's speaker notes.

Speaker notes are the strongest signal — a note like "use this for customer logos" settles
the role outright. The image settles what the structural facts cannot: whether a row of
small text boxes is a timeline, a process, a stat row, or a team grid.

Assign each slide:
- role: exactly one of {", ".join(ROLES)}
- purpose: one concrete sentence describing what belongs on this slide. Describe the slide
  itself, not the sample content ("four-milestone horizontal roadmap", not "our Q1-Q4 plan").
- keywords: 2-6 lowercase terms someone would use when looking for this slide.
- repeatable: true ONLY if duplicating this exact slide with different content would
  produce a sensible deck — a generic content slide used once per section, a per-item
  profile. It is false for:
    * one-off slides: the cover, the agenda, the closer, a single summary;
    * slides that already hold a fixed set internally (a four-person team grid holds the
      team in one slide; you do not repeat the slide, you fill its four slots);
    * one of several ALTERNATIVE layouts for the same job. A template offering a
      seven-person, a six-person and a four-person team grid is asking you to pick the
      one that fits, not to duplicate any of them.
  When in doubt, false. A wrongly repeatable slide produces duplicated agendas.
- capacity: items = how many content entries fit before it looks broken; chars_per_item =
  a sensible per-entry character budget. Judge from the rendered size of the boxes.
- fields: for every field key you were given, a short human label saying what goes in it
  ("second milestone caption"). Use the exact keys you were given and do not invent any.
  Image, chart and table fields are listed for context but are not filled with text, so
  label them by what belongs there ("team member headshot").

Be literal. If a slide is mostly decorative with one headline, say so rather than inventing
structure that is not there."""

RECONCILE_SYSTEM = f"""You are reviewing role assignments for a slide template as a whole.

You get every slide's index, assigned role, and purpose. Fix only what is wrong when seen
in deck context:
- The first slide is almost always `title`; a bare "Questions?"/"Thank you" ending is `qa`
  or `closing`.
- Slides built on the same structure should share a role.
- A slide whose role duplicates a neighbour's but is genuinely distinct keeps its role.

Then re-judge `repeatable` with the whole deck in view, and be strict about it:
- Several consecutive slides offering the same role at different capacities are
  ALTERNATIVES the author is asking you to choose between. Alternatives are NOT
  repeatable — none of them.
- A role that appears exactly once and reads as structural (cover, agenda, divider,
  budget, closer) is NOT repeatable.
- Mark repeatable only for a genuinely generic slide that a deck would plausibly use
  several times over with different content.
Most slides in a well-built template are not repeatable. Defaulting to true is a bug.

Return every slide, changed or not. Roles must come from: {", ".join(ROLES)}"""


def _slide_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "role", "purpose", "keywords", "repeatable", "capacity", "fields"],
        "properties": {
            "id": {"type": "string"},
            "role": {"type": "string", "enum": list(ROLES)},
            "purpose": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "repeatable": {"type": "boolean"},
            "capacity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["items", "chars_per_item"],
                "properties": {
                    "items": {"type": "integer"},
                    "chars_per_item": {"type": "integer"},
                },
            },
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "label"],
                    "properties": {"key": {"type": "string"}, "label": {"type": "string"}},
                },
            },
        },
    }


def _response_format(name: str, item_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": name,
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["slides"],
                "properties": {"slides": {"type": "array", "items": item_schema}},
            },
        }
    }


def describe(slide: Slide) -> str:
    """The structural facts for one slide, as the model sees them."""
    lines = [
        f"slide {slide.id} (position {slide.index})",
        f"  layout: {slide.layout_name}",
    ]
    if slide.notes:
        lines.append(f"  speaker notes: {slide.notes}")
    if slide.visuals.any():
        lines.append(
            f"  visuals: {slide.visuals.images} images, {slide.visuals.charts} charts, "
            f"{slide.visuals.tables} tables"
        )
    visible = [f for f in slide.fields if not f.boilerplate]
    if not visible:
        lines.append("  fields: none (decorative or fully empty slide)")
    for f in visible:
        left, top, width, height = f.anchor
        sample = f.sample.replace("\n", " / ")[:120]
        lines.append(
            f"  field {f.key}: kind={f.kind} at x={left:.2f} y={top:.2f} "
            f"w={width:.2f} h={height:.2f}, {f.lines} line(s)"
            + (f', sample="{sample}"' if sample else ", empty")
        )
    return "\n".join(lines)


def _image_part(path: Path) -> dict[str, Any]:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "input_image", "detail": "auto", "image_url": f"data:image/png;base64,{data}"}


def _call(client: Any, model: str, system: str, content: list[dict[str, Any]], fmt: dict[str, Any]) -> dict[str, Any]:
    response = client.responses.create(
        model=model,
        instructions=system,
        input=[{"role": "user", "content": content}],
        text=fmt,
    )
    text = getattr(response, "output_text", "") or ""
    if not text.strip():
        raise RuntimeError("Labeling model returned no text.")
    return json.loads(text)


def _label_chunk(
    client: Any, model: str, slides: list[Slide], thumbs: dict[str, Path]
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": f"Catalog these {len(slides)} slides. Each image below is preceded by its facts.",
        }
    ]
    for slide in slides:
        content.append({"type": "input_text", "text": describe(slide)})
        thumb = thumbs.get(slide.id)
        if thumb is not None:
            content.append(_image_part(thumb))
    data = _call(client, model, SYSTEM, content, _response_format("slide_catalog", _slide_schema()))
    return data.get("slides", [])


def _reconcile(client: Any, model: str, template: Template) -> None:
    summary = "\n".join(
        f"{s.id} (position {s.index}, layout {s.layout_name}): role={s.role} · {s.purpose}"
        for s in template.slides
    )
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "role", "repeatable"],
        "properties": {
            "id": {"type": "string"},
            "role": {"type": "string", "enum": list(ROLES)},
            "repeatable": {"type": "boolean"},
        },
    }
    data = _call(
        client,
        model,
        RECONCILE_SYSTEM,
        [{"type": "input_text", "text": summary}],
        _response_format("role_review", item_schema),
    )
    for entry in data.get("slides", []):
        slide = template.get_slide(entry.get("id", ""))
        if slide is None:
            continue
        role = entry.get("role")
        if role in ROLES:
            slide.role = role
        if isinstance(entry.get("repeatable"), bool):
            slide.repeatable = entry["repeatable"]


def label(
    template: Template,
    thumbs: dict[str, Path],
    *,
    client: Any,
    model: str,
    chunk_size: int = CHUNK_SIZE,
    reconcile: bool = True,
    on_event: Callable[[str], None] = lambda _: None,
) -> Template:
    """Fill in the semantic half of every slide. Mutates and returns `template`."""
    chunks = [template.slides[i : i + chunk_size] for i in range(0, len(template.slides), chunk_size)]
    on_event(f"labeling {len(template.slides)} slides in {len(chunks)} chunk(s) with {model}")

    with ThreadPoolExecutor(max_workers=min(4, len(chunks) or 1)) as pool:
        results = list(pool.map(lambda chunk: _label_chunk(client, model, chunk, thumbs), chunks))

    for entries in results:
        for entry in entries:
            slide = template.get_slide(entry.get("id", ""))
            if slide is None:
                continue
            _apply(slide, entry)

    # Anything the model skipped still needs a usable role, or the planner would never
    # consider that slide at all.
    for slide in template.slides:
        if not slide.role:
            slide.role = UNKNOWN_ROLE
            slide.purpose = slide.purpose or f"{slide.layout_name} slide"
            on_event(f"  {slide.id}: no label returned, defaulted to {UNKNOWN_ROLE}")

    if reconcile and template.slides:
        on_event("reconciling roles across the deck")
        _reconcile(client, model, template)

    for slide in template.slides:
        on_event(f"  {slide.id} · {slide.role}{' (repeatable)' if slide.repeatable else ''} · {slide.purpose}")
    return template


def _apply(slide: Slide, entry: dict[str, Any]) -> None:
    role = entry.get("role")
    slide.role = role if role in ROLES else UNKNOWN_ROLE
    slide.purpose = (entry.get("purpose") or "").strip()
    slide.keywords = [k.strip().lower() for k in entry.get("keywords", []) if k.strip()][:6]
    slide.repeatable = bool(entry.get("repeatable"))

    capacity = entry.get("capacity") or {}
    slide.capacity = {
        key: int(capacity[key])
        for key in ("items", "chars_per_item")
        if isinstance(capacity.get(key), (int, float))
    }

    # Labels attach to existing keys only. A hallucinated key must never create a field,
    # because a field with no anchor is a field the agent cannot click.
    for item in entry.get("fields", []):
        target = slide.get_field(item.get("key", ""))
        if target is not None:
            target.label = (item.get("label") or "").strip()[:120]
