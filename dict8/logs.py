"""Where Dict8's own warnings go.

Before this (U4, 2026-09-19) the `dict8` package logged through stdlib `logging` with no
handler configured at all, so every warning fell through to the handler of last resort and
landed on **stderr**. Two things were wrong with that. Running `dict8 classify-backfill`
over 40 turns printed a wall of per-call timeout lines on top of the summary it prints on
purpose, and none of it survived the run — `paths.logs` existed in config.yml and nothing
but the hook ever wrote there, so there was no record to go back to when a number looked
off the next day.

The split this module sets up:

* **file** — `paths.logs/dict8.log`, everything from WARNING up, one line each. Durable,
  greppable, and the thing to read after the fact.
* **stderr** — ERROR and up only. The CLI's own human-readable summaries are `print()`
  calls and are untouched; what this removes from stderr is the per-call noise underneath
  them, which was never the summary and always drowned it.

**`paths.logs/hooks.log` is a different file and stays that way.** `dict8.hooks.log()`
opens `O_NONBLOCK`, writes once and closes, because a hook sits in front of Denis's prompt
and a FIFO at that path once hung it for 2 min 17 s. `logging.FileHandler` makes none of
those promises — it opens blocking, keeps the handle, and locks. Routing the hook through
here would trade a measured fix for a tidier import graph.

**No prompt text is ever logged**, here or by any caller. `privacy.store_transcripts:
features_only` is not only about the database (see `dict8.hooks`' docstring for the same
rule one level down). This module cannot enforce that — it only formats what it is given —
so it is stated at every call site instead.

Failure here is never fatal. An unwritable or unresolvable `paths.logs` costs the file
handler and nothing else: `setup()` returns None, stderr keeps working, and the command
runs. A logging setup that can abort a command is worse than no logging setup.
"""

from __future__ import annotations

import logging
from pathlib import Path

# The fixed artifact name inside the directory `paths.logs` names — the same convention as
# `dict8.hooks.LOG_FILENAME`: config decides *where* logs live, not what they are called,
# so "read dict8.log" stays a true answer on every machine.
LOG_FILENAME = "dict8.log"

# The package logger every module under `dict8.` already writes to by using
# `logging.getLogger(__name__)`. Configuring this one and not the root leaves other
# libraries' logging exactly as it was.
ROOT_LOGGER = "dict8"

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

_configured = False


def log_path(cfg) -> Path:
    """`paths.logs`/`dict8.log`. Does not create anything — the handler does, lazily."""
    return cfg.path("paths.logs") / LOG_FILENAME


def setup(cfg, *, force: bool = False) -> Path | None:
    """Attach the file and stderr handlers to the `dict8` logger. Idempotent.

    Returns the log file's path, or None if no file handler could be attached (and the
    reason is then reported through the stderr handler, which is attached first precisely
    so that it exists to report with).

    The file handler is opened with `delay=True`: the file is created on the first record
    actually written, not on every `dict8 usage` that has nothing to say. A directory that
    does not exist yet is created here, because `delay=True` defers the open, not the
    directory check, and a first warning is the wrong moment to discover the parent is
    missing.
    """
    global _configured
    logger = logging.getLogger(ROOT_LOGGER)
    if _configured and not force:
        return getattr(logger, "_dict8_log_path", None)

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    logger.setLevel(logging.WARNING)
    # Do not also hand these to the root logger: with no root handler configured that is
    # the path that put them on stderr in the first place.
    logger.propagate = False

    formatter = logging.Formatter(_FORMAT)

    stderr = logging.StreamHandler()
    stderr.setLevel(logging.ERROR)
    stderr.setFormatter(formatter)
    logger.addHandler(stderr)

    path: Path | None = None
    try:
        path = log_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8", delay=True)
        handler.setLevel(logging.WARNING)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except Exception as exc:
        # Reported, not raised: a broken log destination must not end the command that was
        # only trying to say something in passing.
        logger.error("could not open the log file (%r) — warnings go to stderr only", exc)
        path = None

    logger._dict8_log_path = path  # type: ignore[attr-defined]
    _configured = True
    return path
