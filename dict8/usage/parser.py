"""Claude Code transcript JSONL → typed records.

Every field read here is pinned in `docs/verified-schemas.md`, observed on real files on
this machine (CLAUDE.md invariant 4). Section references below point into that document.

Two things are deliberately structural rather than conventional:

* `HumanPrompt` has no text field. `privacy.store_transcripts` is `features_only`, and the
  cheapest way to guarantee raw prompt text is never persisted is to give the record
  nowhere to put it. Length and word count are computed here and the text is dropped.
* Only the four flat token fields are read. `usage.cache_creation` and
  `usage.output_tokens_details` are decompositions of fields already counted (§4.1, §4.2);
  reading them would double-count. `server_tool_use` is request counts, not tokens (§4.3).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# §2 — only these two line types carry anything the usage layer needs. Every other type
# (attachment, queue-operation, ai-title, relocated, summary, …) is ignored rather than
# errored on: new types appear across Claude Code releases and must never break a parse.
TYPE_ASSISTANT = "assistant"
TYPE_USER = "user"


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """One usage-bearing assistant line. Identity is `message_id` (§5)."""

    message_id: str
    session_id: str
    project: str
    cwd: str | None
    model: str
    ts: datetime
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    is_sidechain: bool
    cc_version: str | None
    uuid: str | None
    parent_uuid: str | None
    file_paths: tuple[str, ...]
    src_file: str
    src_line: int

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )

    @property
    def dedup_order(self) -> tuple:
        """Deterministic winner among duplicates: earliest timestamp, then file, then line.

        The reference implementation keeps whichever copy `rglob` reaches first, which makes
        per-project attribution of a cross-project duplicate vary between runs (§5). Ordering
        explicitly makes a backfill reproducible.
        """
        return (self.ts, self.src_file, self.src_line)


@dataclass(frozen=True, slots=True)
class HumanPrompt:
    """A prompt a person actually typed or dictated — not a tool_result (§6).

    Carries no text: see module docstring.
    """

    uuid: str
    session_id: str
    project: str
    cwd: str | None
    ts: datetime
    chars: int
    words: int
    src_file: str
    src_line: int

    @property
    def dedup_order(self) -> tuple:
        return (self.ts, self.src_file, self.src_line)


@dataclass
class Turn:
    """Everything a person said before one reply, plus that reply (§6).

    Identity is the *first* prompt's line `uuid`, which survives a session replay while
    `sessionId` does not — 6 of 409 prompt uuids on this machine appear in more than one
    file, so a turn needs the same dedup discipline as a token total.

    A turn can hold more than one prompt. Queued messages produce several human lines in a
    row with no assistant line between them, and Claude answers them together — measured:
    44 such prompts on this machine. Treating each as its own turn hands the whole cost to
    the last one and records the rest as free, which is how a 15-word prompt ends up
    attributed 35 M tokens. The estimator is trained on prompt size, so the input it
    learns from has to be everything that was actually in front of the model.
    """

    prompts: list[HumanPrompt]
    messages: list[AssistantMessage] = field(default_factory=list)

    @property
    def prompt(self) -> HumanPrompt:
        """The turn's identity — the first thing said."""
        return self.prompts[0]

    @property
    def chars(self) -> int:
        return sum(p.chars for p in self.prompts)

    @property
    def words(self) -> int:
        return sum(p.words for p in self.prompts)

    @property
    def message_ids(self) -> list[str]:
        return [m.message_id for m in self.messages]

    @property
    def files_touched(self) -> int:
        paths: set[str] = set()
        for m in self.messages:
            paths.update(m.file_paths)
        return len(paths)

    @property
    def model(self) -> str | None:
        """The model that did the work. Last one wins on a mid-turn switch."""
        return self.messages[-1].model if self.messages else None

    @property
    def wall_ms(self) -> int | None:
        if not self.messages:
            return None
        last = max(m.ts for m in self.messages)
        return max(0, int((last - self.prompt.ts).total_seconds() * 1000))


def decode_project(encoded: str) -> str:
    """Decode an `~/.claude/projects/` directory name back to a path (§1).

    Claude Code replaces both `/` and `.` with `-`, so a hidden directory encodes to `--`;
    that must be decoded first or the dot is lost and a stray `//` appears. Lossy for a
    segment that legitimately contained `-`, which is why `cwd` is preferred when present.
    """
    if encoded.startswith("-"):
        return "/" + encoded[1:].replace("--", "/.").replace("-", "/")
    return encoded


def project_for(path: Path, root: Path, cwd: str | None) -> str:
    """Prefer the exact `cwd` carried on the line over the lossy encoded directory name.

    The reference has only the directory name to work from. We have both, and `cwd` is
    present on every assistant line observed (§2), so the lossy decode is a fallback.
    """
    if cwd:
        return cwd
    try:
        rel = path.resolve().relative_to(root.resolve())
        encoded = rel.parts[0] if rel.parts else ""
    except (ValueError, OSError):
        encoded = path.parent.name
    return decode_project(encoded)


def parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _tool_use_paths(content: Any) -> tuple[str, ...]:
    """Distinct `file_path` values from any tool_use block that has one (§7).

    Read generically rather than from a list of tool names: a tool added in a later Claude
    Code release is then picked up with no code change, and no tool name lands in code
    (invariant 3).
    """
    if not isinstance(content, list):
        return ()
    found: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        inp = block.get("input")
        if isinstance(inp, dict):
            fp = inp.get("file_path")
            if isinstance(fp, str) and fp:
                found.append(fp)
    return tuple(dict.fromkeys(found))


_SYNTHETIC_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z_-]*)>.*?</\1>", re.DOTALL)
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[Image:[^\]]*\]")
_SKILL_PREAMBLE_PREFIX = "Base directory for this skill:"


def _strip_synthetic(text: str) -> str:
    """Remove harness-injected content so word/char counts and the classifier see only
    what a person actually said — CORRECTION to the original §6 characterization.

    §6 as first written treated "a string, or a list with a text block and no tool_result
    block" as sufficient for "human prompt". Verified wrong on 2026-09-15 against this
    corpus's `turns` table: several "turns" came back with 14,000+ prompt_words, and the
    text behind them was `<ide_selection>...`, `<local-command-caveat>...`, and Skill
    preambles opening with "Base directory for this skill:" — harness-injected context on
    a user-role line, not anything typed or dictated. One of those alone (a Skill's full
    instructions) is bigger than this session's entire real prompt history combined, and
    it was silently becoming the single most "expensive-looking" row in the estimator's
    training data.

    `isMeta: True` looked like the obvious flag for this and is NOT: measured on this
    corpus, a 15,681-word line that is a genuine, deliberately composed human prompt also
    carries `isMeta: True`, alongside `[Image: ...]` attachment captions, Skill preambles,
    and local-command boilerplate. Blanket-filtering on it would have silently dropped a
    real prompt from the data instead of an synthetic one — worse, since it fails quietly.
    Content-based stripping is what's actually verified to work: every `<tag>...</tag>`
    block observed here (`system-reminder`, `ide_selection`, `ide_opened_file`,
    `local-command-caveat`, `command-name`, `command-message`, `command-args`,
    `local-command-stdout`, `task-notification`, ...) is well-formed and safe to strip
    generically without a hardcoded tag list — a tag Claude Code adds later is still
    caught. `[Image: ...]` placeholders and the Skill-preamble prefix are separate,
    verified patterns: every observed Skill-preamble line was 100% synthetic (0 residual
    words once stripped), so a line starting with that exact prefix is dropped entirely.

    Known remaining gap, accepted rather than chased further: a handful of other
    system-authored lines (a hook-activation notice, a scheduled goal check-in) carry
    none of these markers and pass through as "human". Rare in this corpus (a small
    handful of ~1,900 user lines) — worth revisiting if it shows up in the bucket
    distribution, not worth an ever-growing pattern blocklist today.
    """
    text = _SYNTHETIC_TAG_RE.sub("", text)
    text = _IMAGE_PLACEHOLDER_RE.sub("", text)
    stripped = text.strip()
    if stripped.startswith(_SKILL_PREAMBLE_PREFIX):
        return ""
    return stripped


def is_human_prompt(content: Any) -> bool:
    """§6: a string, or a list with a text block and no tool_result block — AND what's
    left after `_strip_synthetic` is non-empty. See that function for why the stripping
    step is load-bearing, not cosmetic.

    A tool_result-bearing line is the harness returning output, not a person typing.
    Counting those as prompts inflates the turn count several-fold.
    """
    if isinstance(content, str):
        return bool(_strip_synthetic(content))
    if not isinstance(content, list):
        return False
    has_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            return False
        if btype == "text" and _strip_synthetic(str(block.get("text") or "")):
            has_text = True
    return has_text


def _prompt_text(content: Any) -> str:
    """Join the spoken/typed text of a prompt, synthetic wrapper content stripped
    (see `_strip_synthetic`). Used for length only, never persisted."""
    if isinstance(content, str):
        return _strip_synthetic(content)
    parts = [
        _strip_synthetic(str(b.get("text") or ""))
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p)


def parse_line(
    raw: str,
    *,
    path: Path,
    root: Path,
    line_no: int,
) -> AssistantMessage | HumanPrompt | None:
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None

    ltype = d.get("type")
    if ltype not in (TYPE_ASSISTANT, TYPE_USER):
        return None

    msg = d.get("message")
    if not isinstance(msg, dict):
        return None

    ts = parse_ts(d.get("timestamp"))
    if ts is None:
        return None

    # §2: sessionId equals the filename stem on all 8,480 lines observed, but the field is
    # what the schema promises, so read it and fall back rather than trusting the filename.
    session_id = d.get("sessionId") or path.stem
    cwd = d.get("cwd") if isinstance(d.get("cwd"), str) else None
    project = project_for(path, root, cwd)

    if ltype == TYPE_ASSISTANT:
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return None
        message_id = msg.get("id")
        if not message_id:
            return None

        def tok(key: str) -> int:
            try:
                return int(usage.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return AssistantMessage(
            message_id=str(message_id),
            session_id=str(session_id),
            project=project,
            cwd=cwd,
            model=str(msg.get("model") or "?"),
            ts=ts,
            # §4 — these four and nothing else.
            input_tokens=tok("input_tokens"),
            output_tokens=tok("output_tokens"),
            cache_creation_tokens=tok("cache_creation_input_tokens"),
            cache_read_tokens=tok("cache_read_input_tokens"),
            is_sidechain=bool(d.get("isSidechain")),
            cc_version=d.get("version") if isinstance(d.get("version"), str) else None,
            uuid=d.get("uuid") if isinstance(d.get("uuid"), str) else None,
            parent_uuid=d.get("parentUuid") if isinstance(d.get("parentUuid"), str) else None,
            file_paths=_tool_use_paths(msg.get("content")),
            src_file=str(path),
            src_line=line_no,
        )

    content = msg.get("content")
    if not is_human_prompt(content):
        return None
    uuid = d.get("uuid")
    if not uuid:
        return None
    text = _prompt_text(content)
    return HumanPrompt(
        uuid=str(uuid),
        session_id=str(session_id),
        project=project,
        cwd=cwd,
        ts=ts,
        chars=len(text),
        words=len(text.split()),
        src_file=str(path),
        src_line=line_no,
    )
    # `text` goes out of scope here and is never returned: features_only, structurally.


def read_prompt_text(path: Path, line_no: int) -> str | None:
    """Re-read one human prompt's text directly from the source JSONL, by line number.

    For a TRANSIENT purpose only — feeding a classifier — never for persistence.
    `privacy.store_transcripts: features_only` means Dict8's own store carries no text
    column anywhere (see dict8.usage.store), so a caller that needs the actual words goes
    back to Claude Code's own on-disk transcript, which already exists independent of
    Dict8. Returns None if the line no longer looks like a human prompt (file rewritten,
    line renumbered since the turn was recorded).
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if i != line_no:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    return None
                if not isinstance(d, dict):
                    return None
                msg = d.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if not is_human_prompt(content):
                    return None
                return _prompt_text(content)
    except OSError:
        return None
    return None


def parse_file(path: Path, root: Path, *, start_offset: int = 0) -> Iterator[tuple[Any, int]]:
    """Yield `(record, byte_offset_after_line)` from `path`, starting at `start_offset`.

    The offset is what lets the tail-reader resume without re-reading the whole file.
    """
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        if start_offset:
            fh.seek(start_offset)
        line_no = 0
        while True:
            line = fh.readline()
            if not line:
                break
            line_no += 1
            # A partial final line means the writer is mid-append; stop before it and let
            # the next tick re-read from this offset rather than parsing half an object.
            if not line.endswith("\n"):
                break
            rec = parse_line(line, path=path, root=root, line_no=line_no)
            if rec is not None:
                yield rec, fh.tell()


def assemble_turns(records: list[Any]) -> list[Turn]:
    """Group records into turns: a human prompt owns every assistant message after it,
    within the same session, until the next human prompt (§6).
    """
    by_session: dict[str, list[Any]] = {}
    for rec in records:
        by_session.setdefault(rec.session_id, []).append(rec)

    turns: list[Turn] = []
    for session_records in by_session.values():
        session_records.sort(key=lambda r: (r.ts, r.src_file, r.src_line))
        current: Turn | None = None
        for rec in session_records:
            if isinstance(rec, HumanPrompt):
                # Consecutive prompts with no reply between them were queued and answered
                # together: fold this one into the open turn rather than starting a new one.
                if current is not None and not current.messages:
                    current.prompts.append(rec)
                else:
                    current = Turn(prompts=[rec])
                    turns.append(current)
            elif current is not None:
                current.messages.append(rec)
            # An assistant message before any human prompt in the session is a replayed
            # prefix; its tokens are still counted at the message level, it just has no
            # turn to belong to.
    return turns
