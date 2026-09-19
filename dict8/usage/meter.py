"""The live meter: tokens spent in the active Claude Code session, and since the last
dictation, next to that dictation's estimate. Pure Python over the Store — no AppKit — so
it is tested headless; `dict8 app` polls it on a thread every `cli.tail_interval_s`.

**Reuse, not a second reader.** A tick is `dict8.usage.reader.scan()` — the same
byte-offset cursors, parser and upsert the backfill and `dict8 tail` use — followed by one
query for the rows that scan inserted. Those rows are found by SQLite `rowid`: a new
`message_id` is an INSERT and gets the next rowid, while a copy of a message already stored
(session resume, `/rewind`, a subagent replay) hits the `ON CONFLICT(message_id) DO
UPDATE` in `Store.upsert_messages`, which updates in place and keeps its rowid. So the
per-tick delta is incremental over new `message.id`s by construction (invariant 5, and
Phase 6's "never a full recompute per tick"): a duplicate cannot come back as "new".

A session's running total is seeded once, the first tick it becomes active, from
`Store.session_tokens(..., max_rowid=cursor)` — which is how a session started before Dict8
launched is picked up on the next tick — and grows by the deltas after that.

**Active session** = the session behind the newest-mtime `*.jsonl` under the transcript
root, read off that file's own `messages.session_id` rows (docs/verified-schemas.md §2 —
including subagent files under `<session>/subagents/`, whose lines carry the parent's
`sessionId`, §1.1). A file with no assistant message yet falls back to its path: the
session uuid is the file stem, or the directory above `subagents/` (§1, §1.1).

**Estimate vs actual.** `open_dictation()` writes a `dictation_estimates` row at release;
the estimate is filled in when the advisory thread has one; `close()` — on the next
dictation's press, or after `meter.turn_idle_s` with no new tokens — writes the deduped
tokens spent in the bound session from the release up to the close. The dictation is bound
to the first active session whose transcript was touched after the release: that is the
one the prompt went into.

Unit: tokens. Never USD (invariant 6).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dict8.usage.reader import scan
from dict8.usage.store import TOKEN_COLUMNS

log = logging.getLogger(__name__)

_TOTAL = " + ".join(TOKEN_COLUMNS)

CLOSE_REASONS = ("next_dictation", "idle", "no_session", "shutdown")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def fmt_tokens(n: int | None) -> str:
    """Compact token count for a menu-bar title: 850, 12K, 1.2M, 34M."""
    if n is None:
        return "—"
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.0f}K"
    if n < 10_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1_000_000:.0f}M"


@dataclass
class Dictation:
    row_id: int
    at: datetime                        # the release, UTC
    session_id: str | None = None       # bound on the first session touched after `at`
    since_tokens: int = 0
    last_activity: datetime | None = None
    low: int | None = None
    high: int | None = None
    n: int | None = None
    group: str | None = None
    has_estimate: bool = False
    closed: bool = False
    actual: int | None = None


@dataclass
class MeterState:
    session_id: str | None = None
    session_tokens: int | None = None
    dictation: Dictation | None = None
    scan_ms: float = 0.0
    max_tx_ms: float = 0.0
    new_messages: int = 0
    errors: list[str] = field(default_factory=list)

    def title_suffix(self) -> str:
        """`0.8M/3.0M`: tokens since the last dictation / that dictation's estimate high.
        Empty until there is a dictation to measure against."""
        d = self.dictation
        if d is None:
            return ""
        spent = d.actual if d.closed and d.actual is not None else d.since_tokens
        high = fmt_tokens(d.high) if d.has_estimate and d.high is not None else "—"
        return f"{fmt_tokens(spent)}/{high}"


class Meter:
    def __init__(self, store, root: Path, cfg, *,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store
        self.root = Path(root).expanduser()
        self.idle_s = float(cfg.require("meter.turn_idle_s"))
        self.clock = clock
        self.cursor = self._max_rowid()
        self.totals: dict[str, int] = {}
        self.active: str | None = None
        self.active_mtime: float | None = None
        self.current: Dictation | None = None

    # -- internals ---------------------------------------------------------------------

    def _max_rowid(self) -> int:
        return int(self.store.conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0])

    def _newest_file(self) -> tuple[Path, float] | None:
        best: tuple[Path, float] | None = None
        if not self.root.is_dir():
            return None
        for p in self.root.rglob("*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if best is None or m > best[1]:
                best = (p, m)
        return best

    def _session_of(self, path: Path) -> str:
        row = self.store.conn.execute(
            "SELECT session_id FROM messages WHERE src_file = ? ORDER BY ts DESC LIMIT 1",
            (str(path),)).fetchone()
        if row is not None:
            return str(row[0])
        if path.parent.name == "subagents":
            return path.parent.parent.name
        return path.stem

    def _seed(self, session_id: str) -> None:
        if session_id not in self.totals:
            self.totals[session_id] = self.store.session_tokens(session_id,
                                                                max_rowid=self.cursor)

    # -- the tick ----------------------------------------------------------------------

    def tick(self) -> MeterState:
        t0 = time.perf_counter()
        res = scan(self.store, self.root)
        now = self.clock()
        rows = self.store.conn.execute(
            f"SELECT rowid, session_id, ts, ({_TOTAL}) AS tok FROM messages "
            f"WHERE rowid > ? ORDER BY rowid", (self.cursor,)).fetchall()
        d = self.current
        for r in rows:
            sid, tok = str(r["session_id"]), int(r["tok"])
            if sid in self.totals:
                self.totals[sid] += tok
            if (d is not None and not d.closed and d.session_id == sid
                    and r["ts"] >= iso(d.at)):
                d.since_tokens += tok
                d.last_activity = now
            self.cursor = max(self.cursor, int(r["rowid"]))

        newest = self._newest_file()
        if newest is not None:
            self.active = self._session_of(newest[0])
            self.active_mtime = newest[1]
            self._seed(self.active)

        if d is not None and not d.closed:
            if (d.session_id is None and self.active is not None
                    and self.active_mtime is not None
                    and self.active_mtime >= d.at.timestamp()):
                d.session_id = self.active
                d.since_tokens = self.store.session_tokens(d.session_id, since=iso(d.at),
                                                           max_rowid=self.cursor)
                d.last_activity = now
            idle_from = d.last_activity or d.at
            if (now - idle_from).total_seconds() >= self.idle_s:
                self._close(d, "idle" if d.session_id else "no_session", now)

        return MeterState(
            session_id=self.active,
            session_tokens=self.totals.get(self.active) if self.active else None,
            dictation=self.current, scan_ms=(time.perf_counter() - t0) * 1000,
            max_tx_ms=self.store.max_tx_ms, new_messages=len(rows), errors=res.errors)

    # -- dictations --------------------------------------------------------------------

    def open_dictation(self, *, at: datetime, words: int, source: str) -> Dictation:
        """Close the previous dictation (if still open) at `at`, then open a new one."""
        self.close(at, reason="next_dictation")
        row = self.store.open_dictation(source=source, dictated_at=iso(at), words=words)
        self.current = Dictation(row_id=row, at=at)
        return self.current

    def set_estimate(self, row_id: int, *, bucket: str | None, recommended: str | None,
                     override_model: str | None, est) -> None:
        """`est` is a `dict8.advise.estimator.Estimate` or None (estimator failed)."""
        self.store.set_dictation_estimate(
            row_id, bucket=bucket, recommended=recommended, override_model=override_model,
            method=getattr(est, "method", None), est_group=getattr(est, "group", None),
            n=getattr(est, "n", None), low=getattr(est, "low", None),
            point=getattr(est, "point", None), high=getattr(est, "high", None),
            ood=getattr(est, "ood", None))
        d = self.current
        if d is not None and d.row_id == row_id:
            d.has_estimate = est is not None
            if est is not None:
                d.low, d.high, d.n, d.group = est.low, est.high, est.n, est.group

    def close(self, at: datetime, *, reason: str) -> Dictation | None:
        """Close the open dictation at `at` (the next press, or shutdown). A scan runs
        first so the actual includes everything written up to now."""
        d = self.current
        if d is None or d.closed:
            return None
        self.tick()
        if d.closed:            # the tick itself closed it on idle
            return d
        self._close(d, reason if d.session_id else "no_session", at)
        return d

    def _close(self, d: Dictation, reason: str, at: datetime) -> None:
        actual = (self.store.session_tokens(d.session_id, since=iso(d.at), until=iso(at))
                  if d.session_id else None)
        self.store.close_dictation(d.row_id, session_id=d.session_id, actual_tokens=actual,
                                   closed_at=iso(at), reason=reason)
        d.closed, d.actual = True, actual
        log.info("meter: dictation %d closed (%s), actual %s tokens", d.row_id, reason,
                 actual)
