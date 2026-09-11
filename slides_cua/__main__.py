"""CLI: build a Google Slides deck by driving the real browser.

Three subcommands, matching the three stages:
  template ingest   one-time: .pptx (+ .pdf) -> templates/<name>/template.json
  plan              content + template.json  -> plan.json
  build             plan.json                -> the deck, via the CUA loop

`build` is the default, so the original bare invocation still works.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .agent import run_code_loop
from .browser import open_new_deck, open_template
from .executor import Executor, FailsafeTriggered, check_desktop
from .planner import Plan, plan_deck
from .prompts import INSTRUCTIONS, TEMPLATE_INSTRUCTIONS, build_plan_prompt, build_prompt
from .template import Template, ingest, load

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_ROOT = PROJECT_ROOT / "templates"
SUBCOMMANDS = {"build", "plan", "template"}
DEFAULT_MODEL = "gpt-5.6-luna"


def load_config() -> None:
    local = PROJECT_ROOT / ".env"
    if local.exists():
        load_dotenv(local, override=False)


def make_run_dir() -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = PROJECT_ROOT / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def make_client():
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set. Put it in .env or export it.")
    from openai import OpenAI

    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def derive_title(content: str, fallback: Path) -> str:
    headings = [line.lstrip("# ").strip() for line in content.splitlines() if line.startswith("# ")]
    return headings[0] if headings else fallback.stem.replace("-", " ").title()


def echo(message: str) -> None:
    print(message)


# --------------------------------------------------------------------------- template


def cmd_template_ingest(args: argparse.Namespace) -> int:
    pptx_path: Path = args.pptx
    if not pptx_path.exists():
        raise SystemExit(f"pptx not found: {pptx_path}")

    name = args.name or pptx_path.parent.name
    if name in {".", "", "templates"}:
        name = pptx_path.stem
    out_dir = TEMPLATES_ROOT / name

    pdf_path: Path | None = args.pdf
    if pdf_path is None:
        sibling = pptx_path.with_suffix(".pdf")
        pdf_path = sibling if sibling.exists() else None
        if pdf_path is not None:
            echo(f"using {pdf_path.name} for thumbnails")
    if pdf_path is not None and not pdf_path.exists():
        raise SystemExit(f"pdf not found: {pdf_path}")

    client = None if args.no_label else make_client()
    ingest(
        pptx_path,
        out_dir,
        name=name,
        pdf_path=pdf_path,
        client=client,
        model=args.label_model,
        reconcile=not args.no_reconcile,
        on_event=echo,
    )
    return 0


def cmd_template_show(args: argparse.Namespace) -> int:
    template = load(TEMPLATES_ROOT, args.name)
    print(f'{template.name} · {len(template.slides)} slides · aspect {template.aspect:.2f}')
    if template.theme.get("fonts"):
        print(f"theme fonts: {template.theme['fonts']}")
    for slide in template.slides:
        flag = " (repeatable)" if slide.repeatable else ""
        print(f"\n{slide.id}  position {slide.index}  role={slide.role}{flag}")
        print(f"  layout:  {slide.layout_name}")
        if slide.purpose:
            print(f"  purpose: {slide.purpose}")
        if slide.keywords:
            print(f"  keywords: {', '.join(slide.keywords)}")
        if slide.capacity:
            print("  capacity: " + ", ".join(f"{k}={v}" for k, v in sorted(slide.capacity.items())))
        for field in slide.fields:
            x, y = field.center()
            label = f" — {field.label}" if field.label else ""
            print(f"    {field.key:14s} {field.kind:8s} at ({x:.2f}, {y:.2f}){label}")
    return 0


def cmd_template_extract(args: argparse.Namespace) -> int:
    """Structure only, no LLM and no files written. The debugging entry point."""
    from .template.extract import extract as run_extract
    from .template.label import describe

    template = run_extract(args.pptx, args.pptx.stem)
    if args.json:
        print(json.dumps(template.to_dict(), indent=2, ensure_ascii=False))
        return 0
    print(f"{len(template.slides)} slides · {len(template.layouts)} layouts · theme {template.theme}")
    for slide in template.slides:
        print()
        print(describe(slide))
    return 0


# ------------------------------------------------------------------------------ plan


def resolve_content(args: argparse.Namespace) -> tuple[str, str]:
    if not args.content.exists():
        raise SystemExit(f"content file not found: {args.content}")
    content = args.content.read_text(encoding="utf-8")
    return content, args.title or derive_title(content, args.content)


def cmd_plan(args: argparse.Namespace) -> int:
    template = load(TEMPLATES_ROOT, args.template)
    content, title = resolve_content(args)
    plan = plan_deck(
        template,
        content,
        client=make_client(),
        model=args.model,
        title=title,
        on_event=echo,
    )
    out = args.out or make_run_dir() / "plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    plan.write(out, template)
    print(json.dumps(plan.to_dict(template), indent=2, ensure_ascii=False))
    print(f"\nplan written to {out}")
    return 0


# ----------------------------------------------------------------------------- build


def cmd_build(args: argparse.Namespace) -> int:
    if args.check:
        import pyautogui

        width, height = check_desktop(pyautogui)
        print(f"desktop OK · {width}x{height} · platform {sys.platform}")
        return 0

    template: Template | None = None
    plan: Plan | None = None
    content = title = ""

    if args.template:
        template = load(TEMPLATES_ROOT, args.template)
        if not args.template_url and not args.dry_run:
            raise SystemExit("--template-url is required when building from a template.")

    if args.plan:
        if template is None:
            raise SystemExit("--plan requires --template.")
        plan = Plan.read(args.plan)
        title = plan.title
    else:
        content, title = resolve_content(args)

    if template is not None and plan is None:
        # Plan and build in one shot. The plan still lands on disk for inspection.
        plan = plan_deck(
            template, content, client=make_client(), model=args.model, title=title, on_event=echo
        )

    if template is not None and plan is not None:
        instructions = TEMPLATE_INSTRUCTIONS
        prompt = build_plan_prompt(plan, template)
    else:
        instructions = INSTRUCTIONS
        prompt = build_prompt(title, content)

    if args.dry_run:
        print(instructions)
        print("=" * 70)
        print(prompt)
        return 0

    client = make_client()
    run_dir = make_run_dir()
    if plan is not None and template is not None:
        plan.write(run_dir / "plan.json", template)

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

    # A template build has a fixed setup cost (copy, prune, measure) plus per-slide work,
    # so a flat budget that suits a blank deck starves a 10-slide plan.
    max_turns = args.max_turns
    if max_turns is None:
        max_turns = 40 if plan is None else max(40, 25 + 4 * len(plan.slides))

    print(f"run directory: {run_dir}")
    print(f"model: {args.model} · effort: {args.effort} · max turns: {max_turns}")
    print(f'deck title: "{title}"')
    if template is not None:
        print(f"template: {template.name} · {len(plan.slides)} slides planned")
    print("\nOpening Google Slides. Keep the Chrome window frontmost and do not touch")
    print("the mouse or keyboard. Move the pointer to a screen corner to abort.\n")

    executor = Executor(run_dir)
    if template is not None:
        open_template(args.template_url)
    else:
        open_new_deck()

    try:
        answer = run_code_loop(
            client=client,
            model=args.model,
            instructions=instructions,
            prompt=prompt,
            executor=executor,
            max_turns=max_turns,
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


# ------------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    # Argparse resolves defaults when the parser is built, so .env must already be loaded
    # by now or SLIDES_CUA_MODEL silently loses to the fallback. main() handles that.
    parser = argparse.ArgumentParser(prog="slides-cua")
    subparsers = parser.add_subparsers(dest="command")

    default_model = os.environ.get("SLIDES_CUA_MODEL", DEFAULT_MODEL)

    build = subparsers.add_parser("build", help="Build a deck (default command)")
    build.add_argument("--content", type=Path, default=PROJECT_ROOT / "content/example.md",
                       help="Markdown/text file describing the deck")
    build.add_argument("--title", default=None, help="Deck title (default: first heading in the content)")
    build.add_argument("--template", default=None, help="Name of an ingested template")
    build.add_argument("--template-url", default=None, help="Google Slides URL of that template")
    build.add_argument("--plan", type=Path, default=None, help="Prebuilt plan.json to execute")
    build.add_argument("--model", default=default_model)
    build.add_argument("--max-turns", type=int, default=None,
                       help="Turn budget (default: 40 blank, or scaled to the plan's slide count)")
    build.add_argument("--effort", default="medium", choices=["low", "medium", "high"])
    build.add_argument("--check", action="store_true", help="Verify desktop permissions and exit")
    build.add_argument("--dry-run", action="store_true", help="Print the prompt and exit")
    build.set_defaults(func=cmd_build)

    plan = subparsers.add_parser("plan", help="Map content onto template slides")
    plan.add_argument("--template", required=True, help="Name of an ingested template")
    plan.add_argument("--content", type=Path, default=PROJECT_ROOT / "content/example.md")
    plan.add_argument("--title", default=None)
    plan.add_argument("--model", default=default_model)
    plan.add_argument("--out", type=Path, default=None, help="Where to write plan.json")
    plan.set_defaults(func=cmd_plan)

    template = subparsers.add_parser("template", help="Ingest and inspect templates")
    template_sub = template.add_subparsers(dest="template_command", required=True)

    ingest_parser = template_sub.add_parser("ingest", help="Build template.json from a .pptx export")
    ingest_parser.add_argument("pptx", type=Path)
    ingest_parser.add_argument("--pdf", type=Path, default=None, help="PDF export for thumbnails")
    ingest_parser.add_argument("--name", default=None, help="Template name (default: parent folder)")
    ingest_parser.add_argument("--label-model", default=default_model)
    ingest_parser.add_argument("--no-label", action="store_true", help="Structure only, no LLM pass")
    ingest_parser.add_argument("--no-reconcile", action="store_true", help="Skip the deck-level review")
    ingest_parser.set_defaults(func=cmd_template_ingest)

    show = template_sub.add_parser("show", help="Print an ingested template's catalog")
    show.add_argument("name")
    show.set_defaults(func=cmd_template_show)

    extract_parser = template_sub.add_parser("extract", help="Dump raw structure from a .pptx, no LLM")
    extract_parser.add_argument("pptx", type=Path)
    extract_parser.add_argument("--json", action="store_true")
    extract_parser.set_defaults(func=cmd_template_extract)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Keep the original bare invocation working: anything that is not a subcommand is
    # treated as `build` arguments.
    if not argv or (argv[0] not in SUBCOMMANDS and argv[0] not in {"-h", "--help"}):
        argv.insert(0, "build")

    load_config()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
