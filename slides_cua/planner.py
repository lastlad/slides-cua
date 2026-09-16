"""Map content onto template slides before the browser is touched.

Keeping this out of the CUA loop is deliberate: a bad slide choice discovered mid-run
costs many turns to undo, and the choices themselves get buried in the transcript. Here
the decision is one cheap call, written to disk, and editable by hand before the build.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .template.schema import Template

SYSTEM = """You are planning a slide deck by choosing slides from an existing template.

You get the template catalog (every slide with its role, purpose, capacity and field keys)
and the content to present. Produce an ordered deck plan.

Rules:
1. Pick template slides whose role and purpose actually fit the content. Never invent a
   slide that is not in the catalog.
2. Prefer ASCENDING template order. The deck is built by copying the template and deleting
   what is unused, so a plan that follows template order needs no reordering at all. Depart
   from it only when the narrative genuinely demands it.
3. Respect capacity. If a section has more items than a slide holds, use that slide more
   than once (list it again at the next position) rather than cramming it. Prefer a slide
   marked repeatable when you need to reuse one. Reusing a slide that is not marked
   repeatable is allowed when nothing else fits, but never reuse a cover, an agenda, or
   a closer.
4. Fill every field key the slide offers, using the exact keys from the catalog. Never
   invent a key. If the content gives you nothing for a field, omit that key entirely
   rather than inventing filler.
5. Respect chars_per_item as a budget, not a target. Tighten the writing to fit.
6. A field that holds several bullets takes ONE string with the bullets separated by
   newlines ("\n"), one bullet per line. Never join bullets with semicolons or commas
   onto a single line, and never prefix them with "-" or "*".
7. Open with a title slide and close with whatever closer the template provides, if it has them.

Write the field values as finished slide copy: short, concrete, no markdown syntax, no
trailing punctuation on bullets."""


@dataclass
class PlannedSlide:
    position: int
    template_slide: str
    role: str
    fields: dict[str, str] = field(default_factory=dict)
    note: str = ""


@dataclass
class Plan:
    template: str
    title: str
    slides: list[PlannedSlide] = field(default_factory=list)
    needs_reorder: bool = False

    @property
    def used(self) -> list[str]:
        """Template slide ids kept, in first-use order."""
        seen: list[str] = []
        for slide in self.slides:
            if slide.template_slide not in seen:
                seen.append(slide.template_slide)
        return seen

    def deletions(self, template: Template) -> list[str]:
        """Template slides to delete from the copy."""
        used = set(self.used)
        return [s.id for s in template.slides if s.id not in used]

    def duplications(self) -> dict[str, int]:
        """Template slide id -> how many copies the plan needs (only where > 1)."""
        counts: dict[str, int] = {}
        for slide in self.slides:
            counts[slide.template_slide] = counts.get(slide.template_slide, 0) + 1
        return {key: value for key, value in counts.items() if value > 1}

    def to_dict(self, template: Template | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "template": self.template,
            "title": self.title,
            "slides": [
                {
                    "position": s.position,
                    "template_slide": s.template_slide,
                    "role": s.role,
                    "fields": s.fields,
                    **({"note": s.note} if s.note else {}),
                }
                for s in self.slides
            ],
            "duplicate": self.duplications(),
            "needs_reorder": self.needs_reorder,
        }
        if template is not None:
            data["delete"] = self.deletions(template)
        return data

    def write(self, path: Path, template: Template | None = None) -> None:
        path.write_text(json.dumps(self.to_dict(template), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> "Plan":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            template=data.get("template", ""),
            title=data.get("title", ""),
            needs_reorder=bool(data.get("needs_reorder")),
            slides=[
                PlannedSlide(
                    position=int(s["position"]),
                    template_slide=s["template_slide"],
                    role=s.get("role", ""),
                    fields=dict(s.get("fields") or {}),
                    note=s.get("note", ""),
                )
                for s in data.get("slides", [])
            ],
        )


def catalog(template: Template) -> str:
    """The planner's view of the template: semantics and field keys, no geometry.

    Anchors and thumbnails are deliberately withheld — they are the build stage's
    concern, and including them would just crowd the context.
    """
    lines = [f'Template "{template.name}" · {len(template.slides)} slides']
    for slide in template.slides:
        head = f"\n{slide.id} (position {slide.index}) · role={slide.role}"
        if slide.repeatable:
            head += " · repeatable"
        lines.append(head)
        lines.append(f"  purpose: {slide.purpose}")
        if slide.keywords:
            lines.append(f"  keywords: {', '.join(slide.keywords)}")
        if slide.capacity:
            lines.append(
                "  capacity: "
                + ", ".join(f"{key}={value}" for key, value in sorted(slide.capacity.items()))
            )
        if slide.visuals.any():
            lines.append(
                f"  visuals (left to the template, not filled by you): {slide.visuals.images} images, "
                f"{slide.visuals.charts} charts, {slide.visuals.tables} tables"
            )
        fillable = slide.content_fields()
        if fillable:
            for f in fillable:
                descriptor = f"  field {f.key} ({f.kind})"
                if f.label:
                    descriptor += f": {f.label}"
                if f.max_chars:
                    descriptor += f" [~{f.max_chars} chars in the template]"
                lines.append(descriptor)
        else:
            lines.append("  fields: none to fill")
    return "\n".join(lines)


def _schema(template: Template) -> dict[str, Any]:
    return {
        "format": {
            "type": "json_schema",
            "name": "deck_plan",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "needs_reorder", "slides"],
                "properties": {
                    "title": {"type": "string"},
                    "needs_reorder": {"type": "boolean"},
                    "slides": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["template_slide", "role", "fields"],
                            "properties": {
                                "template_slide": {
                                    "type": "string",
                                    "enum": [s.id for s in template.slides],
                                },
                                "role": {"type": "string"},
                                "fields": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["key", "value"],
                                        "properties": {
                                            "key": {"type": "string"},
                                            "value": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
    }


def plan_deck(
    template: Template,
    content: str,
    *,
    client: Any,
    model: str,
    title: str | None = None,
    on_event: Callable[[str], None] = lambda _: None,
) -> Plan:
    prompt = "\n".join(
        [
            catalog(template),
            "",
            "=" * 60,
            "",
            f'Deck title: "{title}"' if title else "Choose a deck title from the content.",
            "",
            "Content to present:",
            "---",
            content.strip(),
            "---",
        ]
    )
    on_event(f"planning against template {template.name!r} with {model}")
    response = client.responses.create(
        model=model,
        instructions=SYSTEM,
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        text=_schema(template),
    )
    text = getattr(response, "output_text", "") or ""
    if not text.strip():
        raise RuntimeError("Planner model returned no text.")
    data = json.loads(text)

    plan = Plan(
        template=template.name,
        title=title or data.get("title") or template.name,
        needs_reorder=bool(data.get("needs_reorder")),
    )
    for position, entry in enumerate(data.get("slides", []), start=1):
        source = template.get_slide(entry["template_slide"])
        if source is None:
            continue
        plan.slides.append(
            PlannedSlide(
                position=position,
                template_slide=source.id,
                role=entry.get("role") or source.role,
                fields=_clean_fields(entry.get("fields", []), source),
            )
        )
    _warn(plan, template, on_event)
    return plan


def _clean_fields(entries: list[dict[str, str]], source: Any) -> dict[str, str]:
    """Keep only keys that exist AND can hold pasted text.

    A value aimed at an image or chart field has nowhere to go — the agent fills text
    boxes, not pictures — and a value aimed at boilerplate would overwrite the footer.
    """
    valid = {f.key for f in source.content_fields()}
    cleaned: dict[str, str] = {}
    for entry in entries:
        key, value = entry.get("key", ""), (entry.get("value") or "").strip()
        if key in valid and value:
            cleaned[key] = value
    return cleaned


def _warn(plan: Plan, template: Template, on_event: Callable[[str], None]) -> None:
    """Surface the two failure modes that cost the most CUA turns to recover from."""
    positions = [template.get_slide(s.template_slide).index for s in plan.slides]  # type: ignore[union-attr]
    if positions != sorted(positions) and not plan.needs_reorder:
        on_event("  note: plan departs from template order; the build will reorder slides")
        plan.needs_reorder = True

    for slide in plan.slides:
        source = template.get_slide(slide.template_slide)
        if source is None:
            continue
        if not slide.fields:
            on_event(f"  warning: slide {slide.position} ({source.id}) has no content")
        budget = source.capacity.get("chars_per_item")
        if budget:
            over = [key for key, value in slide.fields.items() if len(value) > budget * 1.5]
            if over:
                on_event(
                    f"  warning: slide {slide.position} ({source.id}) may overflow in {', '.join(over)}"
                )

    counts = plan.duplications()
    for slide_id, count in counts.items():
        source = template.get_slide(slide_id)
        if source is not None and not source.repeatable:
            on_event(f"  warning: {slide_id} used {count}x but is not marked repeatable")
