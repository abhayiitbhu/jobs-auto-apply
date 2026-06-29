"""Job count limits — 0 means unlimited."""

from __future__ import annotations

SCRAPE_CAP = 2000


def is_unlimited(limit: int) -> bool:
    return limit <= 0


def apply_cap(limit: int) -> int | None:
    """Return max jobs to apply, or None for no cap."""
    return None if is_unlimited(limit) else limit


def scrape_limit(limit: int, *, multiplier: int = 1) -> int:
    """Max listings to pull from a platform search in one pass."""
    if is_unlimited(limit):
        return SCRAPE_CAP
    return max(limit, 1) * multiplier
