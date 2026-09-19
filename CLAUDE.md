# CLAUDE.md — Dict8

## Response length
- Artifacts (prompts, CLAUDE.md, configs, code) go to files and get presented,
  not pasted inline. Inline only if under ~20 lines.
- Prose around an artifact: 6 lines max — what changed, what's assumed, what's blocked.
- Exploratory messages ("idea: X", "what about Y", "is Z useful") get an answer,
  not a package: verdict, the one thing that breaks it, what you'd need to proceed.
  Under 10 lines. Build the artifact only when asked to.
- Nothing unsolicited: no bonus sections, no pre-empting the next three questions.
- Stop when the question is answered. Length is not thoroughness.

## What Dict8 is
Push-to-talk dictation for Claude Code on macOS (Apple Silicon). Hold hotkey → speak → release →
transcript lands in the frontmost app. Layered on top: a model recommendation,
a usage-cost range, and a live spend meter. Wispr Flow is the reference for *feel*
(sub-second, invisible, never in the way), not for feature surface.

## Invariants — violating any of these is a build failure
1. **The user's words are sacred.** Raw mode injects the transcript verbatim — that is the
   default and the fallback for every failure. Enhanced mode may restructure, but it may not
   invent: no requirement, constraint, or detail the user did not say, and every constraint
   they did say survives at least as strongly. Recommendation and estimate are displayed
   alongside, never merged into the prompt text.
2. **Advisory, never blocking.** Every AI layer degrades to plain dictation on failure
   or timeout. A dead classifier means no chip on the overlay — it never means a lost prompt.
3. **Config over constants.** No path, hotkey, model name, or threshold appears in code.
   All read from `config.yml`. A `TBD` surfaces in the UI as a labeled gap.
4. **Verify, don't assume.** The Claude Code transcript JSONL schema and hook payloads are
   read from real files on this machine before any parser is written, and pinned in
   `docs/verified-schemas.md` with the observed Claude Code version. If a field isn't there,
   stop and report — do not code against a remembered shape.
5. **Deduplicate by `message.id`.** Claude Code writes the same assistant message into
   multiple JSONL files on session resume, `/rewind`, and subagent runs. Every read path —
   backfill, tail-reader, live meter — counts each `message.id` exactly once. Counting
   occurrences over-reports token totals substantially. This is the single easiest way to
   ship a wrong number, and a wrong number is worse than no number.
6. **Quota %, not dollars.** Denis is on a subscription. USD cost is never displayed and
   never used as the estimator's target. The unit is tokens, calibrated to weekly quota %
   via the manual check-in.
7. **Local only.** Audio and transcripts never leave the machine. No telemetry, no cloud STT.
   Inference runs on the Neural Engine / GPU via the backend chosen in ADR-001 — there is no
   CUDA on this machine, so any CUDA-shaped code path is a sign something was pattern-matched
   from a different platform.
7b. **Permissions are a first-class failure mode.** Microphone, Accessibility, and Input
   Monitoring can each be missing, revoked by an OS update, or defeated by Secure Input in
   another app. Every one of those produces a visible toast naming the specific permission —
   never a silent no-op, which is how synthetic keystrokes fail on macOS by default.
   Every dictation re-checks the grant; permission state is never cached as "granted once".
   A missing grant degrades to pasteboard-only with a toast, never to a swallowed prompt.
8. **Hooks fail open.** A Dict8 hook that errors, hangs, or finds no database lets the
   prompt through untouched and logs why. Dict8 sits in front of Denis's daily driver;
   it never becomes the reason a prompt didn't send.
9. **Latency is the product.** Every commit keeps release→text within `latency_budget_ms`.
   `scripts/bench_latency.py` prints p50/p95 from the last 20 real dictations.

## Stack (locked in ADR-001, do not revisit)
Decision and rationale live in `docs/ADR-001-shell.md`. Summary: a single Python process —
PyObjC for the menu-bar item, the non-activating overlay panel, `CGEventPost` injection and the
`CGEventTap` hotkey; the STT model held warm in-process. No web shell, no sidecar, no native
code to maintain. Shipped as a signed `.app` with a stable bundle ID, because the Accessibility
grant attaches to the bundle identity. Platform re-target recorded in `docs/ADR-000-platform.md`.

## Third-party
- `claude-tokens` (PyPI, MIT, pure stdlib) — a CLI that reads the same JSONL files and
  reports per-day/model/project token totals. It is a **reference implementation and a
  reconciliation oracle only**. Read its parser; adopt its dedup rule; run it in the Phase 1
  gate to check our numbers. Do NOT add it to the runtime path, do not import its pricing
  module, and set `CLAUDE_TOKENS_TZ` (its default timezone is Asia/Shanghai).
- `mlx-lm` (PyPI, Apple Silicon only) — **runtime dependency**, unlike everything else
  here. Runs `classifier.model` locally, in a resident subprocess (mirrors
  `stt.keep_resident`). Chosen over a cloud classifier call (Denis, 2026-09-15): invariant
  7 is local-only, and detection can hand the classifier text that was never meant for
  Claude Code at all — a cloud call would upload that regardless. See
  `dict8/advise/classifier.py`.

## Repo map
- `dict8/audio/` — capture, VAD, ring buffer
- `dict8/stt/` — STT backend wrapper, warm model held in memory
- `dict8/inject/` — CGEvent unicode path + pasteboard fallback + Secure Input detection
- `dict8/permissions/` — TCC checks, onboarding requests, Settings deep links
- `dict8/usage/` — JSONL tail-reader, dedup index, SQLite writer, backfill
- `dict8/advise/` — detector, classifier client, enhancer, config-driven model lookup, estimator
- `dict8/hooks/` — UserPromptSubmit / PostToolUse / SessionEnd handlers (fail-open)
- `dict8/ui/` — tray icon, overlay, toasts. Nothing else.
- `prompts/` — classifier and enhancer prompts + their eval sets
- `docs/verified-schemas.md` — observed JSONL + hook payload shapes
- `docs/ADR-000-platform.md` — platform re-target (windows -> macos), supersedes the seeded locks
- `docs/ADR-001-shell.md` — locked stack decision
- `docs/ADR-002-hotkey.md` — locked hotkey mechanism (Phase 3, not yet written)
- `bench/` — STT benchmark harness + its results table

## Out of scope for v1 — do not build, do not scaffold "for later"
Any OS but macOS, and any Mac that isn't Apple Silicon. Any tool but Claude Code.
Non-English dictation (v1 is English-only; revisiting it changes the STT model choice).
Other providers' quota routing (no local data exists to fit on). USD cost display. Any UI
beyond tray icon + overlay + toast. Settings GUI — `config.yml` is the settings surface.

## Build order
Usage layer ships first and headless (Phases 1–2: parser, estimator, `UserPromptSubmit`
hook), because it is useful with no dictation attached and it accumulates calibration data
while the shell is being built. Dictation follows (3–4), then detection/enhancement (5),
then the live meter (6). MCP was considered and rejected: an MCP tool runs inside the
model's turn, which is after the tokens are committed — the wrong side of the decision.

## Phase state
Phases 1-2 are done: Phase 1 gate 8/8, Phase 2 gate 5/5, U1-U4 in `docs/LOOP.md`. The
`UserPromptSubmit` hook is live, project-scoped. **Bar lowered by Denis, 2026-09-19:** a phase
moves on when its gate output is logged in `docs/LOOP.md`, with no separate sign-off wait.
Findings that survive one fix round become logged caveats. Values needed to test go into
config as `provisional`. Next up is the dictation MVP; see the `docs/LOOP.md` queue.

Out-of-order work already banked, from before the phases were renumbered (see git history):
- **Phase 3 partial.** `docs/ADR-001-shell.md` written and LOCKED. `docs/ADR-002-hotkey.md`
  NOT written. `bench/stt_bench.py` harness built but **never run** — it needs real speech on
  this box, so `stt.*`, `hardware.mic_device` stay TBD and the Phase 3 gate is open.
  Phase 3 is not done and does not count as done.
- **Phase 5's classifier pulled forward** (Denis, 2026-09-15): Phase 2's estimator needs a
  real feature to fit on — prompt length measured r=-0.009 against tokens (n=81), i.e.
  none. `prompts/classify.md` runs locally via `dict8/advise/classifier.py` (`mlx-lm`, see
  Third-party); eval gate 12/12 on 09-15, 11/12 on 09-18 (one call timed out under
  transient load — the latency margin is thin). Re-measured 2026-09-18 on 82 turns: 69
  classified, 13 not — every unclassified turn is >=93 words and every classified one is
  <=155, so the 800ms budget covers roughly the first ~100 words and long, detailed
  prompts get no bucket. Bucket medians are ordered as hoped (unknown 657K, quick-fix
  832K, debug 1.5M, feature-build 2.9M tokens; no `architecture` turns) but the
  interquartile ranges overlap heavily and n is 10-28 per bucket: a weak signal, not the
  ~10x spread first reported on n=8-26. Detection and enhancement (the rest of Phase 5)
  are NOT built. Full state and caveats: `docs/HANDOFF.md`.
