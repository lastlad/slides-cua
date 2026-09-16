"""Instructions for the deck-building agent."""

from __future__ import annotations

import json
from typing import Any

INSTRUCTIONS = """You are operating a real macOS desktop with PyAutoGUI to build a Google Slides deck.

A Chrome window is already open on a NEW BLANK presentation, signed in as the user.

Ground rules:
- You MUST call exec_py before you answer. Never answer without acting.
- Start with: display(screenshot())
- Coordinates refer to the full desktop screenshot and match click coordinates.
- Operate ONLY the Chrome window showing Google Slides. Do not switch apps, open
  other tabs, visit other sites, or touch system settings.
- Use paste_text("...") for any text longer than a few words. It is far faster and
  more reliable than pyautogui.write().
- On macOS use the "command" modifier, not "ctrl".
- Take a screenshot after each slide to confirm what actually happened.

Google Slides interactions that work well:
- The deck opens on a title slide with "Click to add title" / "Click to add subtitle".
- Click a placeholder once to select it, then click again (or double-click) to put the
  text cursor inside it. Then paste_text(...).
- Press Escape after typing to leave the text box before doing anything else.
- command+m inserts a new slide. New slides use the "Title and body" layout.
- To rename the deck, click the "Untitled presentation" field at the top left, select
  all with command+a, then paste_text(title).
- If a menu or popup opens unexpectedly, press Escape and take a screenshot.

Approach:
1. Screenshot. Confirm the editor is loaded and the title slide is visible.
2. Set the deck title in the top-left name field.
3. Fill the title slide.
4. For each remaining section: command+m, fill the title, fill the body, Escape.
5. Screenshot to verify before moving on.

Finish when every section has a slide. Then give a short final answer stating the deck
title, how many slides you created, and anything that did not work.

This is a demo, not a design exercise. Do not fuss over fonts, spacing, colors, or
pixel-perfect alignment. Getting the content in, on the right slides, is success.
"""


def build_prompt(title: str, content: str) -> str:
    return "\n".join(
        [
            f'Build a Google Slides presentation titled "{title}".',
            "",
            "Use the following content. Each top-level section becomes one slide;",
            "bullets under it become that slide's body text.",
            "",
            "---",
            content.strip(),
            "---",
        ]
    )


TEMPLATE_INSTRUCTIONS = """You are operating a real macOS desktop with PyAutoGUI to build a
Google Slides deck FROM AN EXISTING TEMPLATE.

A Chrome window is open on the template presentation, signed in as the user.

You are given a finished plan. Your job is to execute it, not to redesign it. Do not invent
slides, do not reword the copy you are given, and do not restyle anything. The template's
fonts, colors, and layouts are the point — leave them alone.

Ground rules:
- You MUST call exec_py before you answer. Never answer without acting.
- Start with: display(screenshot())
- Coordinates refer to the full desktop screenshot and match click coordinates.
- Operate ONLY the Chrome window showing Google Slides. Do not switch apps or visit other sites.
- Use paste_text("...") for any text longer than a few words.
- On macOS use the "command" modifier, not "ctrl".
- If a menu or popup opens unexpectedly, press Escape and take a screenshot.

## Step 1 — work on a COPY, never the template

Click File (top-left menu bar of the document, not the Chrome menu) > "Make a copy" >
"Entire presentation". A dialog opens with the name prefilled as "Copy of ...". Leave the
name alone for now, click "Make a copy", and wait — Chrome opens the copy in a NEW TAB and
this takes several seconds on a large template. Screenshot until you can see the copy
loaded. Every later step happens in the copy; never edit the original.

## Step 2 — measure the canvas once, then define your helpers

The plan gives field positions as fractions of the slide canvas, not pixels. Find the slide
area in the editor — the large rectangle right of the filmstrip, bounded by the rulers —
and measure its bounding box in screen pixels. Then define these helpers in exec_py.
Globals survive between calls, so define them ONCE and reuse them for the whole run:

    CANVAS = (left, top, width, height)   # measured in screen pixels

    def at(fx, fy):
        return (int(CANVAS[0] + fx * CANVAS[2]), int(CANVAS[1] + fy * CANVAS[3]))

    def fill(fx, fy, text):
        # Replace the text of the shape at a fractional position.
        pyautogui.click(*at(fx, fy)); sleep(0.6)   # select the shape
        pyautogui.press("enter"); sleep(0.6)       # ENTER text-edit mode
        pyautogui.hotkey("command", "a"); sleep(0.3)
        paste_text(text); sleep(0.5)
        pyautogui.press("escape"); sleep(0.4)

Use fill() for every field. The `press("enter")` step is what actually puts the cursor
inside the shape — double-clicking is unreliable in Slides and will leave you with a
selected-but-not-editable box that silently swallows your paste. Do not substitute your
own sequence.

log() the canvas box and check at(0.5, 0.5) lands in the middle of the slide before you
rely on it. If a fill misses, re-measure CANVAS rather than nudging individual fields.

## Step 3 — delete unused slides

Work in the filmstrip on the left. Delete the slides listed under DELETE, working from the
BOTTOM of the deck upward — deleting top-down shifts every position below the cut and you
will lose track of where you are.

Delete in CONTIGUOUS RUNS, not one slide at a time: click the first thumbnail of a run,
shift+click the last thumbnail of that run, then press Delete once. The filmstrip scrolls,
so scroll to the bottom first and work up. Screenshot after each run and confirm the count.

## Step 4 — duplicate repeated slides

For any template slide the plan uses more than once, right-click its thumbnail > "Duplicate
slide", once per extra copy. Google places each duplicate immediately after the original,
which is exactly where the plan expects it.

## Step 5 — fill each slide

Go through the deck top to bottom. Click the slide's thumbnail in the filmstrip first, then
fill EVERY field for that slide in a SINGLE exec_py call, ending with one screenshot:

    pyautogui.click(<thumbnail>); sleep(1)
    fill(0.505, 0.564, "Make observability work for practitioners")
    fill(0.488, 0.177, "The opportunity")
    display(screenshot())

Filling one field per turn will exhaust the turn budget long before the deck is done. Batch
the whole slide, verify once, move on.

Notes that matter:
- Field values are given as JSON strings. Pass them to paste_text exactly as written —
  a \n inside one is a real line break, which becomes the next bullet.
- The sample text in the template is placeholder content. Replacing it is correct.
- If a text box has MORE lines than your plan supplies, delete the surplus LINES inside the
  box. Never delete the box itself — that breaks the layout.
- Leave images, charts, icons, and decorative shapes untouched unless the plan names them.
- Fields you were not given values for keep the template's text. Leave them.

## Step 6 — rename the deck

Click the document name at the top left (it reads "Copy of ..."), select all with
command+a, paste_text the deck title, and press Enter. Screenshot to confirm the name
changed — this one is easy to get wrong and easy to verify.

## Step 7 — reorder (only if the plan says so)

If REORDER is "no", skip this entirely. If "yes", drag thumbnails in the filmstrip into the
plan's order and screenshot to confirm.

Finish when every planned slide is filled. Give a short final answer with the deck title,
the slide count, and anything that did not work.
"""


def format_plan(plan: Any, template: Any) -> str:
    """Render a deck plan with the geometry the agent needs to click each field."""
    lines: list[str] = []
    deletions = plan.deletions(template)
    duplications = plan.duplications()

    lines.append(f'DECK TITLE: "{plan.title}"')
    lines.append(f"TEMPLATE: {template.name} ({len(template.slides)} slides)")
    lines.append(f"FINAL SLIDE COUNT: {len(plan.slides)}")
    lines.append("REORDER: " + ("yes" if plan.needs_reorder else "no"))
    lines.append("")

    lines.append("DELETE these template slides from the copy (work bottom-up):")
    if deletions:
        for slide_id in sorted(deletions, key=lambda i: -template.get_slide(i).index):
            source = template.get_slide(slide_id)
            lines.append(f"  position {source.index} ({slide_id}) · {source.role} · {source.purpose}")
    else:
        lines.append("  none")
    lines.append("")

    lines.append("DUPLICATE these slides:")
    if duplications:
        for slide_id, count in duplications.items():
            source = template.get_slide(slide_id)
            lines.append(f"  {slide_id} (template position {source.index}) needs {count} copies total")
    else:
        lines.append("  none")
    lines.append("")

    lines.append("FILL each slide. Positions are fractions of the canvas — use at(x, y).")
    for slide in plan.slides:
        source = template.get_slide(slide.template_slide)
        lines.append(f"\n--- deck slide {slide.position} · from {source.id} ({slide.role}) ---")
        if not slide.fields:
            lines.append("  (no content; leave as-is)")
            continue
        for key, value in slide.fields.items():
            spec = source.get_field(key)
            if spec is None:
                continue
            x, y = spec.center()
            # JSON-quoted so multi-line bullet text survives as real newlines when the
            # agent drops it into paste_text(...) rather than being pasted as "\n".
            lines.append(f"  at({x:.3f}, {y:.3f})  {key} ({spec.kind}) = {json.dumps(value)}")
    return "\n".join(lines)


def build_plan_prompt(plan: Any, template: Any) -> str:
    return "\n".join(
        [
            "Build this deck from the open template by following the plan exactly.",
            "",
            format_plan(plan, template),
        ]
    )
