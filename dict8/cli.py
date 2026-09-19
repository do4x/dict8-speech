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
from dict8 import logs
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
    # `--interval` overrides; the default lives in config.yml, not in argparse (invariant 3).
    interval = (args.interval if args.interval is not None
                else float(cfg.require("cli.tail_interval_s")))
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
    # Both windows come from config.yml unless a flag says otherwise (invariant 3):
    # `cli.reconcile_days` and `cli.reconcile_tolerance_pct`, the latter being
    # prompts/build.md's Phase 1 gate figure rather than a number chosen here.
    days_window = args.days if args.days is not None else int(cfg.require("cli.reconcile_days"))
    tolerance = (args.tolerance if args.tolerance is not None
                 else float(cfg.require("cli.reconcile_tolerance_pct")))

    # claude-tokens 0.2.1 has no IANA tz database — parse_tz() accepts only UTC,
    # Asia/Shanghai, or a fixed +N/-N offset. `Europe/Bucharest` is rejected outright, so
    # the gate command as literally written in prompts/build.md cannot run. Both sides are
    # therefore bucketed with the *same fixed offset*, derived from the real zone at the
    # start of the window, which keeps the comparison honest. A window spanning a DST
    # change would misbucket on the oracle's side; --days is kept short for that reason.
    anchor = datetime.now(tz) - timedelta(days=days_window - 1)
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
    date_from = date_to - timedelta(days=days_window - 1)

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
            flag = "" if delta <= tolerance else "   <-- OVER TOLERANCE"
            print(f"{'':<12} {'delta':<8}{delta:>14.3f}%{flag}")
    print("-" * len(header))

    o_tot, d_tot = sum(sums["oracle"].values()), sum(sums["dict8"].values())
    print(f"{'WINDOW':<12} {'oracle':<8}" + "".join(f"{_fmt(sums['oracle'][k]):>15}" for k in ORACLE_COLUMNS) + f"{_fmt(o_tot):>16}")
    print(f"{'':<12} {'dict8':<8}" + "".join(f"{_fmt(sums['dict8'][k]):>15}" for k in ORACLE_COLUMNS) + f"{_fmt(d_tot):>16}")

    per_col_ok = True
    for k in ORACLE_COLUMNS:
        ov, dv = sums["oracle"][k], sums["dict8"][k]
        delta = abs(dv - ov) / max(ov, 1) * 100
        status = "ok" if delta <= tolerance else "FAIL"
        if status == "FAIL":
            per_col_ok = False
        print(f"  {k:<22} oracle={_fmt(ov):>14}  dict8={_fmt(dv):>14}  delta={delta:6.3f}%  {status}")

    total_delta = abs(d_tot - o_tot) / max(o_tot, 1) * 100
    print(f"\nworst per-day delta : {worst:.3f}%")
    print(f"window total delta  : {total_delta:.3f}%  "
          f"(tolerance {tolerance}%, cli.reconcile_tolerance_pct)")
    ok = per_col_ok and total_delta <= tolerance
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


def cmd_estimate(args, cfg) -> int:
    """Estimate this turn's token cost as a range, or print the held-out comparison.

    Exit codes — note that 4 is an ANSWER, not a failure:
      0  a range was produced
      2  bad input (an unknown bucket name)
      3  a config gap (handled in main())
      4  out of distribution: the estimator declined to invent a range. U3's hook must
         treat 4 as a normal outcome and simply add no estimate to the prompt context;
         treating it as an error would turn "we don't know" into a logged crash.
    """
    from dict8.advise import estimator as est_mod

    with _open_store(cfg) as store:
        rows = est_mod.load_turns(store)
        gate = est_mod.Gate.from_config(cfg)

        if args.eval:
            # --json describes one estimate; --eval is a validation report with no
            # estimate in it. Accepting both and quietly printing the table would answer
            # a question the caller did not ask, in a format they cannot parse.
            if args.json:
                print("estimate: --json and --eval are different outputs — --json is one "
                      "estimate for the hook, --eval is the held-out table. Pick one.",
                      file=sys.stderr)
                return 2
            return _render_eval(rows, gate, cfg)

        if args.words is None:
            print("estimate: --words N is required (or --eval for the held-out table). "
                  "Nothing was estimated.", file=sys.stderr)
            return 2

        # A negative count is not a feature value the store could ever hold, so it would
        # be compared against a word range and a band built from non-negative history and
        # silently answered. Reject it rather than answer a question nobody asked.
        for flag, value in (("--words", args.words), ("--files", args.files)):
            if value < 0:
                print(f"estimate: {flag} {value} is negative; both are counts. "
                      f"Nothing was estimated.", file=sys.stderr)
                return 2

        buckets = list(cfg.require("classifier.buckets"))
        if args.bucket is not None and args.bucket not in buckets:
            print(f"estimate: {args.bucket!r} is not one of classifier.buckets "
                  f"({', '.join(buckets)}). Nothing was estimated.", file=sys.stderr)
            return 2

        feats = est_mod.Features(words=args.words, files=args.files,
                                 bucket=args.bucket, model=args.model)
        try:
            estimate = est_mod.select_method(cfg, rows)(rows, gate).predict(feats)
        except est_mod.EstimatorError as exc:
            print(f"estimate: {exc}", file=sys.stderr)
            return 2

        if args.json:
            print(json.dumps(est_mod.to_json(estimate), indent=2))
        else:
            for line in est_mod.render(estimate):
                print(line)
    return 4 if estimate.low is None else 0


def _render_eval(rows, gate, cfg) -> int:
    """The held-out comparison table. Reproducible: no sampling, no seed — leave-one-out."""
    from dict8.advise import estimator as est_mod

    share = est_mod.cache_read_share(rows)
    if not rows or share is None:
        # A held-out table over zero turns would print a row of NaNs and look like a
        # result. The gate has to be able to FAIL on an empty measurement, so it does.
        print(f"estimate --eval: {len(rows)} usable turns in {cfg.path('paths.db')} — "
              f"nothing to validate against. Run `dict8 backfill` (and "
              f"`dict8 classify-backfill` for the buckets) first.", file=sys.stderr)
        return 1

    counts = est_mod.bucket_counts(rows)
    print(f"turns fitted     : {len(rows)}  (message_count > 0 and total tokens > 0)")
    print(f"cache-read share : {share * 100:.2f}% of all tokens in the fit — the target is "
          f"mostly context re-reads (HANDOFF §4.8)")
    print(f"buckets          : " + "  ".join(f"{b}={n}" for b, n in counts.items()))
    print(f"models           : " + "  ".join(
        f"{m}={sum(1 for r in rows if r.model == m)}"
        for m in sorted({r.model for r in rows if r.model})))
    print(f"band             : {gate.low_q:g}/{gate.high_q:g} "
          f"(nominal coverage {(gate.high_q - gate.low_q) * 100:g}%)")
    print(f"floors           : ood_min_bucket_samples={gate.ood_min_bucket}  "
          f"min_samples_for_bucket={gate.min_bucket}")
    print(f"validation       : leave-one-out over all {len(rows)} turns\n")

    hdr = (f"{'method':<12}{'held-out':>10}{'refused':>9}{'MAPE %':>10}{'MdAPE %':>10}"
           f"{'coverage':>10}{'band hi/lo':>12}")
    print(hdr)
    print("-" * len(hdr))
    results = []
    for cls in (est_mod.QuantileEstimator, est_mod.RegressionEstimator):
        r = est_mod.leave_one_out(rows, gate, cls)
        results.append(r)
        print(f"{r.method:<12}{r.n:>10}{r.refused:>9}{r.mape:>10.1f}{r.mdape:>10.1f}"
              f"{r.coverage:>10.3f}{r.median_width:>12.1f}")
    print("-" * len(hdr))

    scored = [r for r in results if r.n]
    if not scored:
        # Every held-out point was refused, so MAPE and coverage are NaN. A table of NaNs
        # that exits 0 is a gate that passes on an empty measurement — it must not.
        print(f"\nestimate --eval: no held-out point produced a range "
              f"({results[0].refused} of {len(rows)} refused by the out-of-distribution "
              f"gate). There is nothing measured here to pass a gate on.", file=sys.stderr)
        return 1

    floor = int(cfg.require("estimate.min_samples_for_regression"))
    winner = min(scored, key=lambda r: r.mdape)
    print(f"\nlower MdAPE      : {winner.method}")
    # `estimate.method: regression` under the sample floor is a refusal, not a crash:
    # the config comment tells the reader to try exactly that, and --eval is the report
    # that should explain why it will not ship. The table above is still valid output.
    try:
        print(f"shipped          : {str(cfg.require('estimate.method'))} -> "
              f"{est_mod.select_method(cfg, rows).name}")
    except est_mod.EstimatorError as exc:
        print(f"shipped          : NOTHING — {exc}")
    if winner.method == est_mod.RegressionEstimator.name:
        print(f"NOT shipped      : the regression needs "
              f"estimate.min_samples_for_regression={floor} turns and there are "
              f"{len(rows)}. The floor holds whatever it scores.")

    print(f"\nband sweep (shipped method, leave-one-out) — the measurement behind "
          f"estimate.interval_low_q/high_q")
    hdr2 = f"{'band':<14}{'nominal':>9}{'coverage':>10}{'band hi/lo':>12}{'MdAPE %':>10}"
    print(hdr2)
    print("-" * len(hdr2))
    for (lo, hi), r in est_mod.sweep_bands(rows, gate, est_mod.EVAL_BANDS):
        print(f"{f'{lo:g}/{hi:g}':<14}{(hi - lo) * 100:>8.0f}%{r.coverage:>10.3f}"
              f"{r.median_width:>12.1f}{r.mdape:>10.1f}")

    print(f"\npooling sweep (shipped method, leave-one-out) — the measurement behind "
          f"estimate.min_samples_for_bucket:\nat and above the floor a bucket uses its own "
          f"band, below it the pooled one. Refusal floor pinned at 1 so nothing is refused.")
    hdr3 = f"{'floor':<8}{'held-out':>10}{'refused':>9}{'MAPE %':>10}{'MdAPE %':>10}{'coverage':>10}{'band hi/lo':>12}"
    print(hdr3)
    print("-" * len(hdr3))
    for f, r in est_mod.sweep_pooling(rows, gate, est_mod.EVAL_FLOORS):
        print(f"{f:<8}{r.n:>10}{r.refused:>9}{r.mape:>10.1f}{r.mdape:>10.1f}"
              f"{r.coverage:>10.3f}{r.median_width:>12.1f}")

    print(f"\nrefusal sweep (shipped method, leave-one-out) — the measurement behind "
          f"estimate.ood_min_bucket_samples:\nONLY the refusal floor moves; "
          f"min_samples_for_bucket stays at {gate.min_bucket}, so this table is about "
          f"refusing, not about pooling.")
    print(hdr3)
    print("-" * len(hdr3))
    for f, r in est_mod.sweep_bucket_floor(rows, gate, est_mod.EVAL_FLOORS):
        print(f"{f:<8}{r.n:>10}{r.refused:>9}{r.mape:>10.1f}{r.mdape:>10.1f}"
              f"{r.coverage:>10.3f}{r.median_width:>12.1f}")

    print(f"\npooled band vs n — the only evidence available for "
          f"estimate.ood_min_pooled_samples.\nThe band a fresh install would print from "
          f"its first n turns. Leave-one-out cannot measure this floor: the pooled group "
          f"is\nalways the whole store minus one, far above any floor worth arguing about.")
    hdr4 = f"{'n':<8}{'low':>16}{'median':>16}{'high':>16}{'hi/lo':>10}"
    print(hdr4)
    print("-" * len(hdr4))
    for n, lo, med, hi in est_mod.pooled_band_vs_n(rows, gate, est_mod.EVAL_PREFIXES):
        flag = "  <- below estimate.ood_min_pooled_samples: refused" \
            if n < gate.ood_min_pooled else ""
        print(f"{n:<8}{lo:>16,}{med:>16,}{hi:>16,}{hi / max(lo, 1):>10.1f}{flag}")
    all_totals = sorted(r.total_tokens for r in rows)
    print(f"{len(rows):<8}{est_mod.quantile(all_totals, gate.low_q):>16,.0f}"
          f"{est_mod.quantile(all_totals, 0.5):>16,.0f}"
          f"{est_mod.quantile(all_totals, gate.high_q):>16,.0f}"
          f"{est_mod.quantile(all_totals, gate.high_q) / max(est_mod.quantile(all_totals, gate.low_q), 1):>10.1f}"
          f"  <- today")

    print(f"\ncaveat           : unit is TOKENS. {share * 100:.1f}% of them are cache reads; "
          f"how quota % weights those is unknown until `dict8 quota` readings exist.")
    print(f"                   the regression's band comes from its own IN-SAMPLE residual "
          f"quantiles, so its held-out coverage above is slightly flattered.")
    return 0


# The hook handlers, by the event name the settings file names them with. Adding
# PostToolUse / SessionEnd in Phase 6 is a line each here and a module in dict8.hooks.
HOOK_HANDLERS = {"user-prompt-submit": "dict8.hooks.user_prompt_submit"}

# Printed when a command needs `classifier.backend` and the optional extra is not installed.
# Phrased as the command to run, because "ModuleNotFoundError: mlx_lm" is not one.
MISSING_EXTRA = ("mlx-lm is not installed. It is an optional extra so the headless usage "
                 "layer stays light — re-run as `uv run --extra classify dict8 "
                 "classify-backfill …`. Nothing was classified and nothing was written.")

HOOK_HELP = """usage: dict8 hook <event> [--config PATH]

Run a Claude Code hook handler. Reads the hook payload as JSON on stdin and writes the
hook's JSON response on stdout.

events:
  """ + "\n  ".join(sorted(HOOK_HANDLERS)) + """

This command ALWAYS exits 0 and never writes to stderr (CLAUDE.md invariant 8): a hook
that errors, hangs, finds no database or hits a config gap lets the prompt through and
writes one line to `paths.logs`/hooks.log saying why. Exit code 2 is how a Claude Code
hook blocks a prompt, so no path here can reach it — which is also why this command does
not go through argparse, whose own parse errors exit 2.
"""


def cmd_hook(argv: list[str]) -> int:
    """`dict8 hook <event>` — parsed by hand, on purpose.

    argparse exits 2 on an unrecognised argument, and 2 is the one exit code that BLOCKS
    the user's prompt. A typo in .claude/settings.json must degrade to "no estimate this
    time", not to "your prompt did not send", so this path never constructs a parser.
    """
    # Imported FIRST, before the config read: `dict8.hooks` starts the clock that
    # `hooks.timeout_ms` is measured against, so anything imported or read after this
    # line counts against the budget — including config.yml itself.
    from dict8 import hooks as hooks_pkg

    event: str | None = None
    config_path: str | None = None
    tokens = list(argv)
    while tokens:
        tok = tokens.pop(0)
        if tok in ("-h", "--help"):
            print(HOOK_HELP)
            return 0
        if tok == "--config":
            config_path = tokens.pop(0) if tokens else None
            continue
        if tok.startswith("--config="):
            config_path = tok.split("=", 1)[1]
            continue
        if event is None and not tok.startswith("-"):
            event = tok

    # A named config that will not load is reported as an ERROR, never swapped for the
    # default one. `cfg = None` used to mean both "none was named" and "the named one
    # failed", and the handler resolved that ambiguity by loading the default — so a
    # scratch config with a typo in it estimated from, and wrote a row into, the real
    # store. The two cases are now separate values.
    cfg = None
    cfg_error: str | None = None
    try:
        cfg = config_mod.load(Path(config_path)) if config_path else config_mod.load()
    except Exception as exc:
        cfg_error = f"{type(exc).__name__}: {exc}"
        if config_path is None:
            # Nothing was named, so there is no wrong file to have fallen back to and
            # nothing to distinguish; the handler's own silent path covers it.
            cfg_error = None

    module_name = HOOK_HANDLERS.get(event or "")
    if module_name is None:
        if cfg is not None:
            hooks_pkg.log(cfg, hook=event, outcome="failed-open",
                          reason=f"unknown hook event {event!r}; known: "
                                 f"{', '.join(sorted(HOOK_HANDLERS))}")
        return 0
    from importlib import import_module
    return int(import_module(module_name).main(cfg=cfg, cfg_error=cfg_error))


def cmd_hook_args(args, cfg) -> int:
    """The argparse-reachable spelling (`dict8 --config X hook <event>`), so the command
    is discoverable in `dict8 --help`. Same handler, same guarantee of exit 0."""
    argv = [args.event]
    if args.config:
        argv += ["--config", str(args.config)]
    return cmd_hook(argv)


def cmd_classify_backfill(args, cfg) -> int:
    """Classify every turn with no task_type yet.

    Reads prompt text transiently from Claude Code's own on-disk transcripts (never from
    Dict8's store, which has no text column) and writes back only the resulting bucket —
    see dict8.usage.parser.read_prompt_text and dict8.advise.classifier.
    """
    from dict8.advise.classifier import BackendUnavailable, Classifier, backend_available

    # `classifier.backend` ships as the optional `classify` extra, so the base install of
    # the headless layer does not carry it. Say so once, in one line, before anything is
    # loaded or opened — a stack trace out of a worker process is not an error message.
    if not backend_available():
        print(f"classify-backfill: {MISSING_EXTRA}", file=sys.stderr)
        return 1

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
        try:
            clf.warm()  # pay the model-load cost once, up front, not on turn 1
        except BackendUnavailable as exc:
            print(f"classify-backfill: {exc}", file=sys.stderr)
            return 1
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
            # No confidence: prompts/classify.md no longer asks for one (U4). The column
            # stays and takes NULL — see Store.set_task_type.
            store.set_task_type(turn["prompt_uuid"], result.bucket, clf.model_id)
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
    t.add_argument("--interval", type=float, default=None,
                   help="seconds between ticks (default: cli.tail_interval_s)")
    t.add_argument("--once", action="store_true", help="single tick, then exit")
    t.set_defaults(func=cmd_tail)

    r = sub.add_parser("reconcile", help="cross-check our totals against claude-tokens")
    r.add_argument("--days", type=int, default=None,
                   help="settled days in the window (default: cli.reconcile_days)")
    r.add_argument("--tolerance", type=float, default=None,
                   help="max %% delta to pass (default: cli.reconcile_tolerance_pct)")
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

    e = sub.add_parser(
        "estimate", help="token range for a turn, from the backfill (never USD)",
        epilog="exit codes: 0 a range was produced · 2 bad input · 3 a config gap · "
               "4 out of distribution, no range. 4 is an ANSWER, not a failure: the "
               "estimator has no comparable history and declines to invent one. A caller "
               "(the UserPromptSubmit hook) treats 4 as 'no estimate this time' and adds "
               "nothing to the prompt — never as an error.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    e.add_argument("--words", type=int,
                   help="prompt length in words. Required unless --eval.")
    e.add_argument("--files", type=int, default=0,
                   help="files already touched. At UserPromptSubmit time this is 0 by "
                        "definition — nothing has been touched yet — which is why it "
                        "defaults to 0 rather than to a guess.")
    e.add_argument("--bucket", help="classifier bucket; must be one of classifier.buckets. "
                                    "Omit when unclassified: the pooled distribution is used.")
    e.add_argument("--model", help="model id as it appears in the transcripts. Omit to leave "
                                   "the model unconstrained.")
    e.add_argument("--json", action="store_true", help="machine shape for the U3 hook")
    e.add_argument("--eval", action="store_true",
                   help="print the leave-one-out comparison of both methods instead")
    e.set_defaults(func=cmd_estimate)

    h = sub.add_parser("hook", help="run a Claude Code hook handler (payload on stdin)",
                       epilog="always exits 0 and never writes to stderr — invariant 8.",
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    h.add_argument("event", help=f"one of: {', '.join(sorted(HOOK_HANDLERS))}")
    h.set_defaults(func=cmd_hook_args, is_hook=True)

    cb = sub.add_parser("classify-backfill", help="fill task_type on unclassified turns")
    cb.add_argument("--limit", type=int, help="classify at most N turns (oldest first)")
    cb.set_defaults(func=cmd_classify_backfill)

    # Hooks bypass argparse entirely (see cmd_hook): its parse errors and its
    # `required=True` subcommand both exit 2, and a hook that exits 2 blocks the prompt.
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "hook":
        return cmd_hook(raw[1:])

    args = p.parse_args(argv)
    try:
        cfg = config_mod.load(args.config)
    except Exception:
        if not getattr(args, "is_hook", False):
            raise
        cfg = None
    if cfg is not None:
        # Warnings from anywhere under `dict8.` go to `paths.logs/dict8.log` from here on,
        # and only errors reach stderr. The human-readable summaries these commands print
        # are `print()` calls and are unaffected; what stops appearing underneath them is
        # the per-call noise (see dict8.logs). Hooks are NOT routed through this — they
        # keep their own non-blocking hooks.log, for reasons measured in dict8.hooks.
        logs.setup(cfg)
    try:
        return args.func(args, cfg)
    except config_mod.ConfigGap as exc:
        print(f"config gap: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
