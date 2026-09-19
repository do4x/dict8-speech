"""Bucket -> model: a deterministic lookup in `config.models`, nothing else.

prompts/build.md Phase 5: "The bucket->model mapping is a deterministic lookup in
`config.models`, so the LLM can never name a model Denis doesn't have." The classifier
returns a bucket; this module turns it into one `models[]` entry or into nothing. There is
no fallback model, no "closest" match and no default: a bucket no entry lists (today
`unknown`) and a bucket two entries list (ambiguous config) both mean **no
recommendation**, which the overlay shows as no chip (invariant 2).

The only ids this module can ever return are ids read from `models[]` in config.yml, and an
entry whose id is missing or still TBD is not an entry — it is skipped and logged, so a
half-filled config cannot turn a gap into a recommendation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from dict8.config import TBD

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelEntry:
    id: str
    label: str
    strength_line: str
    buckets: tuple[str, ...]


def _usable(value) -> bool:
    return isinstance(value, str) and value.strip() != "" and value != TBD


def models(cfg) -> list[ModelEntry]:
    """Every well-formed `models[]` entry, in config order. Malformed ones are dropped
    with a log line rather than raised: this feeds an advisory chip, never a send."""
    out: list[ModelEntry] = []
    for i, raw in enumerate(cfg.get("models") or []):
        if not isinstance(raw, dict) or not _usable(raw.get("id")):
            log.warning("recommend: models[%d] has no usable id — skipped", i)
            continue
        buckets = raw.get("buckets") or []
        if not isinstance(buckets, list):
            log.warning("recommend: models[%d].buckets is not a list — skipped", i)
            continue
        label = raw.get("label")
        strength = raw.get("strength_line")
        out.append(ModelEntry(
            id=raw["id"],
            label=label if _usable(label) else raw["id"],
            strength_line=strength if _usable(strength) else "",
            buckets=tuple(str(b) for b in buckets),
        ))
    return out


def by_id(cfg, model_id: str | None) -> ModelEntry | None:
    if not model_id:
        return None
    for m in models(cfg):
        if m.id == model_id:
            return m
    return None


def recommend(cfg, bucket: str | None) -> ModelEntry | None:
    """The single `models[]` entry whose `buckets` lists `bucket`, or None.

    None when: no bucket (classifier timed out or is not installed), the bucket is not in
    `classifier.buckets`, no entry lists it, or more than one does.
    """
    if not bucket:
        return None
    known = set(cfg.get("classifier.buckets") or [])
    if bucket not in known:
        return None
    hits = [m for m in models(cfg) if bucket in m.buckets]
    if len(hits) > 1:
        log.warning("recommend: bucket %r is listed by %d models[] entries — no "
                    "recommendation until config.yml picks one", bucket, len(hits))
        return None
    return hits[0] if hits else None
