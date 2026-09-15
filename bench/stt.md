# bench/stt.md — Phase 0 STT benchmark

**Status: NOT RUN.** No numbers here yet, and none will be invented. The results table below
is written by `bench/stt_bench.py` on the target Mac; until it runs, `stt.*` in `config.yml`
stays `TBD` and the Phase 0 gate is open.

This benchmark could not be run in the session that built the harness: that session ran in a
Linux container with no microphone and no Apple Silicon. Measuring STT anywhere but the box
Dict8 runs on would produce a number that is worse than no number.

## What changed from the seeded plan

The seed assumed CUDA and compared `float16` vs `int8_float16` within faster-whisper. On
Apple Silicon there is no CUDA and CTranslate2 has no Metal backend, so the comparison is
between three genuinely different engines — see `docs/ADR-000-platform.md`.

| Backend | Compute | Why it is a candidate |
|---|---|---|
| `mlx-whisper` | Metal (MLX) | Apple-Silicon-native, in-process Python |
| `whispercpp-metal` | Metal (GGML) | Fastest mature C++ path; in-process via `pywhispercpp` |
| `faster-whisper-cpu` | CPU int8 | The seeded backend, honestly measured on its only Mac path |

All three are in-process from Python, which is why the shell decision (`ADR-001`) does not
depend on which one wins.

## Clip sets — one per microphone

A **clip set** is a microphone. `bench/scripts.yml` holds the exact words to read; every
set records *those same words* through a different input device, so a WER difference
between sets is attributable to the mic rather than to the sentence. Sets live in
`bench/audio/<set>/` and every row in the results table names the set it came from.

This exists because `hardware.mic_device` is an **output** of Phase 0, not an input. The
harness originally read the device from `config.yml`, which cannot work while the device
is the thing being decided — so `--device` selects it explicitly at record time and
`config.yml` is consulted only once the mic is settled.

## How to run it

```sh
# 1. hardware — fills config.yml hardware.*
uv run bench/stt_bench.py probe
uv run bench/stt_bench.py devices        # exact device names for --device below

# 2. clips — real speech, real mic. `session` walks all five, printing each script.
#    Run this yourself in a terminal: it waits on Enter and you have to hear the countdown.
uv run bench/stt_bench.py session --set mbp     --device "MacBook Pro Microphone"
uv run bench/stt_bench.py session --set crusher --device "Crusher Evo"

#    `session` seeds bench/audio/<set>/{coding_prompt,unicode}.txt from the script.
#    If you deviated from the script, EDIT those files to what you actually said —
#    WER is scored against them.

# 3. measure — one invocation per backend, results accumulate across backends and sets
uv run --with mlx-whisper     bench/stt_bench.py run
uv run --with pywhispercpp    bench/stt_bench.py run
uv run --with faster-whisper  bench/stt_bench.py run
```

### Clip quality is checked, and a bad clip stops the run

A clip that is merely *quiet* still transcribes into confident-looking text, so a bad
recording shows up as a bad **model** rather than as a bad **recording**. The original
check was `peak < 1500`, which cannot catch this: one keypress or breath pins the peak
near full scale while the speech sits 20 dB below it. The first real recording session
produced three clips that passed that check and were unusable.

Each clip is now scored on RMS level and on how much of the window actually carries
speech, and `run` refuses to measure a clip that fails (`--allow-poor-audio` to override
deliberately):

| check | threshold | what it catches |
|---|---|---|
| RMS level | > -40 dBFS | mic too far away, input gain too low |
| speech coverage | > 50% of the window | started late, finished early, mostly room tone |
| peak | < -1 dBFS | clipping |

Coverage uses a threshold of `min(noise_floor + 12 dB, loud_frames - 20 dB)`. Anchoring
only to the loud frames puts the threshold *below* the room tone on a mostly-silent clip,
so silence scores as speech; anchoring only to the noise floor cuts into speech on a clip
with no pauses. Validated against both failure modes plus a noisy room.

### The mic opens wider than the window

Capture runs `LEAD_IN_S` (2 s) before and `TAIL_S` (1.5 s) after the window that is kept,
because human reaction time and audio-device spin-up — a Bluetooth headset especially —
otherwise eat the front of every clip. Timing clips are trimmed back to exactly their
nominal length afterwards, since RTF is computed against that number; fidelity clips keep
the padding, because only their words matter and trailing silence is harmless.

The harness refuses to run if a clip is missing, skips a backend that is not installed
rather than guessing, and records a backend that fails to load as a `FAILED` row rather
than dropping it from the comparison. Within one `run`, a model is loaded once and reused
across clip sets, so only the first set carries a real cold-start number; the others are
marked `warm*` rather than reporting a warm number in a column labelled cold.

## How to read it

- **Warm median is the product number.** Cold first call is paid once per launch and is
  reported separately; it is not inside `release_to_text_p50`.
- **The budget column is headroom, not slack.** `latency_budget_ms.release_to_text_p50` is
  1200 ms for the *whole* path — capture stop, VAD trim, STT, injection, overlay. A model
  that spends 1100 ms on a 10 s clip has not passed anything.
- **WER is an aid to the by-eye judgement, not the gate.** A split identifier
  (`get_user_data` → `get user data`) can score WER 1.0 on a short clip while a fluent-but-wrong
  transcript scores better. Read the hypotheses in `bench/results.json`. The Phase 1 gate is
  whether `snake_case_id, {braces}, "quotes"` and the non-ASCII characters land byte-identical.

## Settled

- **English only for v1** (Denis, 2026-09-14). All three candidates stay in the race,
  `stt.language` is `en`, and the language-detection pass is off the hot path. Re-opening
  other languages disqualifies `small.en` and `medium.en` and forces a re-run.
- **Two microphones, measured, not assumed** (Denis, 2026-09-15). The box has a built-in
  array and a Bluetooth headset; the headset enumerates at 16 kHz, i.e. HFP/SCO narrowband.
  Rather than guess what that costs, both are recorded as clip sets and scored side by side.
  `hardware.mic_device` is set from the result.
- The non-ASCII clip is **not** gone — it is now English speech that *renders* as non-ASCII
  (curly quotes, em dash, `café`/`naïve`). Invariant 1 still has to carry those verbatim
  through injection, and English models do emit them.

## Open question — Denis decides, measurement cannot

1. **Model size vs unified memory.** The model is held warm in memory for the life of the app
   alongside everything else on the machine. `hardware.unified_memory_gb` (from `probe`) is a
   real constraint on `large-v3-turbo`, and it is not visible in a transcription-time table.
2. **Which microphone to standardise on.** The table will show what the headset costs in
   WER. Whether that cost is worth paying is a question about how you actually work, not
   about accuracy — measurement can price it, it cannot decide it.

<!-- BENCH:BEGIN — generated by bench/stt_bench.py, do not hand-edit -->
_No run yet. `uv run bench/stt_bench.py run` replaces this block._
<!-- BENCH:END -->
