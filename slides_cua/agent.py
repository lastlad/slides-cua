"""Responses API code loop.

Same shape as openai-cua-sample-app/python-app/app/responses_loop.py: validate the
whole response before running any of it, send every tool result back keyed by
call_id, thread turns with previous_response_id, and fail loudly if the turn
budget runs out without a final answer.
"""

from __future__ import annotations

import json
from typing import Any, Callable

MAX_CODE_BYTES = 64 * 1024


class ModelResponseError(RuntimeError):
    pass


def build_tool_definitions(platform: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "exec_py",
            "strict": True,
            "description": "Execute Python in the persistent PyAutoGUI desktop session for this run.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code"],
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "\n".join(
                            [
                                f"Python to execute on the local {platform} desktop. Globals survive between calls.",
                                "Available globals: pyautogui, screenshot(), paste_text(str), sleep(seconds),",
                                "log(value) for text output, and display(image) for a Pillow image.",
                                "Only log() and display() are visible to you; plain return values are not.",
                                "Screenshots share the coordinate system used by clicks, including on Retina displays.",
                                "All coordinates refer to the full desktop screenshot.",
                                "Do not change the PyAutoGUI fail-safe setting.",
                            ]
                        ),
                    }
                },
            },
        }
    ]


def classify_response(response: dict[str, Any]) -> dict[str, Any]:
    """Validate *all* output items before any model code is allowed to run."""
    if not isinstance(response, dict):
        raise ModelResponseError("Malformed Responses API response.")
    if response.get("status") != "completed" or response.get("error"):
        error = response.get("error")
        message = error.get("message", "") if isinstance(error, dict) else ""
        raise ModelResponseError(
            f"Response did not complete (status: {response.get('status', 'missing')}). {message}".strip()
        )
    if not isinstance(response.get("id"), str) or not response["id"].strip():
        raise ModelResponseError("Response ID is missing.")
    if not isinstance(response.get("output"), list):
        raise ModelResponseError("Response output is missing.")

    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    commentary: list[str] = []
    final: list[str] = []
    refusal: str | None = None

    for item in response["output"]:
        if not isinstance(item, dict):
            raise ModelResponseError("Malformed response output item.")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") == "function_call":
            if item.get("name") != "exec_py":
                raise ModelResponseError(f"Unexpected function call: {item.get('name')!r}.")
            if "status" in item and item["status"] != "completed":
                raise ModelResponseError("Response contains an unfinished function call.")
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ModelResponseError("Function call ID is missing.")
            if call_id in seen:
                raise ModelResponseError(f"Duplicate function call ID: {call_id}.")
            if not isinstance(item.get("arguments"), str):
                raise ModelResponseError("Function call arguments must be a JSON string.")
            try:
                args = json.loads(item["arguments"])
            except ValueError as error:
                raise ModelResponseError("Function call arguments are not valid JSON.") from error
            if not isinstance(args, dict) or set(args) != {"code"} or not isinstance(args.get("code"), str):
                raise ModelResponseError("exec_py requires a code string and no other arguments.")
            if not args["code"].strip() or len(args["code"].encode()) > MAX_CODE_BYTES:
                raise ModelResponseError("Python code must be nonempty and at most 64 KiB.")
            seen.add(call_id)
            calls.append({"call_id": call_id, "code": args["code"]})
            continue
        if item.get("type") != "message":
            raise ModelResponseError(f"Unsupported response output: {item.get('type')}.")
        if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
            raise ModelResponseError("Response contains an invalid assistant message.")
        for part in item["content"]:
            if not isinstance(part, dict):
                raise ModelResponseError("Malformed assistant message content.")
            if part.get("type") == "refusal":
                text = part.get("refusal")
                refusal = text.strip() if isinstance(text, str) and text.strip() else "The model declined."
                continue
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise ModelResponseError("Unsupported assistant message content.")
            text = part["text"].strip()
            if text:
                (commentary if item.get("phase") == "commentary" else final).append(text)

    if refusal:
        raise ModelResponseError(refusal)
    progress = "\n\n".join(commentary)
    if calls:
        return {"kind": "calls", "calls": calls, "commentary": progress}
    if final:
        return {"kind": "final", "text": "\n\n".join(final), "commentary": progress}
    if commentary:
        return {"kind": "commentary", "commentary": progress}
    raise ModelResponseError("Response contained no tool calls and no final message.")


def run_code_loop(
    *,
    client: Any,
    model: str,
    instructions: str,
    prompt: str,
    executor: Any,
    max_turns: int,
    reasoning_effort: str,
    on_event: Callable[[str, str], None],
) -> str:
    previous_response_id: str | None = None
    next_input: Any = prompt.strip()

    for turn in range(1, max_turns + 1):
        request: dict[str, Any] = {
            "instructions": instructions,
            "input": next_input,
            "model": model,
            "parallel_tool_calls": False,
            "reasoning": {"effort": reasoning_effort},
            "truncation": "auto",
            "tools": build_tool_definitions(executor.platform),
        }
        if previous_response_id is not None:
            request["previous_response_id"] = previous_response_id

        response = client.responses.create(**request).model_dump(exclude_none=True)
        classified = classify_response(response)
        previous_response_id = response["id"]

        usage = response.get("usage") or {}
        on_event(
            "turn",
            f"turn {turn}/{max_turns} · {usage.get('input_tokens', 0)} in · {usage.get('output_tokens', 0)} out",
        )
        if classified["commentary"]:
            on_event("model", classified["commentary"])

        if classified["kind"] == "final":
            return classified["text"]
        if classified["kind"] == "commentary":
            next_input = []
            continue

        tool_outputs = []
        for call in classified["calls"]:
            on_event("code", call["code"])
            output = executor.execute(call["code"])
            for item in output:
                if item["type"] == "input_text":
                    on_event("result", item["text"])
                else:
                    on_event("result", "[screenshot]")
            tool_outputs.append(
                {"call_id": call["call_id"], "output": output, "type": "function_call_output"}
            )
        next_input = tool_outputs

    raise RuntimeError(
        f"Exhausted the {max_turns}-turn budget without a final answer. "
        "Raise --max-turns or simplify the content."
    )
