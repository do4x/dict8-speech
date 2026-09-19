#!/usr/bin/env python3
"""p50/p95 release->text over the last N real dictations, against `latency_budget_ms`.

CLAUDE.md invariant 9: "`scripts/bench_latency.py` prints p50/p95 from the last 20 real
dictations." Reads `paths.logs/dictations.jsonl` (written by dict8.dictation — numbers only,
no transcript text). Only rows that actually delivered text count; discarded taps,
cancels and empty transcripts are reported but never averaged in.

"Real" means `source: hotkey`. `dict8 dictate --file` rows are excluded unless
`--include-file`, because a WAV read from disk skips the microphone and is not what Denis
feels. With no qualifying rows this FAILS (exit 1) rather than printing an empty pass.

    uv run python scripts/bench_latency.py [--last 20] [--include-file]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dict8 import config as config_mod  # noqa: E402
from dict8.dictation import log_path  # noqa: E402


def pct(xs: list[float], q: float) -> float:
    """Nearest-rank percentile — with n=20 the p95 is the 19th value, no interpolation."""
    s = sorted(xs)
    k = max(0, min(len(s) - 1, -(-int(q * 100) * len(s) // 100) - 1))
    return s[k]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=20)
    ap.add_argument("--include-file", action="store_true")
    a = ap.parse_args()

    cfg = config_mod.load()
    p50_budget = float(cfg.require("latency_budget_ms.release_to_text_p50"))
    p95_budget = float(cfg.require("latency_budget_ms.release_to_text_p95"))
    path = log_path(cfg)
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    sources = {"hotkey", "file"} if a.include_file else {"hotkey"}
    mine = [r for r in rows if r.get("source") in sources]
    done = [r for r in mine if r.get("outcome") == "injected"
            and isinstance(r.get("release_to_text_ms"), (int, float))][-a.last:]
    other = {}
    for r in mine:
        if r.get("outcome") != "injected":
            other[r.get("outcome")] = other.get(r.get("outcome"), 0) + 1

    print(f"log: {path}  ({len(rows)} rows; sources {sorted(sources)})")
    if other:
        print("not counted: " + ", ".join(f"{k}={v}" for k, v in sorted(other.items())))
    if not done:
        print("FAIL: no delivered dictations to measure — nothing is a pass on no data")
        return 1
    r2t = [float(r["release_to_text_ms"]) for r in done]
    stt = [float(r.get("stt_ms", 0)) for r in done]
    inj = [float(r.get("inject_ms", 0)) for r in done]
    paths: dict[str, int] = {}
    for r in done:
        paths[r.get("path")] = paths.get(r.get("path"), 0) + 1
    p50, p95 = pct(r2t, 0.50), pct(r2t, 0.95)
    print(f"n={len(done)} (last {a.last})  paths: "
          + ", ".join(f"{k}={v}" for k, v in sorted(paths.items())))
    print(f"release->text  p50 {p50:.0f} ms (budget {p50_budget:.0f})   "
          f"p95 {p95:.0f} ms (budget {p95_budget:.0f})")
    print(f"  stt          p50 {pct(stt, .5):.0f} ms   p95 {pct(stt, .95):.0f} ms")
    print(f"  inject       p50 {pct(inj, .5):.0f} ms   p95 {pct(inj, .95):.0f} ms")
    ok = p50 <= p50_budget and p95 <= p95_budget
    if len(done) < a.last:
        print(f"note: only {len(done)} of the {a.last} dictations the gate asks for")
    print("PASS" if ok else "FAIL: over budget")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
