"""`UserPromptSubmit` — the estimate's delivery slot.

Claude Code runs this before it processes a prompt. Dict8 answers with a token range for
the turn about to start, drawn from this machine's own history, plus the last manual
quota check-in. It is **context, never the prompt**: the range travels in
`hookSpecificOutput.additionalContext` (invariant 1), the user's words are passed through
untouched, and this module never even keeps a copy of them — only their word count.

**No model call.** `dict8.advise.estimator` is imported and called in-process. Shelling
out to `dict8 estimate` would pay the `uv run` and interpreter start a second time inside
a budget measured in tens of milliseconds, and it would turn the estimator's
out-of-distribution exit code 4 into a subprocess failure to be re-interpreted.

**What is known at submit time, and what is not.**
- `words`  — `len(prompt.split())`, the same count `dict8.usage.parser` records as
  `turns.prompt_words`, so the feature is on the scale the estimator was fitted on.
- `files`  — 0 by definition. Nothing has been touched yet.
- `bucket` — `None`. The classifier is a ~700 ms local model call
  (`classifier.timeout_ms: 800`); the whole hook budget is a fraction of that, and
  invariant 9 says latency is the product. So the estimate comes from the POOLED group,
  which is exactly why `estimate.ood_min_pooled_samples` exists.
- `model`  — `None`. Which model will answer is not in the payload
  (docs/verified-schemas.md section 8). The estimator's OOD gate already skips the
  unseen-model check when the model is None (`check_distribution`: `if feats.model is not
  None`), so nothing here weakens it — an unknown model is not checked against the
  observed set rather than being waved past it.

**A refusal is an answer.** `estimate.refuse_when_out_of_distribution` makes the
estimator decline rather than extrapolate; on a thin store that is the normal outcome, not
an error. It surfaces as the refusal sentence ("not enough similar history …") in the same
context block, gets one log line, and is recorded in `hook_estimates` like any other
estimate — Phase 6 cannot calibrate against predictions that were never written down.

**Failure is also an answer, and it is silence.** Invariant 8: a crash, an unreadable or
missing database, a config gap, a slow disk, an overrun of `hooks.timeout_ms` — every one
of them exits 0 with empty stdout and one line in `paths.logs/hooks.log`. `hooks.fail_open`
in config.yml is recorded when it is false but NOT obeyed downward: invariant 8 is not a
setting, and a config edit must not be able to turn a dictation tool into the reason a
prompt did not send.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from dict8 import config as config_mod
from dict8 import hooks
from dict8.usage import quota as quota_mod

EVENT = "UserPromptSubmit"

# Header on the context block. It says whose words these are, because the one thing the
# model must not do with this text is treat it as part of what the user asked for.
CONTEXT_HEADER = ("Dict8 usage estimate for the prompt above — advisory context from the "
                  "user's local history, NOT part of the user's request. Do not act on it.")

_HOURS_PER_DAY = 24.0           # unit conversion, not a threshold


# ---- quota ------------------------------------------------------------------------


def _as_utc(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _reading(row) -> quota_mod.QuotaReading:
    return quota_mod.QuotaReading(
        weekly_pct=float(row["weekly_pct"]), taken_at=_as_utc(row["ts"]),
        source=str(row["source"]), recorded_at=_as_utc(row["recorded_at"]),
        row_id=int(row["id"]),
    )


def quota_lines(store, cfg, now: datetime) -> list[str]:
    """The quota reading, its age and provenance, and the burn rate — or, far more often,
    the exact reason there is no burn rate.

    Two readings are the minimum a rate can be computed from, and BOTH have to be within
    `quota.stale_after_hours` of now: a pace derived from a stale pair describes a week
    that may already be over. While that key is TBD the warning can never fire, and this
    says so rather than picking 24 because it is a round number (invariant 3).
    """
    limit, why_no_limit = quota_mod.parse_limit(cfg.get("quota.stale_after_hours"))
    rows = store.recent_quota_readings(2)

    if rows:
        newest = _reading(rows[0])
        age_h = quota_mod.hours(newest.age(now))
        lines = [f"quota      : {newest.weekly_pct:g}% weekly, {age_h:.1f} h old, "
                 f"source={newest.source} ({quota_mod.provenance(newest.source)})"]
    else:
        lines = ["quota      : no check-in recorded yet — nothing is inferred in its "
                 "place. Run `dict8 quota <pct>` with the weekly percent from /usage."]

    # The threshold's status is stated on every path, including the one where there is no
    # reading to judge: "we have no rule for staleness" and "we have no reading" are
    # different gaps, and a burn-rate warning's absence should never be ambiguous between
    # them.
    if limit is None:
        lines.append(f"burn rate  : NOT COMPUTED — {why_no_limit}")
        return lines
    if len(rows) < 2:
        lines.append(f"burn rate  : NOT COMPUTED — needs two readings no older than "
                     f"quota.stale_after_hours = {limit:g} h; {len(rows)} on record.")
        return lines

    older = _reading(rows[1])
    older_age_h = quota_mod.hours(older.age(now))
    if age_h > limit or older_age_h > limit:
        lines.append(f"burn rate  : NOT COMPUTED — the two most recent readings are "
                     f"{age_h:.1f} h and {older_age_h:.1f} h old, past "
                     f"quota.stale_after_hours = {limit:g} h. A pace off stale readings "
                     f"describes a week that may already be over.")
        return lines

    span_h = quota_mod.hours(newest.taken_at - older.taken_at)
    # Guarded at the precision this line PRINTS, not at zero. Two readings 40 seconds
    # apart have a positive span and would divide a real percentage difference by 0.011 h,
    # printing a confident "90 %/h — gone in 0.05 days" next to "over 0.0 h". A rate whose
    # own denominator rounds away is not a measurement of anything.
    if span_h <= 0 or f"{span_h:.1f}" == "0.0":
        lines.append(f"burn rate  : NOT COMPUTED — the two most recent readings are "
                     f"{span_h * 60:.1f} minutes apart, too close together to divide by. "
                     f"A pace needs two readings taken far enough apart to tell usage "
                     f"from noise.")
        return lines
    delta = newest.weekly_pct - older.weekly_pct
    if delta == 0:
        lines.append(f"burn rate  : none — quota is unchanged at {newest.weekly_pct:g}% "
                     f"across both readings, {span_h:.1f} h apart. Nothing to project.")
        return lines
    if delta < 0:
        lines.append(f"burn rate  : none — quota went DOWN, {older.weekly_pct:g}% to "
                     f"{newest.weekly_pct:g}% over {span_h:.1f} h (a weekly reset looks "
                     f"like this). Nothing to project.")
        return lines
    rate = delta / span_h                                  # percent per hour
    days = (quota_mod.PCT_MAX - newest.weekly_pct) / rate / _HOURS_PER_DAY
    lines.append(f"burn rate  : +{delta:g} pts over {span_h:.1f} h = {rate:.2f} %/h — at "
                 f"this pace, quota's gone in {days:.1f} days. Both readings are within "
                 f"quota.stale_after_hours = {limit:g} h.")
    return lines


# ---- the handler -------------------------------------------------------------------


def build_context(store, cfg, prompt: str, now: datetime):
    """(context lines, the Estimate). Imports the estimator here rather than at module
    level so the kill-switch path never pays for it."""
    from dict8.advise import estimator as est_mod

    words = len(prompt.split())
    feats = est_mod.Features(words=words, files=0, bucket=None, model=None)
    rows = est_mod.load_turns(store)
    gate = est_mod.Gate.from_config(cfg)
    est = est_mod.select_method(cfg, rows)(rows, gate).predict(feats)

    lines = [CONTEXT_HEADER, ""]
    lines.extend(est_mod.render(est))
    lines.append(f"features   : {words} words, 0 files touched (nothing is touched yet), "
                 f"no task bucket (no model call at submit time), model unknown")
    lines.extend(quota_lines(store, cfg, now))
    return lines, est, words


def run(raw_stdin: str, cfg) -> tuple[str | None, dict]:
    """The whole handler, minus process concerns. Returns (stdout text or None, log
    fields). Raises nothing the caller has to interpret: exceptions are the caller's
    fail-open envelope to catch, and it always answers 0."""
    payload = hooks.read_payload(raw_stdin)
    event = str(payload.get("hook_event_name") or EVENT)
    session_id = payload.get("session_id")
    prompt_id = payload.get("prompt_id")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None, {"hook": EVENT, "event": event, "outcome": "skipped",
                      "reason": "payload carried no non-empty `prompt` field",
                      "session_id": session_id}

    from dict8.usage.store import Store

    now = datetime.now(timezone.utc)
    db = cfg.path("paths.db")
    if not db.exists():
        # Explicit, because `Store()` would helpfully CREATE an empty database here and
        # the estimate would then be refused for "no history" rather than for the real
        # reason. A hook must not create state as a side effect of a prompt, either.
        return None, {"hook": EVENT, "event": event, "outcome": "no-estimate",
                      "reason": f"no database at {db} — run `dict8 backfill`",
                      "session_id": session_id}

    with Store(db) as store:
        lines, est, words = build_context(store, cfg, prompt, now)
        store.add_hook_estimate(
            session_id=None if session_id is None else str(session_id),
            prompt_id=None if prompt_id is None else str(prompt_id),
            ts=now.isoformat(), prompt_words=words, method=est.method,
            est_group=est.group, n=est.n, low=est.low, point=est.point, high=est.high,
            ood=est.ood,
        )

    return hooks.additional_context(event, "\n".join(lines)), {
        "hook": EVENT, "event": event,
        "outcome": "no-estimate" if est.low is None else "estimate",
        "session_id": session_id, "prompt_words": words, "method": est.method,
        "group": est.group, "n": est.n, "low": est.low, "point": est.point,
        "high": est.high, "ood": est.ood,
        "reason": est.reason,
    }


def main(argv: list[str] | None = None, cfg=None, cfg_error: str | None = None) -> int:
    """Always 0. Never stderr. The envelope invariant 8 describes, in one function.

    `cfg_error` is how the caller says "a config was named and it would not load". That is
    NOT the same as "no config was named", and the difference matters more than it looks:
    falling back to the default config when an explicit `--config` fails means a scratch
    run silently estimates from — and writes a row into — the real store. It happened:
    a gate config that failed to parse produced a real estimate against Denis's own
    database. An unloadable config is now a fail-open no-estimate, full stop.
    """
    if cfg_error is not None:
        # The default config is loaded here for ONE purpose — finding `paths.logs` so the
        # failure can be reported. Nothing downstream runs: no store is opened, no estimate
        # is made, no row is written. If the default will not load either, the line is lost
        # and the prompt still goes through.
        try:
            hooks.log(config_mod.load(), hook=EVENT, outcome="failed-open",
                      reason=f"config could not be loaded: {cfg_error}",
                      detail="no estimate was made and nothing was written; an explicit "
                             "--config that fails to load never falls back to the default",
                      elapsed_ms=round(hooks.elapsed_ms(), 1))
        except Exception:
            pass
        return 0

    try:
        cfg = cfg if cfg is not None else config_mod.load()
    except Exception:
        # No config means no `paths.logs` to explain it in. Silence beats a stack trace
        # in front of the user's prompt.
        return 0

    try:
        if not cfg.require("hooks.user_prompt_submit"):
            hooks.log(cfg, hook=EVENT, outcome="disabled",
                      reason="hooks.user_prompt_submit is false in config.yml",
                      elapsed_ms=round(hooks.elapsed_ms(), 1))
            return 0
        budget_ms = float(cfg.require("hooks.timeout_ms"))
    except Exception as exc:
        hooks.log(cfg, hook=EVENT, outcome="failed-open", reason=f"config gap: {exc}",
                  error=type(exc).__name__, elapsed_ms=round(hooks.elapsed_ms(), 1))
        return 0

    if cfg.get("hooks.fail_open", True) is False:
        hooks.log(cfg, hook=EVENT, outcome="note",
                  reason="hooks.fail_open is false in config.yml; ignored — CLAUDE.md "
                         "invariant 8 is not a setting and this hook still fails open")

    def on_expire(ms: float) -> None:
        hooks.log(cfg, hook=EVENT, outcome="failed-open", reason="timeout",
                  detail=f"exceeded hooks.timeout_ms={budget_ms:g}", elapsed_ms=round(ms, 1))

    # The watchdog stays armed until the last byte is out. An earlier revision cancelled it
    # before the stdout write to rule out a truncated JSON object, which left the two
    # writes — stdout and the log line — outside any budget at all: with a FIFO at
    # `paths.logs/hooks.log` the hook sat there for 2 min 17 s and then cheerfully logged
    # `elapsed_ms: 8.6`. Both holes are closed, from both ends: the log write cannot block
    # (dict8.hooks.log opens O_NONBLOCK) and the window covers everything either way.
    with hooks.Watchdog(budget_ms, on_expire):
        try:
            out, fields = run(sys.stdin.read(), cfg)
        except Exception as exc:
            hooks.log(cfg, hook=EVENT, outcome="failed-open", error=type(exc).__name__,
                      reason=str(exc), elapsed_ms=round(hooks.elapsed_ms(), 1))
            return 0
        if out is not None:
            sys.stdout.write(out)
            sys.stdout.flush()
        hooks.log(cfg, **fields, budget_ms=budget_ms,
                  elapsed_ms=round(hooks.elapsed_ms(), 1))
    return 0
