# Dict8 — build prompt package

Push-to-talk dictation in front of Claude Code, macOS on Apple Silicon, with model
recommendation, usage estimate, and a live spend meter layered on top.

## Files
- `config.yml` — the only source of truth. Fill the TBDs as phases resolve them.
- `CLAUDE.md` — commit at repo root. Persistent context + invariants for Claude Code.
- `prompts/build.md` — the phased build prompt. Paste into Claude Code (Opus, max effort).
- `prompts/classify.md` — the task-type classifier prompt (Phase 5).
- `prompts/classify.evals.yml` — its eval set. Ship gate is 10/12.
- `prompts/enhance.md` — the dictation enhancer (Phase 5).
- `prompts/enhance.evals.yml` — its eval set. Ship gate is 8/8; these test invention and
  dropped constraints, not output prettiness.

## Run order
1. Fresh repo. Commit `config.yml` and `CLAUDE.md` at root, `prompts/` beside them.
2. Resolve the TBDs you can answer now without building anything: `hotkey.push_to_talk`,
   `stt.language`, `privacy.store_transcripts`, and the `models` list. The rest are filled
   by the phase gates.
3. `uv tool install claude-tokens==0.2.1` — needed as the Phase 1 reconciliation oracle,
   not as a runtime dependency.
4. Paste `prompts/build.md`. It stops at every phase gate and waits for approval.

## Decisions still open
- Hotkey that survives the terminal, the IDE, and macOS's own reserved combinations, plus a
  second one for forced-enhance.
- `enhance.default`: auto, manual, or off — the onboarding question.
- `stt.language`: `auto` costs latency and misfires on code identifiers; `en` breaks
  Romanian dictation.
- `privacy.store_transcripts`: changes the SQLite schema, so answer before Phase 2.
