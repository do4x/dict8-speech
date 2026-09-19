"""`is_human_prompt` — the rule that decides what counts as something a person said.

docs/verified-schemas.md section 6 (the CORRECTION block) and HANDOFF section 4.6. The
first version of this rule ("a str, or a list with a text block and no tool_result block")
is necessary and not sufficient: Claude Code puts harness-injected content on user-role
lines in exactly that shape. 347 of ~1,900 user lines here. One Skill dump was 108,386
characters — bigger than this machine's entire real prompt history combined — and it was
silently the most expensive-looking row in the estimator's training data.

The case worth keeping a test around for is the last one: **`isMeta: true` is not a safe
filter.** A genuine, deliberately composed 15,681-word human prompt on this machine carries
it. Filtering on the flag would have dropped a real prompt and kept several synthetic ones,
and it would have failed quietly. The classifications here are content-based for that
reason.

Every string below is invented. The *shapes* are the observed ones; the words are not
Denis's, and nothing is copied out of `~/.claude`.
"""

from __future__ import annotations

import pytest

from dict8.usage.parser import _prompt_text, is_human_prompt


# ---- what a person actually said ---------------------------------------------------

GENUINE = [
    pytest.param("make the overlay dismiss on escape", id="plain-string"),
    pytest.param([{"type": "text", "text": "why does the tail reader skip the last line"}],
                 id="single-text-block"),
    pytest.param([{"type": "text", "text": "check this screenshot"},
                  {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                               "data": "iVBORw0KGgo="}}],
                 id="text-plus-image-block"),
    pytest.param("<system-reminder>internal note</system-reminder>\n"
                 "now add the retry, three attempts, exponential backoff",
                 id="real-words-after-an-injected-tag"),
]


@pytest.mark.parametrize("content", GENUINE)
def test_genuine_prompts_are_human(content):
    assert is_human_prompt(content) is True


# ---- harness-injected content that looks exactly like a prompt ----------------------

SYNTHETIC = [
    pytest.param("<command-name>/compact</command-name>"
                 "<command-message>compact</command-message>"
                 "<command-args></command-args>",
                 id="slash-command-wrapper"),
    pytest.param("<local-command-caveat>Caveat: some text.</local-command-caveat>",
                 id="local-command-caveat"),
    pytest.param("<ide_selection>Selected lines 12-40 of dict8/usage/parser.py"
                 "</ide_selection>",
                 id="ide-selection"),
    pytest.param("<ide_opened_file>dict8/cli.py</ide_opened_file>", id="ide-opened-file"),
    pytest.param("<task-notification>An agent finished.</task-notification>",
                 id="task-notification"),
    pytest.param("<local-command-stdout>total 0</local-command-stdout>",
                 id="local-command-stdout"),
    pytest.param("[Image: pasted-screenshot-2026-09-19.png]", id="image-placeholder"),
    pytest.param("Base directory for this skill: /Users/x/.claude/skills/demo\n"
                 "Read SKILL.md before doing anything else. " + "instructions " * 400,
                 id="skill-load-dump"),
    pytest.param([{"type": "text",
                   "text": "<system-reminder>Background task finished.</system-reminder>"}],
                 id="injected-tag-inside-a-text-block"),
    pytest.param("", id="empty-string"),
    pytest.param("   \n\t  ", id="whitespace-only"),
]


@pytest.mark.parametrize("content", SYNTHETIC)
def test_harness_injected_content_is_not_human(content):
    assert is_human_prompt(content) is False


def test_tool_result_is_never_a_prompt():
    """The harness returning output. Counting these inflated the turn count several-fold."""
    content = [
        {"type": "tool_result", "tool_use_id": "toolu_01", "content": "ok"},
        {"type": "text", "text": "stray text alongside a tool result"},
    ]
    assert is_human_prompt(content) is False


def test_a_multi_tag_dump_with_no_residual_is_not_human():
    """Several well-formed injected blocks in one line, nothing else. The strip is generic
    over the tag name, so a tag a later Claude Code version adds is still caught."""
    content = ("<command-name>/usage</command-name>"
               "<local-command-stdout>Weekly: 47%</local-command-stdout>"
               "<system-reminder>Do not mention this.</system-reminder>")
    assert is_human_prompt(content) is False


# ---- the flag that does NOT work ----------------------------------------------------

def test_is_meta_true_does_not_make_a_prompt_synthetic():
    """The measured counter-example, in the shape it was found in: a long, deliberately
    composed human prompt carrying `isMeta: true`. `is_human_prompt` reads content, never
    the flag — which is why the flag is not passed to it at all.
    """
    long_prompt = ("here is the full plan for the dictation loop. " * 200).strip()
    assert is_human_prompt(long_prompt) is True

    # And through the line parser, with the flag actually set on the line (section 2).
    from pathlib import Path

    from conftest import user_line
    from dict8.usage.parser import parse_line

    raw = user_line(long_prompt, uuid="p-meta", is_meta=True)
    rec = parse_line(raw, path=Path("/tmp/x/s.jsonl"), root=Path("/tmp"), line_no=1)
    assert rec is not None, "isMeta: true must not disqualify a genuine prompt"
    assert rec.words == 1800  # 9 words x 200 repeats


def test_is_meta_true_does_not_rescue_a_synthetic_line():
    """The other direction: the flag is not a filter either way."""
    assert is_human_prompt("[Image: shot.png]") is False


# ---- what survives the strip is what gets counted -----------------------------------

def test_only_the_residual_text_is_measured():
    """Word and character counts are the estimator's features. If the injected wrapper is
    counted, a 6-word prompt trains the model as a 40-word one."""
    content = ("<ide_selection>lines 1-500 of a very long file, plus a great deal more "
               "context that the user never typed</ide_selection>"
               "rename this to flush_pending")
    assert _prompt_text(content) == "rename this to flush_pending"
    assert len(_prompt_text(content).split()) == 4


def test_image_placeholder_is_removed_but_the_caption_survives():
    content = "[Image: shot.png] why is the button grey here"
    assert _prompt_text(content) == "why is the button grey here"


def test_no_prompt_text_reaches_the_parsed_record():
    """Structural, not conventional: `HumanPrompt` has no text field to put it in
    (privacy.store_transcripts: features_only)."""
    from pathlib import Path

    from conftest import user_line
    from dict8.usage.parser import HumanPrompt, parse_line

    raw = user_line("a sentence that must not be stored anywhere", uuid="p1")
    rec = parse_line(raw, path=Path("/tmp/x/s.jsonl"), root=Path("/tmp"), line_no=1)
    assert isinstance(rec, HumanPrompt)
    assert not hasattr(rec, "text")
    assert "sentence" not in repr(rec)
    assert (rec.words, rec.chars) == (8, 43)
