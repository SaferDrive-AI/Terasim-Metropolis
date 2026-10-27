"""Derive a corridor's IANA timezone from its own coordinates.

Signal schedules are LOCAL wall-clock tables, so timezone-aware timestamps
must be converted into the corridor's local time before selecting a
time-of-day pattern. The timezone is a function of WHERE the corridor is —
the coordinates every request already carries — so it is derived, never
configured: there is deliberately no environment variable, request field or
chart value for it. A knob would only add a way to silently run another
city's clock.

Lookup uses ``tzfpy`` (offline polygon data, ~10 MB, sub-millisecond after
the first call). Over open water tzfpy returns nautical ``Etc/GMT±N`` zones
rather than ``None`` — irrelevant for road corridors, which sit on land.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def timezone_for(lat: float, lon: float) -> "str | None":
    """IANA timezone name at a WGS84 point, or ``None`` when undecidable.

    ``None`` means the lookup backend is unavailable or errored — callers
    must fail loud rather than guess a clock.
    """
    try:
        from tzfpy import get_tz
    except ImportError:  # pragma: no cover - tzfpy is a declared dependency
        logger.warning(
            "tzfpy is not installed; cannot derive a local timezone from "
            "coordinates"
        )
        return None
    try:
        # tzfpy argument order is LONGITUDE FIRST — the classic trap.
        return get_tz(float(lon), float(lat)) or None
    except Exception as exc:  # defensive: never let tz lookup kill a run
        logger.warning("timezone lookup failed for (%s, %s): %s", lat, lon, exc)
        return None


__all__ = ["timezone_for"]
