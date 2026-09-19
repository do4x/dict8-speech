"""Scanning: full backfill and incremental tail, one code path.

A backfill is just a scan with the cursors cleared, so there is no second parser to keep in
sync with the first — the gate requires backfill and tail to agree, and the cheapest way to
guarantee that is for them to be the same code.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from dict8.usage.parser import AssistantMessage, HumanPrompt, assemble_turns, parse_file
from dict8.usage.store import Store


@dataclass
class ScanResult:
    files_seen: int = 0
    files_read: int = 0
    lines_parsed: int = 0
    new_messages: int = 0
    new_turns: int = 0
    linked_orphans: int = 0
    elapsed_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        out = [
            f"files seen      : {self.files_seen}",
            f"files read      : {self.files_read}",
            f"records parsed  : {self.lines_parsed}",
            f"new messages    : {self.new_messages}",
            f"new turns       : {self.new_turns}",
            f"orphans linked  : {self.linked_orphans}",
            f"elapsed         : {self.elapsed_s:.2f}s",
        ]
        if self.errors:
            out.append(f"errors          : {len(self.errors)}")
            out.extend(f"  - {e}" for e in self.errors[:10])
        return "\n".join(out)


def scan(store: Store, root: Path, *, full: bool = False) -> ScanResult:
    """Scan every `*.jsonl` under `root`, reading only bytes not already consumed.

    `rglob` rather than a flat glob: this Claude Code version keeps subagent turns inline
    (docs/verified-schemas.md §1), but the documented subagent subdirectory layout would be
    picked up without a config change if a future version starts writing it.
    """
    started = time.monotonic()
    result = ScanResult()
    root = Path(root).expanduser()

    if full:
        store.clear_cursors()

    if not root.is_dir():
        result.errors.append(f"{root} is not a directory")
        result.elapsed_s = time.monotonic() - started
        return result

    for path in sorted(root.rglob("*.jsonl")):
        result.files_seen += 1
        try:
            st = path.stat()
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")
            continue

        offset = 0
        cursor = None if full else store.get_cursor(str(path))
        if cursor is not None:
            if st.st_size < cursor["size"]:
                # Truncated or rewritten under us — the offset means nothing now, re-read.
                offset = 0
            elif st.st_size == cursor["size"] and st.st_mtime == cursor["mtime"]:
                continue  # nothing appended since last tick
            else:
                offset = cursor["offset"]

        messages: list[AssistantMessage] = []
        prompts: list[HumanPrompt] = []
        final_offset = offset
        try:
            for rec, after in parse_file(path, root, start_offset=offset):
                final_offset = after
                result.lines_parsed += 1
                if isinstance(rec, AssistantMessage):
                    messages.append(rec)
                else:
                    prompts.append(rec)
        except OSError as exc:
            result.errors.append(f"{path}: {exc}")
            continue

        result.files_read += 1

        if messages or prompts:
            batch = [*prompts, *messages]
            turns = assemble_turns(batch)
            result.new_messages += store.upsert_messages(messages)
            result.new_turns += store.upsert_turns(turns)
            # Assistant messages whose prompt arrived on an earlier tick have no turn in
            # this batch; attach them to the turn already open in their session.
            claimed = {mid for t in turns for mid in t.message_ids}
            orphans = [m for m in messages if m.message_id not in claimed]
            if orphans:
                result.linked_orphans += store.link_messages_to_open_turns(orphans)

        store.set_cursor(str(path), final_offset, st.st_size, st.st_mtime)

    # Only when something was read. The refresh is a write transaction over every turn;
    # the live meter scans every cli.tail_interval_s, and an idle tick has nothing to
    # refresh — taking the write lock anyway would only contend with the UserPromptSubmit
    # hook, which shares the file under a hooks.timeout_ms watchdog.
    if full or result.files_read:
        store.refresh_turn_aggregates()
    result.elapsed_s = time.monotonic() - started
    return result
