"""Voice commands — `send`, `cancel` / `scratch that`, `use <model>` — matched on whole
sentences of the transcript, never on a prefix.

The seeded rule was "exact-prefix, case-insensitive", which makes "send the email to Ana"
fire `send` and "use opus-style naming" fire an override: the user's words would be eaten
and something they never asked for would happen. So each command has a POSITION and must
be a WHOLE SENTENCE there (config.yml `voice_commands`):

    cancel / scratch that   the whole utterance, or its final sentence -> discard everything
    send                    the final sentence                          -> strip, inject, Return
    use opus|sonnet|haiku   the leading sentence                        -> strip, show override

A sentence ends at `.`, `!` or `?` followed by whitespace or the end, or at a newline —
Whisper's punctuation. Matching lowercases the sentence and turns every run of
non-alphanumerics into one space, so "Send." / "send!" / " SEND " all read "send", while
"use opus-style naming" reads "use opus style naming" and matches nothing.

**Invariant 1.** Anything not matched is injected verbatim. What is injected when a command
matched is an exact slice of the transcript with the command sentence cut off at its
boundary and the whitespace at that joint trimmed — no character inside the kept text is
touched. The module never logs text: `commands` carries fixed names only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

SEND = "send"
CANCEL = "cancel"
OVERRIDE = "override"
COMMANDS = (OVERRIDE, SEND, CANCEL)

# Sentence boundary: terminal punctuation followed by whitespace/end, or a newline run.
_END = re.compile(r"[.!?]+(?=\s|$)|\n+")
# Everything that is not a letter or digit (underscore included) collapses to one space.
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class VoiceResult:
    text: str                        # what to inject — a slice of the transcript
    cancel: bool = False
    send: bool = False
    override: str | None = None      # a models[].id, only ever from config
    commands: tuple[str, ...] = ()   # names of what was stripped, in order


def normalize(sentence: str) -> str:
    return _NON_WORD.sub(" ", sentence.lower()).strip()


def sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) spans of the non-blank sentences in `text`, end exclusive, each span
    including its own terminal punctuation."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _END.finditer(text):
        if text[start:m.end()].strip():
            spans.append((start, m.end()))
        start = m.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _phrases(cfg, key: str) -> set[str]:
    raw = cfg.get(f"voice_commands.{key}") or []
    if isinstance(raw, str):
        raw = [raw]
    return {normalize(str(p)) for p in raw if normalize(str(p))}


def _overrides(cfg) -> dict[str, str]:
    """Spoken phrase -> models[].id. A phrase whose id is not a models[] entry is dropped
    here (and logged), so it can never be stripped: an override that cannot name a model
    Denis has would eat his words for nothing."""
    from dict8.advise.recommend import by_id

    raw = cfg.get("voice_commands.override_model") or {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        log.warning("voice: voice_commands.override_model is not a phrase -> model map; "
                    "overrides disabled")
        return out
    for phrase, model_id in raw.items():
        if by_id(cfg, str(model_id)) is None:
            log.warning("voice: override phrase maps to an id not in models[] — ignored")
            continue
        key = normalize(str(phrase))
        if key:
            out[key] = str(model_id)
    return out


def parse(cfg, transcript: str) -> VoiceResult:
    text = transcript or ""
    spans = sentences(text)
    if not spans:
        return VoiceResult(text=text.strip())

    cancel = _phrases(cfg, "cancel")
    if normalize(text) in cancel or normalize(text[slice(*spans[-1])]) in cancel:
        log.info("voice command stripped: %s", CANCEL)
        return VoiceResult(text="", cancel=True, commands=(CANCEL,))

    commands: list[str] = []
    start, end = 0, len(text)
    override = None
    first_used = False
    model_id = _overrides(cfg).get(normalize(text[slice(*spans[0])]))
    if model_id is not None:
        override, start, first_used = model_id, spans[0][1], True
        commands.append(OVERRIDE)

    send = False
    last = spans[-1]
    if not (first_used and len(spans) == 1) and normalize(text[slice(*last)]) in _phrases(cfg, "send"):
        send, end = True, last[0]
        commands.append(SEND)

    for c in commands:
        log.info("voice command stripped: %s", c)
    kept = text[start:end].strip() if commands else text.strip()
    return VoiceResult(text=kept, send=send, override=override, commands=tuple(commands))
