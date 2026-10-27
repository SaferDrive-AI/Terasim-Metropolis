"""Aggregate multiple TMC snapshot payloads into a single TMC payload.

This lives on the SERVICE side deliberately: how a flow file is statistically
assembled (snapshot buffering, windowing, cadence) is business-layer concern.
The core flow module (terasim_city.flow) consumes exactly one flow file and
knows nothing about snapshots.

Contract: all snapshots must share the same ``corridor_id``; counts are summed
per intersection / direction / movement; the output ``observation_interval`` is
the sum of the input windows expressed in seconds (parsed with the exact same
semantics as :meth:`terasim_city.flow.descriptors.TMCData.observation_seconds`).
Snapshots must cover the same intersection ids, keep each id's coordinates
stable, and describe non-overlapping observation windows. The earliest start is
selected chronologically after parsing timezone offsets. The returned dict is a
valid TMC payload — it round-trips through ``TMCData.from_json`` when written
to disk.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Sequence

from terasim_city.flow.descriptors import TMCData


def _observation_seconds(interval: str, *, index: int) -> float:
    """Parse an observation interval using TMCData's own parser.

    Reuses :meth:`TMCData.observation_seconds` (rather than duplicating its
    regex) by constructing a minimal instance, so the accepted formats stay in
    lockstep with the rest of the flow pipeline.
    """
    probe = TMCData(
        corridor_id="",
        observation_interval=str(interval),
        timestamp_start="",
        intersections=(),
    )
    try:
        return probe.observation_seconds()
    except ValueError as exc:
        raise ValueError(f"snapshot[{index}]: {exc}") from exc


def _timestamp_epoch(value: str, *, index: int) -> float:
    """Parse one snapshot start with the same naive-as-UTC ingest contract."""

    text = str(value)
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"snapshot[{index}]: invalid timestamp_start {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def aggregate_snapshots(snapshots: Sequence[dict]) -> dict:
    """Merge >=1 TMC payload dicts into a single aggregated TMC payload.

    Args:
        snapshots: TMC payload dicts (same schema as
            ``examples/corridor_demo/demo_tmc.json``).

    Returns:
        A TMC payload dict with counts summed per intersection/direction/
        movement, ``observation_interval`` equal to the summed input windows
        (``"<seconds>s"``), and ``timestamp_start`` equal to the earliest
        input ``timestamp_start``.

    Raises:
        ValueError: On an empty sequence, mixed ``corridor_id`` values, any
            snapshot missing required fields / malformed counts, negative
            counts, a duplicate or overlapping observation window, coordinate
            drift for the same intersection id, or a snapshot whose
            intersection-id set differs from the first snapshot's (a partial
            snapshot would silently under-count missing demand).
    """
    if not snapshots:
        raise ValueError("aggregate_snapshots: got an empty snapshot list.")

    corridor_id: str | None = None
    total_seconds = 0.0
    timestamps: list[tuple[float, str]] = []
    seen_timestamps: set[float] = set()
    observation_windows: list[tuple[float, float, int, str]] = []
    expected_ids: set[str] | None = None
    # intersection_id -> {"lat","lon","counts"}; insertion order = first-seen.
    merged: dict[str, dict] = {}

    for index, snapshot in enumerate(snapshots):
        if not isinstance(snapshot, dict):
            raise ValueError(
                f"snapshot[{index}]: expected a dict payload, "
                f"got {type(snapshot).__name__}."
            )
        TMCData.from_payload(snapshot, source_path=f"snapshot[{index}]")
        for required in ("corridor_id", "observation_interval", "intersections"):
            if required not in snapshot:
                raise ValueError(
                    f"snapshot[{index}] is missing required field {required!r}."
                )

        snap_corridor = str(snapshot["corridor_id"])
        if corridor_id is None:
            corridor_id = snap_corridor
        elif snap_corridor != corridor_id:
            raise ValueError(
                f"snapshot[{index}] has corridor_id={snap_corridor!r}, "
                f"expected {corridor_id!r} (all snapshots must share one corridor)."
            )

        observation_seconds = _observation_seconds(
            snapshot["observation_interval"], index=index
        )
        total_seconds += observation_seconds

        timestamp = str(snapshot.get("timestamp_start", "") or "")
        if timestamp:
            timestamp_epoch = _timestamp_epoch(timestamp, index=index)
            if timestamp_epoch in seen_timestamps:
                raise ValueError(
                    f"snapshot[{index}]: duplicate timestamp_start "
                    f"{timestamp!r} — the same observation window appears "
                    f"more than once, which would double-count its demand."
                )
            seen_timestamps.add(timestamp_epoch)
            timestamps.append((timestamp_epoch, timestamp))
            observation_windows.append(
                (
                    timestamp_epoch,
                    timestamp_epoch + observation_seconds,
                    index,
                    timestamp,
                )
            )

        raw_intersections = snapshot["intersections"]
        if not isinstance(raw_intersections, list) or not raw_intersections:
            raise ValueError(
                f"snapshot[{index}]: 'intersections' must be a non-empty list."
            )

        snapshot_ids: set[str] = set()
        for j, item in enumerate(raw_intersections):
            prefix = f"snapshot[{index}] intersection[{j}]"
            if not isinstance(item, dict):
                raise ValueError(
                    f"{prefix}: expected an object, got {type(item).__name__}."
                )
            for required in ("intersection_id", "lat", "lon", "counts"):
                if required not in item:
                    raise ValueError(
                        f"{prefix}: missing required field {required!r}."
                    )
            iid = str(item["intersection_id"])
            snapshot_ids.add(iid)
            raw_counts = item["counts"]
            if not isinstance(raw_counts, dict):
                raise ValueError(
                    f"{prefix}: 'counts' must be a dict, "
                    f"got {type(raw_counts).__name__}."
                )

            try:
                item_lat = float(item["lat"])
                item_lon = float(item["lon"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{prefix}: lat/lon must be numeric ({exc})."
                ) from exc
            if iid not in merged:
                # Coordinates become immutable for this aggregation window.
                merged[iid] = {
                    "intersection_id": iid,
                    "lat": item_lat,
                    "lon": item_lon,
                    "counts": {},
                }
            else:
                expected = merged[iid]
                if not (
                    math.isclose(
                        item_lat, expected["lat"], rel_tol=0.0, abs_tol=1e-7
                    )
                    and math.isclose(
                        item_lon, expected["lon"], rel_tol=0.0, abs_tol=1e-7
                    )
                ):
                    raise ValueError(
                        f"{prefix}: coordinates changed for {iid!r}: "
                        f"({item_lat}, {item_lon}) != "
                        f"({expected['lat']}, {expected['lon']})"
                    )

            counts = merged[iid]["counts"]
            for direction, turns in raw_counts.items():
                if not isinstance(turns, dict):
                    raise ValueError(
                        f"{prefix}: counts[{direction!r}] must be a dict, "
                        f"got {type(turns).__name__}."
                    )
                dir_counts = counts.setdefault(str(direction), {})
                for turn, turn_count in turns.items():
                    try:
                        value = int(turn_count)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"{prefix}: counts[{direction!r}][{turn!r}] must "
                            f"be int-coercible, got {turn_count!r} ({exc})."
                        ) from exc
                    if value < 0:
                        raise ValueError(
                            f"{prefix}: counts[{direction!r}][{turn!r}] must "
                            f"be >= 0, got {value}."
                        )
                    key = str(turn)
                    dir_counts[key] = dir_counts.get(key, 0) + value

        if expected_ids is None:
            expected_ids = snapshot_ids
        elif snapshot_ids != expected_ids:
            missing = sorted(expected_ids - snapshot_ids)
            extra = sorted(snapshot_ids - expected_ids)
            details = []
            if missing:
                details.append(f"missing intersection_id(s) {missing}")
            if extra:
                details.append(f"extra intersection_id(s) {extra}")
            raise ValueError(
                f"snapshot[{index}] (timestamp_start={timestamp!r}) has "
                f"{' and '.join(details)} relative to the first snapshot — "
                f"every snapshot must cover the same intersections, otherwise "
                f"the aggregated counts silently under-represent the "
                f"missing ones."
            )

    previous: tuple[float, float, int, str] | None = None
    for current in sorted(observation_windows):
        if previous is not None and current[0] < previous[1]:
            raise ValueError(
                f"snapshot[{current[2]}] window starting {current[3]!r} overlaps "
                f"snapshot[{previous[2]}] starting {previous[3]!r}; overlapping "
                "counts cannot be summed without double-counting demand"
            )
        previous = current

    if total_seconds.is_integer():
        interval = f"{int(total_seconds)}s"
    else:
        interval = f"{total_seconds}s"

    return {
        "corridor_id": corridor_id,
        "observation_interval": interval,
        "timestamp_start": min(timestamps)[1] if timestamps else "",
        "intersections": list(merged.values()),
    }
