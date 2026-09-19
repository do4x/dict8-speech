"""Manual weekly-quota check-ins — the estimator's only ground truth.

Invariant 6: Denis is on a subscription, so the unit is percent of the weekly quota and
never dollars. There is no API for that percentage — Claude Code prints it in `/usage`, a
human reads it off the screen and types it in. That makes a reading two facts rather than
one: a number, and the moment it was true. Nothing here hands out the first without the
second, because a 30-hour-old reading and a 30-second-old one support completely different
claims, and whichever is on screen is the one an estimate will be trusted against.

`quota.stale_after_hours` is what turns age into staleness. While that key is TBD this
module reports the threshold as unset and judges nothing — invariant 3: a labeled gap beats
a plausible guess, and 24 being a round number is not a reason to pick it.

Nothing here infers a reading. With no check-in recorded the answer is "none recorded",
never a number derived from token counts: an inferred figure displayed in the slot a read
one occupies is indistinguishable from the real thing, which is the failure this whole
layer exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# The domain of a percentage, not a tunable threshold. Invariant 3 is about decisions that
# are Denis's to make; "a percent runs 0 to 100" is not one of them, and putting it in
# config.yml would invite someone to widen it and quietly break the unit.
# The bounds of a percentage, not a tunable range: /usage reports 0-100 and a reading
# outside it is a typo, not a policy question. Units, in the same sense as the
# conversions below.
PCT_MIN = 0.0
PCT_MAX = 100.0

_SECONDS_PER_HOUR = 3600.0      # unit conversion, not a threshold

# The one `source` value that means a human read the number off Claude Code's /usage.
# An enum member, not a setting: `quota.source` in config.yml chooses which provenance new
# readings are stamped with, and a reading stamped with anything else did NOT come from a
# person reading a screen. The sentence describing a reading is therefore derived from the
# row's own source, never from config — otherwise editing one config line makes every
# inferred figure claim to have been read, which is the failure this module exists to stop.
# It fails conservative on purpose: rename the config value and a genuinely manual reading
# loses the human phrasing, which merely understates provenance. The opposite error — an
# inferred number wearing "read off /usage" — is the one that must be impossible.
MANUAL_SOURCE = "manual"

_MAX_HOUR_PLACES = 6            # display precision ceiling, not a threshold

LABEL_WIDTH = 14               # column width for the aligned output, not a threshold


class QuotaError(ValueError):
    """Bad input from a human at a keyboard; the message is the user-facing one."""


@dataclass(frozen=True, slots=True)
class QuotaReading:
    weekly_pct: float
    taken_at: datetime          # when the number was true, i.e. when /usage showed it
    source: str                 # config quota.source; "manual" = a human typed it
    recorded_at: datetime       # when Dict8 wrote it down
    row_id: int | None = None

    def age(self, now: datetime) -> timedelta:
        return now - self.taken_at


# ---- parsing ---------------------------------------------------------------------


def parse_pct(raw: object) -> float:
    """Validate the typed percentage. Raises QuotaError; never coerces, never clamps.

    Clamping 101 to 100 would store a number Denis never read, which is exactly the
    "inferred presented as read" failure one line lower down the stack.
    """
    try:
        value = float(str(raw).strip().rstrip("%"))
    except (TypeError, ValueError):
        raise QuotaError(
            f"{raw!r} is not a number. `dict8 quota <pct>` takes the weekly percentage "
            "Claude Code's /usage shows, e.g. `dict8 quota 47`. Nothing was stored."
        ) from None
    # NaN and the infinities fail this comparison too, which is the point of writing it
    # as a range test rather than two guards.
    if not PCT_MIN <= value <= PCT_MAX:
        # The input verbatim, then what it parsed to. `{value:g}` alone rendered 100.0001
        # as "100 is outside 0-100", an error message that refutes itself.
        raise QuotaError(
            f"{raw!r} is outside {PCT_MIN:g}–{PCT_MAX:g} (parsed as {value!r}). weekly_pct "
            "is a percentage of the weekly quota, so a value outside that range is a typo, "
            "not a reading. Nothing was stored."
        )
    return value


def parse_taken_at(raw: str | None, now: datetime) -> datetime:
    """When the reading was taken. `None` means now.

    A naive timestamp is read as local time — someone typing `--at 2026-09-17T06:00` means
    six in the morning where they are sitting, not UTC.
    """
    if raw is None:
        return now
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        raise QuotaError(
            f"{raw!r} is not an ISO-8601 timestamp (e.g. 2026-09-17T06:00 or "
            "2026-09-17T04:00:00+00:00). Nothing was stored."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()        # naive: interpret as this machine's local time
    parsed = parsed.astimezone(timezone.utc)
    if parsed > now:
        raise QuotaError(
            f"{parsed.isoformat()} is in the future — a reading cannot have been taken "
            "later than now, and storing it would make its age negative. Nothing was stored."
        )
    return parsed


# ---- store round-trip ------------------------------------------------------------


def _as_utc(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def record(store, weekly_pct: float, source: str, taken_at: datetime,
           now: datetime | None = None) -> QuotaReading:
    now = now or datetime.now(timezone.utc)
    taken_at = taken_at.astimezone(timezone.utc)
    row_id = store.add_quota_reading(
        weekly_pct=weekly_pct, ts=taken_at.isoformat(), source=source,
        recorded_at=now.isoformat(),
    )
    return QuotaReading(float(weekly_pct), taken_at, source, now, row_id)


def latest(store) -> QuotaReading | None:
    row = store.latest_quota_reading()
    if row is None:
        return None
    return QuotaReading(
        weekly_pct=float(row["weekly_pct"]),
        taken_at=_as_utc(row["ts"]),
        source=str(row["source"]),
        recorded_at=_as_utc(row["recorded_at"]),
        row_id=int(row["id"]),
    )


# ---- presentation ----------------------------------------------------------------


def hours(delta: timedelta) -> float:
    return delta.total_seconds() / _SECONDS_PER_HOUR


def provenance(source: str) -> str:
    """How the number got here, in words, from the reading's own `source`."""
    if source == MANUAL_SOURCE:
        return "read off Claude Code's /usage and typed in — never inferred by Dict8"
    return (f"NOT read by a human — recorded by source {source!r}. Derived, not observed: "
            f"only a {MANUAL_SOURCE!r} reading is calibration ground truth.")


def _hours_str(age_h: float, limit: float | None) -> str:
    """Hours at the coarsest precision that still disagrees with `limit` when the real
    value does.

    `.1f` alone prints self-contradictions at the boundary: 23:58:12 against a 24 h
    threshold renders as "fresh — 24.0 h old, within 24 h", a number arguing with its own
    label. A figure that contradicts the verdict beside it is worse than no figure.
    """
    for places in range(1, _MAX_HOUR_PLACES + 1):
        text = f"{age_h:.{places}f}"
        if limit is None or float(text) != limit:
            return text
    return f"{age_h!r}"         # only reachable when age is exactly the threshold


def _elapsed(delta: timedelta) -> str:
    """H:MM:SS (with days) rather than a rounded-off phrase — the exact figure is short
    enough to print, and "about a day ago" is the kind of smoothing this module exists to
    refuse."""
    return str(timedelta(seconds=round(delta.total_seconds())))


def _line(label: str, body: str) -> str:
    return f"{label:<{LABEL_WIDTH}}: {body}"


def parse_limit(stale_after_hours: object) -> tuple[float | None, str | None]:
    """(threshold in hours, why it cannot be used). Exactly one of the two is None."""
    if stale_after_hours is None:
        return None, ("quota.stale_after_hours is unset (TBD) in config.yml, so there is no "
                      "threshold to judge against. The age above is the whole answer; Dict8 "
                      "will not pick a number.")
    try:
        return float(stale_after_hours), None
    except (TypeError, ValueError):
        return None, (f"quota.stale_after_hours is {stale_after_hours!r}, which is not a "
                      "number of hours. Fix it in config.yml.")


def staleness_line(delta: timedelta, stale_after_hours: object) -> str:
    """The one line that is allowed to be a judgement — and only once a threshold exists."""
    limit, reason = parse_limit(stale_after_hours)
    if limit is None:
        return _line("staleness", f"NOT JUDGED — {reason}")
    age_h = hours(delta)
    stale = age_h > limit
    return _line("staleness",
                 f"{'STALE' if stale else 'fresh'} — {_hours_str(age_h, limit)} h old "
                 f"({_elapsed(delta)}), {'past' if stale else 'within'} "
                 f"quota.stale_after_hours = {limit:g} h")


def describe(reading: QuotaReading, stale_after_hours: object,
             now: datetime | None = None) -> list[str]:
    """Every line a reading is ever shown with. Source and age are not optional extras:
    they are what make the number a reading rather than an assertion."""
    now = now or datetime.now(timezone.utc)
    delta = reading.age(now)
    limit, _ = parse_limit(stale_after_hours)
    return [
        _line("weekly_pct", f"{reading.weekly_pct:g}%  ({provenance(reading.source)})"),
        _line("source", reading.source),
        _line("taken at", reading.taken_at.isoformat()),
        _line("recorded at", reading.recorded_at.isoformat()),
        _line("age", f"{_hours_str(hours(delta), limit)} h  ({_elapsed(delta)} ago)"),
        staleness_line(delta, stale_after_hours),
    ]


NO_READING = (
    "no quota check-in recorded yet — and nothing is being inferred in its place.\n"
    "Run `dict8 quota <pct>` with the weekly percentage Claude Code's /usage shows."
)
