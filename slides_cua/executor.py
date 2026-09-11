"""Persistent PyAutoGUI execution namespace.

Lifted from the openai-cua-sample-app worker: log()/display() are the only
observation channels, output is capped, and tracebacks go back to the model so
it can correct itself instead of the run dying.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import io
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from PIL import Image

TEXT_LIMIT = 60 * 1024
IMAGE_LIMIT = 8 * 1024 * 1024
ITEM_LIMIT = 250


class FailsafeTriggered(RuntimeError):
    """The user moved the pointer into a fail-safe corner."""


def check_desktop(pyautogui: Any) -> tuple[int, int]:
    width, height = pyautogui.size()
    if width <= 0 or height <= 0:
        raise RuntimeError("No desktop available. Run inside a graphical session.")
    if sys.platform == "darwin":
        services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        services.AXIsProcessTrusted.restype = ctypes.c_bool
        if not services.AXIsProcessTrusted():
            raise RuntimeError(
                "macOS Accessibility permission is required. Enable the app launching this "
                "(Terminal / iTerm / VS Code) in System Settings > Privacy & Security > "
                "Accessibility, then restart that app."
            )
        import Quartz

        if not Quartz.CGPreflightScreenCaptureAccess():
            raise RuntimeError(
                "macOS Screen Recording permission is required. Enable the launching app in "
                "System Settings > Privacy & Security > Screen Recording, then restart it."
            )
    return width, height


def normalize_screenshots(pyautogui: Any) -> None:
    """Retina captures use physical pixels while input uses logical points."""
    capture = pyautogui.screenshot

    def screenshot(imageFilename=None, region=None, **kwargs):  # noqa: N803 - match PyAutoGUI
        image = capture(**kwargs)
        size = tuple(pyautogui.size())
        if image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        if region is not None:
            x, y, width, height = region
            image = image.crop((x, y, x + width, y + height))
        if imageFilename is not None:
            image.save(imageFilename)
        return image

    pyautogui.screenshot = screenshot


def _set_clipboard(text: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        return
    raise RuntimeError("paste_text currently supports macOS only.")


def _read_clipboard() -> str | None:
    try:
        return subprocess.run(["pbpaste"], capture_output=True, check=True).stdout.decode("utf-8")
    except Exception:  # noqa: BLE001 - a failed read just means "not settled yet"
        return None


def _await_clipboard(text: str, timeout: float = 2.0) -> None:
    """Block until the pasteboard actually holds `text`.

    pbcopy exits before the pasteboard is necessarily visible to other processes. Pressing
    command+v too early pastes stale or partial content, which on a Slides canvas looks
    like a half-typed field and sends the agent into a retry spiral.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _read_clipboard() == text:
            return
        time.sleep(0.05)
    raise RuntimeError("Clipboard did not settle; refusing to paste stale text.")


class Executor:
    """Runs model Python against the real desktop, keeping globals between calls."""

    def __init__(self, run_dir: Path) -> None:
        import pyautogui

        check_desktop(pyautogui)
        normalize_screenshots(pyautogui)
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.15

        self.pyautogui = pyautogui
        self.platform = sys.platform
        self.run_dir = run_dir
        self.shot_index = 0
        self._input_may_be_held = False

        def paste_text(text: str) -> None:
            """Clipboard paste. Far faster and more reliable than typing."""
            value = str(text)
            _set_clipboard(value)
            _await_clipboard(value)
            # A modifier still logically held from an earlier hotkey turns command+v into
            # a bare "v", typing a stray character instead of pasting.
            for key in ("command", "shift", "option", "ctrl"):
                with contextlib.suppress(Exception):
                    pyautogui.keyUp(key)
            time.sleep(0.1)
            pyautogui.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")

        self.namespace: dict[str, Any] = {
            "__builtins__": __builtins__,
            "pyautogui": pyautogui,
            "screenshot": pyautogui.screenshot,
            "paste_text": paste_text,
            "sleep": time.sleep,
        }

    def _save(self, png: bytes) -> None:
        self.shot_index += 1
        (self.run_dir / f"{self.shot_index:03d}.png").write_bytes(png)

    def execute(self, code: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        text_bytes = 0
        image_bytes = 0

        def log(*values: Any) -> None:
            nonlocal text_bytes
            text = " ".join(str(value) for value in values)
            text_bytes += len(text.encode("utf-8"))
            if text_bytes > TEXT_LIMIT or len(output) >= ITEM_LIMIT:
                raise ValueError("Text output exceeds its size limit.")
            output.append({"type": "input_text", "text": text})

        def display(value: Any) -> None:
            nonlocal image_bytes
            if isinstance(value, Image.Image):
                buffer = io.BytesIO()
                value.save(buffer, format="PNG")
                value = buffer.getvalue()
            if not isinstance(value, bytes) or not value.startswith(b"\x89PNG\r\n\x1a\n"):
                raise TypeError("display expects a Pillow image or PNG bytes.")
            image_bytes += len(value)
            if image_bytes > IMAGE_LIMIT or len(output) >= ITEM_LIMIT:
                raise ValueError("Image output exceeds its size limit.")
            self._save(value)
            output.append(
                {
                    "type": "input_image",
                    "detail": "original",
                    "image_url": "data:image/png;base64," + base64.b64encode(value).decode("ascii"),
                }
            )

        class Writer:
            def write(self, text: str) -> int:
                if text.strip():
                    log(text)
                return len(text)

            def flush(self) -> None:
                pass

        self.namespace.update(log=log, display=display)
        self._input_may_be_held = True
        try:
            with contextlib.redirect_stdout(Writer()), contextlib.redirect_stderr(Writer()):
                exec(compile(code, "<exec_py>", "exec"), self.namespace)  # noqa: S102 - deliberate
        except self.pyautogui.FailSafeException as error:
            raise FailsafeTriggered("Desktop fail-safe activated.") from error
        except BaseException:  # noqa: BLE001 - report model errors, keep the session alive
            output.append({"type": "input_text", "text": traceback.format_exc()[-4000:]})
        return output or [{"type": "input_text", "text": "exec_py completed with no output."}]

    def release_inputs(self) -> None:
        """Best effort release so a crash cannot leave keys or buttons held."""
        if not self._input_may_be_held:
            return
        for key in ("command", "ctrl", "alt", "shift", "option", "fn"):
            with contextlib.suppress(Exception):
                self.pyautogui.keyUp(key)
        for button in ("left", "middle", "right"):
            with contextlib.suppress(Exception):
                self.pyautogui.mouseUp(button=button)
        self._input_may_be_held = False
