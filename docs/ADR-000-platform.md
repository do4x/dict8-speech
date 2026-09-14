# ADR-000: Target platform re-targeted from Windows to macOS (Apple Silicon)

Status: Accepted — supersedes the `LOCKED` platform lines in `config.yml` and `CLAUDE.md`
Date: 2026-09-14
Decider: Denis

## Context

The seed documents (`CLAUDE.md`, `config.yml`, `prompts/build.md`) were written against
Windows and marked several lines `LOCKED`:

- `meta.platform: "windows"  # LOCKED`
- `injection.primary: "sendinput_unicode"`
- `hardware.gpu: "RTX 5070 Ti"`, `stt.device: "cuda"`
- `CLAUDE.md` out-of-scope: "Any OS but Windows"

Denis confirmed the actual target is **macOS on Apple Silicon**. The Windows lock was
written against a machine that is not the one Dict8 runs on. A `LOCKED` line is not
evidence; it records a decision, and this one was made on a wrong premise.

## Decision

Target macOS on Apple Silicon. The Windows lines are void, not amended-around. Everything
they implied is re-opened and re-derived below, and anything that cannot be re-derived
from a document is marked `TBD` rather than translated by analogy.

## What the change invalidates

| Windows assumption | Status on macOS | Replacement |
|---|---|---|
| `SendInput` + `KEYEVENTF_UNICODE` | Does not exist | `CGEventCreateKeyboardEvent` + `CGEventKeyboardSetUnicodeString` + `CGEventPost` |
| Injection needs no OS permission | **False** | Accessibility (TCC) grant required, revocable at any time, silently no-ops without it |
| `RegisterHotKey` / `WH_KEYBOARD_LL` for hold-to-talk | Does not exist | `CGEventTap` on keyDown/keyUp/flagsChanged (same Accessibility grant), or Carbon `RegisterEventHotKey` for combo-only |
| CUDA / `stt.device: "cuda"` | No CUDA on Apple Silicon | Metal / ANE / CPU — decided by the Phase 0 benchmark |
| faster-whisper as the default backend | Runs, but **CPU-only** — CTranslate2 has no Metal backend | Backend itself becomes a benchmark output, not a default |
| `%LOCALAPPDATA%/Dict8/` | Not a macOS path | `~/Library/Application Support/Dict8/`, `~/Library/Logs/Dict8/` |
| Ship a bare script or loose `.exe` | Breaks the permission model | Signed `.app` bundle with a **stable bundle ID** — TCC grants attach to the bundle identity |

## Consequences

1. **The Phase 0 benchmark matrix is different, not merely re-parameterised.** Windows was
   one backend across compute types. Apple Silicon is three genuinely different backends
   (`mlx-whisper`, `whisper.cpp` + Metal, `faster-whisper` on CPU) across models. Comparing
   `float16` vs `int8_float16` is not the question here.
2. **A new failure class exists that Windows did not have.** The Accessibility grant can be
   absent on first run, revoked by the user, or reset by re-signing the app. Under invariant 2
   (advisory, never blocking) this must never lose a prompt — hence the three-tier injection
   path and invariant 9.
3. **`stt.backend` cannot keep its seeded default.** `config.yml` said faster-whisper with
   "whisper.cpp only if the Phase 0 benchmark beats it". On Apple Silicon that default
   pre-judges a question that the benchmark exists to answer, so it reverts to `TBD`.

## Finding — RESOLVED 2026-09-14: English-only for v1

The seeded model candidates were `small.en | medium.en | large-v3-turbo`. The `.en` Whisper
models are **English-only**: they have no language token and cannot emit Romanian text. The
Phase 1 gate requires a dictation containing Romanian diacritics to land byte-identical.
Denis scoped v1 to **English only**. All three candidates therefore stay in the race and
`stt.language` is `en` rather than `auto` — which also drops the language-detection pass from
the hot path. The injection path must still be unicode-safe: English dictation produces curly
quotes, em dashes and accented loanwords, and invariant 1 covers those the same way.

Re-opening non-English dictation disqualifies `small.en` and `medium.en` outright and forces
a re-run of the Phase 0 benchmark.
