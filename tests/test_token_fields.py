"""The two decomposed usage fields, and the one that is not tokens at all.

docs/verified-schemas.md sections 4.1-4.3, HANDOFF section 4.2:

  * `usage.cache_creation` (`ephemeral_5m_input_tokens` + `ephemeral_1h_input_tokens`) sums
    exactly to `cache_creation_input_tokens`. It is a breakdown of a field already counted.
  * `usage.output_tokens_details.thinking_tokens` is *inside* `output_tokens`.
  * `usage.server_tool_use` is request counts, not tokens.

Adding any of them double-counts, and the failure is quiet: the totals stay plausible and
reconciliation against `claude-tokens` drifts by a few percent. This test holds the four
flat fields — and only those four — to an exact arithmetic result.
"""

from __future__ import annotations

from dict8.usage.reader import scan
from dict8.usage.store import TOKEN_COLUMNS

from conftest import assistant_line, write_transcript


def test_decomposed_fields_are_not_added_to_the_totals(projects_root, store):
    """A line carrying both decompositions, in the observed shape and summing correctly."""
    write_transcript(projects_root, "s-decomp", [
        assistant_line(
            message_id="msg_decomp",
            input_tokens=11,
            output_tokens=1000,
            cache_creation_input_tokens=700,
            cache_read_input_tokens=50_000,
            # 4.1 — sums to cache_creation_input_tokens above, exactly.
            cache_creation={"ephemeral_5m_input_tokens": 400,
                            "ephemeral_1h_input_tokens": 300},
            # 4.2 — already part of output_tokens above.
            thinking_tokens=640,
        ),
    ])

    scan(store, projects_root, full=True)

    row = store.totals()
    got = {c: row[c] for c in TOKEN_COLUMNS}
    assert got == {
        "input_tokens": 11,
        "output_tokens": 1000,          # not 1640
        "cache_creation_tokens": 700,   # not 1400
        "cache_read_tokens": 50_000,
    }
    assert sum(got.values()) == 51_711, "a decomposition leaked into the total"


def test_the_store_has_no_column_for_a_decomposition(store):
    """Structural: the double-count is unavailable, not merely avoided."""
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(messages)")}
    assert "thinking_tokens" not in cols
    assert "cache_creation" not in cols
    assert "ephemeral_5m_input_tokens" not in cols
    assert {"input_tokens", "output_tokens",
            "cache_creation_tokens", "cache_read_tokens"} <= cols


def test_no_usd_column_exists_anywhere(store):
    """Invariant 6. Denis is on a subscription; the unit is tokens, calibrated to quota %.
    A cost column is the kind of thing that gets added 'just to have it'."""
    tables = [r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        for row in store.conn.execute(f"PRAGMA table_info({table})"):
            name = row[1].lower()
            assert "usd" not in name and "cost" not in name and "price" not in name, \
                f"{table}.{row[1]}"


def test_turn_aggregates_equal_the_sum_of_their_messages(projects_root, store):
    """The aggregates are derived, never written at insert time (HANDOFF section 4.7) — a
    turn written on the tick it was first seen would otherwise freeze at what had arrived."""
    from conftest import ts, user_line

    write_transcript(projects_root, "s-agg", [
        user_line("Do the work.", uuid="p1", session_id="s-agg", timestamp=ts(0)),
        assistant_line(message_id="m1", session_id="s-agg", timestamp=ts(10),
                       input_tokens=3, output_tokens=5, cache_read_input_tokens=100),
        assistant_line(message_id="m2", session_id="s-agg", timestamp=ts(20),
                       input_tokens=7, output_tokens=11, cache_read_input_tokens=200,
                       uuid="aaaaaaaa-0000-4000-8000-000000000002"),
    ])

    scan(store, projects_root, full=True)

    turn = store.conn.execute("SELECT * FROM turns WHERE prompt_uuid='p1'").fetchone()
    assert (turn["input_tokens"], turn["output_tokens"], turn["cache_read_tokens"]) == \
        (10, 16, 300)
    assert turn["message_count"] == 2


def test_files_touched_counts_distinct_paths(projects_root, store):
    """Section 7: `file_path` is read generically off any tool_use block, so a tool added
    in a later Claude Code release is picked up with no code change — and the same file
    edited twice is one file."""
    from conftest import ts, user_line

    write_transcript(projects_root, "s-files", [
        user_line("Touch some files.", uuid="p1", session_id="s-files", timestamp=ts(0)),
        assistant_line(message_id="m1", session_id="s-files", timestamp=ts(10),
                       file_paths=("/tmp/a.py", "/tmp/b.py")),
        assistant_line(message_id="m2", session_id="s-files", timestamp=ts(20),
                       file_paths=("/tmp/a.py",),
                       uuid="aaaaaaaa-0000-4000-8000-000000000002"),
    ])

    scan(store, projects_root, full=True)

    turn = store.conn.execute("SELECT files_touched FROM turns WHERE prompt_uuid='p1'").fetchone()
    assert turn["files_touched"] == 2
