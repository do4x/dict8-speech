# Dict8 — autonomous build loop

Started 2026-09-18 on Denis's instruction: "implement a self recursive agentic loop, deploying
opus 5 on high/max subagents". Runs inside the interactive session on this Mac. Stops by itself
at the first thing that needs Denis's hands or a decision only he can make.

## Bar lowered — Denis, 2026-09-19

"Decrease the certainty level needed to move to the next phase." Denis also set a budget: finish
inside 50-70% of the remaining usage, and he wants to test the thing himself. What changes:

- **One verifier pass, only where a failure is silent.** Injection, permissions, and hooks get
  one fresh-context verifier at high effort. Everything else gets an orchestrator spot-check:
  re-run the gate, read the diff stat, commit.
- **Findings don't block.** A verifier FAIL gets one builder fix round. Whatever is left after
  that is logged as a caveat and the unit is committed.
- **The builder runs at high effort, not max.**
- **Provisional defaults instead of stopping.** A value needed to test goes into `config.yml` as a
  reasoned default marked `provisional`. Denis can change it at any time, and it is listed under
  Needs Denis. Only a TCC click, spoken audio, spend, or relaxing an invariant still stops the loop.
- **Phase sign-off is the gate output plus this log.** There is no separate wait.

## Roles

| Role | Who | Effort | Does |
|---|---|---|---|
| Orchestrator | the interactive session | — | picks the next unit, briefs, spot-checks, commits, logs, reschedules |
| builder | `.claude/agents/builder.md` (Opus 5) | high | one unit end to end, runs its gate, reports measured output |
| verifier | `.claude/agents/verifier.md` (Opus 5) | high | one fresh-context pass, only on units marked `verify` |

## One iteration

1. Take the first `todo` unit and set it to `in-progress`.
2. Spawn `builder`. The builder does not commit.
3. If the unit is marked `verify`, spawn `verifier` once. On FAIL, the builder gets one fix round.
4. The orchestrator re-runs the gate, commits on the branch without pushing, logs it, and sets the unit to `done`.
5. Reschedule. The loop ends when no `todo` unit is left.

## Stop rules

A TCC permission click, spoken audio, any spend, or relaxing a CLAUDE.md invariant.

## Provisional defaults (2026-09-19, change any in config.yml)

- **Hotkey:** hold right Option to talk, press Esc while holding to cancel, using a CGEventTap. fn/Globe is
  taken by macOS dictation and emoji. Right Option alone types nothing.
- **STT:** mlx-whisper, with the model picked by a quick warm-latency bench on clips made by macOS `say`.
  The real-speech bench stays a Denis item.
- **`models[]`:** the models seen in Denis's own transcripts, with a one-line strength each and the bucket to
  model mapping marked provisional.
- **Run from the terminal:** the app runs under the terminal's or VS Code's TCC grants for testing. The signed
  `.app` (ADR-001) is deferred until after the MVP works.
- **Live meter:** the app polls the transcript tail with the existing scanner instead of registering a
  PostToolUse hook. It reads the same data and adds no latency to every tool call.
- **Deferred past the MVP:** enhancement and detection (the rest of Phase 5). The brief's "done" needs neither.

## Queue

| Unit | Status | Scope | Gate |
|---|---|---|---|
| U1 quota check-in | done | `dict8 quota` | see Log |
| U2 estimator | done | `dict8 estimate` | see Log |
| U3 UserPromptSubmit hook | done | estimate as context on every prompt in this repo | see Log |
| U4 debts | done | thresholds to config, optional mlx-lm, logging, 72 unit tests | see Log |
| U5 dictation MVP | in-progress, verify | `dict8 app`: menu-bar item, hold-to-talk, record, mlx-whisper, inject with pasteboard fallback, permission toasts, latency log; `dict8 dictate --file` headless path; ADR-002 as one page | pytest green; `dictate --file` on `say` clips lands text byte-identical on the pasteboard; the app starts, shows its icon and names every missing permission; warm STT latency measured |
| U6 overlay, advice, meter | todo | overlay panel on release: transcript status, recommended model (bucket to `models[]` lookup), estimate range; voice commands `send` / `cancel` / `use <model>` stripped and acted on; menu-bar meter of the session's live tokens against its estimate | pytest green; overlay renders with a fake transcript; meter matches `dict8 usage` for a finished session |
| U7 real-task test | needs Denis | grant Microphone, Accessibility and Input Monitoring; dictate a real prompt into Claude Code; read the overlay; watch the meter | brief's "Done means" |
| ~~U5 ADR-002 / U6 STT bench / U7 dictation code~~ | replaced 2026-09-19 | folded into the new U5 and U6 | — |

## Needs Denis

- Say which usage figure the 50-70% budget refers to. If it is the weekly quota, `dict8 quota <pct>` with today's `/usage` number makes it measurable.
- Confirm or change the provisional hotkey, STT model and `models[]`.
- Three TCC grants and a real-task test (U7).
- `quota.stale_after_hours` / `checkin_cadence`: the burn-rate warning stays off until these are set.
- The real-speech STT bench, `packaging.*` and the signed `.app`, `enhance.default`, `privacy.transcript_retention`.

## Log

| When | Unit | Round | Verdict | Gate numbers | Commit |
|---|---|---|---|---|---|
| 2026-09-18 23:05 | U1 | 1 | PASS, 4 caveats sent back (provenance phrase hardcoded, `--at` ignored in read mode, boundary rounding, USD gate covers 2 tables only) | quota stored/re-read/stale/reject all reproduced by verifier; gate_phase1 8/8; migration v3→v4 row counts unchanged | pending fix round |
| 2026-09-18 23:20 | U1 | 2 | PASS — 4 caveats fixed, orchestrator re-ran: empty state exit 0, `--at` read-mode exit 2, verbatim rejection, quota_readings 0 rows, schema v4 | gate_phase1 8/8; check 8 scans 9 tables / 60 columns | a830f4a |
| 2026-09-18 23:45 | U2 | 1 | FAIL — pooled path has no sample floor (n=1 DB yields a 1,000–1,000 "50% band", exit 0); `--eval` crashes with method=regression; `estimate.method` still "heuristic"; sweep confounds two floors | LOO reproduced: quantile MdAPE 73.8 / cov 0.464, regression 67.8 / 0.488, 6 refused each; OLS matches numpy to 2.8e-14; gate_phase1 8/8 | fix round sent |
| 2026-09-19 00:05 | U2 | 2 | PASS — pooled floor non-bypassable (1/3-turn DBs exit 4, flag=false still exit 4, key removed → config gap exit 3); `--eval` under method=regression exit 0; method=quantile | LOO n=93: quantile 174.2/74.0/0.471/6.8×, regression 130.2/65.0/0.494/3.7×, 6 refused each; gate_phase1 8/8; 26 TBDs | b59d88b |
| 2026-09-19 00:35 | U3 | 1 | FAIL — settings.json command can exit 2 on empty `$CLAUDE_PROJECT_DIR` (blocks the prompt); post-watchdog log write unbounded (hung 2m17s on a FIFO); unloadable `--config` silently falls back to the real config/DB; live `claude -p` opt-out in the gate (spend); check 5 vacuous on empty store; latency claims inconsistent | gate_phase2 5/5 and gate_phase1 8/8 reproduced; watchdog fires at ~255 ms under a real lock; 14 hostile inputs all rc=0; sentinel never reaches log or DB; invariant 1 separation confirmed in transcript | fix round sent |
| 2026-09-19 00:55 | U3 | 2 | PASS — settings command rc=0 in 7 hostile env cases; FIFO log path 52/43 ms (was 2m17s); failed `--config` → no estimate, no row, no fallback; gate live run opt-in; check 5 fails on empty store | gate_phase2 5/5 (no live), gate_phase1 8/8; watchdog fires at ~354 ms under a real lock; TBDs 25 | 344040e |
| 2026-09-19 01:20 | U4 | 1 | PASS, 4 caveats sent back (dead `dedup_order` property, two stale HANDOFF sentences, try a voice-command rule for the "use opus" eval flip) | pytest 72/72 in 0.22 s, mutation-checked; gate_phase1 8/8; gate_phase2 5/5; gate_classifier 11/12 at median 337 ms (was 636); coverage on scratch copy 74→92 of 109 turns, max words 155→595; base env 326 MB→964 KB; TBDs 25 | pending fix round |
| 2026-09-19 09:50 | U4 | 1+fix | PASS: dead code removed, 2 stale HANDOFF lines fixed; voice-command classifier rule skipped (caveat) | pytest 72/72; gate_phase1 8/8; gate_phase2 5/5; lock clean; TBDs 25 | f00ae6b |
