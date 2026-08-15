"""Topic-timestamp timeline with release-event bursts.

The forum needs creation timestamps that are interesting, not uniform. The
seeder's "Injected forum dynamics" contract
(`sample/docs/analysis/seeder-internals.md`) asks for topic-creation spikes
around fictional release events — patch drops, anniversaries, beta
announcements — so the visualizer's time-window slider has real signal.

Mechanics:

- `base_epoch` is today − 365 days (UTC midnight); the corpus spans the
  365-day window `[base_epoch, base_epoch + 365 d]`. Offsets are integer
  days in `[0, 365]`.
- For each topic, with ~50% probability we draw an offset within ±3 days of
  one of the product's `RELEASE_EVENTS`; otherwise we draw uniformly across
  the whole window. The 50/50 split is what lets the burst-density invariant
  in the test suite be satisfied: ~50% of mass concentrated in 4 narrow
  windows, ~50% spread thin over the rest.
- Offsets are pre-computed at construction time and sorted ascending. This
  matters for downstream generators (Sit 5+ assigns topic[i] the i-th
  offset, which guarantees chronological IDs).

Determinism: uses `spec.rng("timeline")`. Same `(spec, total_topics)` -> same
`Timeline` (offsets equal element-wise).

Validation: malformed `RELEASE_EVENTS` (any offset out of `[0, 365]`, or
fewer than 3 distinct days) raises at module load AND in the constructor —
the burst-density invariant relies on having at least 3 distinct event days,
so we surface the error at the earliest possible point per the
generator-hygiene rule in `sample/CLAUDE.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..universe import GenerationSpec


# Window length in days. Chosen to match the design-doc "today − 12 months"
# epoch base — every offset must lie in `[0, _WINDOW_DAYS]` (inclusive).
_WINDOW_DAYS = 365

# Burst radius in days around a release event. ±3 days = a 7-day burst window
# per event, which matches the per-week bucket size used by the test's
# burst-density invariant (so a single event reliably populates one bucket).
_BURST_RADIUS_DAYS = 3

# Probability a given topic falls in a burst window vs. uniform spread.
# 50/50 lets the law of large numbers carry the burst-density invariant for
# medium-and-up scales without overshooting (which would starve the uniform
# baseline and make non-burst weeks suspiciously empty).
_BURST_PROBABILITY = 0.5

# Minimum distinct release-event days. The burst-density invariant asserts
# ≥3 dense windows; with fewer than 3 distinct event days we can't possibly
# satisfy it, and a generator producing a forum that quietly violates a
# design-doc invariant is exactly the failure the hygiene rule forbids.
_MIN_DISTINCT_RELEASE_DAYS = 3


def _validate_release_events(events: list[tuple[int, str]]) -> None:
    """Raise `ValueError` if `events` is malformed.

    Invariants:
        - Non-empty.
        - Every offset is an integer in `[0, 365]`.
        - Every label is a non-empty string.
        - At least `_MIN_DISTINCT_RELEASE_DAYS` distinct offsets.

    Called at module import (over `crown_of_brine.RELEASE_EVENTS`) and in
    `make_timeline` against the spec's product. Module-load validation is
    cheap insurance against a future product-constant edit slipping by.
    """
    if not events:
        raise ValueError("RELEASE_EVENTS must be non-empty")
    distinct_days: set[int] = set()
    for idx, entry in enumerate(events):
        if (
            not isinstance(entry, tuple)
            or len(entry) != 2
            or not isinstance(entry[0], int)
            or not isinstance(entry[1], str)
        ):
            raise ValueError(
                f"RELEASE_EVENTS[{idx}] must be a (int, str) tuple, got {entry!r}"
            )
        offset, label = entry
        if offset < 0 or offset > _WINDOW_DAYS:
            raise ValueError(
                f"RELEASE_EVENTS[{idx}] offset {offset} out of range "
                f"[0, {_WINDOW_DAYS}]"
            )
        if not label.strip():
            raise ValueError(
                f"RELEASE_EVENTS[{idx}] label must be a non-empty string"
            )
        distinct_days.add(offset)
    if len(distinct_days) < _MIN_DISTINCT_RELEASE_DAYS:
        raise ValueError(
            f"RELEASE_EVENTS must have at least {_MIN_DISTINCT_RELEASE_DAYS} "
            f"distinct days, got {len(distinct_days)} ({sorted(distinct_days)})"
        )


# Module-load validation: catches a malformed product constant before any
# spec is even built. Imports the default product directly because it's the
# only one shipping today; future products would each need to satisfy the
# same invariants and would be validated in their own module imports if they
# defined `RELEASE_EVENTS` at module scope.
from ..product import crown_of_brine as _default_product  # noqa: E402

_validate_release_events(_default_product.RELEASE_EVENTS)


def _today_utc_midnight() -> datetime:
    """Return today's UTC date at 00:00:00.

    Wrapped so tests / future callers can monkey-patch a frozen "today" if
    needed — Sit 4 doesn't need that, but isolating the wall-clock read keeps
    the option open.
    """
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


@dataclass(frozen=True)
class Timeline:
    """Pre-computed topic-creation timestamps for a forum bake.

    Attributes:
        base_epoch: UTC-midnight datetime exactly 365 days before "today".
            All offsets are measured in days from this anchor.
        total_topics: number of topics this timeline was sized for. Every
            `offset_for_topic(idx)` call requires `0 <= idx < total_topics`.
        release_events: the (offset_days, label) list used to seed bursts.
            Stored on the timeline so callers don't need to reach back into
            the product module to ask "which event drove this date?".
        _offsets: the pre-computed list of length `total_topics`, sorted
            ascending. Constructed by `make_timeline`; treat as private.
    """

    base_epoch: datetime
    total_topics: int
    release_events: tuple[tuple[int, str], ...]
    _offsets: tuple[int, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if self.total_topics <= 0:
            raise ValueError(
                f"Timeline.total_topics must be positive, got {self.total_topics}"
            )
        if len(self._offsets) != self.total_topics:
            raise ValueError(
                f"Timeline._offsets has {len(self._offsets)} entries, "
                f"expected {self.total_topics}"
            )
        # Re-validate release events here too: defends against a caller
        # constructing a Timeline directly with a hand-built tuple instead of
        # going through `make_timeline`.
        _validate_release_events(list(self.release_events))
        for off in self._offsets:
            if off < 0 or off > _WINDOW_DAYS:
                raise ValueError(
                    f"Timeline offset {off} out of range [0, {_WINDOW_DAYS}]"
                )
        if list(self._offsets) != sorted(self._offsets):
            raise ValueError("Timeline._offsets must be sorted ascending")

    @property
    def offsets(self) -> tuple[int, ...]:
        """Read-only view over the pre-computed offsets list."""
        return self._offsets

    def offset_for_topic(self, idx: int) -> int:
        """Return the day-offset (0-365) for the i-th topic.

        Topics are addressed by index in `[0, total_topics)`. Out-of-range
        access raises `IndexError` so callers don't silently wrap.
        """
        if idx < 0 or idx >= self.total_topics:
            raise IndexError(
                f"topic index {idx} out of range [0, {self.total_topics})"
            )
        return self._offsets[idx]

    def timestamp_for_topic(self, idx: int) -> datetime:
        """Return `base_epoch + timedelta(days=offset_for_topic(idx))`."""
        return self.base_epoch + timedelta(days=self.offset_for_topic(idx))

    def release_event_dates(self) -> list[datetime]:
        """Return concrete datetimes of every `RELEASE_EVENTS` entry, sorted."""
        dates = [
            self.base_epoch + timedelta(days=off)
            for off, _label in self.release_events
        ]
        dates.sort()
        return dates

    def is_burst_window(
        self, ts: datetime, radius_days: int = _BURST_RADIUS_DAYS
    ) -> bool:
        """Return True if `ts` falls within `±radius_days` of any release event.

        Comparison is by absolute day-difference between `ts` and each event
        date. `ts` need not be at UTC midnight; we measure with whole-day
        granularity so callers can hand in arbitrary timestamps.
        """
        if radius_days < 0:
            raise ValueError(f"radius_days must be non-negative, got {radius_days}")
        for event_date in self.release_event_dates():
            delta = ts - event_date
            day_delta = abs(delta.total_seconds()) / 86400.0
            if day_delta <= radius_days:
                return True
        return False


def make_timeline(spec: GenerationSpec, total_topics: int) -> Timeline:
    """Return a deterministic `Timeline` for `spec` and `total_topics`.

    Pre-computes one offset per topic. ~50% are drawn within ±3 days of a
    randomly-picked release event; ~50% are drawn uniformly from the whole
    `[0, 365]` window. The result is sorted ascending so iterating topics by
    index yields chronological timestamps.

    Same `(spec, total_topics)` -> same `Timeline` (full equality of offsets
    via `tuple` interning + `frozen=True`).
    """
    if total_topics <= 0:
        raise ValueError(
            f"make_timeline: total_topics must be positive, got {total_topics}"
        )

    product = spec.product
    events: list[tuple[int, str]] = list(product.RELEASE_EVENTS)
    # Re-validate at construction time so a malformed third-party product
    # module surfaces here instead of producing junk offsets.
    _validate_release_events(events)

    rng = spec.rng("timeline")

    offsets: list[int] = []
    for _ in range(total_topics):
        # `pick_int(0, 99)` then compare to threshold so we exhaust exactly
        # one rng draw per coin flip — keeps the per-topic rng advance bound
        # stable across refactors.
        coin = rng.pick_int(0, 99)
        if coin < int(_BURST_PROBABILITY * 100):
            event_offset, _label = rng.pick_one(events)
            lo = max(0, event_offset - _BURST_RADIUS_DAYS)
            hi = min(_WINDOW_DAYS, event_offset + _BURST_RADIUS_DAYS)
            offsets.append(rng.pick_int(lo, hi))
        else:
            offsets.append(rng.pick_int(0, _WINDOW_DAYS))

    offsets.sort()

    return Timeline(
        base_epoch=_today_utc_midnight() - timedelta(days=_WINDOW_DAYS),
        total_topics=total_topics,
        release_events=tuple(events),
        _offsets=tuple(offsets),
    )
