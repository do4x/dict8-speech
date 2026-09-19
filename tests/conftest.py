"""Synthetic transcript fixtures, shaped from `docs/verified-schemas.md`.

**Nothing here is copied from `~/.claude`, and no line contains anything Denis said.**
Every prompt string below is invented for the test that reads it. That is not squeamishness
about the repo's size: `privacy.store_transcripts: features_only` says his prompt text is
never persisted by Dict8, and a fixture file committed to git is the most persistent store
there is. The shapes are real; the words are not.

The shapes come from the pinned document, section by section:
  section 2  the top-level line object (`type`, `sessionId`, `cwd`, `timestamp`, `uuid`,
             `parentUuid`, `isSidechain`, `version`)
  section 3  the assistant `message` object (`id`, `model`, `content`, `usage`)
  section 4  the four flat token fields, plus the two decompositions that must NOT be added
  section 6  the user `message.content` shapes, and the harness-injected content that looks
             exactly like a prompt and is not
  section 7  `tool_use` blocks carrying `input.file_path`

The gates in `scripts/` are the integration tests — they run against this machine's real
`~/.claude` and would notice a schema change. These are the hermetic ones: a scratch DB per
test, no network, no model, no `~`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# A fixed instant to build timestamps from. UTC with a `Z`, exactly as observed (section 2).
BASE_TS = "2026-09-19T10:00:00.000Z"


def ts(seconds: int = 0, *, minutes: int = 0) -> str:
    """An ISO-8601 UTC timestamp `seconds`/`minutes` after BASE_TS, in the observed shape."""
    total = seconds + minutes * 60
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    return f"2026-09-19T{10 + hh:02d}:{mm:02d}:{ss:02d}.000Z"


def assistant_line(
    *,
    message_id: str,
    session_id: str = "11111111-1111-4111-8111-111111111111",
    cwd: str = "/tmp/dict8-test-project",
    model: str = "claude-opus-4-5-20260101",
    timestamp: str = BASE_TS,
    input_tokens: int = 10,
    output_tokens: int = 20,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation: dict | None = None,
    thinking_tokens: int | None = None,
    file_paths: tuple[str, ...] = (),
    uuid: str = "aaaaaaaa-0000-4000-8000-000000000001",
    parent_uuid: str | None = None,
    is_sidechain: bool = False,
    version: str = "2.1.274",
) -> str:
    """One `type: "assistant"` line, section 3 + section 4 shape.

    `cache_creation` and `thinking_tokens` exist so a test can prove the decompositions are
    NOT added to the totals (section 4.1, 4.2) — they are present on real lines.
    """
    usage: dict = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "service_tier": "standard",
    }
    if cache_creation is not None:
        usage["cache_creation"] = cache_creation
    if thinking_tokens is not None:
        usage["output_tokens_details"] = {"thinking_tokens": thinking_tokens}

    content: list[dict] = [{"type": "text", "text": "Synthetic assistant reply."}]
    for i, fp in enumerate(file_paths):
        content.append({
            "type": "tool_use",
            "id": f"toolu_{message_id}_{i}",
            "name": "Edit",
            "input": {"file_path": fp, "old_string": "a", "new_string": "b"},
        })

    return json.dumps({
        "type": "assistant",
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": timestamp,
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "isSidechain": is_sidechain,
        "version": version,
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": "end_turn",
            "usage": usage,
        },
    })


def user_line(
    content,
    *,
    uuid: str,
    session_id: str = "11111111-1111-4111-8111-111111111111",
    cwd: str = "/tmp/dict8-test-project",
    timestamp: str = BASE_TS,
    is_meta: bool | None = None,
    parent_uuid: str | None = None,
) -> str:
    """One `type: "user"` line. `content` is a str or a block list (section 6)."""
    line: dict = {
        "type": "user",
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": timestamp,
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "version": "2.1.274",
        "message": {"role": "user", "content": content},
    }
    if is_meta is not None:
        line["isMeta"] = is_meta
    return json.dumps(line)


def write_transcript(root: Path, session_id: str, lines: list[str],
                     project_dir: str = "-tmp-dict8-test-project") -> Path:
    """Lay lines out the way section 1 says Claude Code does:
    `<root>/<encoded-cwd>/<session-uuid>.jsonl`, one JSON object per line.
    """
    directory = root / project_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    """A throwaway stand-in for `~/.claude/projects`, empty."""
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture
def store(tmp_path: Path):
    """A scratch SQLite store, one per test, deleted with the tmp_path.

    Never the real `paths.db`: that file is Denis's production usage history and these
    tests write to it constantly.
    """
    from dict8.usage.store import Store

    with Store(tmp_path / "scratch.sqlite") as s:
        yield s
