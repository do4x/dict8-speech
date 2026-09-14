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
    uv run bench/stt_bench.py devices          # pick hardware.mic_device
    uv run bench/stt_bench.py record 3s 3      # record a clip with the real mic
    uv run bench/stt_bench.py record coding_prompt 20
    uv run bench/stt_bench.py record romanian 15
    uv run --with mlx-whisper bench/stt_bench.py run
    uv run --with pywhispercpp bench/stt_bench.py run
    uv run --with faster-whisper bench/stt_bench.py run

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
REPORT = BENCH / "stt.md"
SAMPLE_RATE = 16_000
WARM_REPS = 5

TIMING_CLIPS = ["3s", "10s", "30s"]
FIDELITY_CLIPS = ["coding_prompt", "romanian"]


def load_config() -> dict:
    return yaml.safe_load((ROOT / "config.yml").read_text())


def load_model_ids() -> dict:
    return yaml.safe_load((BENCH / "model_ids.yml").read_text())


def clip_path(name: str) -> pathlib.Path:
    return AUDIO / f"{name}.wav"


def reference_path(name: str) -> pathlib.Path:
    return AUDIO / f"{name}.txt"


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


def cmd_record(args) -> int:
    import numpy as np
    import sounddevice as sd
    import wave

    AUDIO.mkdir(parents=True, exist_ok=True)
    dest = clip_path(args.name)
    if dest.exists() and not args.force:
        print(f"! {dest.relative_to(ROOT)} exists — pass --force to overwrite.", file=sys.stderr)
        return 2

    cfg = load_config()
    device = cfg.get("hardware", {}).get("mic_device")
    device = None if device in (None, "TBD") else device
    print(f"Recording {args.seconds}s from {device or 'system default'} — speak now.")
    for n in (3, 2, 1):
        print(f"  {n}...", flush=True)
        time.sleep(1)
    buf = sd.rec(int(args.seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                 channels=1, dtype="int16", device=device)
    sd.wait()

    with wave.open(str(dest), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(buf.tobytes())

    peak = int(np.abs(buf).max())
    print(f"wrote {dest.relative_to(ROOT)}  peak={peak}/32767")
    if peak < 1500:
        print("! that is very quiet — check the input device before trusting the numbers.")
    if args.name in FIDELITY_CLIPS:
        ref = reference_path(args.name)
        if not ref.exists():
            print(f"! now write what you actually said into {ref.relative_to(ROOT)} "
                  f"— fidelity scoring needs it.")
    return 0


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

def require_clips() -> list[str]:
    missing = [n for n in TIMING_CLIPS + FIDELITY_CLIPS if not clip_path(n).exists()]
    for name in FIDELITY_CLIPS:
        if clip_path(name).exists() and not reference_path(name).exists():
            missing.append(f"{name}.txt (reference transcript)")
    return missing


def cmd_run(args) -> int:
    cfg = load_config()
    ids = load_model_ids()
    candidates = cfg["stt"]["candidates"]
    language = None if cfg["stt"]["language"] in (None, "TBD", "auto") else cfg["stt"]["language"]

    missing = require_clips()
    if missing:
        print("! refusing to run — record these first (real mic, real speech):", file=sys.stderr)
        for m in missing:
            print(f"    {m}", file=sys.stderr)
        print("\n    uv run bench/stt_bench.py record 3s 3", file=sys.stderr)
        return 2

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
            print(f"\n=== {backend} / {model} ({model_id})")
            try:
                row = _measure(fn, backend, model, model_id, ids[model], language)
            except Exception as exc:  # a backend that cannot load is a result, not a crash
                print(f"  FAILED: {type(exc).__name__}: {exc}")
                row = {"backend": backend, "model": model, "error":
                       f"{type(exc).__name__}: {exc}"[:200]}
            rows = [r for r in rows
                    if not (r.get("backend") == backend and r.get("model") == model)]
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


def _measure(fn, backend, model, model_id, model_meta, language) -> dict:
    row = {"backend": backend, "model": model, "model_id": model_id, "timing": {}}

    t0 = time.perf_counter()
    fn(model_id, str(clip_path("3s")), language)          # cold: load + lazy kernel compile
    row["cold_first_call_s"] = round(time.perf_counter() - t0, 3)
    print(f"  cold first call: {row['cold_first_call_s']}s")

    for clip in TIMING_CLIPS:
        wav, samples = str(clip_path(clip)), []
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
        if clip == "romanian" and model_meta.get("english_only"):
            row["fidelity"][clip] = {"skipped": "english-only model — cannot emit Romanian"}
            print(f"  {clip}: n/a (english-only model)")
            continue
        ref = reference_path(clip).read_text()
        hyp, _ = fn(model_id, str(clip_path(clip)), language)
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


def _write_report(cfg: dict, rows: list) -> None:
    budget = cfg["latency_budget_ms"]["release_to_text_p50"]
    out = ["<!-- BENCH:BEGIN — generated by bench/stt_bench.py, do not hand-edit -->",
           f"_Generated {time.strftime('%Y-%m-%d %H:%M %Z')} on "
           f"`{sysctl('machdep.cpu.brand_string')}`._", "",
           "### Transcription time (warm, median of 5)", "",
           "| backend | model | cold 1st call | 3s | 10s | 30s | RTF@10s | 10s vs p50 budget |",
           "|---|---|---|---|---|---|---|---|"]

    for r in sorted(rows, key=lambda r: (r["backend"], r["model"])):
        if "error" in r:
            out.append(f"| {r['backend']} | {r['model']} | — | — | — | — | — | "
                       f"FAILED: {r['error']} |")
            continue
        t = r["timing"]
        ten = t["10s"]["median_ms"]
        headroom = budget - ten
        verdict = f"{headroom:+d} ms" + ("" if headroom > 0 else "  **over**")
        out.append(f"| {r['backend']} | {r['model']} | {r['cold_first_call_s']}s | "
                   f"{t['3s']['median_ms']} ms | {ten} ms | {t['30s']['median_ms']} ms | "
                   f"{t['10s']['rtf']} | {verdict} |")

    out += ["", "STT is one term in release-to-text; the budget column is headroom for "
            "capture, VAD, injection and overlay, not slack.", "",
            "### Fidelity", "",
            "| backend | model | coding-prompt WER | Romanian WER | diacritics |",
            "|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["backend"], r["model"])):
        if "error" in r:
            continue
        f = r["fidelity"]
        cp = f.get("coding_prompt", {})
        ro = f.get("romanian", {})
        ro_cell = "n/a (english-only)" if "skipped" in ro else f"{ro.get('wer', '—')}"
        out.append(f"| {r['backend']} | {r['model']} | {cp.get('wer', '—')} | {ro_cell} | "
                   f"{ro.get('non_ascii', cp.get('non_ascii', '—'))} |")

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
    rec = sub.add_parser("record", help="record a clip with the real mic")
    rec.add_argument("name", choices=TIMING_CLIPS + FIDELITY_CLIPS)
    rec.add_argument("seconds", type=float)
    rec.add_argument("--force", action="store_true")
    sub.add_parser("run", help="benchmark every importable backend x model")

    args = ap.parse_args()
    return {"probe": cmd_probe, "devices": cmd_devices,
            "record": cmd_record, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
