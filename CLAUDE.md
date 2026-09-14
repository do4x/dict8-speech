# CLAUDE.md — Dict8

## What Dict8 is
Push-to-talk dictation for Claude Code on Windows. Hold hotkey → speak → release →
transcript lands in the focused terminal. Layered on top: a model recommendation,
a usage-cost range, and a live spend meter. Wispr Flow is the reference for *feel*
(sub-second, invisible, never in the way), not for feature surface.

## Invariants — violating any of these is a build failure
1. **The user's words are sacred.** Dict8 injects the transcript verbatim. It never
   rewrites, summarizes, expands, or "improves" a dictated prompt. Recommendation and
   estimate are displayed alongside, never merged into the text.
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
8. **Latency is the product.** Every commit keeps release→text within `latency_budget_ms`.
   `scripts/bench_latency.py` prints p50/p95 from the last 20 real dictations.

## Stack (locked in Phase 0, do not revisit)
Decision and rationale live in `docs/ADR-001-shell.md`. Summary: <filled in Phase 0>.

## Third-party
- `claude-tokens` (PyPI, MIT, pure stdlib) — a CLI that reads the same JSONL files and
  reports per-day/model/project token totals. It is a **reference implementation and a
  reconciliation oracle only**. Read its parser; adopt its dedup rule; run it in the Phase 2
  gate to check our numbers. Do NOT add it to the runtime path, do not import its pricing
  module, and set `CLAUDE_TOKENS_TZ` (its default timezone is Asia/Shanghai).

## Repo map
- `dict8/audio/` — capture, VAD, ring buffer
- `dict8/stt/` — faster-whisper wrapper, warm model held in memory
- `dict8/inject/` — SendInput unicode path + clipboard fallback
- `dict8/usage/` — JSONL tail-reader, dedup index, SQLite writer, backfill
- `dict8/advise/` — classifier client, config-driven model lookup, estimator
- `dict8/ui/` — tray icon, overlay, toasts. Nothing else.
- `prompts/` — classifier prompt + eval set
- `docs/verified-schemas.md` — observed JSONL + hook payload shapes
- `docs/ADR-001-shell.md` — locked stack decision

## Out of scope for v1 — do not build, do not scaffold "for later"
Any OS but Windows. Any tool but Claude Code. Other providers' quota routing (no local data
exists to fit on). USD cost display. Any UI beyond tray icon + overlay + toast. Settings GUI —
`config.yml` is the settings surface.

## Phase state
Current phase: <N>. Phases are sequential and gated. Do not start phase N+1 until the
phase N gate has been run and its output approved by Denis.
