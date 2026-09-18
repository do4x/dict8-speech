# Check: HANDOFF.md and the "Voice → Claude Code MVP" brief — 2026-09-18

Re-ran the handoff's three checks on this machine today. Everything it claims holds.

| Check | Handoff said | Today |
|---|---|---|
| `scripts/gate_phase1.py` | 8/8 | 8/8 (3,473 occurrences → 1,401 unique ids; reconcile delta 0.000%) |
| `scripts/gate_classifier.py` | 11/12, margin 0–170 ms | 11/12, 619–725 ms; the one miss was again a first call timing out at 801 ms while the model loaded |
| `dict8 usage --days 7` | per-day table, no USD | 5 days, 475 msgs, 85.7M tokens, 95.8% cache reads |
| config TBDs | 26 | 26 |
| Hooks registered | none | none (`~/.claude/settings.json` has no `hooks` key) |
| Turns in DB | 82 | 83 (this session writes transcripts) |

## The brief is the pre-repo Windows brief, not a new one

The handoff expected a "new prompt (sharper feature set, new architecture)". The text pasted
is the original brief that `d8bda26` was seeded from: Windows first, RTX 5070 Ti, faster-whisper
on CUDA, dictation before usage logging. Denis voided the platform line on 2026-09-14
(`docs/ADR-000-platform.md`); this machine is an Apple M5 with no NVIDIA GPU. Everything below
is checked on macOS. If Windows is real again, that is a restart, not a rework — say so and the
loop in `docs/LOOP.md` stops.

## Requirement by requirement

| # | Brief says | Exists? | Evidence | Action |
|---|---|---|---|---|
| 1 | Hold hotkey → speak → release → text in the active terminal | No | `dict8/` has `usage/ advise/ cli.py config.py` only; no `audio stt inject permissions ui` | Build (Phase 4). Needs Denis: hotkey keys, three TCC grants, 20 real dictations |
| 2 | Classify the prompt, recommend a configured model | Half | Classifier: `dict8/advise/classifier.py`, 11/12 today. Recommendation: `models[]` is 3 TBDs, no lookup code | Keep classifier. Denis fills `models`. Build lookup + overlay chip |
| 3 | Estimated usage/quota cost range before send | No | `dict8 --help`: no `estimate`, no `quota`. `estimate.method: heuristic`, nothing fitted | Build (Phase 2: U1–U3 in LOOP.md) |
| 4 | Send, or spoken override ("use Opus" / "send") | No | `voice_commands` designed in `config.yml`; nothing consumes it | Build (Phase 5), after 1 |
| 5 | Live meter while Claude Code works | No | No `dict8/hooks/`; no hooks registered | Build (Phase 6). The tail-reader it needs exists and passes the live-tail gate |
| 6 | Windows first, RTX 5070 Ti, CUDA | **Conflict** | ADR-000 (Denis, 09-14): macOS / Apple Silicon. `hardware.chip: Apple M5` | Proceed on macOS. Needs Denis only if Windows is real |
| 7 | Python or Tauri/Electron shell, justified once | Yes | `docs/ADR-001-shell.md`, LOCKED: one Python process, PyObjC | Keep |
| 8 | Local faster-whisper / whisper.cpp | Half | `bench/stt_bench.py` written, never run; `stt.*` 4 TBDs. faster-whisper is CPU-only on a Mac | Run bench (Phase 3, U6) |
| 9 | Global hotkey, keystroke injection, clipboard fallback | Designed | `config.yml` `injection.*` three-tier path; `hotkey.*` 4 TBDs; ADR-002 unwritten | ADR-002 (U5), then Phase 4 |
| 10 | Local SQLite logging | Yes | `dict8/usage/store.py` schema v3; DB in `~/Library/Application Support/Dict8/` | Keep |
| B1 | Dictation loop first | Reversed | CLAUDE.md "Build order": usage first (Denis-provided) | Keep repo order: dictation needs Denis's hands at every step; the estimator does not |
| B2 | Per-prompt length, files-touched, model, tokens; manual weekly-% entry | Half | `turns` has all four (`files_touched` > 0 on 29/83). `dict8 quota` missing; `quota.last_reading: null` | Build `dict8 quota` (U1) |
| B3 | One cheap model call, 4 buckets, user-configured list | Half | Local mlx-lm call, not cloud (Denis 09-15). Prompts over ~100 words get no bucket (13/82) | Keep; trim output / raise budget (U4) |
| B4 | Regression on length, files touched, bucket → range | No | Length vs tokens r = −0.009 (n=81). 96% of tokens are cache reads | Build (U2): bucket quantiles first, regression only if it wins held-out |
| B5 | PreToolUse / PostToolUse / SessionEnd, burn-rate warning | No | Repo plans UserPromptSubmit + PostToolUse + SessionEnd (`config.yml` `hooks:`). PreToolUse adds nothing: tokens come from the JSONL, hooks only say when to re-read | Build (Phase 6). `quota.stale_after_hours` TBD gates the warning |
| — | "Done means": spoken prompt lands with recommendation + estimate, meter runs | No | Nothing spoken lands anywhere yet | — |

Could not run: nothing. Both gates and the CLI ran today. The STT bench still needs real speech.
