"""Claude Code hook handlers — and the fail-open envelope they all run inside.

**Invariant 8 is the whole design.** Dict8's hooks sit in front of Denis's daily driver.
A hook that errors, hangs, finds no database or hits a config gap lets the prompt through
untouched and writes one line saying why. There is exactly one way out of every handler
in this package: exit code 0 with nothing on stderr. Not a single path returns 2 (which
is how a Claude Code hook BLOCKS a prompt), and none of them writes a diagnostic to
stderr, because stderr on a hook is shown to the user — a stack trace in front of every
prompt is a worse product than a missing estimate.

**The watchdog is self-enforced.** Claude Code has its own per-hook `timeout`, and it is
configured as a second belt in `.claude/settings.json`, but it is measured by the parent
process and covers the whole `uv run` — so it cannot distinguish "uv took a while" from
"our sqlite read blocked on a locked WAL". `hooks.timeout_ms` is ours, enforced from
inside, and it is enforced by a THREAD that calls `os._exit(0)`, not by `signal.alarm`:
a signal is only delivered at a bytecode boundary, so a blocking `sqlite3` C call — the
one slow thing this package does — would ignore it for exactly as long as it was going to
block anyway. `os._exit` skips interpreter shutdown, which is the point: nothing that was
half-written can flush to stdout behind the timeout's back.

**Nothing here writes prompt text.** `paths.logs/hooks.log` carries counts, ids,
durations and reasons. `privacy.store_transcripts: features_only` is not only about the
database.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Wall-clock origin for the self-enforced budget. Module import is the earliest moment
# in-process code can observe; the interpreter start and `uv`'s own resolution happen
# before any Dict8 code runs and are covered by the second belt (the per-hook `timeout`
# in .claude/settings.json), never by `hooks.timeout_ms`.
_T0 = time.monotonic()

# The log file's name inside the directory `paths.logs` names. Not a path and not a
# threshold — the decision (where logs live) is config's; this is the fixed artifact name
# within it, the same way `dict8.sqlite` is fixed inside `paths.db`'s directory. Making it
# configurable would mean a support question could not be answered with "read hooks.log".
LOG_FILENAME = "hooks.log"

# Claude Code's JSON contract for a UserPromptSubmit hook that wants to add context.
# OBSERVED, not remembered: docs/verified-schemas.md section 8 records the payload we read
# and the round-trip that proved this shape reaches the model as context.
HOOK_OUTPUT_KEY = "hookSpecificOutput"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def elapsed_ms() -> float:
    """Milliseconds since this package was imported — what `hooks.timeout_ms` bounds."""
    return (time.monotonic() - _T0) * 1000.0


# ---- logging ---------------------------------------------------------------------


def log_path(cfg) -> Path:
    """`paths.logs`/`hooks.log`, with the directory created if it does not exist."""
    directory = cfg.path("paths.logs")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / LOG_FILENAME


def log(cfg, **fields: Any) -> None:
    """Append one JSON line. Never raises and **never blocks**.

    Opened `O_NONBLOCK`, written with a single `os.write`, closed. The nonblocking flag is
    not decoration: `paths.logs/hooks.log` is a path a user controls, and if it is a FIFO
    — or anything else with no reader — a plain `open(..., "a")` blocks in the kernel
    forever. Measured before this fix: a FIFO there hung the hook for 2 min 17 s, with the
    watchdog powerless because the hang was in the log call that reports a timeout. Now
    the open fails ENXIO (no reader) or the write fails EAGAIN (reader not draining) and
    the line is dropped, which is the correct trade: invariant 8 says the prompt goes
    through, and a diagnostic is never worth a stall in front of it.

    `O_NONBLOCK` is a no-op for a regular file on a local disk, which is the normal case —
    it costs nothing there and saves the pathological one.

    No prompt text is ever a field here. Callers pass counts and reasons; if a future
    caller passes text, that is the bug, not this function's silence.
    """
    fd = None
    try:
        line = json.dumps({"ts": now_iso(), **fields}, ensure_ascii=False, default=str)
        fd = os.open(log_path(cfg),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK, 0o644)
        os.write(fd, (line + "\n").encode("utf-8"))
    except Exception:
        # Deliberately silent. stderr is shown to the user in front of their prompt, and
        # an unwritable log directory is not their problem to solve mid-sentence.
        pass
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass


# ---- the envelope ----------------------------------------------------------------


class Watchdog:
    """Self-enforced `hooks.timeout_ms`. On expiry: one log line, then exit 0.

    It stays armed across the handler's own stdout write, not just the work before it.
    An expiry mid-write can truncate the JSON, and that is still the better outcome: the
    only way to be mid-write at the deadline is for the consumer to have stopped reading,
    and then the alternative is not clean output — it is stalling until Claude Code's own
    per-hook `timeout` kills the process, several seconds later, mid-write anyway."""

    def __init__(self, budget_ms: float, on_expire: Callable[[float], None]) -> None:
        self.budget_ms = float(budget_ms)
        self._on_expire = on_expire
        self._timer: threading.Timer | None = None

    def __enter__(self) -> "Watchdog":
        remaining_s = max(self.budget_ms - elapsed_ms(), 0.0) / 1000.0
        self._timer = threading.Timer(remaining_s, self._fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, *exc) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def _fire(self) -> None:
        try:
            self._on_expire(elapsed_ms())
        finally:
            # No flush. If stdout is what stalled, flushing here would park the watchdog
            # thread on the same blocked pipe and nothing would ever exit — the timeout
            # would be enforced by the thing it exists to escape. Discarding the buffer is
            # also the documented outcome of an overrun: exit 0, empty stdout.
            os._exit(0)


def read_payload(raw: str) -> dict:
    """The hook payload as a dict. A blank or malformed stdin yields `{}` rather than an
    exception: every field is optional at the parser (docs/verified-schemas.md section 8),
    so a handler given nothing simply has nothing to say."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def additional_context(event: str, context: str) -> str:
    """The JSON a hook prints to hand Claude Code extra context.

    Invariant 1: this is context delivered ALONGSIDE the prompt. It is never merged into
    the prompt text, and no handler in this package rewrites, trims or re-orders a single
    word the user typed — it does not even keep a copy.
    """
    return json.dumps({
        HOOK_OUTPUT_KEY: {"hookEventName": event, "additionalContext": context}
    }, ensure_ascii=False)
