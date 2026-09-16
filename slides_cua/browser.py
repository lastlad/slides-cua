"""Open Google Slides in the user's own Chrome profile.

The whole point of the CUA approach here is riding the browser session you are
already signed into, so this deliberately uses the default profile instead of a
clean automation profile.
"""

from __future__ import annotations

import subprocess
import sys
import time

NEW_DECK_URL = "https://slides.new"


def open_url(url: str, settle_seconds: float = 6.0) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("This MVP opens Chrome via macOS `open`. Add a branch for your platform.")
    subprocess.run(["open", "-a", "Google Chrome", url], check=True)
    # Bring Chrome forward so PyAutoGUI acts on the right window.
    time.sleep(1.0)
    subprocess.run(["open", "-a", "Google Chrome"], check=True)
    time.sleep(settle_seconds)


def open_new_deck(url: str = NEW_DECK_URL, settle_seconds: float = 6.0) -> None:
    open_url(url, settle_seconds)


def open_template(url: str, settle_seconds: float = 8.0) -> None:
    """Open the template itself. The agent makes its own copy from here, so the
    original is only ever read."""
    open_url(url, settle_seconds)
