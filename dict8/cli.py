"""`dict8` command line — Phase 1 surface: backfill, usage, tail, reconcile.
Plus `quota`, the manual weekly check-in (Phase 2's calibration ground truth).
Plus the classifier (prompts/classify.md), pulled forward from Phase 5 (Denis, 2026-09-15).

Invariant 6: no USD figure is printed by any command. `claude-tokens` emits a
`cost_usd_estimate` field; `reconcile` reads its token columns and drops that one on the
floor. Denis is on a subscription — dollars are not the unit and never become it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dict8 import config as config_mod
from dict8.usage import quota as quota_mod
from dict8.usage.parser import read_prompt_text
from dict8.usage.reader import scan
from dict8.usage.store import TOKEN_COLUMNS, Store

# claude-tokens' JSON column names -> ours. Its `cost_usd_estimate` is deliberately absent.
ORACLE_COLUMNS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_create": "cache_creation_tokens",
    "cache_read": "cache_read_tokens",
}


def _fmt(n: int) -> str:
    return f"{n:,}"


def _open_store(cfg) -> Store:
    return Store(cfg.path("paths.db"))


def _projects_root(cfg) -> Path:
    """Resolve the transcript root from `paths.claude_projects_glob`.

    The config value is a glob so it stays readable and matches the documented layout; the
    scanner needs the directory it is anchored at.
    """
    raw = str(cfg.require("paths.claude_projects_glob"))
    root = Path(raw).expanduser()
    while any(ch in root.name for ch in "*?[") and root.parent != root:
        root = root.parent
    return root


def _tz_for(cfg):
    """The IANA zone Dict8 buckets days in."""
    return ZoneInfo(str(cfg.require("usage_oracle.tz_env")))


# ---- commands -------------------------------------------------------------------


def cmd_backfill(args, cfg) -> int:
    root = Path(args.root).expanduser() if args.root else _projects_root(cfg)
    with _open_store(cfg) as store:
        print(f"backfilling from {root} …")
        result = scan(store, root, full=True)
        print(result.render())
        print(f"\nmessages in db  : {_fmt(store.count('messages'))}")
        print(f"turns in db     : {_fmt(store.count('turns'))}")
    return 1 if result.errors else 0


def cmd_usage(args, cfg) -> int:
    with _open_store(cfg) as store:
        tz = _tz_for(cfg)
        rows = store.by_day(tz)
        if args.days:
            cutoff = (datetime.now(tz).date() - timedelta(days=args.days - 1)).isoformat()
            rows = [r for r in rows if r["day"] >= cutoff]

        if args.json:
            totals = {c: sum(r[c] for r in rows) for c in TOKEN_COLUMNS}
            print(json.dumps({
                "tz": str(tz), "group": "day", "rows": rows,
                "totals": {**totals, "total_tokens": sum(totals.values()),
                           "messages": sum(r["messages"] for r in rows)},
            }, indent=2))
            return 0

        if not rows:
            print("No usage recorded yet. Run `dict8 backfill` first.")
            return 0

        header = f"{'day':<12}{'msgs':>7}{'input':>14}{'output':>12}{'cache w':>14}{'cache r':>16}{'total':>16}"
        print(header)
        print("-" * len(header))
        for r in rows:
            total = sum(r[c] for c in TOKEN_COLUMNS)
            print(f"{r['day']:<12}{r['messages']:>7}{_fmt(r['input_tokens']):>14}"
                  f"{_fmt(r['output_tokens']):>12}{_fmt(r['cache_creation_tokens']):>14}"
                  f"{_fmt(r['cache_read_tokens']):>16}{_fmt(total):>16}")
        print("-" * len(header))
        tot = {c: sum(row[c] for row in rows) for c in TOKEN_COLUMNS}
        grand = sum(tot.values())
        print(f"{'TOTAL':<12}{sum(r['messages'] for r in rows):>7}"
              f"{_fmt(tot['input_tokens']):>14}{_fmt(tot['output_tokens']):>12}"
              f"{_fmt(tot['cache_creation_tokens']):>14}{_fmt(tot['cache_read_tokens']):>16}"
              f"{_fmt(grand):>16}")
        print(f"\nturns recorded  : {_fmt(store.count('turns'))}  "
              f"(tokens only; no prompt text stored — privacy.store_transcripts=features_only)")
    return 0


def cmd_tail(args, cfg) -> int:
    root = Path(args.root).expanduser() if args.root else _projects_root(cfg)
    interval = args.interval
    with _open_store(cfg) as store:
        if args.once:
            result = scan(store, root)
            print(result.render())
            return 0
        print(f"tailing {root} every {interval}s — Ctrl-C to stop")
        try:
            while True:
                result = scan(store, root)
                if result.new_messages or result.new_turns or result.linked_orphans:
                    stamp = datetime.now().strftime("%H:%M:%S")
                    print(f"[{stamp}] +{result.new_messages} msg  +{result.new_turns} turn  "
                          f"+{result.linked_orphans} linked  "
                          f"({result.files_read} file(s), {result.elapsed_s:.2f}s)")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


def cmd_reconcile(args, cfg) -> int:
    """Gate check: our backfill vs the `claude-tokens` oracle, same window, side by side."""
    iana = str(cfg.require("usage_oracle.tz_env"))
    tz = ZoneInfo(iana)

    # claude-tokens 0.2.1 has no IANA tz database — parse_tz() accepts only UTC,
    # Asia/Shanghai, or a fixed +N/-N offset. `Europe/Bucharest` is rejected outright, so
    # the gate command as literally written in prompts/build.md cannot run. Both sides are
    # therefore bucketed with the *same fixed offset*, derived from the real zone at the
    # start of the window, which keeps the comparison honest. A window spanning a DST
    # change would misbucket on the oracle's side; --days is kept short for that reason.
    anchor = datetime.now(tz) - timedelta(days=args.days - 1)
    offset = anchor.utcoffset() or timedelta(0)
    hours = int(offset.total_seconds() // 3600)
    minutes = int((offset.total_seconds() % 3600) // 60)
    oracle_tz = f"{'+' if hours >= 0 else '-'}{abs(hours):02d}:{minutes:02d}"
    fixed = timezone(offset)

    # Default to SETTLED days only. Today's file is still being appended to, so the oracle
    # and our store read it microseconds apart and legitimately disagree — a difference
    # that says nothing about the parser. Measured: today showed 1.98% mid-session and
    # 0.000% once both sides saw the same bytes. Excluding today makes the check mean
    # "do we parse the same data the same way", which is what the gate is actually asking.
    date_to = datetime.now(fixed).date()
    if not args.include_today:
        date_to -= timedelta(days=1)
    date_from = date_to - timedelta(days=args.days - 1)

    env = {**os.environ, "CLAUDE_TOKENS_TZ": oracle_tz}
    cmd = ["claude-tokens", "--json", "--group", "day",
           "--from", date_from.isoformat(), "--to", date_to.isoformat(),
           "--tz", oracle_tz]
    print(f"oracle : {' '.join(cmd)}")
    print(f"zone   : {iana} resolved to fixed {oracle_tz} for the window "
          f"({date_from} .. {date_to})")
    print(f"window : {'includes today (live, drift expected)' if args.include_today else 'settled days only — today is still being written'}\n")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)
    except FileNotFoundError:
        print("claude-tokens not installed — `uv tool install claude-tokens==0.2.1`", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"claude-tokens failed:\n{exc.stderr}", file=sys.stderr)
        return 2

    oracle_rows = {r["day"]: r for r in json.loads(proc.stdout)["rows"]}

    with _open_store(cfg) as store:
        ours_all = store.by_day(fixed)
    ours = {r["day"]: r for r in ours_all
            if date_from.isoformat() <= r["day"] <= date_to.isoformat()}

    days = sorted(set(oracle_rows) | set(ours))
    if not days:
        print("no overlapping data in window")
        return 1

    cols = list(ORACLE_COLUMNS.items())
    header = f"{'day':<12} {'source':<8}" + "".join(f"{o:>15}" for o, _ in cols) + f"{'total':>16}"
    print(header)
    print("-" * len(header))

    worst = 0.0
    sums = {"oracle": dict.fromkeys(ORACLE_COLUMNS, 0), "dict8": dict.fromkeys(ORACLE_COLUMNS, 0)}
    for day in days:
        o = oracle_rows.get(day, {})
        d = ours.get(day, {})
        o_vals = {k: int(o.get(k) or 0) for k in ORACLE_COLUMNS}
        d_vals = {k: int(d.get(mine) or 0) for k, mine in ORACLE_COLUMNS.items()}
        for k in ORACLE_COLUMNS:
            sums["oracle"][k] += o_vals[k]
            sums["dict8"][k] += d_vals[k]
        print(f"{day:<12} {'oracle':<8}" + "".join(f"{_fmt(o_vals[k]):>15}" for k in ORACLE_COLUMNS)
              + f"{_fmt(sum(o_vals.values())):>16}")
        print(f"{'':<12} {'dict8':<8}" + "".join(f"{_fmt(d_vals[k]):>15}" for k in ORACLE_COLUMNS)
              + f"{_fmt(sum(d_vals.values())):>16}")
        ot, dt = sum(o_vals.values()), sum(d_vals.values())
        if ot or dt:
            delta = abs(dt - ot) / max(ot, 1) * 100
            worst = max(worst, delta)
            flag = "" if delta <= args.tolerance else "   <-- OVER TOLERANCE"
            print(f"{'':<12} {'delta':<8}{delta:>14.3f}%{flag}")
    print("-" * len(header))

    o_tot, d_tot = sum(sums["oracle"].values()), sum(sums["dict8"].values())
    print(f"{'WINDOW':<12} {'oracle':<8}" + "".join(f"{_fmt(sums['oracle'][k]):>15}" for k in ORACLE_COLUMNS) + f"{_fmt(o_tot):>16}")
    print(f"{'':<12} {'dict8':<8}" + "".join(f"{_fmt(sums['dict8'][k]):>15}" for k in ORACLE_COLUMNS) + f"{_fmt(d_tot):>16}")

    per_col_ok = True
    for k in ORACLE_COLUMNS:
        ov, dv = sums["oracle"][k], sums["dict8"][k]
        delta = abs(dv - ov) / max(ov, 1) * 100
        status = "ok" if delta <= args.tolerance else "FAIL"
        if status == "FAIL":
            per_col_ok = False
        print(f"  {k:<22} oracle={_fmt(ov):>14}  dict8={_fmt(dv):>14}  delta={delta:6.3f}%  {status}")

    total_delta = abs(d_tot - o_tot) / max(o_tot, 1) * 100
    print(f"\nworst per-day delta : {worst:.3f}%")
    print(f"window total delta  : {total_delta:.3f}%  (tolerance {args.tolerance}%)")
    ok = per_col_ok and total_delta <= args.tolerance
    print("RECONCILE: " + ("PASS" if ok else "FAIL — a gap this size is a parser bug, not rounding"))
    return 0 if ok else 1


def cmd_quota(args, cfg) -> int:
    """Record or show the manual weekly-quota check-in.

    The DB is the store of record, not `quota.last_reading` in config.yml: readings
    accumulate, config holds decisions. `dict8 usage` deliberately says nothing about
    quota — mixing a typed-in percentage into a table of counted tokens would blur which
    figures were read and which were measured.
    """
    # `--at` says when a reading was taken, so it only means anything while recording one.
    # Accepting it in read mode and ignoring it silently would answer a question the user
    # did not ask (the current reading) as if it were the one they did (the reading as of
    # that time) — a wrong answer wearing a right one's clothes.
    if args.pct is None and args.at is not None:
        print(f"quota: --at {args.at} only applies when recording a reading "
              "(`dict8 quota <pct> --at ...`). `dict8 quota` always shows the latest "
              "reading; there is no as-of lookup. Nothing was read or stored.",
              file=sys.stderr)
        return 2

    with _open_store(cfg) as store:
        now = datetime.now(timezone.utc)
        if args.pct is not None:
            try:
                pct = quota_mod.parse_pct(args.pct)
                taken_at = quota_mod.parse_taken_at(args.at, now)
            except quota_mod.QuotaError as exc:
                print(f"quota: {exc}", file=sys.stderr)
                return 2
            # source comes from config and is required, not defaulted here: if it were
            # ever TBD, a row claiming "manual" would assert a provenance nobody chose.
            reading = quota_mod.record(store, pct, str(cfg.require("quota.source")),
                                       taken_at, now=now)
            print(f"stored reading #{reading.row_id} in {store.path}\n")
        else:
            reading = quota_mod.latest(store)
            if reading is None:
                print(quota_mod.NO_READING)
                return 0
        for line in quota_mod.describe(reading, cfg.get("quota.stale_after_hours"), now):
            print(line)
    return 0


def cmd_classify_backfill(args, cfg) -> int:
    """Classify every turn with no task_type yet.

    Reads prompt text transiently from Claude Code's own on-disk transcripts (never from
    Dict8's store, which has no text column) and writes back only the resulting bucket —
    see dict8.usage.parser.read_prompt_text and dict8.advise.classifier.
    """
    from dict8.advise.classifier import Classifier

    with _open_store(cfg) as store:
        work = store.unclassified_turns()
        if args.limit:
            work = work[: args.limit]
        if not work:
            print("nothing to classify")
            return 0

        print(f"loading {cfg.get('classifier.model')} …")
        clf = Classifier(cfg)
        t_load0 = time.monotonic()
        clf.warm()  # pay the model-load cost once, up front, not on turn 1
        print(f"loaded in {time.monotonic() - t_load0:.1f}s\n")

        counts: dict[str, int] = {}
        omitted = 0
        latencies = []
        for i, (turn, locs) in enumerate(work, 1):
            parts = [read_prompt_text(Path(f), n) for f, n in locs]
            text = "\n".join(p for p in parts if p)
            if not text.strip():
                omitted += 1
                continue
            result = clf.classify(text)
            if result is None:
                omitted += 1
                continue
            store.set_task_type(turn["prompt_uuid"], result.bucket, result.confidence, clf.model_id)
            counts[result.bucket] = counts.get(result.bucket, 0) + 1
            latencies.append(result.latency_ms)
            if i % 25 == 0 or i == len(work):
                print(f"  {i}/{len(work)}")
        clf.close()

    print(f"\nclassified : {sum(counts.values())}")
    print(f"omitted    : {omitted}  (empty text, timeout, or parse failure — fail-open)")
    for bucket, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {bucket:<14} {n}")
    if latencies:
        latencies.sort()
        print(f"\nlatency: min={latencies[0]:.0f}ms median={latencies[len(latencies)//2]:.0f}ms "
              f"max={latencies[-1]:.0f}ms  (budget: classifier.timeout_ms="
              f"{cfg.get('classifier.timeout_ms')}ms)")
    return 0


# ---- entry point ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dict8", description="Dict8 — usage layer (Phase 1)")
    p.add_argument("--config", type=Path, help="override config.yml location")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("backfill", help="scan the full transcript history into SQLite")
    b.add_argument("--root", help="transcript root (default: from config paths.claude_projects_glob)")
    b.set_defaults(func=cmd_backfill)

    u = sub.add_parser("usage", help="per-day token totals (never USD)")
    u.add_argument("--days", type=int, help="limit to the last N days")
    u.add_argument("--json", action="store_true")
    u.set_defaults(func=cmd_usage)

    t = sub.add_parser("tail", help="watch for new turns and record them incrementally")
    t.add_argument("--root")
    t.add_argument("--interval", type=float, default=2.0, help="seconds between ticks")
    t.add_argument("--once", action="store_true", help="single tick, then exit")
    t.set_defaults(func=cmd_tail)

    r = sub.add_parser("reconcile", help="cross-check our totals against claude-tokens")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--tolerance", type=float, default=0.5, help="max %% delta to pass")
    r.add_argument("--include-today", action="store_true",
                   help="include the still-being-written current day (expect drift)")
    r.set_defaults(func=cmd_reconcile)

    q = sub.add_parser("quota", help="record or show the manual weekly-quota check-in")
    q.add_argument("pct", nargs="?",
                   help="weekly quota percent from Claude Code's /usage; omit to show the "
                        "last reading with its source and age")
    q.add_argument("--at", metavar="ISO8601",
                   help="when the reading was taken, if not now (naive = this machine's "
                        "local time). For a check-in typed in after the fact.")
    q.set_defaults(func=cmd_quota)

    cb = sub.add_parser("classify-backfill", help="fill task_type on unclassified turns")
    cb.add_argument("--limit", type=int, help="classify at most N turns (oldest first)")
    cb.set_defaults(func=cmd_classify_backfill)

    args = p.parse_args(argv)
    cfg = config_mod.load(args.config)
    try:
        return args.func(args, cfg)
    except config_mod.ConfigGap as exc:
        print(f"config gap: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
