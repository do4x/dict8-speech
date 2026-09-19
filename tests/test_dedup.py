"""Invariant 5: every read path counts a `message.id` exactly once.

This is the single easiest way to ship a wrong number — measured on this machine, naive
counting over-reports by 154-158% (docs/verified-schemas.md section 5), because Claude Code
writes the same assistant message into several files on session resume, `/rewind` and
subagent runs, and often onto consecutive lines of one file.

The rule has two halves and both are tested here:
  1. the id is counted once, whether the copies are in one file or several;
  2. WHICH copy survives is deterministic — earliest timestamp, then source path, then line
     number. That ordering lives in exactly one place, the row-value `WHERE` clause in
     `Store.upsert_messages` (and its twin in `upsert_turns`), so these tests drive it
     through the store rather than asserting on a Python mirror of it. The reference
     implementation keeps whichever copy `rglob` reaches first, so a cross-project duplicate
     can change project between runs; that is the bug this ordering exists to avoid, and a
     test that only counted rows would not see it.
"""

from __future__ import annotations

from dict8.usage.reader import scan
from dict8.usage.store import TOKEN_COLUMNS

from conftest import assistant_line, ts, write_transcript

DUPE_ID = "msg_dedup_0001"


def _totals(store) -> dict:
    row = store.totals()
    return {c: row[c] for c in TOKEN_COLUMNS}


def test_same_message_id_twice_in_one_file_counts_once(projects_root, store):
    """Intra-file duplication — the common case on this machine (section 5)."""
    line = assistant_line(message_id=DUPE_ID, input_tokens=100, output_tokens=7)
    write_transcript(projects_root, "s-intra", [line, line])

    scan(store, projects_root, full=True)

    assert store.count("messages") == 1
    assert _totals(store)["input_tokens"] == 100, "the second copy was added, not deduped"


def test_same_message_id_across_two_files_counts_once(projects_root, store):
    """Cross-file duplication — 35 ids span files here, some across projects."""
    write_transcript(projects_root, "s-first",
                     [assistant_line(message_id=DUPE_ID, session_id="s-first",
                                     input_tokens=100, timestamp=ts(0))])
    write_transcript(projects_root, "s-second",
                     [assistant_line(message_id=DUPE_ID, session_id="s-second",
                                     input_tokens=100, timestamp=ts(30))],
                     project_dir="-tmp-other-project")

    scan(store, projects_root, full=True)

    assert store.count("messages") == 1
    assert _totals(store)["input_tokens"] == 100


def test_winner_is_the_earliest_timestamp(projects_root, store):
    """Tie-break 1. The earlier copy wins even though the later file sorts first by path."""
    write_transcript(projects_root, "s-aaa",
                     [assistant_line(message_id=DUPE_ID, session_id="late",
                                     model="model-late", timestamp=ts(minutes=5))],
                     project_dir="-aaa-sorts-first")
    write_transcript(projects_root, "s-zzz",
                     [assistant_line(message_id=DUPE_ID, session_id="early",
                                     model="model-early", timestamp=ts(0))],
                     project_dir="-zzz-sorts-last")

    scan(store, projects_root, full=True)

    row = store.conn.execute("SELECT model, session_id FROM messages").fetchone()
    assert row["model"] == "model-early"
    assert row["session_id"] == "early"


def test_tie_on_timestamp_is_broken_by_path_then_line(projects_root, store):
    """Tie-breaks 2 and 3, on copies that share a timestamp to the millisecond.

    Same instant, so only path and line can decide. `-aaa-…` sorts before `-zzz-…`, and
    within the winning file line 1 beats line 2 — which is what makes a re-run of the
    backfill reproduce the same project attribution instead of following disk order.
    """
    same = ts(0)
    write_transcript(projects_root, "s-tie",
                     [assistant_line(message_id=DUPE_ID, model="line-1", timestamp=same),
                      assistant_line(message_id=DUPE_ID, model="line-2", timestamp=same)],
                     project_dir="-aaa-sorts-first")
    write_transcript(projects_root, "s-tie",
                     [assistant_line(message_id=DUPE_ID, model="other-file", timestamp=same)],
                     project_dir="-zzz-sorts-last")

    scan(store, projects_root, full=True)

    assert store.count("messages") == 1
    assert store.conn.execute("SELECT model FROM messages").fetchone()["model"] == "line-1"


def test_dedup_is_order_independent(projects_root, tmp_path):
    """Scanning the same corpus twice, with the cursors cleared, reproduces the winner.

    A dedup rule that depends on which copy is *seen* first is not a rule, it is a race —
    and it is the specific defect the reference implementation has. `full=True` re-reads
    everything, so a second scan is a genuine re-decision, not a no-op.
    """
    from dict8.usage.store import Store

    same = ts(0)
    write_transcript(projects_root, "s-a",
                     [assistant_line(message_id=DUPE_ID, model="winner", timestamp=same)],
                     project_dir="-aaa-sorts-first")
    write_transcript(projects_root, "s-b",
                     [assistant_line(message_id=DUPE_ID, model="loser", timestamp=same)],
                     project_dir="-zzz-sorts-last")

    with Store(tmp_path / "one.sqlite") as store:
        scan(store, projects_root, full=True)
        first = store.conn.execute("SELECT model FROM messages").fetchone()["model"]
        scan(store, projects_root, full=True)
        second = store.conn.execute("SELECT model FROM messages").fetchone()["model"]

    assert first == second == "winner"


def test_rescan_without_new_bytes_adds_nothing(projects_root, store):
    """The tail path's half of invariant 5: a tick that re-reads a file it has already
    consumed must not re-add its messages."""
    write_transcript(projects_root, "s-idem", [
        assistant_line(message_id="msg_a", input_tokens=5),
        assistant_line(message_id="msg_b", input_tokens=5,
                       uuid="aaaaaaaa-0000-4000-8000-000000000002"),
    ])

    scan(store, projects_root, full=True)
    before = _totals(store)
    result = scan(store, projects_root)

    assert result.new_messages == 0
    assert _totals(store) == before
