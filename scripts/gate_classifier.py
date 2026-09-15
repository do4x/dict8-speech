#!/usr/bin/env -S uv run --quiet --with mlx-lm --with pyyaml python
"""Classifier gate: the real eval set, run for real, against the real local model.

Ship gate per prompts/classify.evals.yml: >=10/12.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dict8 import config as config_mod
from dict8.advise.classifier import Classifier

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    cfg = config_mod.load()
    cases = yaml.safe_load((REPO / "prompts" / "classify.evals.yml").read_text())

    clf = Classifier(cfg)
    t0 = time.monotonic()
    clf.warm()
    print(f"model loaded in {time.monotonic() - t0:.1f}s ({clf.model_id})\n")

    passed = 0
    latencies = []
    for c in cases:
        result = clf.classify(c["in"])
        got = result.bucket if result else None
        ok = got == c["expect"]
        passed += ok
        lat = f"{result.latency_ms:.0f}ms" if result else "n/a"
        print(f"[{'PASS' if ok else 'FAIL'}] expect={c['expect']:<14} got={str(got):<14} "
              f"{lat:<8} in={c['in'][:55]!r}")
        if result:
            latencies.append(result.latency_ms)
    clf.close()

    print(f"\n{passed}/{len(cases)}  (gate: >=10/12)  restarts={clf.restarts}")
    if latencies:
        latencies.sort()
        print(f"latency: min={latencies[0]:.0f}ms median={latencies[len(latencies)//2]:.0f}ms "
              f"max={latencies[-1]:.0f}ms  (budget: classifier.timeout_ms="
              f"{cfg.get('classifier.timeout_ms')}ms)")
    return 0 if passed >= 10 else 1


if __name__ == "__main__":
    raise SystemExit(main())
