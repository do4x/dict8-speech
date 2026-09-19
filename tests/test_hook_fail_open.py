"""Invariant 8: the hook never becomes the reason a prompt did not send.

A Claude Code hook that exits 2 BLOCKS the prompt (docs/verified-schemas.md section 8), and
stderr from a hook is shown to the user. So there is exactly one way out of every handler
in `dict8.hooks`: exit 0, nothing on stderr, and one line in `paths.logs/hooks.log` saying
why there was no estimate.

These are the hostile-input cases run in-process. The Phase 2 gate covers the same ground
end to end through `uv run dict8 hook user-prompt-submit`; these run in milliseconds and
fail loudly in a unit run instead of only at gate time.
"""

from __future__ import annotations

import json

import pytest

from dict8 import config as config_mod
from dict8.hooks import read_payload
from dict8.hooks import user_prompt_submit as ups

# The captured payload from docs/verified-schemas.md section 8, with the real ids replaced.
PAYLOAD = {
    "session_id": "33333333-3333-4333-8333-333333333333",
    "transcript_path": "/tmp/dict8-test/projects/-tmp-x/33333333.jsonl",
    "cwd": "/tmp/dict8-test",
    "prompt_id": "44444444-4444-4444-8444-444444444444",
    "permission_mode": "default",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "add a spinner while the backfill runs",
}

HOSTILE_STDIN = [
    pytest.param("", id="empty"),
    pytest.param("   \n", id="whitespace"),
    pytest.param("not json at all", id="not-json"),
    pytest.param("{", id="truncated-object"),
    pytest.param("[]", id="json-but-a-list"),
    pytest.param('"a bare string"', id="json-but-a-string"),
    pytest.param("null", id="json-null"),
    pytest.param("{}", id="empty-object"),
    pytest.param(json.dumps({"prompt": ""}), id="empty-prompt"),
    pytest.param(json.dumps({"prompt": "   "}), id="whitespace-prompt"),
    pytest.param(json.dumps({"prompt": None}), id="null-prompt"),
    pytest.param(json.dumps({"prompt": 42}), id="numeric-prompt"),
    pytest.param(json.dumps({"hook_event_name": "UserPromptSubmit"}), id="no-prompt-key"),
]


@pytest.fixture(autouse=True)
def _fresh_hook_clock(monkeypatch):
    """`hooks.elapsed_ms()` counts from the `dict8.hooks` import, which is process start for
    a real hook. In a pytest run that import happens at COLLECTION, so once the tests that
    run first take longer than `hooks.timeout_ms`, the watchdog here fires `os._exit(0)`
    mid-suite: pytest dies with exit code 0 and no summary line — a vacuous green. Found in
    U5 (2026-09-19), when the suite first grew past 250 ms. Each test gets the clock a
    freshly started hook process would have."""
    import time

    from dict8 import hooks

    monkeypatch.setattr(hooks, "_T0", time.monotonic())


@pytest.fixture
def scratch_cfg(tmp_path):
    """The real config.yml with `paths.db` and `paths.logs` redirected into tmp_path.

    Read as text and rewritten rather than reconstructed, so the test exercises the actual
    shipped values for every other key — a hand-built dict would quietly stop reflecting
    config.yml the first time a key was added.
    """
    import yaml

    data = yaml.safe_load(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["paths"]["db"] = str(tmp_path / "scratch.sqlite")
    data["paths"]["logs"] = str(tmp_path / "logs")
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return config_mod.Config.load(path)


@pytest.mark.parametrize("raw", HOSTILE_STDIN)
def test_hostile_stdin_never_produces_output_or_raises(raw, scratch_cfg):
    """No database exists in tmp_path either, so this is two fail-open reasons at once."""
    out, fields = ups.run(raw, scratch_cfg)

    assert out is None
    assert fields["hook"] == "UserPromptSubmit"
    assert fields["outcome"] in {"skipped", "no-estimate"}
    assert fields["reason"], "a fail-open path must say why, per invariant 8"


@pytest.mark.parametrize("raw", HOSTILE_STDIN)
def test_read_payload_always_returns_a_dict(raw):
    assert isinstance(read_payload(raw), dict)


def test_main_exits_zero_on_hostile_stdin(monkeypatch, scratch_cfg, capsys):
    """The whole entry point, including the watchdog and the log write. 0 is the only
    acceptable exit code; 2 would block the prompt."""
    for raw in [p.values[0] for p in HOSTILE_STDIN]:
        monkeypatch.setattr("sys.stdin", _FakeStdin(raw))
        assert ups.main(cfg=scratch_cfg) == 0
    captured = capsys.readouterr()
    assert captured.err == "", "a hook must never write to stderr — the user sees it"
    assert captured.out == ""


def test_main_exits_zero_when_the_database_is_missing(monkeypatch, scratch_cfg, capsys):
    """The named case in the U3 gate: DB deleted, prompt still goes through, log says why."""
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps(PAYLOAD)))

    assert ups.main(cfg=scratch_cfg) == 0

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    log_lines = _log_lines(scratch_cfg)
    assert log_lines, "the fail-open path must leave a record"
    assert log_lines[-1]["outcome"] == "no-estimate"
    assert "no database" in log_lines[-1]["reason"]


def test_main_exits_zero_when_the_config_would_not_load(capsys):
    """`cfg_error` — an explicit --config that failed. It must not fall back to the real
    config and estimate against Denis's own store (that happened once)."""
    assert ups.main(cfg_error="scratch.yml: could not be parsed") == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_a_real_payload_produces_context_beside_the_prompt_never_inside_it(scratch_cfg):
    """Invariant 1. The estimate travels in `hookSpecificOutput.additionalContext`; the
    user's words are not rewritten, not echoed, and not carried in the response at all."""
    from dict8.usage.store import Store

    # A store with enough history to answer rather than refuse.
    db = scratch_cfg.path("paths.db")
    with Store(db) as store:
        _seed_turns(store, n=40)

    out, fields = ups.run(json.dumps(PAYLOAD), scratch_cfg)

    assert out is not None
    payload = json.loads(out)
    block = payload["hookSpecificOutput"]
    assert block["hookEventName"] == "UserPromptSubmit"
    assert PAYLOAD["prompt"] not in out, "the user's words must not be echoed back"
    assert "$" not in block["additionalContext"], "invariant 6: no USD anywhere"
    assert fields["prompt_words"] == len(PAYLOAD["prompt"].split())
    # And the estimate was recorded for Phase 6 to calibrate against.
    with Store(db) as store:
        assert store.count("hook_estimates") == 1


def test_the_hook_log_never_carries_prompt_text(scratch_cfg, monkeypatch):
    from dict8.usage.store import Store

    with Store(scratch_cfg.path("paths.db")) as store:
        _seed_turns(store, n=40)
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps(PAYLOAD)))

    ups.main(cfg=scratch_cfg)

    raw = (scratch_cfg.path("paths.logs") / "hooks.log").read_text(encoding="utf-8")
    assert PAYLOAD["prompt"] not in raw
    for word in PAYLOAD["prompt"].split():
        assert f'"{word}"' not in raw


# ---- helpers -----------------------------------------------------------------------


class _FakeStdin:
    def __init__(self, raw: str) -> None:
        self._raw = raw

    def read(self) -> str:
        return self._raw


def _log_lines(cfg) -> list[dict]:
    path = cfg.path("paths.logs") / "hooks.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _seed_turns(store, *, n: int) -> None:
    """Enough synthetic turns for the pooled floors in `estimate.*` to be satisfied.

    Token counts are arbitrary invented numbers with a spread, so the quantile band has
    something to span. No prompt text is involved — `turns` has no column for it.
    """
    with store.tx() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO turns (prompt_uuid, session_id, project, cwd, ts, "
                "prompt_chars, prompt_words, prompt_count, src_file, src_line, "
                "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, "
                "message_count, files_touched) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"seed-{i}", "seed-session", "/tmp/seed", "/tmp/seed",
                 f"2026-09-{(i % 28) + 1:02d}T10:00:00+00:00",
                 40 + i, 6 + i, 1, "/tmp/seed.jsonl", i,
                 100 * (i + 1), 10 * (i + 1), 0, 1000 * (i + 1), 2, 1),
            )
