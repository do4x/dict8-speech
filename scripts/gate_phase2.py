#!/usr/bin/env -S uv run --quiet --with pyyaml python
"""Phase 2 gate — estimator + `UserPromptSubmit` hook.

The five lines of `prompts/build.md`'s phase_2 GATE, each run against something real and
each printing the number behind it. prompts/build.md: "Never report a gate as passed on
the basis of code you believe is correct; pass it on the basis of output you ran."

Two rules this file follows, learned from the Phase 1 gate:

1. **A check that cannot fail on an empty measurement is not a check.** Every assertion
   below is paired with a "did we measure anything at all" clause — a parsed table with no
   rows, a log file with no lines, a context block with no estimate in it all FAIL rather
   than vacuously pass.
2. **Parse the output, do not trust the exit code.** `dict8 estimate --eval` is read back
   line by line and its numbers re-checked for finiteness; the hook's stdout is parsed as
   JSON and its context inspected.

The real database is never written to by this script. Every hook invocation runs against
a scratch config whose `paths.db` is a byte copy of the real one (or a deliberately broken
file) and whose `paths.logs` is a fresh directory, so the gate is re-runnable and leaves
`hook_estimates` exactly as it found it. The one exception is `--live`, which sends one
real `claude -p` prompt through the registered hook — that is the point of it.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dict8 import config as config_mod           # noqa: E402
from dict8.usage.store import Store              # noqa: E402

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n       " + detail.replace("\n", "\n       ") + "\n")


# ---- helpers -----------------------------------------------------------------------


def captured_payload() -> dict:
    """The real hook payload, read back out of `docs/verified-schemas.md` section 8.

    Deliberately not a fixture file of its own: invariant 4 says the pinned document is
    the record of what was observed, so the gate replays the document. If the payload in
    the doc ever stops being a real capture, this check goes with it.
    """
    doc = (REPO / "docs" / "verified-schemas.md").read_text(encoding="utf-8")
    after = doc.split("## 8.", 1)
    if len(after) < 2:
        raise SystemExit("gate: docs/verified-schemas.md has no section 8 — the hook "
                         "payload is not pinned, so there is nothing to replay.")
    block = re.search(r"```json\n(.*?)\n```", after[1], re.S)
    if not block:
        raise SystemExit("gate: section 8 of docs/verified-schemas.md has no json block.")
    return json.loads(block.group(1))


def scratch_config(tmp: Path, name: str, db: Path | None, **dotted) -> Path:
    """A copy of the real config.yml with `paths.db`, `paths.logs` and any dotted key
    overridden. Comments are lost in the round trip; values are not, which is what the
    checks are about."""
    data = yaml.safe_load((REPO / "config.yml").read_text(encoding="utf-8"))
    logs = tmp / f"logs-{name}"
    logs.mkdir(parents=True, exist_ok=True)
    data["paths"]["logs"] = str(logs)
    if db is not None:
        data["paths"]["db"] = str(db)
    for key, value in dotted.items():
        node = data
        parts = key.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    path = tmp / f"config-{name}.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def run_hook(cfg_path: Path, payload: dict | str) -> dict:
    """One cold `uv run dict8 hook user-prompt-submit`. Returns everything observable."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    t0 = time.monotonic()
    proc = subprocess.run(
        ["uv", "run", "--quiet", "dict8", "hook", "user-prompt-submit",
         "--config", str(cfg_path)],
        input=raw.encode(), capture_output=True, cwd=str(REPO),
    )
    wall_ms = (time.monotonic() - t0) * 1000
    cfg = config_mod.load(cfg_path)
    log_file = cfg.path("paths.logs") / "hooks.log"
    lines = []
    if log_file.exists():
        for line in log_file.read_text(encoding="utf-8").splitlines():
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                lines.append({"unparsed": line})
    out = proc.stdout.decode()
    context = None
    if out.strip():
        try:
            context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
            event = json.loads(out)["hookSpecificOutput"]["hookEventName"]
        except Exception:
            context, event = None, None
    else:
        event = None
    return {"rc": proc.returncode, "stdout": out, "stderr": proc.stderr.decode(),
            "context": context, "event": event, "log": lines, "wall_ms": wall_ms}


def quantile(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(int(round(q * (len(s) - 1))), len(s) - 1)]


def db_copy(tmp: Path, name: str, real_db: Path) -> Path:
    """A byte copy of the real store, so a check can exercise real history without
    writing a single row back into it."""
    target = tmp / f"{name}.sqlite"
    shutil.copy2(real_db, target)
    return target


# ---- checks ------------------------------------------------------------------------


def check_1_eval(tmp: Path) -> None:
    """GATE 1: held-out MAPE and interval coverage printed for BOTH methods, and the
    shipped one is named with the reason."""
    proc = subprocess.run(["uv", "run", "--quiet", "dict8", "estimate", "--eval"],
                          capture_output=True, text=True, cwd=str(REPO))
    rows: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^(quantile|regression)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+"
                     r"([\d.]+)\s+([\d.]+)\s*$", line)
        if m:
            rows[m.group(1)] = {"held_out": int(m.group(2)), "refused": int(m.group(3)),
                                "mape": float(m.group(4)), "mdape": float(m.group(5)),
                                "coverage": float(m.group(6)), "band": float(m.group(7))}
    shipped = [l for l in proc.stdout.splitlines() if l.startswith("shipped ")]
    lower = [l for l in proc.stdout.splitlines() if l.startswith("lower MdAPE")]
    notship = [l for l in proc.stdout.splitlines() if l.startswith("NOT shipped")]
    fitted = [l for l in proc.stdout.splitlines() if l.startswith("turns fitted")]

    # Empty-measurement clauses: two method rows, every number finite, at least one
    # held-out point behind each row, and a shipped line that names something.
    measured = (len(rows) == 2
                and all(r["held_out"] > 0 for r in rows.values())
                and all(math.isfinite(v) for r in rows.values()
                        for v in (r["mape"], r["mdape"], r["coverage"], r["band"])))
    ok = bool(measured and shipped and lower and proc.returncode == 0)
    detail = (f"`dict8 estimate --eval` rc={proc.returncode}; "
              f"{fitted[0].strip() if fitted else 'NO turns-fitted line'}\n"
              + "\n".join(
                  f"{m:<11} held-out={r['held_out']:<4} refused={r['refused']:<3} "
                  f"MAPE={r['mape']:.1f}%  MdAPE={r['mdape']:.1f}%  "
                  f"coverage={r['coverage']:.3f}  band hi/lo={r['band']:.1f}x"
                  for m, r in sorted(rows.items()))
              + "\n" + (lower[0].strip() if lower else "NO lower-MdAPE line")
              + "\n" + (shipped[0].strip() if shipped else "NO shipped line")
              + ("\n" + notship[0].strip() if notship else ""))
    record("1. held-out MAPE and coverage printed for both methods, shipped one named", ok, detail)


def check_2_fires(tmp: Path, real_db: Path, live: bool) -> None:
    """GATE 2: the hook fires on a real prompt and the estimate appears."""
    payload = captured_payload()
    db = db_copy(tmp, "fires", real_db)
    with Store(db) as store:
        before = store.count("hook_estimates")
    cfg = scratch_config(tmp, "fires", db)
    r = run_hook(cfg, payload)
    with Store(db) as store:
        after = store.count("hook_estimates")
        row = store.conn.execute(
            "SELECT * FROM hook_estimates ORDER BY id DESC LIMIT 1").fetchone()

    ctx = r["context"] or ""
    est_line = next((l for l in ctx.splitlines() if l.startswith("estimate   :")), "")
    has_range = bool(re.search(r"estimate   : [\d,]+ – [\d,]+ tokens", est_line))
    fields = sorted(payload)
    # The replay is the whole non-vacuity of this check once --live is off: a section 8
    # that has decayed into an empty object, or one with no `prompt`, must FAIL here
    # rather than quietly testing that a hook given nothing says nothing.
    replay_ok = (len(fields) > 1 and isinstance(payload.get("prompt"), str)
                 and bool(payload["prompt"].strip()))
    ok = (replay_ok and r["rc"] == 0 and r["stderr"] == "" and r["event"] == "UserPromptSubmit"
          and has_range and after == before + 1 and row is not None
          and row["prompt_words"] == len(payload["prompt"].split())
          and len(r["log"]) == 1 and r["log"][0].get("outcome") == "estimate")

    live_detail = (
        "live `claude -p` run: SKIPPED — default off, pass --live to spend quota on it.\n"
        "  What stands in for it: the replay above is the payload Claude Code itself wrote\n"
        "  (docs/verified-schemas.md section 8, captured from a real invocation), fed to the\n"
        "  real hook, and section 8.1 records the end-to-end run that proved the response\n"
        "  shape reaches the model — model answered n=100, quantile on 2026-09-18.")
    if live:
        # `--model haiku` is the only model name in this repo outside config.yml, and it
        # names the model CLAUDE CODE answers this probe with — not a model Dict8 chose
        # for anything. It cannot come from `config.models`, which is the recommender's
        # vocabulary and is entirely TBD. It is here because the probe has to spend some
        # of Denis's quota to prove the hook reaches a real model, and the cheapest one
        # is the honest choice for that.
        proc = subprocess.run(
            ["env", "-u", "CLAUDECODE", "-u", "CLAUDE_CODE_SESSION_ID",
             "-u", "CLAUDE_CODE_CHILD_SESSION", "-u", "CLAUDE_CODE_ENTRYPOINT", "claude",
             "-p", "In the Dict8 usage estimate context attached to this prompt, what is "
                   "the n= value and the method name? Reply with just those two.",
             "--model", "haiku"],
            capture_output=True, text=True, cwd=str(REPO), timeout=300)
        answer = proc.stdout.strip()
        want_n, want_method = str(row["n"]), str(row["method"])
        live_ok = want_n in answer and want_method in answer
        live_detail = (f"live `claude -p` through the registered .claude/settings.json "
                       f"hook: model answered {answer!r}; expected n={want_n} and "
                       f"method={want_method} -> {'MATCH' if live_ok else 'MISMATCH'}")
        ok = ok and live_ok

    record(
        "2. hook fires on a real prompt and the estimate appears",
        ok,
        f"payload replayed from docs/verified-schemas.md section 8, {len(fields)} fields "
        f"{fields}\n"
        f"replay is non-empty (fields>1 and a non-blank `prompt`): {replay_ok}, "
        f"prompt words={len(payload.get('prompt', '').split())}\n"
        f"rc={r['rc']} stderr={r['stderr']!r} hookEventName={r['event']!r} "
        f"wall={r['wall_ms']:.0f} ms\n"
        f"context line: {est_line.strip() or 'NO ESTIMATE LINE IN CONTEXT'}\n"
        f"hook_estimates rows {before} -> {after}; stored "
        f"words={row['prompt_words'] if row else None} n={row['n'] if row else None} "
        f"ood={row['ood'] if row else None} method={row['method'] if row else None}\n"
        f"log lines={len(r['log'])}: {r['log'][0] if r['log'] else 'NONE'}\n"
        f"{live_detail}")


def check_3_no_db(tmp: Path, real_db: Path) -> None:
    """GATE 3: with the DB gone — and with it corrupt — the prompt still goes through and
    the log says why."""
    payload = captured_payload()

    missing = tmp / "does-not-exist" / "dict8.sqlite"
    cfg_missing = scratch_config(tmp, "nodb", missing)
    a = run_hook(cfg_missing, payload)

    corrupt = tmp / "corrupt.sqlite"
    corrupt.write_bytes(b"this is not a database, it is 64 bytes of nonsense" + b"\x00" * 14)
    cfg_corrupt = scratch_config(tmp, "corruptdb", corrupt)
    b = run_hook(cfg_corrupt, payload)

    a_ok = (a["rc"] == 0 and a["stdout"] == "" and a["stderr"] == ""
            and len(a["log"]) == 1 and str(missing) in str(a["log"][0].get("reason", "")))
    b_ok = (b["rc"] == 0 and b["stdout"] == "" and b["stderr"] == ""
            and len(b["log"]) == 1 and b["log"][0].get("outcome") == "failed-open"
            and bool(b["log"][0].get("error")))
    record(
        "3. DB deleted (and DB corrupt) — prompt still goes through, log says why",
        a_ok and b_ok,
        f"missing DB  : rc={a['rc']} stdout={a['stdout']!r} stderr={a['stderr']!r} "
        f"wall={a['wall_ms']:.0f} ms\n"
        f"              log: {json.dumps(a['log'][0]) if a['log'] else 'NO LOG LINE'}\n"
        f"corrupt DB  : rc={b['rc']} stdout={b['stdout']!r} stderr={b['stderr']!r} "
        f"wall={b['wall_ms']:.0f} ms\n"
        f"              log: {json.dumps(b['log'][0]) if b['log'] else 'NO LOG LINE'}")


def check_4_burn_rate(tmp: Path, real_db: Path) -> None:
    """GATE 4: the burn-rate warning never fires off a reading older than
    `quota.stale_after_hours`, and cannot fire at all while that key is TBD."""
    payload = captured_payload()
    now = datetime.now(timezone.utc)
    source = str(config_mod.load().require("quota.source"))

    def seeded(name: str, ages_h: list[float], pcts: list[float]) -> Path:
        db = tmp / f"{name}.sqlite"
        with Store(db) as store:
            for age, pct in zip(ages_h, pcts):
                ts = (now - timedelta(hours=age)).isoformat()
                store.add_quota_reading(weekly_pct=pct, ts=ts, source=source,
                                        recorded_at=ts)
        return db

    def burn_line(ctx: str) -> str:
        return next((l for l in (ctx or "").splitlines() if l.startswith("burn rate")),
                    "NO BURN-RATE LINE")

    def quota_line(ctx: str) -> str:
        return next((l for l in (ctx or "").splitlines() if l.startswith("quota ")),
                    "NO QUOTA LINE")

    # (a) threshold 24 h, both readings 30 h and 36 h old -> no warning
    stale_db = seeded("stale", [30.0, 36.0], [61.0, 40.0])
    stale = run_hook(scratch_config(tmp, "stale", stale_db,
                                    **{"quota.stale_after_hours": 24}), payload)
    a_line = burn_line(stale["context"])
    a_ok = ("at this pace" not in (stale["context"] or "") and "NOT COMPUTED" in a_line
            and stale["rc"] == 0)

    # (b) same threshold, both readings fresh and rising -> the warning fires
    fresh_db = seeded("fresh", [1.0, 5.0], [61.0, 40.0])
    fresh = run_hook(scratch_config(tmp, "fresh", fresh_db,
                                    **{"quota.stale_after_hours": 24}), payload)
    b_line = burn_line(fresh["context"])
    b_ok = "at this pace, quota's gone in" in b_line and fresh["rc"] == 0

    # (c) the real config's own value for the threshold — TBD — can never fire
    real_threshold = config_mod.load().get("quota.stale_after_hours")
    real_copy = db_copy(tmp, "realcfg", real_db)
    real = run_hook(scratch_config(tmp, "realcfg", real_copy), payload)
    c_line = burn_line(real["context"])
    c_ok = ("at this pace" not in (real["context"] or "")
            and "unset (TBD)" in c_line and real["rc"] == 0)

    record(
        "4. burn-rate warning never fires off a stale reading, and not at all while "
        "quota.stale_after_hours is TBD",
        a_ok and b_ok and c_ok,
        f"(a) threshold 24 h, readings 30.0 h and 36.0 h old (61% and 40%):\n"
        f"    {quota_line(stale['context'])}\n    {a_line}\n"
        f"(b) threshold 24 h, readings 1.0 h and 5.0 h old (61% and 40%):\n"
        f"    {quota_line(fresh['context'])}\n    {b_line}\n"
        f"(c) real config.yml value quota.stale_after_hours={real_threshold!r}, real DB "
        f"copy (quota_readings={Store(real_copy).count('quota_readings')} rows):\n"
        f"    {quota_line(real['context'])}\n    {c_line}")


def check_5_weird(tmp: Path, real_db: Path) -> None:
    """GATE 5: a deliberately weird prompt gets "not enough similar history" and the hook
    still exits 0."""
    payload = dict(captured_payload())
    payload["prompt"] = " ".join(f"word{i}" for i in range(5000))
    db = db_copy(tmp, "weird", real_db)
    with Store(db) as store:
        observed = store.conn.execute(
            "SELECT MIN(prompt_words), MAX(prompt_words) FROM turns "
            "WHERE message_count > 0").fetchone()
        before = store.count("hook_estimates")
    r = run_hook(scratch_config(tmp, "weird", db), payload)
    with Store(db) as store:
        after = store.count("hook_estimates")
        row = store.conn.execute(
            "SELECT * FROM hook_estimates ORDER BY id DESC LIMIT 1").fetchone()

    ctx = r["context"] or ""
    refusal = next((l for l in ctx.splitlines() if "not enough similar history" in l), "")

    # Rule 1 of this file, applied to this check. "not enough similar history" is also
    # what an EMPTY store says, for a completely different reason (pooled n below
    # estimate.ood_min_pooled_samples) — so against an empty db_copy this check used to
    # pass while checks 2-4 failed, which is a check measuring nothing. Two clauses close
    # it: the store must have an observed word range at all, and the refusal must be the
    # one about LENGTH, naming that range.
    has_range = (observed is not None and observed[0] is not None
                 and observed[1] is not None and observed[1] >= observed[0])
    names_length = (has_range
                    and f"{observed[0]}-{observed[1]} word range" in refusal
                    and "5000 words is outside" in refusal)
    ok = (has_range and names_length and r["rc"] == 0 and r["stderr"] == ""
          and after == before + 1 and row is not None and row["ood"] == 1
          and row["low"] is None and row["prompt_words"] == 5000)
    record(
        "5. a deliberately weird prompt gets \"not enough similar history\", hook still exits 0",
        ok,
        f"prompt = 5000 words; observed prompt_words in this store: "
        f"{observed[0]}-{observed[1]} (non-empty range: {has_range})\n"
        f"refusal names the LENGTH, not just thin history: {names_length}\n"
        f"rc={r['rc']} stderr={r['stderr']!r} wall={r['wall_ms']:.0f} ms\n"
        f"{refusal.strip() or 'NO REFUSAL LINE IN CONTEXT'}\n"
        f"recorded: rows {before} -> {after}, ood={row['ood'] if row else None}, "
        f"low={row['low'] if row else None}, words={row['prompt_words'] if row else None} "
        f"(a refusal is recorded too — Phase 6 calibrates against it)\n"
        f"log: {json.dumps(r['log'][0]) if r['log'] else 'NO LOG LINE'}")


def measure_latency(tmp: Path, real_db: Path, runs: int) -> None:
    """Not one of the five gate lines — the unit's own latency requirement, printed so it
    can be reproduced. Invariant 9: latency is the product."""
    payload = captured_payload()
    db = db_copy(tmp, "latency", real_db)
    cfg = scratch_config(tmp, "latency", db)
    budget = float(config_mod.load().require("hooks.timeout_ms"))
    totals, inproc = [], []
    for _ in range(runs):
        r = run_hook(cfg, payload)
        if r["rc"] != 0 or r["context"] is None:
            print(f"[MEASUREMENT FAILED] a run returned rc={r['rc']} with no context")
            return
        totals.append(r["wall_ms"])
        inproc.append(r["log"][-1]["elapsed_ms"])
    print(f"[MEASURED] hook latency over {runs} cold `uv run` invocations "
          f"(not a gate line)\n"
          f"       whole path  p50={quantile(totals, .5):.1f} ms  "
          f"p95={quantile(totals, .95):.1f} ms  max={max(totals):.1f} ms  "
          f"(includes uv + interpreter start)\n"
          f"       in-process  p50={quantile(inproc, .5):.1f} ms  "
          f"p95={quantile(inproc, .95):.1f} ms  max={max(inproc):.1f} ms  "
          f"against hooks.timeout_ms={budget:g} ms "
          f"({budget / max(quantile(inproc, .95), 0.001):.0f}x headroom at p95)\n")


def main() -> int:
    # Opt-IN. Every gate run used to send a real prompt to a real model, i.e. spend
    # Denis's quota, by default — and a gate is a thing people re-run. The replay of the
    # captured payload is what makes the check non-vacuous; --live is the extra.
    live = "--live" in sys.argv
    runs = 20
    cfg = config_mod.load()
    real_db = cfg.path("paths.db")
    if not real_db.exists():
        print(f"gate: no database at {real_db} — run `uv run dict8 backfill` first.",
              file=sys.stderr)
        return 1
    print(f"repo      : {REPO}\nreal db   : {real_db}\n"
          f"live run  : {'yes, --live (one real `claude -p`, spends quota)' if live else 'no (default; pass --live to include it)'}\n"
          + "=" * 78 + "\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        check_1_eval(tmp)
        check_2_fires(tmp, real_db, live)
        check_3_no_db(tmp, real_db)
        check_4_burn_rate(tmp, real_db)
        check_5_weird(tmp, real_db)
        measure_latency(tmp, real_db, runs)

    print("=" * 78)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\nPhase 2 gate: {passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
