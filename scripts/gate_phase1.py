#!/usr/bin/env -S uv run --quiet --with pyyaml python
"""Phase 1 gate. Every check runs against real files and prints the number behind it.

prompts/build.md: "Never report a gate as passed on the basis of code you believe is
correct; pass it on the basis of output you ran."

The hand-check in check 3 deliberately re-parses the raw JSONL with a *separate* minimal
reader rather than reusing dict8.usage.parser — a cross-check that shares the parser it is
checking proves nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dict8 import config as config_mod
from dict8.usage.reader import scan
from dict8.usage.store import Store

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n       {detail}\n")


def main() -> int:
    cfg = config_mod.load()
    db_path = cfg.path("paths.db")
    root = Path(str(cfg.require("paths.claude_projects_glob"))).expanduser()
    while any(ch in root.name for ch in "*?[") and root.parent != root:
        root = root.parent

    print(f"db   : {db_path}\nroot : {root}\n" + "=" * 78 + "\n")
    store = Store(db_path)

    # -- 1. schemas pinned with the observed Claude Code version ------------------
    doc = Path(__file__).resolve().parent.parent / "docs" / "verified-schemas.md"
    cc_version = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()
    text = doc.read_text() if doc.exists() else ""
    versions_in_db = [r[0] for r in store.conn.execute(
        "SELECT DISTINCT cc_version FROM messages WHERE cc_version IS NOT NULL")]
    pinned = [v for v in versions_in_db if v in text]
    writing = store.conn.execute(
        "SELECT cc_version FROM messages ORDER BY ts DESC LIMIT 1").fetchone()
    writing_version = writing[0] if writing else None
    record(
        "schemas pinned with observed Claude Code version",
        doc.exists() and len(pinned) == len(versions_in_db) and bool(versions_in_db),
        f"{doc.name} exists={doc.exists()}; version actually WRITING transcripts="
        f"{writing_version!r}; `claude --version` on PATH={cc_version!r} (differs: the "
        f"editor extension bundles its own); versions in corpus={sorted(versions_in_db)}; "
        f"all documented={len(pinned)}/{len(versions_in_db)}",
    )

    # -- 2. reconciliation vs claude-tokens ---------------------------------------
    # Scan first so both sides start from the same bytes; the window still excludes today.
    scan(store, root)
    proc = subprocess.run(
        [sys.executable, "-m", "dict8.cli", "reconcile", "--days", "7"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
    )
    tail_line = [l for l in proc.stdout.splitlines() if l.startswith("window total delta")]
    verdict = [l for l in proc.stdout.splitlines() if l.startswith("RECONCILE:")]
    record(
        "backfill agrees with claude-tokens within 0.5%",
        bool(verdict) and "PASS" in verdict[0],
        (tail_line[0] if tail_line else "no delta line") + " | " + (verdict[0] if verdict else "no verdict"),
    )

    # -- 3. ten hand-checked messages, field by field -----------------------------
    sample = store.conn.execute(
        "SELECT * FROM messages ORDER BY ts DESC LIMIT 10").fetchall()
    mismatches: list[str] = []
    checked = 0
    for row in sample:
        raw = None
        with open(row["src_file"], encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (d.get("message") or {}).get("id") == row["message_id"]:
                    raw = d
                    break
        if raw is None:
            mismatches.append(f"{row['message_id']}: not found in {row['src_file']}")
            continue
        checked += 1
        m, u = raw["message"], raw["message"]["usage"]
        expect = {
            "session_id": raw.get("sessionId"),
            "model": m.get("model"),
            "input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
            "cache_creation_tokens": int(u.get("cache_creation_input_tokens") or 0),
            "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
            "is_sidechain": int(bool(raw.get("isSidechain"))),
            "cc_version": raw.get("version"),
            "cwd": raw.get("cwd"),
        }
        for field, want in expect.items():
            if row[field] != want:
                mismatches.append(f"{row['message_id']}.{field}: db={row[field]!r} file={want!r}")
        want_ts = datetime.fromisoformat(raw["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc)
        if datetime.fromisoformat(row["ts"]) != want_ts:
            mismatches.append(f"{row['message_id']}.ts: db={row['ts']} file={want_ts.isoformat()}")
    record(
        "10 hand-checked messages match the raw JSONL field by field",
        checked == 10 and not mismatches,
        f"{checked} messages re-parsed independently, 10 fields each "
        f"({checked * 10} comparisons); mismatches={mismatches or 'none'}",
    )

    # -- 4. a duplicate message.id across files is counted once -------------------
    occurrences: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path in root.rglob("*.jsonl"):
        try:
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                m = d.get("message")
                if isinstance(m, dict) and m.get("usage") and m.get("id"):
                    occurrences[m["id"]].append((str(path), d.get("timestamp", "")))

    cross_file = {mid: occ for mid, occ in occurrences.items()
                  if len({p for p, _ in occ}) > 1}
    detail_lines = []
    dup_ok = bool(cross_file)
    for mid, occ in list(cross_file.items())[:3]:
        n = store.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE message_id = ?", (mid,)).fetchone()[0]
        winner = store.conn.execute(
            "SELECT src_file, ts FROM messages WHERE message_id = ?", (mid,)).fetchone()
        earliest = min(occ, key=lambda x: (x[1], x[0]))
        deterministic = winner is not None and winner["src_file"] == earliest[0]
        if n != 1 or not deterministic:
            dup_ok = False
        detail_lines.append(
            f"{mid} in {len({p for p, _ in occ})} files -> {n} db row(s), "
            f"winner={'earliest-ts copy' if deterministic else 'WRONG COPY'}")

    raw_total = sum(
        1 for occ in occurrences.values() for _ in occ)
    uniq_total = len(occurrences)
    record(
        "duplicate message.id across files counted exactly once",
        dup_ok,
        f"{len(cross_file)} ids span >1 file; {sum(1 for o in occurrences.values() if len(o) > 1)} "
        f"ids duplicated at all; {raw_total} occurrences -> {uniq_total} unique "
        f"(naive over-report +{(raw_total / max(uniq_total, 1) - 1) * 100:.1f}%)\n       "
        + "\n       ".join(detail_lines),
    )

    # -- 5. tail picks up new lines fast, without re-reading the whole file ------
    # Deterministic and built from REAL transcript bytes: a genuine session file is split,
    # the first part scanned, then the true remainder appended. Nothing here is synthesised
    # — it is the same lines Claude Code wrote, delivered in two instalments, which is
    # exactly what an append looks like to the reader. An earlier revision of this check
    # measured whatever the live session happened to have written and passed on 0 bytes;
    # a check that can pass without exercising anything is not a check.
    import shutil
    import tempfile

    donor = max(
        (p for p in root.rglob("*.jsonl") if p.stat().st_size > 200_000),
        key=lambda p: p.stat().st_size,
    )
    donor_lines = donor.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    split_at = int(len(donor_lines) * 0.6)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        proj = tmp_root / donor.parent.name
        proj.mkdir(parents=True)
        target = proj / donor.name

        target.write_text("".join(donor_lines[:split_at]), encoding="utf-8")
        tmp_db = tmp_root / "gate.sqlite"
        tstore = Store(tmp_db)
        first = scan(tstore, tmp_root, full=True)
        size_after_first = target.stat().st_size
        cur1 = tstore.get_cursor(str(target))
        offset1 = cur1["offset"] if cur1 else 0

        with target.open("a", encoding="utf-8") as fh:
            fh.write("".join(donor_lines[split_at:]))
        size_after_append = target.stat().st_size
        appended = size_after_append - size_after_first

        t0 = time.monotonic()
        second = scan(tstore, tmp_root)
        elapsed = time.monotonic() - t0

        cur2 = tstore.get_cursor(str(target))
        offset2 = cur2["offset"] if cur2 else 0
        read_bytes = offset2 - offset1
        tstore.close()

    picked_up = second.new_messages > 0
    incremental = read_bytes <= appended and offset1 > 0
    record(
        "tail picks up a live session within 5 s without re-reading the whole file",
        picked_up and incremental and elapsed < 5.0,
        f"donor {donor.name} ({len(donor_lines)} real lines) split {split_at}/"
        f"{len(donor_lines) - split_at}; first pass ingested {first.new_messages} msgs and "
        f"left the cursor at {offset1:,} B; appended {appended:,} B of real lines; next tick "
        f"ingested {second.new_messages} new msgs in {elapsed:.3f}s reading {read_bytes:,} B "
        f"({read_bytes / size_after_append * 100:.1f}% of the {size_after_append:,} B file, "
        f"not {size_after_append:,} B)",
    )

    # -- 5b. a *concurrent writer* appending while the reader polls -------------
    # What "live session" means mechanically is: another process is appending to the file
    # while we read it. The real Claude Code transcript only flushes at turn boundaries, so
    # it cannot be observed growing inside a single tool call — but the property can be
    # measured honestly by having a separate process append real transcript lines on a
    # timer while the tail loop runs. Real bytes, real second process, real polling.
    import subprocess as sp
    import textwrap

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        proj = tmp_root / "-live-test"
        proj.mkdir(parents=True)
        target = proj / "live-session.jsonl"
        head, rest = donor_lines[:200], donor_lines[200:500]
        target.write_text("".join(head), encoding="utf-8")

        tstore = Store(tmp_root / "live.sqlite")
        scan(tstore, tmp_root, full=True)

        chunks = [rest[i::4] for i in range(4)]
        payload = tmp_root / "chunks.json"
        payload.write_text(json.dumps(["".join(c) for c in chunks]))

        writer = sp.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import json, time
            chunks = json.load(open({str(payload)!r}))
            for c in chunks:
                time.sleep(0.6)
                with open({str(target)!r}, "a", encoding="utf-8") as fh:
                    fh.write(c); fh.flush()
        """)])

        latencies, seen_total = [], 0
        deadline = time.monotonic() + 12.0
        last_size = target.stat().st_size
        pending_since = None
        while time.monotonic() < deadline:
            size_now = target.stat().st_size
            if size_now > last_size and pending_since is None:
                pending_since = time.monotonic()
            r = scan(tstore, tmp_root)
            if r.new_messages:
                seen_total += r.new_messages
                if pending_since is not None:
                    latencies.append(time.monotonic() - pending_since)
                    pending_since = None
                last_size = size_now
            time.sleep(0.2)
            if writer.poll() is not None and target.stat().st_size == last_size and pending_since is None:
                break
        writer.wait(timeout=5)
        scan(tstore, tmp_root)
        tstore.close()

    worst = max(latencies) if latencies else None
    record(
        "picks up a file a SEPARATE PROCESS is appending to, within 5 s",
        bool(latencies) and worst is not None and worst < 5.0 and seen_total > 0,
        f"writer process appended {len(chunks)} batches of real transcript lines on a 0.6 s "
        f"timer; reader ingested {seen_total} new messages across {len(latencies)} pickups; "
        f"pickup latency max={worst:.3f}s median="
        f"{sorted(latencies)[len(latencies)//2]:.3f}s (budget 5 s)"
        if latencies else "no appends were observed — nothing measured",
    )

    # -- 6. the reported totals equal the data ----------------------------------
    # Added after `dict8 usage` shipped a TOTAL row that printed the same figure in all
    # four columns: a dict comprehension whose inner generator shadowed its key variable.
    # Every per-day row was right and the total was nonsense, which is precisely the
    # "a wrong number is worse than no number" failure — so it gets a check.
    out = subprocess.run(
        [sys.executable, "-m", "dict8.cli", "usage", "--json"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
    )
    payload = json.loads(out.stdout)
    row_sums = {c: sum(r[c] for r in payload["rows"]) for c in
                ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")}
    reported = payload["totals"]
    db_tot = store.totals()
    col_ok = all(reported[c] == row_sums[c] == db_tot[c] for c in row_sums)
    record(
        "reported totals equal the sum of the rows and the table",
        col_ok and reported["messages"] == db_tot["messages"],
        "; ".join(f"{c}: reported={reported[c]:,} rows={row_sums[c]:,} db={db_tot[c]:,}"
                  for c in row_sums)
        + f"; messages reported={reported['messages']:,} db={db_tot['messages']:,}",
    )

    # -- self-check items that are cheap to assert --------------------------------
    cols = [r[1] for r in store.conn.execute("PRAGMA table_info(turns)")]
    mcols = [r[1] for r in store.conn.execute("PRAGMA table_info(messages)")]
    bad = [c for c in cols + mcols if "usd" in c.lower() or "cost" in c.lower() or "text" in c.lower()]
    record(
        "no USD column and no prompt-text column anywhere in the schema",
        not bad,
        f"turns={len(cols)} cols, messages={len(mcols)} cols; offending={bad or 'none'} "
        f"(privacy.store_transcripts={cfg.get('privacy.store_transcripts')!r})",
    )

    store.close()

    print("=" * 78)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\nPhase 1 gate: {passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
