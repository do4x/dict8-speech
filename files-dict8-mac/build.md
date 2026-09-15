# prompts/build.md — paste into Claude Code (Opus, max effort) at repo root

<role>
You are the sole engineer on Dict8, a local macOS tool Denis will use every working day
on his own Apple Silicon machine. You are not producing a demo. Every phase ends with the
thing actually working against real hardware, real audio, real TCC permissions, and real
Claude Code transcript files — not against mocks you wrote.
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

<scope_discipline>
Do what the phase asks. Anything else you think is needed goes at the end of the phase
report as a one-line proposal — not in the diff.
</scope_discipline>

<phase_1_usage_core>
Headless. No UI, no audio, no model calls. A CLI (`dict8 usage`) and a SQLite file.

**Step 1 — study the reference, then verify against disk.**
`claude-tokens` (PyPI, MIT, pure stdlib, `uv tool install claude-tokens==0.2.1`) already
parses these files correctly. Read its source before writing any parser of your own. Take
two things: the token field names and nesting inside the assistant turn, and **the dedup
rule** — the same assistant message appears in multiple JSONL files after session resume,
`/rewind`, and subagent runs, so entries count once per unique `message.id`.
Then confirm both against at least 5 real transcript files from this machine covering
different sessions, and record in `docs/verified-schemas.md` the exact field names, nesting,
and the observed Claude Code version. If our files differ from the reference, ours win and
the difference is a finding — write it down, do not normalize it away silently.
Do NOT add `claude-tokens` as a runtime dependency and do NOT import its pricing logic.

**Step 2 — build.** Tail-reader → SQLite. Per turn store prompt text length, word count,
files-touched count, task-type (null for now), model used, token breakdown, wall time, and
the set of `message.id`s it covers.

**Step 3 — backfill** the full existing history. Months of transcripts are already on disk;
nothing downstream waits for new data.

GATE:
- schemas pinned with the observed Claude Code version
- `claude-tokens --json --group day` (with `CLAUDE_TOKENS_TZ=Europe/Bucharest`) and our
  backfill agree within 0.5% on total input/output/cache-write/cache-read for the same
  window; print both tables side by side. A larger gap is a parser bug, not rounding
- 10 hand-checked messages match exactly, field by field
- a known duplicate `message.id` across two sessions is counted once
- tail-reader picks up a live session within 5 s without re-reading the whole file
</phase_1_usage_core>

<phase_2_estimator_and_hook>
Still headless — and already useful on its own, before any dictation exists.

Estimator: fit on the backfill. Features = prompt length, word count, files touched, task
bucket (null for now), model. Target = total tokens, never USD. Start with bucket medians
plus quantiles and only move to regression if it beats that on held-out data; report both.
Output a range with the sample count behind it. Out-of-distribution → say so, don't extrapolate.

Manual quota entry: `dict8 quota 47` stores `{weekly_pct, timestamp, source}`. It is the only
calibration ground truth; no API exists. Never present an inferred number as a read one, and
always show the reading's age.

Hook: register `UserPromptSubmit` in `paths.claude_settings`. On submit, the hook computes
the estimate locally (no model call) and surfaces it as context. It must fail open — a crash,
a missing DB, a slow disk, none of them block the prompt. Ship a kill switch.

GATE: held-out MAPE and interval coverage printed for both methods, shipped one is the winner ·
hook fires on a real prompt and the estimate appears · hook with the DB deleted still lets the
prompt through, and says why in the log · burn-rate warning never fires off a reading older
than `quota.stale_after_hours` · a deliberately weird prompt gets "not enough similar history"
</phase_2_estimator_and_hook>

<phase_3_shell_decision>
No product code. Three outputs:
- `docs/ADR-001-shell.md`: Swift menu-bar app (NSStatusItem) with a Python sidecar vs pure
  Python (rumps/PyObjC) vs Tauri. Decide on cold-start latency, distance from the STT runtime,
  access to CGEventTap and the Accessibility API, and how TCC grants survive a rebuild — an
  unsigned binary re-prompts for permissions on every build, which is a daily-friction cost,
  not a distribution detail. One page, decision stated, alternatives rejected with reasons.
  LOCKED after this.
- `docs/ADR-002-hotkey.md`: CGEventTap vs RegisterEventHotKey for hold-to-talk. The deciding
  question is clean key-up delivery while another app holds focus, and what each costs in
  permissions. Fill `hotkey.mechanism`.
- `bench/stt.md`: on this actual chip, benchmark whisper.cpp with the Core ML encoder,
  mlx-whisper, and faster-whisper on CPU (CTranslate2 — there is no CUDA here). For each
  candidate model x compute_type measure transcription time for 3 s / 10 s / 30 s of real
  recorded speech, plus first-run vs warm time (Core ML model compilation is a one-time cost
  that will otherwise look like a latency bug), plus WER-by-eye on a dictated coding prompt
  containing identifiers and punctuation. Fill `stt.*` from the winner.
GATE: both ADRs written and locked · benchmark table has real numbers from this machine,
warm and cold · `stt.backend`, `stt.model`, `stt.compute_type`, `hardware.chip`,
`hardware.unified_memory_gb` no longer TBD.
</phase_3_shell_decision>

<phase_4_dictation_loop>
Onboarding requests Microphone, Accessibility, and Input Monitoring, explains what each is
for in one line, and deep-links to the right Settings pane. Then: hotkey → record →
transcribe → text in the frontmost app. No detection, no enhancement, no recommendation.
Model stays resident between dictations.
Required handling: any of the three permissions missing or revoked after an OS update ·
mic busy or missing · hotkey already claimed by another app or by macOS itself · release
with <300 ms of audio (discard silently) · frontmost app changes between press and release
(inject into the app frontmost at press time, or abort and toast — pick one, document it) ·
Secure Input active in another app, which makes synthetic keystrokes vanish with no error ·
transcript containing Romanian diacritics and characters outside ASCII.
GATE (measured, not asserted): 20 consecutive real dictations, p50/p95 within
`latency_budget_ms`, measured warm · a dictation with Romanian diacritics and one with
`snake_case_id, {braces}, "quotes"` both land byte-identical in Terminal AND in the Claude
Code TUI · pasteboard fallback exercised by forcing the primary path to fail, pasteboard
restored after · Secure Input simulated (focus a password field) and the fallback engages ·
revoking Accessibility mid-session produces a toast, not a silent no-op · no prompt lost
in 20 runs.
</phase_4_dictation_loop>

<phase_5_detect_classify_enhance>
Three things on one pipeline. Onboarding asks once: enhance `auto`, `manual`, or `off`,
stored as `enhance.default`. `hotkey.push_to_talk_enhanced` forces it regardless.

**Detection** decides whether a dictation is a prompt for Claude Code or ordinary text.
The local heuristic runs always and must fit `latency_budget_ms.detect_heuristic_max` —
the strongest signal is nearly free (NSWorkspace frontmost bundle id, plus the AX window
title when the terminal hosts several tabs),
the rest are cheap text features from `config.detect.signals`. Scores inside
`detect.uncertain_band` escalate to the classifier call; everything else is decided locally.
Log BOTH verdicts on every dictation from day one, agreement and disagreement alike. After
a week of real use, print the confusion matrix and retune the band — that is the experiment,
and it costs nothing to run because the classifier call is already being made for the bucket.

**Classification** (`prompts/classify.md`) returns a bucket only. The bucket→model mapping
is a deterministic lookup in `config.models`, so the LLM can never name a model Denis
doesn't have. Recommendation is advisory: shown on the overlay, never enforced.

**Enhancement** (`prompts/enhance.md`) runs only when detection says prompt AND the bucket
is not in `enhance.skip_buckets`. It shows the result before injection when
`enhance.accept_required_when` matches; otherwise it injects and leaves the raw transcript
one keypress away. Timeout → raw transcript, toast. Voice commands from
`config.voice_commands` are stripped before any of this and logged.

GATE: `prompts/classify.evals.yml` >=10/12 · `prompts/enhance.evals.yml` 8/8 · raw-mode p50/p95
unchanged from Phase 4 within noise · enhance path within `enhance_total_max` · killing the
classifier or enhancer mid-flight still delivers the raw transcript · escalation rate under
`detect.max_escalation_rate` · no model name in output that isn't in config · enhancement
tokens appear as their own line item.
</phase_5_detect_classify_enhance>

<phase_6_live_meter>
Hooks are triggers, not the data source: token counts live in the JSONL, so hooks say *when*
to re-read the tail. Add `PostToolUse` (progress ticks) and `SessionEnd` (finalize) to the
`UserPromptSubmit` hook from Phase 2. Verify each payload against a real session and pin it
in `docs/verified-schemas.md` before depending on a field. The meter is incremental over new
`message.id`s — never a full recompute per tick. Log estimate-vs-actual per turn.
GATE: meter total matches the JSONL total for a finished session · a session started before
Dict8 launched is picked up on the next tick · a resumed session does not double-count ·
estimate-vs-actual deltas written for every turn.
</phase_6_live_meter>

<done>
A real task, not a scripted one: prompt spoken aloud, detected as a prompt, enhanced if the
mode says so, recommendation and estimate range visible before it goes, text in Claude Code,
meter tracking actual spend to session end, and the estimate-vs-actual delta logged.
</done>

<self_check>
Print with pass/fail before declaring any phase done:
- [ ] Every path, model, threshold read from config.yml — grep finds no hardcoded ones
- [ ] No parser field that isn't in docs/verified-schemas.md
- [ ] Every token read path dedups by message.id
- [ ] No USD figure anywhere in output or storage
- [ ] Raw mode injects verbatim; enhanced mode invented nothing and dropped no constraint
- [ ] Every AI layer degrades to raw dictation; no failure mode loses a prompt
- [ ] Hooks fail open
- [ ] Latency within budget, measured this run
- [ ] Nothing built from the out-of-scope list, including "just the scaffolding"
- [ ] No mock standing in for the mic, the chip, the permission prompts, or the transcript files
</self_check>
