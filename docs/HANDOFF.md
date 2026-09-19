# Dict8 — agent handoff

State as of **2026-09-18**. Branch `claude/modest-hamilton-lh72fw`, pushed head `44ea1c3`
(remote `dict8` → github.com/do4x/dict8; no `main` exists on the remote; no PR open).
Written by the previous agent (Claude, Opus 5 then Sonnet 5 sessions). **I have not seen
Denis's new prompt.**

## 0. How to read this

A state report, not instructions. Denis says the new prompt (sharper feature set, new
architecture) reflects current intent. Where it conflicts with `CLAUDE.md`,
`prompts/build.md` or `docs/ADR-001-shell.md`, the prompt wins on scope — but the CLAUDE.md
invariants are Denis's explicit rules, so surface a conflict and get a yes before dropping one.

Claims are tagged **[run 09-18]** (re-verified today), **[run 09-15]** (measured earlier, not
re-run) or **[unverified]**. Check me in two minutes:

```sh
cd /Users/doax/Projects/Dict8
uv run pytest                                      # hermetic unit tests; expect 72 passed, <1 s
uv run --quiet python scripts/gate_phase1.py       # expect 8/8; reads the real ~/.claude/projects
uv run --quiet python scripts/gate_phase2.py       # expect 5/5 (hook); add --live to spend
uv run --quiet dict8 usage --days 7                # per-day token table, never USD
uv run --extra classify --quiet python scripts/gate_classifier.py   # >=10/12; ~320-390ms
uv run --extra classify dict8 classify-backfill    # fill task_type on unclassified turns
```

**`--extra classify` is needed on the last two.** Since 2026-09-19 `mlx-lm` is an optional
extra rather than a base dependency, so a fresh checkout's headless layer (usage, quota,
estimate, hook) installs 2 packages instead of 35. `uv run --extra classify …` installs it;
plain `uv run` does not.

What plain `uv run` does *not* do is remove it again. Measured 2026-09-19: after one
`uv run --extra classify …`, `find_spec("mlx_lm")` still succeeds under plain `uv run`, and
the project env still lists 38 packages. Only an explicit `uv sync` prunes back to the
7-package base (2 runtime + the `dev` group). So on a machine that has already run the
classifier, omitting the flag may appear to work; on a fresh checkout, or after `uv sync`,
it will not. Both commands check for the backend and refuse with the exact command to run,
rather than half-working either way. Everything else, including the registered
`UserPromptSubmit` hook, runs on the base environment by design.

A *new Claude Code version* appearing in the transcripts fails gate check 1 by design (that is
how 2.1.274 was caught today). Re-verify the schema against real files, then document the
version; do not loosen the check.

Suggested deliverable for your check against the new prompt: a table of
requirement | exists? | evidence (file:line or gate output) | action (keep / rework / build).
Say so where you could not run something.

## 1. What exists

A headless usage layer plus a local task classifier. **No dictation, no UI, no hook, no
estimator.** Dirs `audio/ stt/ inject/ permissions/ hooks/ ui/` from the CLAUDE.md repo map do
not exist. `scripts/bench_latency.py` and `docs/ADR-002-hotkey.md` do not exist.

| Piece | Where | State |
|---|---|---|
| Transcript parser | `dict8/usage/parser.py` | done; gate 8/8 [run 09-18] |
| SQLite store, schema v3 | `dict8/usage/store.py` | done; additive migration v2→v3 only |
| Scanner (backfill + tail, one code path) | `dict8/usage/reader.py` | done; byte-offset cursors |
| CLI | `dict8/cli.py` | `backfill usage tail reconcile classify-backfill` |
| Config access | `dict8/config.py` | `get()` returns default for a TBD, `require()` raises `ConfigGap`, `gaps()` lists TBDs |
| Local classifier | `dict8/advise/classifier.py` | works, thin margin, coverage gap — §5 |
| Pinned schemas | `docs/verified-schemas.md` | 8 Claude Code versions documented |
| Gates | `scripts/gate_phase1.py`, `gate_classifier.py` | machine-specific (real `~/.claude`, `claude-tokens` installed) |
| STT benchmark | `bench/` | harness written, **never run** |
| Prompts | `prompts/classify.md` (+evals) used; `enhance.md` (+evals) **unused** | no enhancer client; `enhance.evals.yml` has assertion keys no runner implements |

Corpus [run 09-18]: 149 transcript files, 1,385 unique messages, 82 turns / 19 sessions /
8 projects over 12 days. DB at `~/Library/Application Support/Dict8/dict8.sqlite`; fully
regenerable: `uv run dict8 backfill && uv run dict8 classify-backfill`.

## 2. Status against `prompts/build.md`

| Phase | Status |
|---|---|
| 1 usage core | Gate 8/8 [run 09-18]. **Denis never explicitly signed it off** — I took "what's stopping you from pushing" as go-ahead to commit and push, not as gate sign-off. |
| 2 estimator + `UserPromptSubmit` hook | Not started. `dict8 quota` not built; `quota.last_reading` is null. |
| 3 shell ADR + STT bench | ADR-000/001 written and locked. ADR-002 not written. Bench never run, so `stt.*` and `hardware.mic_device` are TBD. `hardware.*` was filled from `sysctl`/`sw_vers`, not from `bench probe`. |
| 4 dictation loop | Not started. |
| 5 detect / classify / enhance | **Classifier only**, pulled forward. Detection and enhancement not built. |
| 6 live meter | Not started. |

## 3. Decisions, and who made them

- **macOS / Apple Silicon only** (ADR-000, Denis 09-14). The seed spec was Windows/CUDA — a lock made on a wrong premise, voided.
- **English-only v1** (Denis 09-14). Keeps all three STT candidates eligible.
- **ADR-001 LOCKED:** one Python process, PyObjC, signed `.app` with a stable bundle ID. Its own stated falsifier: a Swift-only STT backend (WhisperKit / Core ML) beating the best Python-reachable one enough to change whether p50 1200 ms is met — then it is *superseded, not amended*. **If the new architecture is Swift/native, this is the ADR it collides with.** The parser, store and schema facts do not depend on the shell.
- **`privacy.store_transcripts: features_only`** (Denis 09-15). No prompt text is ever stored; no text column exists. The classifier re-reads text transiently from Claude Code's own JSONL.
- **Classifier runs locally, not against a cloud API** (Denis 09-15): invariant 7, and detection can see text never meant for Claude Code. Model choice (`mlx-community/Qwen2.5-3B-Instruct-4bit` via `mlx-lm`) was mine, picked by measuring against the eval gate.
- **Classifier pulled forward from Phase 5** (Denis 09-15) because the estimator's lead feature is dead (§4.8).
- **No paid API has been called.** Nothing in the repo needs a key.
- I declined the parallel spec's `stt.device: "metal"`: `faster-whisper-cpu` is still a candidate and runs on CPU, so it pre-judges the benchmark.

## 4. Findings that will bite you

1. **Dedup by `message.id` is not optional.** Naive counting over-reports ~154–158% [run 09-15]. Most duplication is *intra*-file (same id on consecutive lines); 35 ids span files, some across projects. Winner among copies = earliest timestamp, then path, then line, so a re-run is reproducible.
2. **Two token fields are decompositions.** `usage.cache_creation` sums exactly to `cache_creation_input_tokens`; `output_tokens_details.thinking_tokens` is inside `output_tokens`. Adding either double-counts. Unchanged on 2.1.274 [run 09-18].
3. **`claude --version` (2.1.267) is not the version writing transcripts (2.1.274 now)** — the editor extension bundles its own. `version: "1.0"` lines are a different client. Never branch on version.
4. **`claude-tokens` 0.2.1 rejects `Europe/Bucharest`** (accepts UTC, Asia/Shanghai, fixed offsets), so the gate command in build.md cannot run as written. `dict8 reconcile` buckets both sides with the same derived fixed offset and excludes today (a live file makes the two readers disagree by ~2% mid-session; 0.000% once settled).
5. **Turn = all human prompts before the next reply** (queued/interrupting prompts merge), and a message belongs to exactly one turn (it is the PK). Both were real bugs first: 9 messages double-linked (turn sums 100.7% of message total); 44 prompts each opening an empty turn.
6. **`is_human_prompt` — a correction to my own first schema doc.** User-role lines include harness-injected content: slash-command wrappers, `<ide_selection>`, Skill-load dumps up to 108 KB, `[Image: …]` captions, `<task-notification>`. 347 of ~1,900 user lines [run 09-15]; the corpus was 234 "turns" before the fix, 68 genuine ones after (82 today). **`isMeta: true` is not a safe filter** — a genuine 15,681-word human prompt carries it. The fix strips any well-formed `<tag>…</tag>` and `[Image: …]`, and drops lines starting `Base directory for this skill:`. Known residual gap: hook-activation notices and scheduled goal check-ins still pass as "human" (single digits).
7. **Turn aggregates are derived**, recomputed from `turn_messages ⋈ messages` after every scan, never written at insert time — otherwise a turn freezes at whatever had arrived on the tick it was first seen.
8. **The estimator's inputs are weak.** Prompt words vs total tokens: r = −0.009 (n = 81) [run 09-18]. And **96.3% of all tokens in the corpus are cache reads** (308M of 320M; output is 0.55%). build.md's target, "total tokens", is therefore mostly a count of context re-reads × tool round-trips. How quota % weights cache reads against output is unknown — there is no API and no manual reading yet. Decide what the target means before fitting anything.
9. **Transcripts flush at turn boundaries**, so a file cannot be watched growing inside one tool call. The gate's live-tail check appends real transcript lines from a separate process instead.
10. **Script gotchas:** system `python3` is too old for `dataclass(slots=True)` — always `uv run`. Any script instantiating `Classifier` needs an `if __name__ == "__main__":` guard (spawn re-imports the caller's main module).

## 5. The classifier

- Resident mlx-lm worker **subprocess**. `classify()` returns `ClassifyResult` or `None` (fail-open, logged); a timeout kills and replaces the worker.
- **Why a subprocess:** the first version used a thread pool that cannot cancel. After one overrun every later call queued behind it and timed out — 164 of 164 in a row. A permanent wedge, indistinguishable from a healthy feature doing nothing.
- **Prompt shape:** `classify.md` minus its last line as the system message, `Input: "<text>"` as the user message, assistant prefilled with `{"bucket":`. Prefill still works on Haiku 4.5 and Qwen; it is rejected on Fable 5/5.1, Opus 5, Sonnet 5 and the 4.6–4.8 family (per the claude-api skill), so a move to those needs structured outputs.
- **Output is the bucket alone since 09-19** (U4). `confidence` and `why` were dropped from `classify.md`: nothing consumed either, build.md Phase 5 says "a bucket only", and every token of them was generated serially inside the 800 ms budget.
- **Eval gate:** 12/12 [run 09-15, twice], 11/12 [run 09-18], **11/12 [run 09-19, twice, after the cut]**. Latency collapsed with the output: **320–388 ms, median 334** [run 09-19] against 605–713 ms on the same gate minutes earlier. The margin against `timeout_ms: 800` was 0–170 ms and is now ~400 ms.
- **The 09-19 miss is a real one, not a timeout.** `"use opus"` (expected `unknown`, the voice-command case) now returns `feature-build`, reproducibly on two runs. The `confidence` field was apparently carrying some of the abstention behaviour that the `"unknown"` rule now has to carry alone. Still clears the gate; worth a prompt-rules fix before Phase 5 leans on it.
- **Coverage gap, mostly closed [run 09-19].** On a scratch copy of the real store: 74 of 109 turns bucketed before, **92 of 109 after**, and the longest prompt that fits the budget went from **155 words to 595**. The old line — "the budget covers roughly the first ~100 words" — now reads roughly the first ~600. The 17 still omitted are 674–4028 words (plus one 21-word turn whose source line no longer parses as a prompt). The long, detailed prompts that used to get nothing now mostly get a bucket.
- **Historical [run 09-18], for comparison:** 69 of 82 turns classified at 598–799 ms; all 13 omitted ≥ 93 words (median 361, max 4028), all classified ≤ 155 (median 16). A second pass omitted the same 13 (not restart noise).
- `turns.task_type_confidence` **is kept and written NULL.** Rows classified before 09-19 keep their values; nothing reads the column either way.
- **Labels are model output, not ground truth.** No labeled real turns exist; accuracy on real text is unmeasured. It was also run on *typed* history, while `classify.md` is written for speech-to-text output. Buckets: unknown 29 (34%, many `[Request interrupted by user]` and one-word turns), debug 15, quick-fix 15, feature-build 10, architecture 0. Median total tokens: unknown 657K, quick-fix 832K, debug 1.47M, feature-build 2.87M — ordered as hoped, but the interquartile ranges overlap heavily and n is 10–28 per bucket. My first report of a "10× spread" was small-n noise; it is ~4×.
- **Levers:** dropping `why`/`confidence` was the big one and is now spent (above, measured). Still untested: reuse the KV cache for the static preamble; raise `timeout_ms`; smaller model.

## 6. Debts

- ~~**Invariant 3 (no thresholds in code)**~~ **PAID 2026-09-19 (U4).** `classifier.max_output_tokens`, `classifier.load_timeout_ms`, `classifier.worker_join_timeout_ms`, `cli.tail_interval_s`, `cli.reconcile_days`, `cli.reconcile_tolerance_pct`, `quota.min_rate_span_hours` (which had been hiding as `f"{span_h:.1f}" == "0.0"`). Each carries a comment saying where its value came from; all seven are the previous literal, none is measured, none is a new TBD. **The parser's regexes and the Skill-preamble string deliberately stay in code** — they are schema, not preference: they describe content Claude Code injects, pinned in `docs/verified-schemas.md` §6, and a wrong value there silently readmits a 108 KB Skill dump into the estimator's training data. They change when the schema is re-verified, not when a config is edited. A comment at the definition says so.
- ~~`pyproject.toml` makes `mlx-lm` a hard dependency~~ **PAID 2026-09-19 (U4).** It is the optional `classify` extra. Base install: **35 packages / 326 MB → 2 packages / 964 KB**, warm-cache install 0.25 s → 0.17 s. `uv run --extra classify …` for `classify-backfill` and `gate_classifier.py`; the registered hook keeps working on the base env.
- ~~No unit tests~~ **PAID 2026-09-19 (U4).** `tests/`, 72 tests, 0.21 s, hermetic (scratch DB per test, synthetic fixtures, no `~`, no network, no model). Covers dedup by `message.id`, turn assembly, `is_human_prompt`, the decomposed token fields, and the hook's fail-open on 13 hostile stdins. Every rule was mutation-checked: breaking it in the source turns the test red.
- ~~Warnings go to stderr; nothing writes to `paths.logs`~~ **PAID 2026-09-19 (U4).** `dict8.logs.setup()` puts WARNING and up in `paths.logs/dict8.log` (created on first use) and leaves only ERROR on stderr; the CLI's `print()` summaries are untouched. `hooks.log` stays separate and non-blocking on purpose — see `dict8.hooks`. `dict8 tail` still polls (now `cli.tail_interval_s`); no daemon or launchd.
- **12 config TBDs** as of 2026-09-19 after U6 (`uv run python -c "from dict8 import config; print(config.load().gaps())"`): detect.uncertain_band, enhance.default, enhance.model, hardware.mic_device, hotkey.push_to_talk_enhanced, packaging.bundle_id, packaging.launch_at_login, packaging.signing, privacy.transcript_retention, quota.checkin_cadence, quota.last_reading, quota.stale_after_hours. Hotkey, STT, injection and `models[]` are filled as `provisional`; see `docs/LOOP.md` "Provisional defaults".
- `files-dict8-mac/` is a stale parallel copy of the spec, merged forward and kept as-is. Root `CLAUDE.md`, `config.yml`, `prompts/` are authoritative; safe to delete once the new prompt lands.

## 7. What ports if the architecture changes

Independent of the shell: `docs/verified-schemas.md`, the dedup / turn / human-prompt rules, the SQLite schema, the prompts and eval sets, the gate logic, the config convention (a TBD is a visible gap, never a default).
Coupled to Python and Apple Silicon: `classifier.py` (mlx-lm, multiprocessing), `cli.py`, `config.py`. ADR-001 says "one process, no sidecar"; the classifier's worker is a second process — same runtime, no TCC-bound calls, so no permission impact, but a deviation in letter.

## 8. Working with Denis

- **Plain language.** "What is the question here? I feel lost in all of the terms." Say what a choice changes, one decision at a time; jargon-dense option lists landed badly.
- **`CLAUDE.md` "Response length"** (added by Denis mid-session): artifacts to files, ≤ 6 lines of prose around them, exploratory questions get a verdict, not a package.
- **Cost-aware.** Asked "how much money are we talking about?" and floated other providers. Quote a dollar figure before any spend; local is preferred.
- **Wants momentum.** "What's stopping you from pushing the commits and moving onto the next phase?" Treat gates as measured checks, not ceremony; stop for real decisions (money, scope, an invariant).
- **Verify-first.** build.md: "never report a gate as passed on code you believe is correct." Several of my early passes were vacuous (a live-tail check that read 0 bytes) until the gate was made to fail on an empty measurement.

## 9. Environment

- Apple M5, 16 GB, macOS 26.6.2 (25G83), arm64; `uv` at `/opt/homebrew/bin`; ~1.7 GB model cache in `~/.cache/huggingface`.
- Harness quirks: auto mode blocked probing the environment for API keys (you do not need any); foreground `sleep` is blocked, use `run_in_background`; commands over 120 s auto-background; macOS needs `sed -i ''`.
- The session you run in writes the transcripts the parser reads, so today's numbers move under you.
- **Uncommitted at time of writing:** `CLAUDE.md`, `config.yml`, `dict8/advise/classifier.py` (corrections to the coverage and bucket-spread claims committed in `44ea1c3`), `docs/verified-schemas.md` (2.1.274 row), this file.
