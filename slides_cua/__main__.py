"""CLI: build a Google Slides deck from a content file by driving the real browser."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .agent import run_code_loop
from .browser import open_new_deck
from .executor import Executor, FailsafeTriggered, check_desktop
from .prompts import INSTRUCTIONS, build_prompt

PROJECT_ROOT = Path(__file__).resolve().parents[1]

def load_config() -> None:
    local = PROJECT_ROOT / ".env"
    if local.exists():
        load_dotenv(local, override=False)

def make_run_dir() -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = PROJECT_ROOT / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(prog="slides-cua")
    parser.add_argument("--content", type=Path, default=PROJECT_ROOT / "content/example.md",
                        help="Markdown/text file describing the deck (default: content/example.md)")
    parser.add_argument("--title", default=None, help="Deck title (default: first heading in the content)")
    parser.add_argument("--model", default=os.environ.get("SLIDES_CUA_MODEL", "gpt-5.6-luna"))
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--check", action="store_true", help="Verify desktop permissions and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print the prompt and exit; no browser, no API calls")
    args = parser.parse_args()

    load_config()

    if args.check:
        import pyautogui

        width, height = check_desktop(pyautogui)
        print(f"desktop OK · {width}x{height} · platform {sys.platform}")
        return 0

    if not args.content.exists():
        parser.error(f"content file not found: {args.content}")
    content = args.content.read_text(encoding="utf-8")

    title = args.title
    if not title:
        headings = [line.lstrip("# ").strip() for line in content.splitlines() if line.startswith("# ")]
        title = headings[0] if headings else args.content.stem.replace("-", " ").title()

    prompt = build_prompt(title, content)
    if args.dry_run:
        print(INSTRUCTIONS)
        print("=" * 70)
        print(prompt)
        return 0

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set. Put it in .env or export it.", file=sys.stderr)
        return 1

    from openai import OpenAI

    run_dir = make_run_dir()
    transcript = (run_dir / "transcript.jsonl").open("w", encoding="utf-8")

    def on_event(kind: str, detail: str) -> None:
        transcript.write(json.dumps({"kind": kind, "detail": detail}) + "\n")
        transcript.flush()
        if kind == "turn":
            print(f"\n=== {detail} ===")
        elif kind == "code":
            print("--- exec_py ---")
            print(detail.strip()[:1200])
        elif kind == "model":
            print(f"[model] {detail.strip()[:600]}")
        else:
            print(f"[result] {detail.strip()[:400]}")

    print(f"run directory: {run_dir}")
    print(f"model: {args.model} · effort: {args.effort} · max turns: {args.max_turns}")
    print(f'deck title: "{title}"')
    print("\nOpening Google Slides. Keep the Chrome window frontmost and do not touch")
    print("the mouse or keyboard. Move the pointer to a screen corner to abort.\n")

    executor = Executor(run_dir)
    open_new_deck()

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    try:
        answer = run_code_loop(
            client=client,
            model=args.model,
            instructions=INSTRUCTIONS,
            prompt=prompt,
            executor=executor,
            max_turns=args.max_turns,
            reasoning_effort=args.effort,
            on_event=on_event,
        )
    except FailsafeTriggered:
        print("\nAborted: desktop fail-safe activated.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        executor.release_inputs()
        transcript.close()

    print("\n=== final answer ===")
    print(answer)
    print(f"\nScreenshots and transcript: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
