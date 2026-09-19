"""Turn assembly — three rules that were all real bugs first.

docs/verified-schemas.md section 6 and HANDOFF section 4.5:

  * A run of human prompts with no assistant reply between them was **queued**, and Claude
    answered them together. They fold into ONE turn. Measured on this machine: 44 such
    prompts. Splitting them credits the whole merged cost to whichever line happened to
    close the run — which is how a 15-word prompt ends up attributed 35 M tokens, in the
    estimator's training data.
  * A message belongs to **exactly one** turn. `turn_messages` has a PK on `message_id`
    for that reason; before it, 9 message_ids were double-linked and the turn sums came to
    100.7% of the message total.
  * A prompt that is only harness-injected content opens **no** turn. Before the
    `is_human_prompt` fix, 44 such prompts each opened an empty one and the corpus read as
    234 turns instead of 68.
"""

from __future__ import annotations

from dict8.usage.parser import assemble_turns, parse_file
from dict8.usage.reader import scan

from conftest import assistant_line, ts, user_line, write_transcript

SESSION = "22222222-2222-4222-8222-222222222222"


def _parse(path, root):
    return [rec for rec, _ in parse_file(path, root)]


def test_queued_prompts_merge_into_one_turn(projects_root):
    """Two prompts, no reply between them, then one reply: one turn, both prompts on it."""
    path = write_transcript(projects_root, SESSION, [
        user_line("Add a retry to the upload step.", uuid="p1",
                  session_id=SESSION, timestamp=ts(0)),
        user_line("Also log the attempt count.", uuid="p2",
                  session_id=SESSION, timestamp=ts(5)),
        assistant_line(message_id="m1", session_id=SESSION, timestamp=ts(20),
                       output_tokens=100),
    ])

    turns = assemble_turns(_parse(path, projects_root))

    assert len(turns) == 1
    assert [p.uuid for p in turns[0].prompts] == ["p1", "p2"]
    assert turns[0].prompt.uuid == "p1", "the turn's identity is the FIRST prompt"
    # The merged turn's features are the sum, because that is what was in front of the model.
    assert turns[0].words == len("Add a retry to the upload step.".split()) \
                             + len("Also log the attempt count.".split())
    assert turns[0].message_ids == ["m1"]


def test_a_reply_closes_the_turn_and_the_next_prompt_opens_a_new_one(projects_root):
    path = write_transcript(projects_root, SESSION, [
        user_line("First request.", uuid="p1", session_id=SESSION, timestamp=ts(0)),
        assistant_line(message_id="m1", session_id=SESSION, timestamp=ts(10)),
        user_line("Second request.", uuid="p2", session_id=SESSION, timestamp=ts(20)),
        assistant_line(message_id="m2", session_id=SESSION, timestamp=ts(30),
                       uuid="aaaaaaaa-0000-4000-8000-000000000002"),
    ])

    turns = assemble_turns(_parse(path, projects_root))

    assert [t.prompt.uuid for t in turns] == ["p1", "p2"]
    assert [t.message_ids for t in turns] == [["m1"], ["m2"]]


def test_a_message_belongs_to_exactly_one_turn(projects_root, store):
    """Through the store, because that is where the double-link happened: turn sums must
    equal the message totals exactly, not 100.7% of them."""
    write_transcript(projects_root, SESSION, [
        user_line("Request one.", uuid="p1", session_id=SESSION, timestamp=ts(0)),
        assistant_line(message_id="m1", session_id=SESSION, timestamp=ts(10),
                       input_tokens=10, output_tokens=1),
        user_line("Request two.", uuid="p2", session_id=SESSION, timestamp=ts(20)),
        assistant_line(message_id="m2", session_id=SESSION, timestamp=ts(30),
                       input_tokens=20, output_tokens=2,
                       uuid="aaaaaaaa-0000-4000-8000-000000000002"),
    ])

    scan(store, projects_root, full=True)

    links = store.conn.execute(
        "SELECT message_id, COUNT(*) AS n FROM turn_messages GROUP BY message_id"
    ).fetchall()
    assert links and all(r["n"] == 1 for r in links)

    message_total = store.conn.execute(
        "SELECT SUM(input_tokens + output_tokens) AS t FROM messages").fetchone()["t"]
    turn_total = store.conn.execute(
        "SELECT SUM(input_tokens + output_tokens) AS t FROM turns").fetchone()["t"]
    assert turn_total == message_total == 33


def test_a_synthetic_only_prompt_opens_no_turn(projects_root, store):
    """An `<ide_selection>` line is not a person typing, so there is no turn to open —
    and the assistant message that follows must not be stranded either."""
    write_transcript(projects_root, SESSION, [
        user_line("Refactor the parser.", uuid="p1", session_id=SESSION, timestamp=ts(0)),
        user_line("<ide_selection>The user selected lines 10-20 of foo.py</ide_selection>",
                  uuid="p-synthetic", session_id=SESSION, timestamp=ts(5)),
        assistant_line(message_id="m1", session_id=SESSION, timestamp=ts(10)),
    ])

    scan(store, projects_root, full=True)

    uuids = [r["prompt_uuid"] for r in store.conn.execute("SELECT prompt_uuid FROM turns")]
    assert uuids == ["p1"]
    assert store.count("turns") == 1
    linked = store.conn.execute("SELECT prompt_uuid FROM turn_messages").fetchall()
    assert [r["prompt_uuid"] for r in linked] == ["p1"]


def test_no_empty_turn_rows_are_created(projects_root, store):
    """Every turn in the store has at least one prompt behind it."""
    write_transcript(projects_root, SESSION, [
        user_line("[Image: screenshot.png]", uuid="p-image", session_id=SESSION,
                  timestamp=ts(0)),
        user_line("Base directory for this skill: /Users/x/.claude/skills/demo\n"
                  "Load these files first.", uuid="p-skill", session_id=SESSION,
                  timestamp=ts(5)),
        user_line("Make the toast dismissable.", uuid="p-real", session_id=SESSION,
                  timestamp=ts(10)),
        assistant_line(message_id="m1", session_id=SESSION, timestamp=ts(20)),
    ])

    scan(store, projects_root, full=True)

    assert store.count("turns") == 1
    assert store.count("turn_prompts") == 1
    row = store.conn.execute("SELECT prompt_uuid, prompt_count, prompt_words "
                             "FROM turns").fetchone()
    assert row["prompt_uuid"] == "p-real"
    assert row["prompt_count"] == 1
    assert row["prompt_words"] == 4


def test_turns_do_not_span_sessions(projects_root):
    """A prompt owns the replies in ITS session. Two sessions interleaved in time must not
    hand one session's tokens to the other's prompt."""
    root = projects_root
    write_transcript(root, "sess-a", [
        user_line("Session A request.", uuid="pa", session_id="sess-a", timestamp=ts(0)),
        assistant_line(message_id="ma", session_id="sess-a", timestamp=ts(30)),
    ])
    write_transcript(root, "sess-b", [
        user_line("Session B request.", uuid="pb", session_id="sess-b", timestamp=ts(10)),
        assistant_line(message_id="mb", session_id="sess-b", timestamp=ts(20)),
    ], project_dir="-tmp-other-project")

    records = []
    for path in sorted(root.rglob("*.jsonl")):
        records.extend(_parse(path, root))
    turns = {t.prompt.uuid: t.message_ids for t in assemble_turns(records)}

    assert turns == {"pa": ["ma"], "pb": ["mb"]}


def test_assistant_message_before_any_prompt_has_no_turn(projects_root):
    """A replayed prefix. Its tokens still count at the message level; it just has no turn
    — and it must not invent one."""
    path = write_transcript(projects_root, SESSION, [
        assistant_line(message_id="m-orphan", session_id=SESSION, timestamp=ts(0)),
        user_line("Now do the thing.", uuid="p1", session_id=SESSION, timestamp=ts(10)),
        assistant_line(message_id="m1", session_id=SESSION, timestamp=ts(20),
                       uuid="aaaaaaaa-0000-4000-8000-000000000002"),
    ])

    turns = assemble_turns(_parse(path, projects_root))

    assert len(turns) == 1
    assert turns[0].message_ids == ["m1"]


def test_tail_links_a_later_message_to_the_open_turn(projects_root, store):
    """The incremental path: the prompt arrived on an earlier tick and is not in this
    batch, so the message would otherwise belong to no turn and the turn would look free."""
    path = write_transcript(projects_root, SESSION, [
        user_line("Start the long job.", uuid="p1", session_id=SESSION, timestamp=ts(0)),
    ])
    scan(store, projects_root)
    assert store.count("turns") == 1

    with path.open("a", encoding="utf-8") as fh:
        fh.write(assistant_line(message_id="m-late", session_id=SESSION, timestamp=ts(60),
                                input_tokens=9, output_tokens=1) + "\n")
    result = scan(store, projects_root)

    assert result.linked_orphans == 1
    row = store.conn.execute("SELECT input_tokens, output_tokens, message_count "
                             "FROM turns WHERE prompt_uuid='p1'").fetchone()
    assert (row["input_tokens"], row["output_tokens"], row["message_count"]) == (9, 1, 1)
