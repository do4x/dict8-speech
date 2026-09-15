#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "numpy", "sounddevice"]
# ///
"""Phase 0 STT benchmark for Dict8 (macOS / Apple Silicon).

Measures real recorded speech through real backends on the real machine. It does not
synthesize audio and it has no mock backend: if a clip is missing it refuses to run, and
if a backend is not installed it says so in the table rather than inventing a row.

Candidates, models and the latency budget are read from config.yml. Backend artifact ids
are read from bench/model_ids.yml. Nothing about the model set is hardcoded here.

Usage (from repo root):
    uv run bench/stt_bench.py probe            # fill hardware.* in config.yml
    uv run bench/stt_bench.py devices          # list input devices
    uv run bench/stt_bench.py session --set mbp --device "MacBook Pro Microphone"
    uv run --with mlx-whisper bench/stt_bench.py run
    uv run --with pywhispercpp bench/stt_bench.py run
    uv run --with faster-whisper bench/stt_bench.py run

A clip SET is a microphone: the same scripted words (bench/scripts.yml) recorded through
each candidate input device, so a WER difference between sets is attributable to the mic
and not to the sentence. `run` measures every recorded set.

`run` appends to bench/results.json and rewrites the table in bench/stt.md, so the three
backend invocations above accumulate into one comparison.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import platform
import re
import statistics
import subprocess
import sys
import time
import unicodedata

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
AUDIO = BENCH / "audio"
RESULTS = BENCH / "results.json"
SCRIPTS = BENCH / "scripts.yml"
REPORT = BENCH / "stt.md"
SAMPLE_RATE = 16_000
WARM_REPS = 5

# Capture opens this much before and after the window that is actually kept. Human
# reaction time and audio-device spin-up (a Bluetooth headset especially) otherwise eat
# the front of every clip: a window exactly as long as the script has no slack at all.
# Timing clips are trimmed back to their exact nominal length afterwards, because RTF is
# computed against that number; fidelity clips keep the padding, since only their words
# matter and trailing silence is harmless.
LEAD_IN_S = 2.0
TAIL_S = 1.5

TIMING_CLIPS = ["3s", "10s", "30s"]
FIDELITY_CLIPS = ["coding_prompt", "unicode"]


def load_config() -> dict:
    return yaml.safe_load((ROOT / "config.yml").read_text())


def load_model_ids() -> dict:
    return yaml.safe_load((BENCH / "model_ids.yml").read_text())


def load_scripts() -> dict:
    return yaml.safe_load(SCRIPTS.read_text())


def audio_dir(clip_set: str) -> pathlib.Path:
    """One subdirectory per clip SET, where a set is a microphone.

    The Phase 0 comparison records the same scripted words into more than one input
    device, so every measured row has to name the device it came from. A flat audio/
    directory cannot express that.
    """
    return AUDIO / clip_set


def discovered_sets() -> list[str]:
    if not AUDIO.exists():
        return []
    return sorted(d.name for d in AUDIO.iterdir()
                  if d.is_dir() and not d.name.startswith("."))


def clip_path(name: str, clip_set: str) -> pathlib.Path:
    return audio_dir(clip_set) / f"{name}.wav"


def reference_path(name: str, clip_set: str) -> pathlib.Path:
    return audio_dir(clip_set) / f"{name}.txt"


# ---------------------------------------------------------------- audio level check
# A clip that is merely QUIET still transcribes into plausible-looking text, so a bad
# recording shows up as a bad model rather than as a bad clip. Peak alone cannot catch
# it: one keypress or breath pins the peak near full scale while the speech sits 20 dB
# below. These thresholds are on RMS and on how much of the window is actually speech.

QUIET_RMS_DBFS = -40.0      # healthy dictation sits around -30 to -20
MIN_SPEECH_COVERAGE = 0.50  # fraction of the window that must carry speech
CLIP_PEAK_DBFS = -1.0


def _dbfs(x: float) -> float:
    import math
    return 20 * math.log10(max(x, 1.0) / 32767.0)


def audio_quality(path: pathlib.Path) -> dict:
    """Level and speech-coverage report for one recorded clip."""
    import wave
    import numpy as np

    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    if a.size == 0:
        return {"verdict": "EMPTY", "notes": ["file contains no samples"],
                "rms_dbfs": -999.0, "peak_dbfs": -999.0, "coverage": 0.0,
                "duration_s": 0.0}

    frame = max(1, rate // 10)  # 100 ms
    usable = a[: a.size // frame * frame].reshape(-1, frame)
    frame_rms = np.sqrt((usable ** 2).mean(axis=1)) if usable.size else np.array([0.0])
    frame_db = np.array([_dbfs(v) for v in frame_rms])

    # Speech threshold, level-independent, taken as the more permissive of two anchors:
    #   noise floor + 12 dB  — speech sits well above room tone
    #   loud frames - 20 dB  — keeps quiet syllables of a well-filled clip inside
    # Anchoring only to the loud frames fails on a clip that is mostly silence (the
    # threshold lands below the room tone and silence scores as speech); anchoring only
    # to the noise floor fails on a clip with no silence at all (it cuts into speech).
    noise = float(np.percentile(frame_db, 5))
    loud = float(np.percentile(frame_db, 95))
    threshold = min(noise + 12.0, loud - 20.0)
    coverage = float((frame_db > threshold).mean())

    rms_db = _dbfs(float(np.sqrt((a ** 2).mean())))
    peak_db = _dbfs(float(np.abs(a).max()))

    notes = []
    if rms_db < QUIET_RMS_DBFS:
        notes.append(f"too quiet: {rms_db:.1f} dBFS RMS (want > {QUIET_RMS_DBFS:.0f}) "
                     f"— move closer to the mic or raise input gain")
    if coverage < MIN_SPEECH_COVERAGE:
        notes.append(f"only {coverage:.0%} of the window carries speech "
                     f"— started late, finished early, or the window is mismatched")
    if peak_db > CLIP_PEAK_DBFS:
        notes.append(f"clipping: peak {peak_db:.1f} dBFS — back off or lower input gain")

    return {"verdict": "ok" if not notes else "POOR", "notes": notes,
            "rms_dbfs": round(rms_db, 1), "peak_dbfs": round(peak_db, 1),
            "coverage": round(coverage, 3), "duration_s": round(a.size / rate, 2)}


def report_quality(path: pathlib.Path, q: dict, indent: str = "  ") -> None:
    print(f"{indent}{path.name}: {q['duration_s']}s  rms {q['rms_dbfs']} dBFS  "
          f"peak {q['peak_dbfs']} dBFS  speech {q['coverage']:.0%}  -> {q['verdict']}")
    for n in q["notes"]:
        print(f"{indent}  ! {n}")


# ---------------------------------------------------------------- hardware probe

def sysctl(key: str) -> str:
    try:
        return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unavailable"


def cmd_probe(_args) -> int:
    if platform.system() != "Darwin":
        print(f"! not macOS (this is {platform.system()}) — probe reports nothing usable.",
              file=sys.stderr)
        return 2
    mem = sysctl("hw.memsize")
    gb = round(int(mem) / 1024 ** 3) if mem.isdigit() else "unavailable"
    try:
        macos = subprocess.run(["sw_vers", "-productVersion"], capture_output=True,
                               text=True, check=True).stdout.strip()
    except Exception:
        macos = "unavailable"
    print("Paste into config.yml hardware.* :\n")
    print(f'  chip: "{sysctl("machdep.cpu.brand_string")}"')
    print(f'  unified_memory_gb: {gb}')
    print(f'  macos_version: "{macos}"')
    print('  mic_device: "<run: stt_bench.py devices>"')
    return 0


def cmd_devices(_args) -> int:
    import sounddevice as sd
    default_in = sd.default.device[0]
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            mark = " <- current default" if idx == default_in else ""
            print(f'[{idx}] "{dev["name"]}"  ch={dev["max_input_channels"]}'
                  f'  sr={int(dev["default_samplerate"])}{mark}')
    print("\nPut the exact name in config.yml hardware.mic_device — never \"default\".")
    return 0


def resolve_device(spec):
    """Accept a device index or a name; return (device_arg, resolved_name).

    hardware.mic_device in config.yml is an OUTPUT of this benchmark, not an input, so
    an explicit --device always wins here. Config is only consulted once the mic has
    actually been decided at the Phase 0 gate.
    """
    import sounddevice as sd
    if spec is None:
        idx = sd.default.device[0]
        return None, sd.query_devices(idx)["name"]
    dev = int(spec) if str(spec).isdigit() else spec
    info = sd.query_devices(dev, "input")
    return dev, info["name"]


def _record_one(name: str, seconds: float, clip_set: str, device, force: bool,
                trim_to: float | None = None) -> int:
    import numpy as np
    import sounddevice as sd
    import wave

    audio_dir(clip_set).mkdir(parents=True, exist_ok=True)
    dest = clip_path(name, clip_set)
    if dest.exists() and not force:
        print(f"! {dest.relative_to(ROOT)} exists — pass --force to overwrite.", file=sys.stderr)
        return 2

    capture = seconds + LEAD_IN_S + TAIL_S
    for n in (3, 2, 1):
        print(f"  {n}...", flush=True)
        time.sleep(1)
    print(f"  >>> SPEAK NOW <<<   (mic open {capture:.1f}s — keep reading to the end)",
          flush=True)
    buf = sd.rec(int(capture * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                 channels=1, dtype="int16", device=device)
    sd.wait()
    print("  ... done.", flush=True)

    if trim_to is not None:
        start = int(LEAD_IN_S * SAMPLE_RATE)
        buf = buf[start:start + int(trim_to * SAMPLE_RATE)]
        if buf.shape[0] < int(trim_to * SAMPLE_RATE):  # never label a short clip as full length
            print(f"  ! captured less than {trim_to}s after trim — not writing.",
                  file=sys.stderr)
            return 2

    with wave.open(str(dest), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(buf.tobytes())

    print(f"  wrote {dest.relative_to(ROOT)}")
    q = audio_quality(dest)
    report_quality(dest, q)
    if q["verdict"] != "ok":
        print(f"  ! RE-RECORD this clip: uv run bench/stt_bench.py record {name} "
              f"--set {clip_set} --device \"<name>\" --force")
    return 0


def cmd_record(args) -> int:
    scripts = load_scripts()
    spec = args.device
    if spec is None:
        cfg_dev = load_config().get("hardware", {}).get("mic_device")
        spec = None if cfg_dev in (None, "TBD") else cfg_dev
    try:
        device, resolved = resolve_device(spec)
    except Exception as exc:
        print(f"! no such input device {spec!r}: {exc}", file=sys.stderr)
        return 2

    seconds = args.seconds if args.seconds is not None else scripts[args.name]["seconds"]
    print(f'Recording "{args.name}" for {seconds}s into set "{args.set}" via {resolved}')
    rc = _record_one(args.name, seconds, args.set, device, args.force,
                     trim_to=seconds if args.name in TIMING_CLIPS else None)
    if rc == 0:
        _write_reference(args.name, args.set, scripts)
    return rc


def _write_reference(name: str, clip_set: str, scripts: dict) -> None:
    """Seed <clip>.txt from the scripted words, for clips that are actually scored."""
    if not scripts.get(name, {}).get("reference"):
        return
    ref = reference_path(name, clip_set)
    text = " ".join(scripts[name]["text"].split())
    if ref.exists() and ref.read_text().strip() != text:
        print(f"  (keeping your edited {ref.relative_to(ROOT)})")
        return
    ref.write_text(text + "\n")
    print(f"  reference: {ref.relative_to(ROOT)}")
    print("  ! if you deviated from the script, EDIT that file to what you actually said "
          "— WER is scored against it.")


def cmd_session(args) -> int:
    """Walk every clip in one set, in order, with the script on screen."""
    scripts = load_scripts()
    try:
        device, resolved = resolve_device(args.device)
    except Exception as exc:
        print(f"! no such input device {args.device!r}: {exc}", file=sys.stderr)
        return 2

    names = TIMING_CLIPS + FIDELITY_CLIPS
    print("=" * 72)
    print(f'CLIP SET "{args.set}"   device: {resolved}')
    print(f"{len(names)} clips. Read each script aloud at a normal dictation pace.")
    print(f"The mic opens {LEAD_IN_S:.0f}s before and {TAIL_S:.1f}s after the window that is")
    print("kept, so a slightly late start costs nothing. Start reading at SPEAK NOW and")
    print("keep going to the end of the script — do not stop early, and do not rush.")
    print("=" * 72)

    for i, name in enumerate(names, 1):
        spec = scripts[name]
        dest = clip_path(name, args.set)
        if dest.exists() and not args.force:
            print(f"\n[{i}/{len(names)}] {name}: already recorded, skipping "
                  f"(--force to redo).")
            continue
        text = " ".join(spec["text"].split())
        print(f"\n[{i}/{len(names)}] {name} — {spec['seconds']}s window")
        print("-" * 72)
        for line in _wrap(text, 70):
            print(f"  {line}")
        print("-" * 72)
        try:
            input("Press Enter when you are ready to read it... ")
        except EOFError:
            print("! session needs an interactive terminal — run it yourself, not "
                  "through a pipe.", file=sys.stderr)
            return 2
        rc = _record_one(name, spec["seconds"], args.set, device, args.force,
                         trim_to=spec["seconds"] if name in TIMING_CLIPS else None)
        if rc != 0:
            return rc
        _write_reference(name, args.set, scripts)

    missing = require_clips(args.set)
    if missing:
        print(f"\n! set \"{args.set}\" still incomplete: {missing}")
        return 2
    print(f'\nSet "{args.set}" complete: {audio_dir(args.set).relative_to(ROOT)}')
    return 0


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width)


# ---------------------------------------------------------------- backends
# Each adapter returns (text, used_gpu_or_None). They are imported lazily so that a box
# with only one backend installed still produces its rows.

def _mlx_whisper(model_id: str, wav: str, language: str | None):
    import mlx_whisper
    out = mlx_whisper.transcribe(wav, path_or_hf_repo=model_id, language=language)
    return out["text"], True  # MLX is Metal-backed by construction


def _whispercpp(model_id: str, wav: str, language: str | None):
    from pywhispercpp.model import Model
    m = _cache_get("whispercpp", model_id) or _cache_put(
        "whispercpp", model_id, Model(model_id, language=language or "en", print_progress=False))
    segs = m.transcribe(wav)
    return "".join(s.text for s in segs), None  # Metal use not reported by the binding


def _faster_whisper(model_id: str, wav: str, language: str | None):
    from faster_whisper import WhisperModel
    m = _cache_get("fw", model_id) or _cache_put(
        "fw", model_id, WhisperModel(model_id, device="cpu", compute_type="int8"))
    segments, _info = m.transcribe(wav, language=language)
    # transcribe() is lazy — the generator must be drained inside the timed region or the
    # measurement is of nothing at all.
    return "".join(s.text for s in segments), False


BACKENDS = {
    "mlx-whisper": (_mlx_whisper, "uv run --with mlx-whisper bench/stt_bench.py run"),
    "whispercpp-metal": (_whispercpp, "uv run --with pywhispercpp bench/stt_bench.py run"),
    "faster-whisper-cpu": (_faster_whisper, "uv run --with faster-whisper bench/stt_bench.py run"),
}

_MODEL_CACHE: dict = {}


def _cache_get(kind, key):
    return _MODEL_CACHE.get((kind, key))


def _cache_put(kind, key, val):
    _MODEL_CACHE[(kind, key)] = val
    return val


# ---------------------------------------------------------------- scoring

def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).strip().lower()
    return re.sub(r"\s+", " ", text)


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate: Levenshtein over word sequences, / reference length."""
    ref, hyp = normalize(reference).split(), normalize(hypothesis).split()
    if not ref:
        return float("nan")
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref)


def non_ascii_survived(reference: str, hypothesis: str) -> str:
    """Phase 1 gate cares about exact non-ASCII characters, not about WER."""
    want = {c for c in unicodedata.normalize("NFC", reference) if ord(c) > 127}
    if not want:
        return "n/a"
    got = {c for c in unicodedata.normalize("NFC", hypothesis) if ord(c) > 127}
    missing = sorted(want - got)
    return "all" if not missing else f"MISSING {''.join(missing)}"


# ---------------------------------------------------------------- run

def require_clips(clip_set: str) -> list[str]:
    missing = [n for n in TIMING_CLIPS + FIDELITY_CLIPS
               if not clip_path(n, clip_set).exists()]
    for name in FIDELITY_CLIPS:
        if clip_path(name, clip_set).exists() and not reference_path(name, clip_set).exists():
            missing.append(f"{name}.txt (reference transcript)")
    return missing


def cmd_run(args) -> int:
    cfg = load_config()
    ids = load_model_ids()
    candidates = cfg["stt"]["candidates"]
    language = None if cfg["stt"]["language"] in (None, "TBD", "auto") else cfg["stt"]["language"]

    sets = args.set or discovered_sets()
    if not sets:
        print("! no clip sets recorded under bench/audio/. Record one first:", file=sys.stderr)
        print('    uv run bench/stt_bench.py session --set mbp --device "MacBook Pro Microphone"',
              file=sys.stderr)
        return 2
    for clip_set in sets:
        missing = require_clips(clip_set)
        if missing:
            print(f'! refusing to run — clip set "{clip_set}" is incomplete '
                  f"(real mic, real speech):", file=sys.stderr)
            for m in missing:
                print(f"    {m}", file=sys.stderr)
            print(f'\n    uv run bench/stt_bench.py session --set {clip_set} '
                  f'--device "<name>"', file=sys.stderr)
            return 2
    poor = []
    for clip_set in sets:
        for name in TIMING_CLIPS + FIDELITY_CLIPS:
            path = clip_path(name, clip_set)
            q = audio_quality(path)
            if q["verdict"] != "ok":
                poor.append((clip_set, path, q))
    if poor:
        print("! refusing to run — these clips are not worth measuring. A quiet or mostly "
              "silent\n  clip still produces confident-looking text, so it would show up as "
              "a bad model\n  rather than as a bad recording:", file=sys.stderr)
        for clip_set, path, q in poor:
            print(f"\n  [set {clip_set}]", file=sys.stderr)
            report_quality(path, q, indent="  ")
        print("\n  Re-record with --force, then run again. To measure anyway and accept "
              "that the\n  fidelity numbers are about the microphone, pass --allow-poor-audio.",
              file=sys.stderr)
        if not args.allow_poor_audio:
            return 2
        print("\n! --allow-poor-audio: measuring degraded clips anyway.", file=sys.stderr)

    print(f"clip sets: {', '.join(sets)}")

    available = [b for b in candidates["backends"] if _importable(b)]
    if not available:
        print("! no candidate backend importable in this environment. Install one:", file=sys.stderr)
        for b in candidates["backends"]:
            print(f"    {BACKENDS[b][1]}", file=sys.stderr)
        return 2

    rows = _load_results()
    for backend in available:
        fn, _ = BACKENDS[backend]
        for model in candidates["models"]:
            model_id = ids.get(model, {}).get(backend)
            if not model_id:
                print(f"- {backend}/{model}: no artifact id in bench/model_ids.yml, skipping")
                continue
            # The model loads once and is reused across clip sets, so only the FIRST set
            # can observe a genuine cold first call. The rest record it as None rather
            # than reporting a warm number in a column labelled cold.
            for n, clip_set in enumerate(sets):
                print(f"\n=== {backend} / {model} / set={clip_set} ({model_id})")
                try:
                    row = _measure(fn, backend, model, model_id, language, clip_set,
                                   measure_cold=(n == 0))
                except Exception as exc:  # a backend that cannot load is a result, not a crash
                    print(f"  FAILED: {type(exc).__name__}: {exc}")
                    row = {"backend": backend, "model": model, "set": clip_set,
                           "error": f"{type(exc).__name__}: {exc}"[:200]}
                rows = [r for r in rows
                        if not (r.get("backend") == backend and r.get("model") == model
                                and r.get("set") == clip_set)]
                rows.append(row)
                _save_results(rows)

    _write_report(cfg, rows)
    print(f"\nwrote {REPORT.relative_to(ROOT)} and {RESULTS.relative_to(ROOT)}")
    return 0


def _importable(backend: str) -> bool:
    import importlib.util
    mod = {"mlx-whisper": "mlx_whisper", "whispercpp-metal": "pywhispercpp",
           "faster-whisper-cpu": "faster_whisper"}[backend]
    return importlib.util.find_spec(mod) is not None


def _measure(fn, backend, model, model_id, language, clip_set, measure_cold=True) -> dict:
    row = {"backend": backend, "model": model, "model_id": model_id,
           "set": clip_set, "timing": {}}

    if measure_cold:
        t0 = time.perf_counter()
        fn(model_id, str(clip_path("3s", clip_set)), language)  # load + lazy kernel compile
        row["cold_first_call_s"] = round(time.perf_counter() - t0, 3)
        print(f"  cold first call: {row['cold_first_call_s']}s")
    else:
        row["cold_first_call_s"] = None  # model already warm from a previous set

    for clip in TIMING_CLIPS:
        wav, samples = str(clip_path(clip, clip_set)), []
        fn(model_id, wav, language)                        # discard warmup
        for _ in range(WARM_REPS):
            t = time.perf_counter()
            fn(model_id, wav, language)
            samples.append(time.perf_counter() - t)
        samples.sort()
        secs = float(clip.rstrip("s"))
        row["timing"][clip] = {
            "median_ms": round(statistics.median(samples) * 1000),
            "max_ms": round(samples[-1] * 1000),
            "rtf": round(statistics.median(samples) / secs, 3),
        }
        print(f"  {clip}: median {row['timing'][clip]['median_ms']}ms "
              f"rtf {row['timing'][clip]['rtf']}")

    row["fidelity"] = {}
    for clip in FIDELITY_CLIPS:
        # v1 is English-only, so no candidate is disqualified here. The english_only flag
        # in model_ids.yml stays as the record of what re-opening other languages would cost.
        ref = reference_path(clip, clip_set).read_text()
        hyp, _ = fn(model_id, str(clip_path(clip, clip_set)), language)
        row["fidelity"][clip] = {
            "wer": round(wer(ref, hyp), 4),
            "non_ascii": non_ascii_survived(ref, hyp),
            "hypothesis": hyp.strip(),
        }
        print(f"  {clip}: wer {row['fidelity'][clip]['wer']:.3f} "
              f"non-ascii {row['fidelity'][clip]['non_ascii']}")
        print(f"    ref: {ref.strip()[:110]}")
        print(f"    hyp: {hyp.strip()[:110]}")
    return row


def _load_results() -> list:
    return json.loads(RESULTS.read_text()) if RESULTS.exists() else []


def _save_results(rows: list) -> None:
    RESULTS.write_text(json.dumps(rows, indent=2, ensure_ascii=False))


def _ordered(rows: list) -> list:
    return sorted(rows, key=lambda r: (r["backend"], r["model"], r.get("set", "")))


def _write_report(cfg: dict, rows: list) -> None:
    budget = cfg["latency_budget_ms"]["release_to_text_p50"]
    sets_used = sorted({r.get("set", "") for r in rows} - {""})
    out = ["<!-- BENCH:BEGIN — generated by bench/stt_bench.py, do not hand-edit -->",
           f"_Generated {time.strftime('%Y-%m-%d %H:%M %Z')} on "
           f"`{sysctl('machdep.cpu.brand_string')}`._", "",
           f"_Clip sets (microphones): {', '.join(sets_used) or 'none'}._", "",
           "### Transcription time (warm, median of 5)", "",
           "| backend | model | mic | cold 1st call | 3s | 10s | 30s | RTF@10s "
           "| 10s vs p50 budget |",
           "|---|---|---|---|---|---|---|---|---|"]

    for r in _ordered(rows):
        if "error" in r:
            out.append(f"| {r['backend']} | {r['model']} | {r.get('set', '—')} | — | — | — "
                       f"| — | — | FAILED: {r['error']} |")
            continue
        t = r["timing"]
        ten = t["10s"]["median_ms"]
        headroom = budget - ten
        verdict = f"{headroom:+d} ms" + ("" if headroom > 0 else "  **over**")
        cold = f"{r['cold_first_call_s']}s" if r.get("cold_first_call_s") is not None \
            else "warm*"
        out.append(f"| {r['backend']} | {r['model']} | {r.get('set', '—')} | {cold} | "
                   f"{t['3s']['median_ms']} ms | {ten} ms | {t['30s']['median_ms']} ms | "
                   f"{t['10s']['rtf']} | {verdict} |")

    out += ["", "STT is one term in release-to-text; the budget column is headroom for "
            "capture, VAD, injection and overlay, not slack.",
            "`warm*` = the model was already resident from the previous clip set, so no "
            "honest cold number exists for that row; the cold cost is the one measured on "
            "this model's first set.", "",
            "### Fidelity", "",
            "| backend | model | mic | coding-prompt WER | unicode-clip WER "
            "| non-ASCII chars |",
            "|---|---|---|---|---|---|"]
    for r in _ordered(rows):
        if "error" in r:
            continue
        f = r["fidelity"]
        cp = f.get("coding_prompt", {})
        uni = f.get("unicode", {})
        out.append(f"| {r['backend']} | {r['model']} | {r.get('set', '—')} | "
                   f"{cp.get('wer', '—')} | {uni.get('wer', '—')} | "
                   f"{uni.get('non_ascii', '—')} |")

    out += ["", "Hypotheses for by-eye reading are in `bench/results.json`. WER is an aid, "
            "not the gate — the gate is whether identifiers and punctuation land verbatim.",
            "", "<!-- BENCH:END -->"]

    body = REPORT.read_text() if REPORT.exists() else ""
    block = "\n".join(out)
    if "<!-- BENCH:BEGIN" in body:
        body = re.sub(r"<!-- BENCH:BEGIN.*?<!-- BENCH:END -->", block, body, flags=re.S)
    else:
        body = body.rstrip() + "\n\n" + block + "\n"
    REPORT.write_text(body)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="read chip/memory/macOS off this box for config.yml")
    sub.add_parser("devices", help="list input devices for config.yml hardware.mic_device")
    ses = sub.add_parser("session", help="record every clip of one set, guided")
    ses.add_argument("--set", required=True, help="clip set name, one per microphone")
    ses.add_argument("--device", help="input device name or index (see: devices)")
    ses.add_argument("--force", action="store_true", help="re-record clips that exist")

    rec = sub.add_parser("record", help="record a single clip with the real mic")
    rec.add_argument("name", choices=TIMING_CLIPS + FIDELITY_CLIPS)
    rec.add_argument("seconds", type=float, nargs="?", default=None,
                     help="window length; defaults to the one in bench/scripts.yml")
    rec.add_argument("--set", required=True, help="clip set name, one per microphone")
    rec.add_argument("--device", help="input device name or index (see: devices)")
    rec.add_argument("--force", action="store_true")

    r = sub.add_parser("run", help="benchmark every importable backend x model x clip set")
    r.add_argument("--set", nargs="*", help="clip sets to measure (default: all recorded)")
    r.add_argument("--allow-poor-audio", action="store_true",
                   help="measure even clips that fail the level/coverage check")

    args = ap.parse_args()
    return {"probe": cmd_probe, "devices": cmd_devices, "session": cmd_session,
            "record": cmd_record, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
