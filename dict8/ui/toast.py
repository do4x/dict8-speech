"""Toasts — the visible half of invariant 7b ("never a silent no-op").

MVP delivery is `osascript -e 'display notification ...'`: no extra permission, no bundle
needed while Dict8 runs from a terminal. The text is passed as argv to an `on run` handler,
never spliced into the AppleScript source, so a quote in a message cannot break (or inject
into) the script. Launched without waiting: a toast must never sit on the dictation path.

Every toast is also written to `dict8.log` at WARNING (the durable record) and handed to the
menu-bar item's "Last message" line when one is registered. **No transcript text is ever
passed here** — messages name what went wrong, not what was said.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Callable

log = logging.getLogger(__name__)

_SCRIPT = ["-e", "on run argv", "-e",
           "display notification (item 2 of argv) with title (item 1 of argv)",
           "-e", "end run"]

# Extra sinks (the tray's "Last message" line, the window's notice line). Called with (title, body).
_listeners: list[Callable[[str, str], None]] = []

# False: log + listeners only, no Notification Center banner (`dict8 app --no-notify`).
notify = True


def add_listener(fn: Callable[[str, str], None]) -> None:
    _listeners.append(fn)


def toast(title: str, body: str) -> None:
    log.warning("toast: %s — %s", title, body)
    for fn in list(_listeners):
        try:
            fn(title, body)
        except Exception:
            pass
    if not notify:
        return
    try:
        subprocess.Popen(["osascript", *_SCRIPT, title, body],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as exc:
        log.error("toast: osascript failed (%s)", exc)
