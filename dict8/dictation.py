"""One dictation, after the audio exists: STT -> frontmost check -> inject -> latency row.

Shared by `dict8 app` (audio from the mic) and `dict8 dictate --file` (audio from a WAV), so
the headless path exercises exactly the code the hotkey path runs.

The latency log is `paths.logs/dictations.jsonl`, one JSON object per dictation. **Numbers
and fixed labels only** — `ROW_KEYS` is the whole schema and `record()` refuses anything
else, so no transcript text can reach disk through here (privacy.store_transcripts:
features_only). `scripts/bench_latency.py` reads it.

Frontmost-app policy (ADR-002): the app frontmost at PRESS is recorded; if a different app
is frontmost when the text is ready, nothing is typed — the transcript goes to the
pasteboard and a toast says why. Typing into an app the user switched to mid-dictation is
the one outcome worse than making them paste.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from dict8.inject import PATH_FAILED, PATH_NONE, InjectResult, Injector, prepare

log = logging.getLogger(__name__)

LOG_FILENAME = "dictations.jsonl"

# The complete row schema. Strings are fixed vocabularies, everything else is a number.
ROW_KEYS = {
    "ts": str, "source": str, "outcome": str, "path": str,
    "audio_ms": float, "stt_ms": float, "inject_ms": float, "release_to_text_ms": float,
    "chars": int, "frontmost_changed": bool,
}
SOURCES = {"hotkey", "file"}
OUTCOMES = {"injected", "transcribed_only", "discarded_short", "cancelled", "empty", "silent",
            "error"}


def log_path(cfg):
    return cfg.path("paths.logs") / LOG_FILENAME


def record(cfg, row: dict) -> None:
    """Append one row. Never raises — a dictation must not fail over its own log line."""
    try:
        clean: dict = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
        for k, v in row.items():
            if k not in ROW_KEYS:
                raise ValueError(f"latency row key {k!r} is not in the schema")
            if v is None:
                continue
            want = ROW_KEYS[k]
            if want is float:
                v = round(float(v), 1)
            elif not isinstance(v, want):
                raise TypeError(f"latency row {k!r} must be {want.__name__}")
            clean[k] = v
        if clean.get("source") not in SOURCES or clean.get("outcome") not in OUTCOMES:
            raise ValueError("latency row needs a known source and outcome")
        p = log_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(clean) + "\n")
    except Exception as exc:
        log.warning("dictation log: row not written (%s)", exc)


@dataclass
class Outcome:
    text: str
    stt_ms: float
    result: InjectResult
    release_to_text_ms: float
    frontmost_changed: bool


def process(cfg, stt, injector: Injector, audio, *, audio_ms: float, t_release: float,
            source: str, force: str | None = None,
            frontmost_changed: Callable[[], bool] | None = None,
            on_text: Callable[[str], None] | None = None) -> Outcome:
    """Transcribe `audio` and deliver it. `t_release` is `time.perf_counter()` at key-up.

    Raises only if STT itself fails; the caller turns that into a toast. From the moment a
    transcript exists nothing raises: `on_text` receives it before any further OS call
    (the app keeps it for menu › Copy last transcript), and any exception from the
    frontmost check or the injector lands the text on the pasteboard with a toast.
    """
    if audio is not None and bool(cfg.require("stt.vad")):
        from dict8.audio import is_silent

        floor = float(cfg.require("audio.silence_floor_dbfs"))
        frame_ms = float(cfg.require("audio.frame_ms"))
        if is_silent(audio, int(stt.sample_rate), floor, frame_ms):
            record(cfg, {"source": source, "outcome": "silent", "audio_ms": audio_ms,
                         "release_to_text_ms": (time.perf_counter() - t_release) * 1000})
            return Outcome("", 0.0, InjectResult(
                PATH_NONE, 0.0, 0, toast=("Dict8: heard nothing",
                                          "No speech reached the microphone (muted, wrong "
                                          "input, or a permission prompt was up). Nothing "
                                          "was typed.")), 0.0, False)
    text, stt_ms = stt.transcribe(audio)
    text = prepare(text)
    if text and on_text is not None:
        try:
            on_text(text)
        except Exception as exc:
            log.warning("dictation: on_text hook raised (%r)", exc)
    changed = False
    try:
        changed = bool(frontmost_changed()) if (frontmost_changed and text) else False
        if not text:
            result = InjectResult(PATH_NONE, 0.0, 0)
        elif changed and force is None:
            result = injector.pasteboard_only(
                text, "Dict8: app changed while you were dictating",
                "A different app is frontmost than when you pressed the talk key, so "
                "nothing was typed.")
        else:
            result = injector.inject(text, force=force)
    except Exception as exc:
        log.warning("dictation: delivery raised (%r) — pasteboard only", exc)
        result = injector.pasteboard_only(
            text, "Dict8: could not check before typing",
            f"A system check failed ({type(exc).__name__}), so nothing was typed.")
    r2t = (time.perf_counter() - t_release) * 1000
    outcome = ("empty" if not text else "error" if result.path == PATH_FAILED
               else "transcribed_only" if result.path == PATH_NONE else "injected")
    record(cfg, {"source": source, "outcome": outcome,
                 "path": result.path, "audio_ms": audio_ms, "stt_ms": stt_ms,
                 "inject_ms": result.ms, "release_to_text_ms": r2t,
                 "chars": result.chars, "frontmost_changed": changed})
    return Outcome(text, stt_ms, result, r2t, changed)
