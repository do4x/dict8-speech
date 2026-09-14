# ADR-001: Application shell

Status: Accepted (Phase 0) — **LOCKED**
Date: 2026-09-14
Decider: Denis
Depends on: `docs/ADR-000-platform.md` (macOS / Apple Silicon)

## Context

Dict8 is an always-resident menu-bar tool on one Mac. It must take a held hotkey, record,
transcribe with a warm model, and put text into whatever app was frontmost — inside
`latency_budget_ms` (p50 1200 ms, p95 2500 ms, release to text).

The choice is the shell the whole thing lives in: a Python process, or a web shell
(Tauri / Electron) with a Python sidecar, or a native Swift app.

`prompts/build.md` names four criteria. Two of them decide this; two are ties, and saying
so is more useful than manufacturing a tiebreak.

## Decision

**A single Python process.** PyObjC for the menu-bar item (`NSStatusItem`), the overlay
(`NSPanel`), injection (`CGEventPost`) and the hotkey (`CGEventTap`). STT model held warm
in the same process. No web shell, no sidecar, no native code to maintain.

Shipped as a **signed `.app` bundle with a stable bundle ID** — not a bare script.

## Criterion 2 — distance from the STT engine (decisive)

The hot path is: audio buffer → STT → text → `CGEventPost`. Nothing else is in the budget,
because the model is already warm. Every process boundary added to that path costs
serialization and adds a way to lose a prompt, which invariant 2 forbids.

All three benchmark candidates are reachable in-process from Python:

| Candidate | Python path | IPC on hot path |
|---|---|---|
| `mlx-whisper` | native Python package | none |
| `faster-whisper` (CPU) | native Python package | none |
| `whisper.cpp` + Metal | `pywhispercpp`, or `ctypes` over `libwhisper.dylib` | none |

A web shell is in-process for **none** of them: it needs a Python sidecar, or the STT
integration rewritten in Rust/JS.

There is a second-order point that matters more than the latency arithmetic: **Python is
the only shell that does not pre-judge the Phase 0 benchmark.** Choosing Swift now would
quietly eliminate `mlx-whisper` and `faster-whisper` (Python-only) before a single
measurement; choosing a web shell taxes all three equally with a sidecar. Phase 0 exists
to let the benchmark decide the backend, and this is the shell that lets it.

## Criterion 3 — event injection and hotkey access (decisive)

The Windows argument here was `SendInput` via `ctypes`. The macOS equivalent is stronger,
because more of the product depends on it:

- **Inject:** `CGEventCreateKeyboardEvent` + `CGEventKeyboardSetUnicodeString` + `CGEventPost`.
  The unicode-string call is what makes invariant 1 (verbatim, including diacritics)
  achievable without keycode mapping.
- **Hotkey:** hold-to-talk needs key **down and up**. `CGEventTap` on
  `keyDown`/`keyUp`/`flagsChanged` gives both, and is the only option that can bind a bare
  modifier (fn, right-option) the way the Wispr Flow feel reference does. Carbon
  `RegisterEventHotKey` cannot.
- **Fallback:** `NSPasteboard` set + restore.

PyObjC exposes all three directly — zero native code, zero build step.

The deciding detail is permissions, not API surface. `CGEventPost` and `CGEventTap` run on
**one** Accessibility (TCC) grant, and TCC binds grants to a **code-signed bundle identity**.
One process means one identity and one grant. A sidecar architecture splits the question —
whichever process posts the event needs the grant, and if the split ever moves, the user
re-authorizes. Electron would additionally need a native addon just to reach `CGEventPost`.

## Criterion 1 — cold-start latency (does not discriminate)

Dict8 starts at login and stays resident. Shell cold start is paid once a day, before the
first dictation, and is not inside release-to-text.

The cold cost that *does* matter is the first dictation after launch: model load plus lazy
Metal/ANE kernel compilation. That belongs to the STT backend, not the shell — it is
identical whichever shell wraps it, and it is measured as its own column in the Phase 0
bench. This criterion is a tie; it is recorded, not weighted.

## Criterion 4 — single-binary distribution (does not discriminate, but constrains packaging)

There is no distribution problem: one user, one Mac. There *is* a packaging requirement,
and it is sharper than the Windows equivalent — a bare `python main.py` attaches the
Accessibility grant to the Python interpreter binary, which re-prompts, breaks on
interpreter upgrade, and hands the same power to every other script that interpreter runs.
So Dict8 must be a signed `.app` with a stable bundle ID **regardless of shell**.

`py2app` / PyInstaller produce that. Tauri produces a smaller `.app` — but once a Python
sidecar and multi-hundred-MB model weights are inside it, the shell's contribution to bundle
size is noise. Tie.

## Alternatives rejected

**Tauri + Python sidecar.** IPC on the hot path, a second runtime to package and sign, and
the TCC identity question above. Its one real advantage — bundle size — is erased by the
sidecar and the weights.

**Swift/SwiftUI + WhisperKit (no sidecar).** The strongest alternative, and the only one
that is not clearly worse: Swift is the native fit for a menu-bar app, and WhisperKit
(Core ML + ANE) is fast on Apple Silicon. Rejected because it pre-decides the Phase 0
benchmark — it eliminates two of three seeded candidates before measurement — and because
WhisperKit is not in `stt.candidates`. See the falsifier below; this one is live.

**Electron.** Native addon required for `CGEventPost`, heaviest runtime, no menu-bar-native
behaviour, worst fit for an always-resident invisible tool. No advantage on any criterion.

**PySide6 or rumps instead of PyObjC.** The sharpest UI constraint in the product is that
**the overlay must never take focus** — if it activates, the frontmost app changes and the
transcript lands in the overlay instead of the terminal. The correct primitive is an
`NSPanel` with `NSWindowStyleMaskNonactivatingPanel`, a floating window level and
`ignoresMouseEvents`, in an app running `NSApplicationActivationPolicyAccessory` (no Dock
icon). PyObjC expresses that exactly. `rumps` covers the menu bar but has no overlay story.
PySide6 can approximate it, at the cost of a large dependency re-wrapping Cocoa primitives
PyObjC already exposes, with less direct control of window level and activation policy.

## What this locks — and what does not reopen it

Locked: one Python process; PyObjC; signed `.app` with a stable bundle ID.

**Does not reopen this ADR:**
- `whisper.cpp` winning the benchmark — `pywhispercpp`/`ctypes` keeps it in-process.
- The non-activating panel proving awkward in PyObjC — falling back to raw Quartz window
  calls is a change inside `dict8/ui/`, not a change of shell.

**Would falsify this ADR:** a Swift-only backend (WhisperKit, or Core ML/ANE directly)
beating the best Python-reachable backend by a margin that changes whether p50 1200 ms is
met at all. Then the shell follows the STT engine into Swift and this ADR is superseded —
not amended. Nothing short of that margin is worth the rewrite.
