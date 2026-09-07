# slides-cua

A minimal computer-use agent that builds a Google Slides deck by driving your real
browser — no Google API, no OAuth. It rides the Chrome profile you are already
signed into.

Inspired by `openai-cua-sample-app`; the agent loop and the `log()`/`display()`
observation model are lifted from its Python app, with the server, web console,
and lab harness stripped out.

## Setup

```bash
uv sync
uv run python -m slides_cua --check     # verify macOS desktop permissions
```

Grant **Accessibility** and **Screen Recording** to the terminal app you launch
from (System Settings → Privacy & Security), then restart that app.

`OPENAI_API_KEY` is read from `.env`

## Run

```bash
uv run python -m slides_cua                          # uses content/example.md
uv run python -m slides_cua --content content/my.md --title "Q3 Review"
uv run python -m slides_cua --dry-run                # print the prompt, do nothing
```

While it runs, PyAutoGUI controls your real mouse and keyboard. Keep the Chrome
window frontmost and hands off. Slam the pointer into a screen corner to abort.

Screenshots and a JSONL transcript land in `runs/<timestamp>/`.

## Layout

```
slides_cua/
├── __main__.py   CLI, run directory, event printing
├── agent.py      Responses API loop + response validation
├── executor.py   PyAutoGUI namespace, log/display, output caps, input release
├── browser.py    opens slides.new in your own Chrome profile
└── prompts.py    instructions and prompt construction
```

## Known MVP limits

- macOS only (`open -a` and `pbcopy`).
- No interruption of a hung `exec_py` call; the sample app used a subprocess with a
  60s deadline for that.
- Layout quality is whatever the model manages by clicking. This demonstrates CUA;
  the Slides API would be the reliable path.
