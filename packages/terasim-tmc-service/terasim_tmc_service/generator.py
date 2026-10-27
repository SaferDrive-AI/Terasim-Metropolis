"""Periodic TMC Generator: fetch VSS behaviors for one window and count turning movements.

Counting reuses the vendored NVIDIA helpers (``nv_generate_tmc``) so the numbers match
``generate_tmc.py --vss-host`` exactly. Two deliberate differences from the script:

* the observation window is grid-aligned to ``observation_interval`` and only taken once
  it ended at least ``settle_lag_s`` ago (behaviors are written when a track ends, and the
  TeraSim aggregator requires non-overlapping windows);
* the behavior fetch goes through httpx with an injectable transport (testability).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

import httpx

from terasim_tmc_service.nv_generate_tmc import (
    COUNTED_OBJECT_TYPES,
    MAX_RESULT_SIZE,
    is_counted_object_type,
    build_tmc,
    format_endpoint_timestamp,
    format_utc_seconds,
    get_assignment_sensor,
    increment_tmc,
)
from terasim_tmc_service.registry import RegisteredIntersection
from terasim_tmc_service.spec import CorridorSpec

DEFAULT_SETTLE_LAG_S = 180.0


# -- windows -------------------------------------------------------------------


def aligned_window(
    now: datetime, interval_s: float, *, settle_lag_s: float = DEFAULT_SETTLE_LAG_S
) -> tuple[str, str]:
    """Latest grid-aligned window that ended at least ``settle_lag_s`` before ``now``.

    Returns ``(timestamp_start, query_end)``: the window covers
    ``[start, start + interval)``; ``query_end`` is the last whole second inside it,
    matching the inclusive ``end:[a TO b]`` query the NVIDIA script issues.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime (UTC)")
    if interval_s <= 0:
        raise ValueError(f"interval_s must be positive, got {interval_s}")
    settled = now.astimezone(timezone.utc).timestamp() - float(settle_lag_s)
    grid_end = math.floor(settled / interval_s) * interval_s
    start = datetime.fromtimestamp(grid_end - interval_s, tz=timezone.utc)
    query_end = datetime.fromtimestamp(grid_end - 1, tz=timezone.utc)
    return format_utc_seconds(start), format_utc_seconds(query_end)


def seconds_until_next_window(
    now: datetime, interval_s: float, settle_lag_s: float, *, margin_s: float = 5.0
) -> float:
    """Seconds until the next grid window becomes settled (plus a small margin)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime (UTC)")
    settled = now.astimezone(timezone.utc).timestamp() - float(settle_lag_s)
    next_grid_end = (math.floor(settled / interval_s) + 1) * interval_s
    due = next_grid_end + float(settle_lag_s) + float(margin_s)
    return max(due - now.astimezone(timezone.utc).timestamp(), 1.0)


# -- behavior source -------------------------------------------------------------


class BehaviorSource(Protocol):
    def fetch(self, sensor_id: str, timestamp_start: str, timestamp_end: str) -> list[dict]:
        """Behavior objects of one sensor whose ``end`` lies inside the window."""
        ...


class VssBehaviorSource:
    """VSS Video Analytics behavior API client (``GET <base>/behavior``)."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, sensor_id: str, timestamp_start: str, timestamp_end: str) -> list[dict]:
        params = {
            "sensorId": sensor_id,
            "queryString": (
                f"end:[{format_endpoint_timestamp(timestamp_start)} TO "
                f"{format_endpoint_timestamp(timestamp_end)}]"
            ),
            "maxResultSize": MAX_RESULT_SIZE,
        }
        response = self._client.get("/behavior", params=params)
        if not response.is_success:
            raise RuntimeError(
                f"GET /behavior for {sensor_id} failed with HTTP {response.status_code}: "
                f"{response.text}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"GET /behavior for {sensor_id} returned non-JSON") from exc
        behaviors = data.get("behaviors") if isinstance(data, dict) else None
        if not isinstance(behaviors, list):
            raise RuntimeError(
                f"GET /behavior for {sensor_id}: expected a 'behaviors' list in the response"
            )
        return behaviors


# -- counting --------------------------------------------------------------------


def filter_counted(
    behaviors: Sequence[Any],
    sensor_id: str,
    counted_object_types: frozenset[str] = COUNTED_OBJECT_TYPES,
) -> list[dict]:
    """Same predicate as the NVIDIA script: this sensor and a counted label.

    ``counted_object_types`` is a canonical set (``parse_counted_object_types``); the default
    is Vehicle and two_wheeler.
    """
    kept = []
    for item in behaviors:
        if not isinstance(item, dict):
            continue
        sensor = item.get("sensor", {})
        obj = item.get("object", {})
        if (
            isinstance(sensor, dict)
            and sensor.get("id") == sensor_id
            and isinstance(obj, dict)
            and is_counted_object_type(obj.get("type"), counted_object_types)
        ):
            kept.append(item)
    return kept


@dataclass(frozen=True)
class GeneratedTmc:
    """The FTMC payload plus per-intersection/per-sensor processing stats."""

    payload: dict
    stats: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)


def build_ftmc(
    spec: CorridorSpec,
    intersections: Sequence[RegisteredIntersection],
    behaviors_by_sensor: Mapping[str, Sequence[Any]],
    *,
    timestamp_start: str,
    counted_object_types: frozenset[str] = COUNTED_OBJECT_TYPES,
) -> GeneratedTmc:
    """Count turning movements for the registered intersections (NVIDIA FTMC + ``name``)."""
    payload: dict[str, Any] = {
        "corridor_id": spec.corridor_id,
        "observation_interval": spec.observation_interval,
        "timestamp_start": timestamp_start,
        "intersections": [],
    }
    stats: dict[str, dict[str, dict[str, int]]] = {}
    for registered in intersections:
        intersection = spec.get(registered.intersection_id)
        tmc = build_tmc(intersection.sensors, intersection.movements)
        sensor_stats: dict[str, dict[str, int]] = {}
        for sensor in intersection.sensors:
            processed = skipped_without_edges = skipped_without_mapping = 0
            for item in filter_counted(
                behaviors_by_sensor.get(sensor, ()), sensor, counted_object_types
            ):
                edges = item.get("edges", [])
                if not edges:
                    skipped_without_edges += 1
                    continue
                processed += 1
                object_id = item.get("object", {}).get("id")
                if not increment_tmc(
                    edges,
                    intersection.headings,
                    intersection.movements,
                    tmc,
                    sensor,
                    object_id,
                    None,
                ):
                    skipped_without_mapping += 1
            sensor_stats[sensor] = {
                "processed_with_edges": processed,
                "skipped_without_edges": skipped_without_edges,
                "skipped_without_mapping": skipped_without_mapping,
            }
        counts: dict[str, dict[str, int]] = {}
        for heading, assignment in intersection.assignments.items():
            sensor = get_assignment_sensor(assignment)
            if sensor is None:
                continue
            counts[heading] = dict(tmc[sensor][heading])
        payload["intersections"].append(
            {
                "intersection_id": registered.intersection_id,
                "name": registered.name,
                "lat": intersection.lat,
                "lon": intersection.lon,
                "counts": counts,
            }
        )
        stats[registered.intersection_id] = sensor_stats
    return GeneratedTmc(payload=payload, stats=stats)


__all__ = [
    "DEFAULT_SETTLE_LAG_S",
    "BehaviorSource",
    "GeneratedTmc",
    "VssBehaviorSource",
    "aligned_window",
    "build_ftmc",
    "filter_counted",
    "seconds_until_next_window",
]
