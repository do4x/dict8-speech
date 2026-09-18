# Dict8 — autonomous build loop

Started 2026-09-18 on Denis's instruction: "implement a self recursive agentic loop, deploying
opus 5 on high/max subagents". Runs inside the interactive session on this Mac. Stops by itself
at the first thing that needs Denis's hands or a decision only he can make.

## Roles

| Role | Who | Effort | Does |
|---|---|---|---|
| Orchestrator | the interactive session (Fable 5.1) | — | picks the next unit, briefs, verifies, commits, logs, reschedules. Writes no product code |
| builder | `.claude/agents/builder.md` (Opus 5) | max | one unit end to end, runs its gate, reports measured output |
| verifier | `.claude/agents/verifier.md` (Opus 5) | high | fresh-context re-run of the gate and diff review against the invariants |

## One iteration

1. Take the first unit with status `todo`. Set it `in-progress`.
2. Spawn `builder` with the unit's scope and gate. The builder does not commit.
3. Spawn `verifier` on the result. `FAIL` → send the findings back to the same builder (its
   context intact), at most 2 more rounds.
4. `PASS` → orchestrator commits on branch `claude/modest-hamilton-lh72fw` (no push), logs the
   gate numbers and the commit hash, sets `done`.
5. Still `FAIL` after 3 rounds → `blocked`, last findings logged, next unit.
6. Reschedule. The loop ends when no `todo` unit is left.

## Stop rules — hand back to Denis instead of guessing

A TCC permission click. A hotkey choice. Spoken audio. Any spend. Relaxing a CLAUDE.md
invariant. Confirming Windows is the real target (see `docs/prompt-check-2026-09-18.md`).
A gate that cannot be made to fail on an empty measurement.

## Standing assumptions (Denis: correct any of these and the loop re-plans)

- The Phase 1 gate (8/8 on 09-15 and 09-18) is treated as signed off by the loop instruction.
- The repo build order stands: usage → estimator/hook → shell → dictation → detect/enhance →
  meter. The brief's "dictation first" needs Denis's hands at every step; the estimator does not.
- Target is macOS / Apple Silicon (ADR-000). The pasted brief's Windows lines are void.
- Hooks get registered in this repo's `.claude/settings.json` first, so they fire only inside
  Dict8. Going global in `~/.claude/settings.json` is Denis's call.
- Each verified unit is committed on the feature branch. Nothing is pushed.

## Queue

| Unit | Status | Scope | Gate |
|---|---|---|---|
| U1 quota check-in | done | `dict8 quota <pct>` stores `{weekly_pct, timestamp, source}` in SQLite (new table, additive migration); `dict8 quota` with no argument shows the last reading and its age. A reading older than `quota.stale_after_hours` is labeled stale; while that key is TBD the output says the threshold is unset rather than picking one. Never presents an inferred number as read. | reading stored and re-read with its age; stale label appears for a backdated reading once a threshold exists; TBD surfaces as a labeled gap, not a default; gate_phase1 still 8/8 |
| U2 estimator | done | `dict8/advise/estimator.py` and `dict8 estimate --words N --files N --bucket B --model M`. Fit on the backfill: bucket medians + quantiles versus a simple regression; held-out MAPE and interval coverage for both; ship the winner; output a range with the sample count behind it; out-of-distribution → "not enough similar history". Target is total tokens, with the cache-read share reported alongside and the HANDOFF section 4.8 caveat in the module docstring. | both methods' held-out MAPE and coverage printed from a real run; a weird prompt gets the refusal; no USD; nothing hardcoded; `estimate.method` in config names the winner with the numbers |
| U3 UserPromptSubmit hook | done | `dict8/hooks/user_prompt_submit.py`: computes the estimate locally (no model call) and returns it as context; fails open on crash, missing DB, slow disk, timeout, with one log line to `paths.logs`; kill switch in config. Registered in this repo's `.claude/settings.json`; `paths.claude_settings` filled with that path and the reason. The real hook payload is captured from a live invocation and pinned in `docs/verified-schemas.md` before any field is parsed. `scripts/gate_phase2.py` covers the five Phase 2 gate lines. | gate_phase2 all PASS with measured output; hook fires on a real prompt in this repo and the estimate appears; DB deleted → prompt still goes through and the log says why |
| U4 debts | in-progress | Thresholds out of code into `config.yml` (HANDOFF section 6 list); `mlx-lm` becomes an optional extra so the headless layer installs light; warnings to `paths.logs`; unit tests for dedup, turn assembly and `is_human_prompt` on real-shaped fixtures with no prompt text committed. Classifier: drop `why` and `confidence` from the output if the eval gate holds, then re-measure the ~100-word coverage line on the DB. | `uv run pytest` green; gate_phase1 8/8; gate_classifier at least 10/12; grep finds no hardcoded thresholds |
| U5 ADR-002 hotkey | todo | `docs/ADR-002-hotkey.md`: CGEventTap versus RegisterEventHotKey, decided on clean key-up while another app has focus and on what each costs in permissions. Probe on this machine what needs no grant (`AXIsProcessTrusted`, `CGPreflightListenEventAccess`, event-tap creation result) and mark every permission claim measured or documented. Fill `hotkey.mechanism`. Keys stay TBD. | ADR written and locked; `hotkey.mechanism` filled with the reason; each permission claim labeled measured or documented |
| U6 STT bench (provisional) | todo | Run `bench/stt_bench.py` for all three backends across the three candidate models with clips synthesized by macOS `say` from `bench/scripts.yml`, cold and warm, 3/10/30 s. Fill `stt.*` with the winner, each comment marked `provisional: synthesized speech`. The real-speech re-run stays a Denis item; `hardware.mic_device` stays TBD. | results table with real numbers from this chip, warm and cold; `stt.backend`, `stt.model`, `stt.compute`, `stt.quantization` filled and labeled provisional |
| U7 dictation code, headless-testable | todo | `dict8/audio stt inject permissions ui` per the repo map; `scripts/bench_latency.py`; permission checks that name the missing grant in a toast; three-tier injection with Secure Input detection; `dict8 dictate --file clip.wav` exercising STT → inject → pasteboard-only fallback without a mic or hotkey. No key wiring beyond the mechanism from U5, because the keys are TBD. | unit tests green; `dictate --file` lands `snake_case_id, {braces}, "quotes"` and curly quotes byte-identical on the pasteboard; the live 20-dictation gate is listed under Needs Denis, not claimed |

## Needs Denis (accumulates as the loop runs)

- Confirm macOS is still the target. The pasted brief says Windows.
- `models[]`: the recommender's whole vocabulary. Id, label, one-line strength for each.
- `hotkey.push_to_talk`, `hotkey.push_to_talk_enhanced`, `hotkey.cancel`.
- `quota.checkin_cadence`, `quota.stale_after_hours`, and the first `dict8 quota <pct>` reading from `/usage`.
- `enhance.default`, `packaging.*`, `privacy.transcript_retention`.
- Three TCC grants (Microphone, Accessibility, Input Monitoring) and 20 real dictations for the Phase 4 gate.
- Real-speech STT bench run (`bench/stt.md`, "How to run it").

## Log

| When | Unit | Round | Verdict | Gate numbers | Commit |
|---|---|---|---|---|---|
| 2026-09-18 23:05 | U1 | 1 | PASS, 4 caveats sent back (provenance phrase hardcoded, `--at` ignored in read mode, boundary rounding, USD gate covers 2 tables only) | quota stored/re-read/stale/reject all reproduced by verifier; gate_phase1 8/8; migration v3→v4 row counts unchanged | pending fix round |
| 2026-09-18 23:20 | U1 | 2 | PASS — 4 caveats fixed, orchestrator re-ran: empty state exit 0, `--at` read-mode exit 2, verbatim rejection, quota_readings 0 rows, schema v4 | gate_phase1 8/8; check 8 scans 9 tables / 60 columns | a830f4a |
| 2026-09-18 23:45 | U2 | 1 | FAIL — pooled path has no sample floor (n=1 DB yields a 1,000–1,000 "50% band", exit 0); `--eval` crashes with method=regression; `estimate.method` still "heuristic"; sweep confounds two floors | LOO reproduced: quantile MdAPE 73.8 / cov 0.464, regression 67.8 / 0.488, 6 refused each; OLS matches numpy to 2.8e-14; gate_phase1 8/8 | fix round sent |
| 2026-09-19 00:05 | U2 | 2 | PASS — pooled floor non-bypassable (1/3-turn DBs exit 4, flag=false still exit 4, key removed → config gap exit 3); `--eval` under method=regression exit 0; method=quantile | LOO n=93: quantile 174.2/74.0/0.471/6.8×, regression 130.2/65.0/0.494/3.7×, 6 refused each; gate_phase1 8/8; 26 TBDs | b59d88b |
| 2026-09-19 00:35 | U3 | 1 | FAIL — settings.json command can exit 2 on empty `$CLAUDE_PROJECT_DIR` (blocks the prompt); post-watchdog log write unbounded (hung 2m17s on a FIFO); unloadable `--config` silently falls back to the real config/DB; live `claude -p` opt-out in the gate (spend); check 5 vacuous on empty store; latency claims inconsistent | gate_phase2 5/5 and gate_phase1 8/8 reproduced; watchdog fires at ~255 ms under a real lock; 14 hostile inputs all rc=0; sentinel never reaches log or DB; invariant 1 separation confirmed in transcript | fix round sent |
| 2026-09-19 00:55 | U3 | 2 | PASS — settings command rc=0 in 7 hostile env cases; FIFO log path 52/43 ms (was 2m17s); failed `--config` → no estimate, no row, no fallback; gate live run opt-in; check 5 fails on empty store | gate_phase2 5/5 (no live), gate_phase1 8/8; watchdog fires at ~354 ms under a real lock; TBDs 25 | 344040e |
