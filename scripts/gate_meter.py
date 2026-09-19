"""U6 gate: the live meter agrees with `dict8 usage` for one finished session, and its
scans hold SQLite's write lock briefly.

    uv run scripts/gate_meter.py --session <session-uuid> [--chunks 12] [--live-ticks 10]

1. Replays the session's transcript file(s) into an EMPTY scratch store, appending them in
   `--chunks` pieces with one meter tick after each — the way a live session grows — and
   reads the meter's running session total (incremental, rowid deltas; dict8.usage.meter).
2. Runs `dict8 usage --session <id> --json` against a byte copy of the real store (so the
   real file is never written) and reads its total.
3. A third number straight from the JSONL: every assistant line's four usage fields,
   counted once per `message.id` (docs/verified-schemas.md sections 3-5) — no Dict8 parser.
PASS when all three agree exactly and are non-zero. Then `--live-ticks` meter ticks on a
copy of the real store against the real transcript root, at `cli.tail_interval_s`, and the
longest write transaction any tick held is printed (the UserPromptSubmit hook shares the
file under a `hooks.timeout_ms` watchdog).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dict8 import config as config_mod  # noqa: E402
from dict8.cli import _projects_root  # noqa: E402
from dict8.usage.meter import Meter  # noqa: E402
from dict8.usage.store import Store  # noqa: E402


def session_files(root: Path, sid: str) -> list[Path]:
    return sorted(p for p in root.rglob("*.jsonl")
                  if p.stem == sid or sid in p.relative_to(root).parts[:-1])


def raw_total(files: list[Path]) -> tuple[int, int]:
    seen: dict[str, int] = {}
    for f in files:
        for line in f.open(encoding="utf-8"):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = d.get("message") if d.get("type") == "assistant" else None
            if not isinstance(msg, dict) or not msg.get("id") or not isinstance(
                    msg.get("usage"), dict):
                continue
            u = msg["usage"]
            seen.setdefault(msg["id"], sum(int(u.get(k) or 0) for k in (
                "input_tokens", "output_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens")))
    return sum(seen.values()), len(seen)


def scratch_config(tmp: Path, db: Path) -> Path:
    data = yaml.safe_load(config_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    data["paths"]["db"] = str(db)
    data["paths"]["logs"] = str(tmp / "logs")
    p = tmp / "config.yml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--chunks", type=int, default=12)
    ap.add_argument("--live-ticks", type=int, default=0)
    args = ap.parse_args()
    cfg = config_mod.load()
    root = _projects_root(cfg)
    real_db = cfg.path("paths.db")
    files = session_files(root, args.session)
    ok = True
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        # 1. progressive replay into an empty store
        sroot = tmp / "projects"
        store = Store(tmp / "meter.sqlite")
        meter = Meter(store, sroot, cfg)
        lines = {f: f.read_bytes().splitlines(keepends=True) for f in files}
        dests = {f: sroot / f.relative_to(root) for f in files}
        for d in dests.values():
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_bytes(b"")
        ticks = 0
        for i in range(args.chunks):
            for f, ls in lines.items():
                lo, hi = len(ls) * i // args.chunks, len(ls) * (i + 1) // args.chunks
                with dests[f].open("ab") as out:
                    out.writelines(ls[lo:hi])
                os.utime(dests[f], None)
            st = meter.tick()
            ticks += 1
        meter_total = meter.totals.get(args.session)
        meter_msgs = store.count("messages")
        replay_tx = store.max_tx_ms
        store.close()
        # 2. dict8 usage on a copy of the real store
        db = tmp / "real-copy.sqlite"
        shutil.copy2(real_db, db)
        out = subprocess.run([sys.executable, "-m", "dict8.cli", "--config",
                              str(scratch_config(tmp, db)), "usage", "--session",
                              args.session, "--json"], capture_output=True, text=True,
                             cwd=REPO, check=True).stdout
        usage = json.loads(out)["totals"]
        # 3. straight from the JSONL
        raw, raw_ids = raw_total(files)
        print(f"session            : {args.session}")
        print(f"transcript files   : {len(files)} ({sum(len(v) for v in lines.values())} lines)")
        print(f"meter (replayed in {ticks} ticks, empty store): {meter_total:,} tokens, "
              f"{meter_msgs} messages")
        print(f"dict8 usage --session (copy of real store)  : {usage['total_tokens']:,} "
              f"tokens, {usage['messages']} messages")
        print(f"raw JSONL, once per message.id              : {raw:,} tokens, {raw_ids} ids")
        agree = meter_total == usage["total_tokens"] == raw and meter_msgs == usage[
            "messages"] == raw_ids
        nonzero = bool(meter_total) and bool(raw_ids)
        print(f"CHECK meter == usage == raw, non-zero       : "
              f"{'PASS' if agree and nonzero else 'FAIL'}")
        ok &= agree and nonzero
        print(f"longest write transaction during replay    : {replay_tx:.2f} ms")
        # 4. live ticks on a copy of the real store
        if args.live_ticks:
            live = tmp / "live.sqlite"
            shutil.copy2(real_db, live)
            s2 = Store(live)
            m2 = Meter(s2, root, cfg)
            interval = float(cfg.require("cli.tail_interval_s"))
            worst_scan, new = 0.0, 0
            for _ in range(args.live_ticks):
                st = m2.tick()
                worst_scan = max(worst_scan, st.scan_ms)
                new += st.new_messages
                time.sleep(interval)
            print(f"live: {args.live_ticks} ticks every {interval:g} s on the real root: "
                  f"{new} new messages, active session {st.session_id} "
                  f"{st.session_tokens:,} tokens, longest tick {worst_scan:.1f} ms, "
                  f"longest write transaction {s2.max_tx_ms:.2f} ms "
                  f"(hooks.timeout_ms = {cfg.require('hooks.timeout_ms')})")
            s2.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
