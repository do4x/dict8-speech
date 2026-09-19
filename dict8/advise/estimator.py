"""Token estimator — how much will this turn cost, in tokens, given a range.

**What the target means, and what it does not.**
The target is total tokens for the turn: `input + output + cache_creation + cache_read`,
i.e. exactly the four columns the parser records, summed. On this machine's backfill
**96.2% of those tokens are cache reads** (docs/HANDOFF.md §4.8; re-measured by
`dict8 estimate --eval`, which prints the share it actually found). So a "total tokens"
estimate is mostly a prediction of how many times the context gets re-read by tool
round-trips, and only marginally a prediction of how much the model writes.

That matters because invariant 6 says the unit Denis cares about is **percent of the
weekly quota**, and how quota % weights a cache read against an output token is unknown:
there is no API for it, and no manual `dict8 quota` reading exists yet to fit against.
Until readings accumulate, a range from here is a range **in tokens** and must not be
presented as a quota fraction. Every human-readable estimate carries that sentence; the
`--json` shape carries it as `caveat`. Delete neither until the calibration exists.

**Why a range, always.**
`estimate.output: "range"`. Prompt words measured r = -0.009 against total tokens
(n = 81, HANDOFF §4.8) — the lead feature is dead. The bucket medians are ordered as
hoped but their interquartile ranges overlap heavily on n = 10-28 per bucket. A point
estimate off this data would be a number with no evidence behind it, and a wrong number
is worse than no number. So: a low/high band from the group's own observed quantiles,
the sample count printed beside it, and a refusal when there is no comparable history.

**Two methods, both evaluated leave-one-out.**
- `quantile` (shipped, `estimate.method: quantile`; `heuristic` is accepted as the older
  spelling of the same thing): the task bucket's own
  median and `estimate.interval_low_q`/`interval_high_q` quantiles, falling back to the
  pooled distribution over every turn when the bucket is thinner than
  `estimate.min_samples_for_bucket`. Uses the bucket only — words, files and model enter
  solely through the out-of-distribution gate.
- `regression`: OLS of log(total tokens) on log1p(words), log1p(files_touched), bucket
  dummies and model dummies, with the band taken from the training residual quantiles in
  log space. **It cannot be shipped below `estimate.min_samples_for_regression` turns**
  regardless of how it scores; `select_method` enforces that floor rather than trusting a
  reader of config.yml to remember it. Note when reading its score: that band comes from
  the fold's own **in-sample** residuals, so its held-out coverage is slightly flattered
  relative to the quantile method's, whose band is likewise fitted in-fold but on the
  target directly. Treat the coverage gap between the two as an upper bound on the
  regression's advantage, not a measurement of it.

Leave-one-out rather than k-fold: n is under 100 and the per-bucket cells are 10-28, so a
5-fold split would fit some buckets on single digits and the score would move with the
fold seed. LOO has no seed, uses every observation for both roles, and at this n the
refits cost milliseconds. The held-out numbers, and the sweeps the config floors were
chosen from, are recorded next to `estimate:` in config.yml and reproduced by
`dict8 estimate --eval`. As measured on 2026-09-18 the regression beats the quantile
method held-out and is still not shipped, because n is under the floor.

**Out of distribution is an answer, not an error.** Four explicit conditions, all read
from config or measured from the data — never a literal here:
  1. the named bucket has fewer than `estimate.ood_min_bucket_samples` turns behind it;
  2. the pooled group — every recorded turn — has fewer than
     `estimate.ood_min_pooled_samples`. This one is load-bearing rather than defensive:
     `--bucket` is optional and the UserPromptSubmit hook has none to pass, so the pooled
     group is the path nearly every real estimate takes, and it was the one group with no
     floor on it until U2 verification found a single-turn store answering
     `1,000 - 1,000 tokens, central 50% band`;
  3. `words` falls outside the word range observed in the group that would be used. On the
     pooled group that range is currently 1-4028 words, i.e. this condition almost never
     fires there — it is a real gate on a narrow bucket (quick-fix is 5-34) and close to a
     no-op on the pooled path. Do not read a pooled answer as "the length was checked";
  4. the named model has never been seen in the backfill at all. That checks the
     **observed** models, not `config.models` — that list is still TBD, and a gap is not a
     vocabulary.
`estimate.refuse_when_out_of_distribution: false` relaxes conditions 3 and 4 into a warned
extrapolation from the pooled group. It does not relax 1 or 2: a sample too small to have
a band still has no band, and no flag can conjure one.

Invariant 5: this read path never sees a raw message. It reads the `turns` table, whose
token columns `refresh_turn_aggregates()` derives from `turn_messages ⋈ messages` — and
`message_id` is the primary key of both, so a replayed message is counted once and
belongs to exactly one turn. Dedup is the schema's job here, not this module's.

Privacy: this module reads token counts and derived features from Dict8's own SQLite
store. It never reads, receives or stores prompt text (`privacy.store_transcripts:
features_only`), and no USD figure exists anywhere in it (invariant 6).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Sequence

from dict8.usage.store import TOKEN_COLUMNS

# Label for turns whose task_type is NULL. A display string, not a tunable: it must not
# collide with a real bucket name from `classifier.buckets`, and parentheses cannot
# appear in one.
UNBUCKETED = "(unclassified)"

# The pooled group: every usable turn, whatever its bucket. Same reasoning as above.
POOLED = "all turns"

# Relative pivot tolerance for the linear solve. A floating-point rank guard, not a
# threshold anyone tunes: two dummy columns that are exactly collinear inside one LOO
# fold (e.g. holding out the only turn a given model produced in a given bucket) make the
# normal matrix singular, and this is what detects it instead of producing inf coefficients.
_PIVOT_EPS = 1e-12


class EstimatorError(RuntimeError):
    """The estimator was asked for something it must refuse — e.g. a shipped method the
    sample floor does not allow. Distinct from an out-of-distribution answer, which is a
    normal result carried in `Estimate.ood`."""


# ---- data ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Features:
    """What is known at the moment an estimate is asked for."""
    words: int
    files: int = 0
    bucket: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class TurnRow:
    words: int
    files: int
    bucket: str | None
    model: str | None
    total_tokens: int
    cache_read_tokens: int

    @property
    def bucket_key(self) -> str:
        return self.bucket or UNBUCKETED


@dataclass(frozen=True, slots=True)
class Estimate:
    method: str
    group: str                      # which population the range came from
    n: int                          # how many turns are behind it
    ood: bool
    low: int | None
    point: int | None
    high: int | None
    cache_read_share: float | None  # of the group's total tokens, 0-1
    band_pct: float = 0.0           # nominal width of [low, high], e.g. 50 for a 25/75 band
    reason: str | None = None       # why it refused, when ood
    notes: tuple[str, ...] = ()     # e.g. the bucket-too-thin fallback


# The target, built from the schema's own column list so it cannot drift from the parser.
_TOTAL_SQL = " + ".join(TOKEN_COLUMNS)

_LOAD_SQL = f"""
SELECT prompt_words AS words, files_touched AS files, task_type AS bucket, model,
       ({_TOTAL_SQL}) AS total_tokens, cache_read_tokens
FROM turns
WHERE message_count > 0 AND ({_TOTAL_SQL}) > 0
ORDER BY ts
"""


def load_turns(store) -> list[TurnRow]:
    """Every turn with a measured cost.

    `message_count > 0` drops turns no assistant message has been linked to yet — those
    are unmeasured, not free, and training on them as zeros would drag every estimate
    down. The `> 0` on the total is the same filter one level down.
    """
    return [
        TurnRow(
            words=int(r["words"]), files=int(r["files"]),
            bucket=r["bucket"], model=r["model"],
            total_tokens=int(r["total_tokens"]),
            cache_read_tokens=int(r["cache_read_tokens"]),
        )
        for r in store.conn.execute(_LOAD_SQL)
    ]


# ---- small statistics (stdlib only) ------------------------------------------------
# numpy appears in uv.lock only as a transitive dependency of mlx-lm, which U4 plans to
# make an optional extra so the headless layer installs light. Importing it here would
# quietly make the headless estimator depend on the STT/classifier stack again.


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile over an already-sorted sequence."""
    if not sorted_values:
        raise ValueError("quantile of an empty sample")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    return float(sorted_values[lo]) + (pos - lo) * float(sorted_values[hi] - sorted_values[lo])


def solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. None when the system is rank-deficient."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    scale = max((abs(v) for row in m for v in row), default=0.0) or 1.0
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < _PIVOT_EPS * scale:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col] / m[col][col]
            if factor:
                for c in range(col, n + 1):
                    m[r][c] -= factor * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


# ---- the out-of-distribution gate --------------------------------------------------


@dataclass(frozen=True, slots=True)
class Gate:
    """The three OOD conditions plus the thin-bucket fallback, in one place so both
    methods are judged against an identical population."""
    low_q: float
    high_q: float
    min_bucket: int             # estimate.min_samples_for_bucket
    ood_min_bucket: int         # estimate.ood_min_bucket_samples
    ood_min_pooled: int         # estimate.ood_min_pooled_samples
    refuse: bool                # estimate.refuse_when_out_of_distribution

    @property
    def band_pct(self) -> float:
        """What the band claims to cover, so the number shown is never wider than the
        promise attached to it."""
        return (self.high_q - self.low_q) * 100.0

    @classmethod
    def from_config(cls, cfg) -> "Gate":
        return cls(
            low_q=float(cfg.require("estimate.interval_low_q")),
            high_q=float(cfg.require("estimate.interval_high_q")),
            min_bucket=int(cfg.require("estimate.min_samples_for_bucket")),
            ood_min_bucket=int(cfg.require("estimate.ood_min_bucket_samples")),
            ood_min_pooled=int(cfg.require("estimate.ood_min_pooled_samples")),
            refuse=bool(cfg.require("estimate.refuse_when_out_of_distribution")),
        )


def _group_rows(rows: Sequence[TurnRow], bucket: str | None) -> list[TurnRow]:
    if bucket is None:
        return list(rows)
    return [r for r in rows if r.bucket_key == bucket]


def pooled_too_thin(rows: Sequence[TurnRow], gate: Gate) -> str | None:
    """The floor on the POOLED group.

    This is not a nicety. `--bucket` is optional and the UserPromptSubmit hook cannot
    supply one — there is no model call at submit time — so the pooled group is the path
    almost every real estimate takes. Without a floor here, a store holding one turn
    answered `1,000 - 1,000 tokens (median 1,000), central 50% band`: a point estimate
    wearing a band's label, which this module's own docstring forbids.
    """
    if len(rows) < gate.ood_min_pooled:
        return (f"not enough similar history (n={len(rows)}) — the pooled distribution "
                f"over every recorded turn holds only {len(rows)}, under "
                f"estimate.ood_min_pooled_samples={gate.ood_min_pooled}. Below that a "
                f"{gate.band_pct:g}% band's edges are just the smallest and largest turn "
                f"on record, which is a point estimate with a range's label on it. Run "
                f"`dict8 backfill`.")
    return None


def select_group(rows: Sequence[TurnRow], feats: Features, gate: Gate
                 ) -> tuple[str, list[TurnRow], str | None, list[str]]:
    """(group label, its rows, refusal reason or None, notes).

    A refusal reason is returned even when `gate.refuse` is False; the caller decides
    whether to honour it. That keeps "we noticed" and "we refused" separate facts.
    """
    notes: list[str] = []
    if feats.bucket is None:
        return POOLED, list(rows), pooled_too_thin(rows, gate), notes

    in_bucket = _group_rows(rows, feats.bucket)
    if len(in_bucket) < gate.ood_min_bucket:
        return (f"bucket {feats.bucket}", in_bucket,
                f"not enough similar history (n={len(in_bucket)}) — bucket "
                f"{feats.bucket!r} needs at least {gate.ood_min_bucket} past turns "
                f"(estimate.ood_min_bucket_samples) before a range means anything",
                notes)
    if len(in_bucket) < gate.min_bucket:
        thin = pooled_too_thin(rows, gate)
        if thin is not None:
            return POOLED, list(rows), thin, notes
        notes.append(
            f"bucket {feats.bucket!r} has only n={len(in_bucket)}, under "
            f"estimate.min_samples_for_bucket={gate.min_bucket}; the range below is the "
            f"pooled distribution over all turns, not this bucket's")
        return POOLED, list(rows), None, notes
    return f"bucket {feats.bucket}", in_bucket, None, notes


def check_distribution(rows: Sequence[TurnRow], group_rows: Sequence[TurnRow],
                       feats: Features) -> str | None:
    """Conditions 2 and 3: word range of the group used, and model ever observed."""
    if feats.model is not None:
        seen = {r.model for r in rows if r.model}
        if feats.model not in seen:
            return (f"not enough similar history (n=0) — model {feats.model!r} has never "
                    f"been seen in the backfill; observed: {', '.join(sorted(seen))}")
    if group_rows:
        w_lo = min(r.words for r in group_rows)
        w_hi = max(r.words for r in group_rows)
        if not w_lo <= feats.words <= w_hi:
            return (f"not enough similar history (n=0 at this length) — {feats.words} words "
                    f"is outside the {w_lo}-{w_hi} word range observed in this group; "
                    f"extrapolating past the data is what estimate."
                    f"refuse_when_out_of_distribution exists to stop")
    return None


def cache_read_share(group_rows: Sequence[TurnRow]) -> float | None:
    total = sum(r.total_tokens for r in group_rows)
    if not total:
        return None
    return sum(r.cache_read_tokens for r in group_rows) / total


# ---- method (a): bucket medians + quantiles ----------------------------------------


class QuantileEstimator:
    """Bucket median plus a quantile band, with the pooled distribution as the fallback."""

    name = "quantile"

    def __init__(self, rows: Sequence[TurnRow], gate: Gate) -> None:
        self.rows = list(rows)
        self.gate = gate

    def predict(self, feats: Features) -> Estimate:
        group, group_rows, reason, notes = select_group(self.rows, feats, self.gate)
        if reason is None:
            reason = check_distribution(self.rows, group_rows, feats)
        if reason is not None and self.gate.refuse:
            return Estimate(self.name, group, len(group_rows), True, None, None, None,
                            cache_read_share(group_rows), self.gate.band_pct, reason,
                            tuple(notes))
        if reason is not None:
            # `refuse: false` licenses EXTRAPOLATION — answering outside the observed
            # range from the pooled distribution. It does not license FABRICATION: a
            # pooled group too thin to have a band still has no band, and no config flag
            # can conjure one. The floor therefore survives the flag.
            thin = pooled_too_thin(self.rows, self.gate)
            if thin is not None:
                return Estimate(self.name, POOLED, len(self.rows), True, None, None, None,
                                cache_read_share(self.rows), self.gate.band_pct, thin,
                                tuple(notes))
            notes.append("estimate.refuse_when_out_of_distribution is false, so a range is "
                         "shown anyway from the pooled distribution — it is an extrapolation")
            group, group_rows = POOLED, list(self.rows)

        totals = sorted(r.total_tokens for r in group_rows)
        return Estimate(
            self.name, group, len(group_rows), reason is not None,
            int(round(quantile(totals, self.gate.low_q))),
            int(round(quantile(totals, 0.5))),      # 0.5 is the median's definition, not a knob
            int(round(quantile(totals, self.gate.high_q))),
            cache_read_share(group_rows), self.gate.band_pct, reason, tuple(notes),
        )


# ---- method (b): log-linear regression ---------------------------------------------


class RegressionEstimator:
    """OLS on log total tokens. The band is the training residual quantiles in log space,
    which makes it a multiplicative band on the original scale — appropriate for a target
    that spans 37K to 56M tokens."""

    name = "regression"

    def __init__(self, rows: Sequence[TurnRow], gate: Gate) -> None:
        self.rows = list(rows)
        self.gate = gate
        self.buckets = sorted({r.bucket_key for r in self.rows})[1:]   # first level = baseline
        self.models = sorted({r.model for r in self.rows if r.model})[1:]
        self.coef: list[float] | None = None
        self.res_lo = self.res_hi = 0.0
        self._fit()

    def _design(self, words: int, files: int, bucket_key: str, model: str | None) -> list[float]:
        return (
            [1.0, math.log1p(max(words, 0)), math.log1p(max(files, 0))]
            + [1.0 if bucket_key == b else 0.0 for b in self.buckets]
            + [1.0 if model == m else 0.0 for m in self.models]
        )

    def _fit(self) -> None:
        if not self.rows:
            return
        x = [self._design(r.words, r.files, r.bucket_key, r.model) for r in self.rows]
        y = [math.log(r.total_tokens) for r in self.rows]
        k = len(x[0])
        if len(x) <= k:
            return
        xtx = [[sum(row[i] * row[j] for row in x) for j in range(k)] for i in range(k)]
        xty = [sum(row[i] * y[n] for n, row in enumerate(x)) for i in range(k)]
        self.coef = solve(xtx, xty)
        if self.coef is None:
            return
        residuals = sorted(y[n] - sum(c * v for c, v in zip(self.coef, row))
                           for n, row in enumerate(x))
        self.res_lo = quantile(residuals, self.gate.low_q)
        self.res_hi = quantile(residuals, self.gate.high_q)

    def predict(self, feats: Features) -> Estimate:
        group, group_rows, reason, notes = select_group(self.rows, feats, self.gate)
        if reason is None:
            reason = check_distribution(self.rows, group_rows, feats)
        if reason is not None and self.gate.refuse:
            return Estimate(self.name, group, len(group_rows), True, None, None, None,
                            cache_read_share(group_rows), self.gate.band_pct, reason,
                            tuple(notes))
        if reason is not None:
            # Same rule as the quantile method: `refuse: false` buys extrapolation, not a
            # band conjured out of a sample too small to have one.
            thin = pooled_too_thin(self.rows, self.gate)
            if thin is not None:
                return Estimate(self.name, POOLED, len(self.rows), True, None, None, None,
                                cache_read_share(self.rows), self.gate.band_pct, thin,
                                tuple(notes))
        if self.coef is None:
            return Estimate(self.name, group, len(self.rows), True, None, None, None,
                            cache_read_share(group_rows), self.gate.band_pct,
                            "the regression could not be fitted on this sample (too few "
                            "turns, or collinear buckets/models)", tuple(notes))
        bucket_key = feats.bucket or UNBUCKETED
        xb = sum(c * v for c, v in zip(
            self.coef, self._design(feats.words, feats.files, bucket_key, feats.model)))
        return Estimate(
            self.name, group, len(group_rows), reason is not None,
            int(round(math.exp(xb + self.res_lo))),
            int(round(math.exp(xb))),
            int(round(math.exp(xb + self.res_hi))),
            cache_read_share(group_rows), self.gate.band_pct, reason, tuple(notes),
        )


METHODS = {
    # config.yml's `estimate.method` value -> implementation. "heuristic" is the name the
    # config has carried since Phase 0; "quantile" is what it is. Both resolve here so
    # renaming the config value later is not a silent behaviour change.
    "heuristic": QuantileEstimator,
    "quantile": QuantileEstimator,
    "regression": RegressionEstimator,
}


def select_method(cfg, rows: Sequence[TurnRow]):
    """The shipped method, with the regression's sample floor enforced here rather than
    left as a comment in config.yml."""
    name = str(cfg.require("estimate.method"))
    try:
        cls = METHODS[name]
    except KeyError:
        raise EstimatorError(
            f"estimate.method is {name!r}; known methods are {', '.join(sorted(METHODS))}"
        ) from None
    floor = int(cfg.require("estimate.min_samples_for_regression"))
    if cls is RegressionEstimator and len(rows) < floor:
        raise EstimatorError(
            f"estimate.method is {name!r} but the store holds {len(rows)} usable turns, "
            f"under estimate.min_samples_for_regression={floor}. The regression does not "
            f"ship below that floor however it scores held-out — change the data, not the "
            f"floor."
        )
    return cls


def estimate(store, cfg, feats: Features) -> Estimate:
    rows = load_turns(store)
    gate = Gate.from_config(cfg)
    return select_method(cfg, rows)(rows, gate).predict(feats)


# ---- held-out evaluation -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvalResult:
    method: str
    n: int              # held-out points that produced a range
    refused: int        # held-out points the OOD gate declined
    mape: float         # mean absolute percentage error of the point estimate
    mdape: float        # median absolute percentage error — MAPE is outlier-dominated here
    coverage: float     # fraction of held-out actuals inside [low, high]
    median_width: float # median high/low ratio; coverage without width is meaningless


def leave_one_out(rows: Sequence[TurnRow], gate: Gate, cls) -> EvalResult:
    """Refit on n-1 and predict the held-out turn, for every turn.

    The held-out point is fed through the *same* OOD gate a live call gets, so a method
    that refuses everything scores zero coverage over zero points rather than a perfect
    one — `refused` is reported beside the metrics for exactly that reason.
    """
    rows = list(rows)
    apes: list[float] = []
    widths: list[float] = []
    covered = refused = 0
    for i, held in enumerate(rows):
        train = rows[:i] + rows[i + 1:]
        model = cls(train, gate)
        est = model.predict(Features(held.words, held.files, held.bucket, held.model))
        if est.ood or est.point is None:
            refused += 1
            continue
        apes.append(abs(est.point - held.total_tokens) / held.total_tokens * 100.0)
        if est.low is not None and est.high is not None:
            widths.append(est.high / max(est.low, 1))
            if est.low <= held.total_tokens <= est.high:
                covered += 1
    n = len(apes)
    if not n:
        return EvalResult(cls.name, 0, refused, float("nan"), float("nan"),
                          float("nan"), float("nan"))
    apes_sorted = sorted(apes)
    return EvalResult(
        cls.name, n, refused,
        sum(apes) / n,
        quantile(apes_sorted, 0.5),
        covered / n,
        quantile(sorted(widths), 0.5) if widths else float("nan"),
    )


def bucket_counts(rows: Sequence[TurnRow]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r.bucket_key] = out.get(r.bucket_key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# The grids `dict8 estimate --eval` sweeps. These are the axes of a report, not runtime
# behaviour: nothing outside `--eval` reads them, and the values config.yml ends up
# holding are *chosen from* these sweeps rather than defaulted to anything here. Kept
# beside the sweep functions so the printed table and the grid cannot drift apart.
EVAL_BANDS: tuple[tuple[float, float], ...] = (
    (0.25, 0.75), (0.10, 0.90), (0.05, 0.95),
)
EVAL_FLOORS: tuple[int, ...] = (1, 2, 3, 5, 8, 10, 11, 12, 15, 16, 20, 29)
EVAL_PREFIXES: tuple[int, ...] = (1, 3, 5, 10, 20, 40, 60, 80)


def sweep_bands(rows: Sequence[TurnRow], gate: Gate,
                bands: Sequence[tuple[float, float]]) -> list[tuple[tuple[float, float], EvalResult]]:
    """Held-out coverage of the shipped method at several band widths — the measurement
    behind `estimate.interval_low_q` / `interval_high_q`."""
    return [(band, leave_one_out(rows, replace(gate, low_q=band[0], high_q=band[1]),
                                 QuantileEstimator))
            for band in bands]


def sweep_bucket_floor(rows: Sequence[TurnRow], gate: Gate,
                       floors: Sequence[int]) -> list[tuple[int, EvalResult]]:
    """Held-out score against `estimate.ood_min_bucket_samples` — the measurement behind
    that key, including when it shows the data cannot distinguish two values.

    Only the refusal floor moves; `min_bucket` stays at its configured value, the way
    `sweep_pooling` pins this one. An earlier version moved both together, which made the
    table a mixture of "refused more" and "pooled more" and could not support a claim
    about either. Where the refusal floor ends up above `min_bucket`, refusal simply wins
    — a bucket thin enough to refuse is never reached by the pooling branch."""
    return [(f, leave_one_out(rows, replace(gate, ood_min_bucket=f), QuantileEstimator))
            for f in floors]


def pooled_band_vs_n(rows: Sequence[TurnRow], gate: Gate,
                     ks: Sequence[int]) -> list[tuple[int, int, int, int]]:
    """(n, low, median, high) of the pooled band over the first n turns, chronologically.

    The only evidence available for `estimate.ood_min_pooled_samples`: leave-one-out
    cannot measure it, because the pooled group is always the whole store minus one and
    is far above any floor worth arguing about. This shows instead how much the band a
    fresh install would print moves as history accumulates — i.e. how little a pooled
    band off a handful of turns is worth. `rows` is already in `ts` order (_LOAD_SQL).
    """
    out = []
    for k in ks:
        if k > len(rows):
            continue
        totals = sorted(r.total_tokens for r in rows[:k])
        out.append((k,
                    int(round(quantile(totals, gate.low_q))),
                    int(round(quantile(totals, 0.5))),
                    int(round(quantile(totals, gate.high_q)))))
    return out


def sweep_pooling(rows: Sequence[TurnRow], gate: Gate,
                  floors: Sequence[int]) -> list[tuple[int, EvalResult]]:
    """Held-out score against `estimate.min_samples_for_bucket` — i.e. how thin a bucket
    has to get before its own distribution is worse than pooling every turn together.
    The refusal floor is pinned low throughout so that raising this one only ever swaps
    a bucket band for the pooled band; it never turns an answer into a refusal, and the
    two effects stay separable. The literal 1 below is that pin — an experiment control
    inside `--eval`, not a shipped threshold: the floor that ships is
    `estimate.ood_min_bucket_samples`, read from config by `Gate.from_config`, and this
    function never touches it outside the sweep it is describing."""
    return [(f, leave_one_out(rows, replace(gate, min_bucket=f, ood_min_bucket=1),
                              QuantileEstimator))
            for f in floors]


# ---- presentation ------------------------------------------------------------------

# The sentence that must travel with every number this module produces. Invariant 6: the
# unit Denis is judged on is quota %, and nothing here can convert to it yet.
CAVEAT_UNIT = (
    "unit is TOKENS, not quota % — how Claude Code's weekly quota weights a cache read "
    "against an output token is unknown until manual `dict8 quota` readings exist to "
    "calibrate against (HANDOFF §4.8)."
)
CAVEAT_CACHE = "{share} of the tokens behind this range are cache reads, so it is mostly a " \
               "prediction of context re-reads. "


def caveat_for(est: Estimate) -> str:
    if est.low is None or est.cache_read_share is None:
        return CAVEAT_UNIT
    return CAVEAT_CACHE.format(share=f"{est.cache_read_share * 100:.1f}%") + CAVEAT_UNIT


def to_json(est: Estimate) -> dict:
    """The shape U3's hook consumes. Small and stable: keys are never removed, only added."""
    return {
        "ood": est.ood,
        "method": est.method,
        "group": est.group,
        "n": est.n,
        "unit": "tokens",
        "band_pct": est.band_pct,
        "low": est.low,
        "point": est.point,
        "high": est.high,
        "cache_read_share": (None if est.cache_read_share is None
                             else round(est.cache_read_share, 4)),
        "reason": est.reason,
        "notes": list(est.notes),
        "caveat": caveat_for(est),
    }


def _fmt(n: int) -> str:
    return f"{n:,}"


def render(est: Estimate) -> list[str]:
    lines: list[str] = []
    if est.low is None or est.high is None:
        lines.append(f"estimate   : NONE — {est.reason}")
        lines.append(f"method     : {est.method}   group: {est.group}   n={est.n}")
    else:
        lines.append(f"estimate   : {_fmt(est.low)} – {_fmt(est.high)} tokens "
                     f"(median {_fmt(est.point or 0)}) — central {est.band_pct:g}% band")
        lines.append(f"method     : {est.method}   group: {est.group}   n={est.n}")
        share = "unknown" if est.cache_read_share is None else f"{est.cache_read_share * 100:.1f}%"
        lines.append(f"cache reads: {share} of the tokens in that group")
    for note in est.notes:
        lines.append(f"note       : {note}")
    if est.ood and est.low is not None:
        lines.append(f"warning    : {est.reason}")
    lines.append(f"caveat     : {caveat_for(est)}")
    return lines
