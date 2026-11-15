"""Injectable clock abstraction for deterministic scheduling, escalation, and tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Protocol providing the current UTC instant."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Production clock backed by system UTC time."""

    def now(self) -> datetime:
        """Return the current UTC timestamp."""
        return datetime.now(UTC)


class FixedClock:
    """Deterministic controllable clock for unit tests and isolated simulation."""

    def __init__(self, initial: datetime | None = None) -> None:
        base = initial or datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)
        if base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        self._current = base.astimezone(UTC)

    def now(self) -> datetime:
        """Return the fixed UTC timestamp."""
        return self._current

    def set(self, dt: datetime) -> None:
        """Set the clock to an explicit timezone-aware datetime."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        self._current = dt.astimezone(UTC)

    def advance(self, **kwargs: float) -> datetime:
        """Advance the fixed clock by a timedelta constructed from kwargs."""
        self._current = self._current + timedelta(**kwargs)
        return self._current
