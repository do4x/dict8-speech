#!/usr/bin/env python3
"""Provisional STT quick bench (U5, 2026-09-19) — SYNTHESIZED speech, mlx-whisper only.

Not the Phase 3 benchmark (`bench/stt_bench.py`, real speech through a real mic, still
unrun). This exists so `stt.*` can hold a provisional, measured value while Denis's
real-speech run is outstanding: clips come from macOS `say` reading bench/scripts.yml text,
so the WER it implies is optimistic and the latency is what carries over.

    uv run --extra app python bench/stt_quick.py short.wav long.wav [--models small.en large-v3-turbo]

Each model runs in a FRESH process so its load is honestly cold: "load" is weights read +
one silent warm-up pass (what `dict8 app` pays once at launch), "first" is the first real
transcription after that, then WARM_RUNS more per clip. Model ids come from
bench/model_ids.yml, candidates from config.yml `stt.candidates.models`.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WARM_RUNS = 3


def _one(model: str, clips: list[str]) -> None:
    sys.path.insert(0, str(ROOT))
    from dict8 import config as config_mod
    from dict8.stt import STT, read_wav

    ids = yaml.safe_load((ROOT / "bench" / "model_ids.yml").read_text())
    cfg = config_mod.load()
    data = json.loads(json.dumps(cfg._data))
    data["stt"].update({"backend": "mlx-whisper", "model": ids[model]["mlx-whisper"],
                        "compute": "metal", "quantization": "fp16", "vad": False})
    stt = STT(config_mod.Config(data))
    out = {"model": model, "load_ms": stt.load(), "clips": []}
    for i, clip in enumerate(clips):
        audio, rate = read_wav(clip)
        assert rate == stt.sample_rate, f"{clip}: {rate} Hz, need {stt.sample_rate}"
        first_text, first_ms = stt.transcribe(audio)
        warm = [stt.transcribe(audio)[1] for _ in range(WARM_RUNS)]
        out["clips"].append({"clip": Path(clip).name, "audio_s": len(audio) / rate,
                             "first_ms": first_ms, "warm_ms": warm, "text": first_text})
    print("JSON " + json.dumps(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--models", nargs="+")
    ap.add_argument("--one", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.one:
        _one(a.one, a.clips)
        return 0

    cfg = yaml.safe_load((ROOT / "config.yml").read_text())
    models = a.models or cfg["stt"]["candidates"]["models"]
    rows = []
    for m in models:
        p = subprocess.run([sys.executable, __file__, "--one", m, *a.clips],
                           capture_output=True, text=True)
        line = next((ln for ln in p.stdout.splitlines() if ln.startswith("JSON ")), None)
        if line is None:
            print(f"{m}: FAILED\n{p.stderr[-2000:]}", file=sys.stderr)
            continue
        rows.append(json.loads(line[5:]))

    print("| model | load + warm-up (cold) | clip | audio | first run | warm runs (ms) | warm median | RTF |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        for c in r["clips"]:
            med = statistics.median(c["warm_ms"])
            print(f"| {r['model']} | {r['load_ms']:.0f} ms | {c['clip']} | {c['audio_s']:.1f} s "
                  f"| {c['first_ms']:.0f} ms | {', '.join(f'{w:.0f}' for w in c['warm_ms'])} "
                  f"| {med:.0f} ms | {med / 1000 / c['audio_s']:.3f} |")
    print()
    for r in rows:
        for c in r["clips"]:
            print(f"{r['model']:<15} {c['clip']:<10} {c['text']!r}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
