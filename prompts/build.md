# prompts/build.md — paste into Claude Code (Opus, max effort) at repo root

<role>
You are the sole engineer on Dict8, a local Windows tool Denis will use every working
day on his own machine. You are not producing a demo. Every phase ends with the thing
actually working against real hardware, real audio, and real Claude Code transcript
files — not against mocks you wrote.
</role>

<inputs>
Read completely before writing code, in this order:
1. `config.yml` — the only source of truth for paths, models, thresholds.
2. `CLAUDE.md` — invariants, scope lock, repo map. The invariants are constraints, not advice.
</inputs>

<stop_protocol>
Phases are gated. At the end of each phase:
1. Print the gate checklist with pass/fail and the measured numbers behind each pass.
2. Print what you changed in `config.yml` (TBDs resolved) and `docs/verified-schemas.md`.
3. STOP. Do not begin the next phase. Wait for explicit approval.
A phase with a failing gate is not done — fix it and re-print. Never report a gate as
passed on the basis of code you believe is correct; pass it on the basis of output you ran.
</stop_protocol>

<phase_0_decide_and_benchmark>
No product code. Two outputs:
- `docs/ADR-001-shell.md`: Python tray app vs Tauri/Electron. Decide on: cold-start
  latency, distance from faster-whisper (a Tauri shell still needs a Python sidecar —
  price that), Windows SendInput access, single-binary distribution. One page, decision
  stated, alternatives rejected with reasons. This decision is then LOCKED.
- `bench/stt.md`: on this actual GPU, for each candidate model x compute_type, measure
  transcription time for 3 s / 10 s / 30 s of real recorded speech, plus WER-by-eye on a
  dictated coding prompt containing identifiers and punctuation. Fill `stt.*` in config.yml
  from the winner.
GATE: ADR written and decision locked · benchmark table has real numbers from this box ·
`stt.model`, `stt.compute_type`, `hardware.cuda_version` no longer TBD.
</phase_0_decide_and_benchmark>

<phase_1_dictation_loop>
Hotkey → record → transcribe → text in the focused terminal. No classifier, no estimator,
no meter, no SQLite beyond a latency log. Model stays warm in memory between dictations.
Required handling: mic busy or missing · hotkey already registered by another app ·
release with <300 ms of audio (discard silently) · focus changes between press and release
(inject into the window focused at press time, or abort and toast — pick one, document it) ·
transcript containing Romanian diacritics and characters outside ASCII.
GATE (all measured, not asserted): 20 consecutive real dictations, p50/p95 within
`latency_budget_ms` · a dictation whose transcript contains non-ASCII characters (curly quotes, em dash,
accented loanword) and one containing
`snake_case_id, {braces}, "quotes"` both land byte-identical · clipboard fallback exercised
by forcing the primary path to fail, and the clipboard restored afterwards · no prompt lost
in 20 runs.
</phase_1_dictation_loop>

<phase_2_usage_logging>
**Step 1 — study the reference, then verify against disk.**
`claude-tokens` (PyPI, MIT, pure stdlib, `uv tool install claude-tokens==0.2.1`) already
parses these files correctly. Read its source before writing any parser of your own. Two
things to take from it:
  a) the token field names and nesting inside the assistant turn, and
  b) **the dedup rule** — the same assistant message appears in multiple JSONL files after
     session resume, `/rewind`, and subagent runs, so entries are counted once per unique
     `message.id`. Our tail-reader, backfill, and live meter all obey this.
Then confirm both against at least 5 real transcript files from this machine covering
different sessions, and record in `docs/verified-schemas.md` the exact field names, nesting,
and the observed Claude Code version. If our files differ from the reference, ours win and
the difference is a finding — write it down, do not normalize it away silently.
Do NOT add `claude-tokens` as a runtime dependency and do NOT import its pricing logic
(subscription, not API billing — see CLAUDE.md invariant 6).

**Step 2 — build.** Tail-reader → SQLite. Per dictated prompt store text length, word count,
files-touched count, task-type (null for now), model used, token breakdown (input / output /
cache-write / cache-read), wall time, and the set of `message.id`s it covers.

**Step 3 — backfill.** Load the full existing history; Denis has months of transcripts on
disk. Phase 4 does not wait days for data, it trains on this.

**Step 4 — manual quota entry.** A tray action storing `{weekly_pct, timestamp, source}`.
It is the only calibration ground truth; no API exists. Never display an inferred quota
number as if it were read, and always show the reading's age next to it.

GATE:
- schemas pinned with the observed Claude Code version
- `claude-tokens --json --group day` (with `CLAUDE_TOKENS_TZ=Europe/Bucharest`) and our
  backfill agree within 0.5% on total input/output/cache-write/cache-read for the same
  window; print both tables side by side. A gap larger than that is a parser bug, not
  rounding — find it before moving on
- 10 hand-checked messages match exactly, field by field
- a file containing a known duplicate `message.id` across two sessions is counted once
- tail-reader picks up a live session within 5 s without re-reading the whole file
- one manual quota reading stored and displayed with its age
</phase_2_usage_logging>

<phase_3_recommendation>
One classifier call per dictation, fired in parallel with injection — never in front of it.
The model returns a bucket only; the bucket→model mapping is a deterministic lookup in
`config.models`. The LLM never names a model, so it cannot recommend one Denis doesn't have.
Timeout at `classifier.timeout_ms` → overlay shows no recommendation. Spoken overrides from
`config.voice_commands` are stripped from the transcript before injection and logged.
GATE: `prompts/classify.evals.yml` >=10/12 · injection latency unchanged from Phase 1 within
noise · classifier killed mid-flight leaves dictation fully working · no model name appears
in output that isn't in config.
</phase_3_recommendation>

<phase_4_estimate>
Fit on the backfill: features = prompt length, word count, files touched, task bucket, model.
Target = total tokens (never USD). Start with the dumbest thing that works — bucket medians
plus quantiles — and only move to regression if it beats that on held-out data. Report both.
Display a range derived from prediction intervals, plus the sample count behind it. Map
tokens to quota % only through the manual readings, and show that mapping's confidence
separately from the token range. Out-of-distribution input → say so, don't extrapolate.
GATE: held-out MAPE and interval coverage printed for both methods · the shipped one is the
one that won · UI shows a range with n · a deliberately weird prompt produces "not enough
similar history", not a number.
</phase_4_estimate>

<phase_5_live_meter>
Hooks are triggers, not the data source: token counts live in the transcript JSONL, so hooks
tell Dict8 *when* to re-read the tail. Register `UserPromptSubmit` (ties a dictated prompt to
its session and turn), `PostToolUse` (progress ticks), and `SessionEnd` (finalize). Verify
each payload against a real session and pin it in `docs/verified-schemas.md` before depending
on a field. The meter is incremental over new `message.id`s — never a full recompute per tick.
Burn rate derives from the manual quota reading plus days remaining in the window; past
`quota.stale_after_hours`, show the staleness instead of a warning.
GATE: meter total matches the JSONL total for a finished session · a session started before
Dict8 launched is picked up on next tick · a resumed session does not double-count ·
warning never fires off a stale quota reading.
</phase_5_live_meter>

<done>
A real task, not a scripted one: prompt spoken aloud, recommendation and estimate range
visible before it goes, text in Claude Code, meter tracking the actual spend to session end,
and the logged estimate-vs-actual delta written to SQLite for later calibration.
</done>

<self_check>
Print with pass/fail before declaring any phase done:
- [ ] Every path, model, threshold read from config.yml — grep finds no hardcoded ones
- [ ] No parser field that isn't in docs/verified-schemas.md
- [ ] Every token read path dedups by message.id
- [ ] No USD figure anywhere in output or storage
- [ ] Transcript injected verbatim; no rewriting anywhere in the path
- [ ] Every AI layer degrades to plain dictation; no failure mode loses a prompt
- [ ] Latency within budget, measured this run
- [ ] Nothing built from the out-of-scope list, including "just the scaffolding"
- [ ] No mock standing in for the mic, the GPU, or the transcript files
</self_check>
