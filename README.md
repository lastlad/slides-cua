# slides-cua

A computer-use agent that builds a Google Slides deck by driving your real browser — no
Google API, no OAuth. It rides the Chrome profile you are already signed into.

Point it at a Google Slides **template** and it works out what each slide in that template
is for, maps your content onto the right slides, and fills them in — keeping the
template's fonts, colors, and layouts intact.

Inspired by `openai-cua-sample-app`; the agent loop and the `log()`/`display()`
observation model are lifted from its Python app, with the server, web console,
and lab harness stripped out.

## Setup

```bash
uv sync
uv run python -m slides_cua --check     # verify macOS desktop permissions
```

Grant **Accessibility** and **Screen Recording** to the terminal app you launch from
(System Settings → Privacy & Security), then restart that app.

Create `.env`:

```bash
OPENAI_API_KEY="sk-..."
SLIDES_CUA_MODEL="gpt-5.6-terra"    # optional; used by every stage unless --model overrides
```

---

# Using your own template

Three stages. Each is independently runnable, and the slide choices are reviewable on
disk before the browser is ever touched.

```
your-template.pptx ──ingest──▶ template.json ──plan──▶ plan.json ──build──▶ deck
       + .pdf                  (once per template)    (once per deck)     (CUA)
```

## Step 1 — export the template

Open the template in Google Slides and download it **twice**:

| Menu | Gives us |
|---|---|
| `File > Download > Microsoft PowerPoint (.pptx)` | structure: layout names, placeholders, geometry, speaker notes |
| `File > Download > PDF Document (.pdf)` | one rendered image per slide |

Put both in `templates/`. Both exports must come from the *same version* of the template —
if the slide counts disagree the ingest aborts rather than mislabeling everything after
the gap.

You also need the template's **edit URL** for step 5. Copy it from the address bar; the
`?slide=...` fragment is fine to leave on.

## Step 2 — ingest it

```bash
uv run python -m slides_cua template ingest templates/acme.pptx \
    --pdf templates/acme.pdf --name acme
```

This writes `templates/acme/template.json` plus `templates/acme/thumbs/`. Roughly a minute
and a handful of API calls for a 36-slide template. You only do this once per template.

What it produces, per slide:

- **role** — one of: `title` `agenda` `section_divider` `content_bullets` `two_column`
  `comparison` `quote` `stat_highlight` `chart` `table` `timeline` `process` `team`
  `image_full` `image_text` `closing` `qa` `contact` `appendix`
- **purpose** — one concrete sentence ("Seven-month launch timeline with four staggered
  campaign stages and their date ranges")
- **keywords**, **capacity** (how many items fit, character budget each)
- **fields** — every editable box with a stable key, a human label, and an **anchor**:
  its position as a fraction of the slide canvas

That anchor is what makes the build work. The agent measures the slide area on screen once,
then converts each anchor into a click point — instead of hunting for text boxes screenshot
by screenshot.

Repeated template chrome (a "Confidential" footer, a page number) is detected and hidden,
as are image, chart, and table fields, which the agent does not fill.

## Step 3 — review the catalog

```bash
uv run python -m slides_cua template show acme
```

Read the roles and purposes. This is the one artifact everything downstream depends on,
and it is worth thirty seconds. If a slide is badly mislabeled, re-run the ingest — labeling
is not deterministic — or edit `templates/acme/template.json` by hand.

## Step 4 — write content and plan the deck

Content is plain markdown. `#` is the deck title, each `##` is a section, bullets underneath
are that section's points. See `content/campaign-example.md`.

```bash
uv run python -m slides_cua plan --template acme --content content/my-deck.md
```

The planner matches on *meaning*, not position — a section called "Budget" finds the budget
matrix, "Who we are talking to" finds the persona slide. It emits slides in template order
wherever the narrative allows, which makes the build cheaper (no reordering).

The plan is printed and written to `runs/<timestamp>/plan.json`:

```json
{"position": 7, "template_slide": "s19", "role": "comparison",
 "fields": {"title": "Channel strategy", "subtitle": "Content + SEO", ...}}
```

**Read it.** Editing this JSON by hand is faster and more predictable than re-rolling the
planner — the field keys are already validated against the template, so you can safely
reword any value, drop a slide from the `slides` array, or reorder them.

`runs/` is gitignored and timestamped, so a plan left there is disposable. To keep one,
write it somewhere of your own choosing — `--plan` accepts any path:

```bash
uv run python -m slides_cua plan --template acme --content content/my-deck.md \
    --out my-deck.plan.json
```

## Step 5 — build

```bash
uv run python -m slides_cua build \
    --template acme \
    --template-url "https://docs.google.com/presentation/d/<id>/edit" \
    --plan my-deck.plan.json
```

The agent opens the template, does `File > Make a copy` — **your template is only ever
read** — then deletes the slides the plan does not use, duplicates any it reuses, fills
every field, and renames the deck. The copy lands in your Drive.

While it runs, PyAutoGUI controls your real mouse and keyboard. Keep the Chrome window
frontmost and hands off. Slam the pointer into a screen corner to abort.

Screenshots and a JSONL transcript land in `runs/<timestamp>/`.

### Turn budget

The default scales with the plan (`25 + 4 × slides`, minimum 40). That suits a deck of
ordinary slides. Override it when your plan is field-heavy:

```bash
--max-turns 90        # e.g. 10 slides / 105 fields, where one slide has 29 fields
```

Running out mid-deck leaves a half-filled copy to discard, so headroom is cheap.

---

# Command reference

```bash
# template
template ingest <file.pptx> [--pdf <file.pdf>] [--name N] [--label-model M]
                            [--no-label]      # structure only, no API calls
                            [--no-reconcile]  # skip the deck-level role review
template show <name>                          # print the catalog
template extract <file.pptx> [--json]         # raw structural facts, no LLM (debugging)

# plan
plan --template <name> --content <file.md> [--title T] [--out plan.json]

# build
build --template <name> --template-url <url> --plan <plan.json> [--max-turns N]
build --template <name> --template-url <url> --content <file.md>   # plan + build in one
build --content <file.md>                     # blank-deck mode, no template
build --dry-run                               # print the prompt, touch nothing
build --check                                 # verify desktop permissions
```

`build` is the default subcommand, so `uv run python -m slides_cua --content x.md` still
works.

Every stage takes `--model` (or `--label-model` for ingest), defaulting to
`SLIDES_CUA_MODEL` from `.env`.

## Blank-deck mode

Without `--template`, the agent starts from `slides.new` and builds by clicking. Layout
quality is whatever the model manages; template mode exists precisely to avoid that.

```bash
uv run python -m slides_cua --content content/example.md --title "Q3 Review"
```

---

# Layout

```
slides_cua/
├── __main__.py   CLI (build / plan / template), run directory, event printing
├── agent.py      Responses API loop + response validation
├── executor.py   PyAutoGUI namespace, log/display, output caps, input release
├── browser.py    opens Slides in your own Chrome profile
├── prompts.py    instructions and prompt construction, blank-deck and template modes
├── planner.py    content + template.json -> a reviewable deck plan
└── template/
    ├── extract.py  python-pptx -> layouts, fields, anchors, notes (no LLM, deterministic)
    ├── thumbs.py   pypdfium2 -> one PNG per slide
    ├── label.py    vision pass -> roles, purposes, capacity
    └── schema.py   the template.json data model and role vocabulary

templates/<name>/  template.json + thumbs/   (gitignored; ingest locally)
runs/<timestamp>/  screenshots, transcript.jsonl, plan.json  (gitignored, disposable)
```

# Troubleshooting

**The agent types character by character instead of pasting.** The clipboard did not settle
before ⌘V. `paste_text` waits for `pbpaste` to confirm, but under load the agent may fall
back to `pyautogui.write()`. Slow, not fatal.

**Clicks land in the wrong place.** The agent mis-measured the slide canvas. Every field
position derives from that one rectangle, so a bad measurement misses everything. Check the
`CANVAS = (...)` line in the transcript against the screenshots.

**A slide is mislabeled.** Labeling is not deterministic — re-run the ingest, or hand-edit
`template.json`. Structure (anchors, keys, boilerplate flags) *is* deterministic and will
not change between runs.

**`repeatable` looks wrong.** It is the least stable field in the catalog — it has swung
widely between models and runs. The planner treats it as a preference, not a hard gate, so
a wrong value degrades rather than blocks. Do not build logic on it.

**Wrong model being used.** `.env` is loaded before argparse resolves defaults. Precedence
is `--model` > shell environment > `.env` > built-in fallback.

# Known limits

- macOS only (`open -a`, `pbcopy`/`pbpaste`).
- No interruption of a hung `exec_py` call; the sample app used a subprocess with a 60s
  deadline for that.
- The agent fills text. It does not insert images, edit charts, or populate tables — those
  are left as the template has them.
- Rendering thumbnails needs the PDF export. Without `--pdf`, ingestion still works but
  labels from structure alone, which is rougher on visually-distinctive slides.
- This demonstrates CUA. For reliability at scale, the Slides API is still the right path.
