"""Snapshot ingestion for scheduled corridor optimization.

TMC snapshot payloads are buffered per corridor; the scheduler later pulls the
recent window and aggregates it into one demand file. Two buffer
implementations with identical semantics:

  * ``InMemorySnapshotBuffer`` — single process; injectable clock for tests.
  * ``RedisSnapshotBuffer`` — shared across processes; a capped Redis sorted
    set in the versioned ``city_snap_v2`` namespace, with a TTL so an abandoned
    corridor's data expires. Versioning avoids colliding with legacy LIST keys.

Both implementations retain only snapshots whose ``timestamp_start`` falls
within the rolling two-hour retention window. ``max_keep`` remains a secondary
cap on the number of retained snapshots. An exact payload retransmission is
idempotent; a different payload at the same timestamp remains visible so the
aggregator can reject the conflicting observation window.

Timestamp contract: ``timestamp_start`` is an ISO-8601 string. For BUFFER
ordering, timezone-aware values are converted to UTC and naive values are
compared as-is (ordering only needs consistency). For time-of-day PATTERN
selection downstream, aware values are converted to the corridor's local
timezone — derived from the corridor's own coordinates, never configured —
and naive values are taken as local wall-clock time; producers should emit
timezone-aware timestamps. POSTed
snapshots whose timestamp lies more than ``FUTURE_SKEW_TOLERANCE_S`` seconds
in the future are rejected with 422 (clock skew tolerance).
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Protocol

from fastapi import APIRouter, Body, HTTPException
from terasim_city.flow.descriptors import TMCData

logger = logging.getLogger(__name__)

REQUIRED_SNAPSHOT_FIELDS = (
    "corridor_id",
    "timestamp_start",
    "observation_interval",
    "intersections",
)

#: POSTed snapshots may lead the server clock by at most this many seconds.
FUTURE_SKEW_TOLERANCE_S = 300.0

#: Snapshots older than this rolling window are discarded from every buffer.
SNAPSHOT_RETENTION_S = 2 * 60 * 60

#: Versioned away from the legacy ``city_snap`` Redis LIST namespace.
REDIS_SNAPSHOT_KEY_PREFIX = "city_snap_v2"


def _parse_timestamp(value: str) -> float:
    """ISO-8601 timestamp -> epoch seconds, normalized to UTC.

    Timezone-aware values convert to UTC; naive values are interpreted as
    UTC (the ingest contract — see the module docstring).
    """
    text = str(value)
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class SnapshotBuffer(Protocol):
    """Per-corridor rolling buffer of TMC snapshot payloads."""

    def append(self, corridor_id: str, payload: dict) -> int:
        """Store one payload; returns the buffered count for the corridor."""
        ...

    def window(self, corridor_id: str, window_s: float) -> list[dict]:
        """Payloads whose timestamp_start is within the last ``window_s`` seconds."""
        ...

    def count(self, corridor_id: str) -> int: ...


class InMemorySnapshotBuffer:
    """Single-process buffer. The clock is injectable so window filtering is
    deterministic in tests."""

    def __init__(
        self, max_keep: int = 48, clock: Callable[[], float] = time.time
    ) -> None:
        if max_keep < 1:
            raise ValueError(f"max_keep must be >= 1, got {max_keep}")
        self._max = max_keep
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, list[dict]] = {}

    def _prune(self, corridor_id: str, *, now: float) -> list[dict]:
        cutoff = now - SNAPSHOT_RETENTION_S
        retained = [
            payload
            for payload in self._data.get(corridor_id, ())
            if _parse_timestamp(payload["timestamp_start"]) >= cutoff
        ]
        retained.sort(key=lambda payload: _parse_timestamp(payload["timestamp_start"]))
        retained = retained[-self._max :]
        if retained:
            self._data[corridor_id] = retained
        else:
            self._data.pop(corridor_id, None)
        return retained

    def append(self, corridor_id: str, payload: dict) -> int:
        key = str(corridor_id)
        with self._lock:
            items = self._data.setdefault(key, [])
            if payload not in items:
                items.append(copy.deepcopy(payload))
            return len(self._prune(key, now=self._clock()))

    def window(self, corridor_id: str, window_s: float) -> list[dict]:
        now = self._clock()
        cutoff = now - float(window_s)
        with self._lock:
            items = list(self._prune(str(corridor_id), now=now))
        selected = []
        for payload in items:
            timestamp = payload.get("timestamp_start", "")
            try:
                epoch = _parse_timestamp(timestamp)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"buffered snapshot for corridor {corridor_id!r} has an "
                    f"unparseable timestamp_start {timestamp!r}: {exc}"
                ) from exc
            if epoch >= cutoff:
                selected.append(copy.deepcopy(payload))
        return selected

    def count(self, corridor_id: str) -> int:
        with self._lock:
            return len(self._prune(str(corridor_id), now=self._clock()))


class RedisSnapshotBuffer:
    """Redis sorted-set buffer shared across processes (production)."""

    def __init__(
        self,
        client,
        key_prefix: str = REDIS_SNAPSHOT_KEY_PREFIX,
        max_keep: int = 48,
        ttl_s: float = SNAPSHOT_RETENTION_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_keep < 1:
            raise ValueError(f"max_keep must be >= 1, got {max_keep}")
        if ttl_s < 1:
            raise ValueError(f"ttl_s must be >= 1, got {ttl_s}")
        self._r = client
        self._prefix = key_prefix
        self._max = max_keep
        self._ttl = ttl_s
        self._clock = clock

    def _key(self, corridor_id: str) -> str:
        return f"{self._prefix}:{corridor_id}"

    def _prune(self, pipe, key: str, *, now: float) -> None:
        cutoff = now - SNAPSHOT_RETENTION_S
        pipe.zremrangebyscore(key, "-inf", f"({cutoff}")

    def append(self, corridor_id: str, payload: dict) -> int:
        key = self._key(str(corridor_id))
        pipe = self._r.pipeline()
        timestamp = _parse_timestamp(payload["timestamp_start"])
        pipe.zadd(key, {json.dumps(payload): timestamp})
        self._prune(pipe, key, now=self._clock())
        pipe.zremrangebyrank(key, 0, -self._max - 1)
        pipe.expire(key, int(self._ttl))
        pipe.zcard(key)
        return int(pipe.execute()[-1])

    def window(self, corridor_id: str, window_s: float) -> list[dict]:
        now = self._clock()
        cutoff = max(now - SNAPSHOT_RETENTION_S, now - float(window_s))
        key = self._key(str(corridor_id))
        pipe = self._r.pipeline()
        self._prune(pipe, key, now=now)
        pipe.zrangebyscore(key, cutoff, "+inf")
        selected = []
        for raw in pipe.execute()[-1]:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            payload = json.loads(text)
            selected.append(payload)
        return selected

    def count(self, corridor_id: str) -> int:
        key = self._key(str(corridor_id))
        pipe = self._r.pipeline()
        self._prune(pipe, key, now=self._clock())
        pipe.zcard(key)
        return int(pipe.execute()[-1])


def _validate_snapshot(payload: dict, *, now: float) -> None:
    """Schema gate for POSTed snapshots; violations map to 422."""
    for field in REQUIRED_SNAPSHOT_FIELDS:
        if field not in payload:
            raise HTTPException(
                status_code=422, detail=f"snapshot is missing required field {field!r}"
            )
    if not isinstance(payload["corridor_id"], str) or not payload["corridor_id"]:
        raise HTTPException(
            status_code=422, detail="corridor_id must be a non-empty string"
        )
    if not isinstance(payload["timestamp_start"], str):
        raise HTTPException(status_code=422, detail="timestamp_start must be a string")
    try:
        epoch = _parse_timestamp(payload["timestamp_start"])
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=(
                "timestamp_start must be an ISO-8601 timestamp, got "
                f"{payload['timestamp_start']!r}"
            ),
        )
    lead_s = epoch - now
    if lead_s > FUTURE_SKEW_TOLERANCE_S:
        raise HTTPException(
            status_code=422,
            detail=(
                f"timestamp_start {payload['timestamp_start']!r} is "
                f"{lead_s:.0f}s in the future; the clock skew tolerance is "
                f"{FUTURE_SKEW_TOLERANCE_S:.0f}s (timestamps are UTC — a "
                f"large lead usually means a non-UTC producer clock)"
            ),
        )
    intersections = payload["intersections"]
    if not isinstance(intersections, list) or len(intersections) < 1:
        raise HTTPException(
            status_code=422, detail="intersections must be a non-empty list"
        )
    try:
        TMCData.from_payload(payload, source_path="<snapshot>")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_snapshot_router(
    buffer: SnapshotBuffer, clock: Callable[[], float] = time.time
) -> APIRouter:
    """Build the snapshot-ingestion router around an injected buffer.

    ``clock`` feeds the future-timestamp (clock skew) check; inject it in
    tests the same way as the buffer's clock.
    """
    router = APIRouter(prefix="/v1/city/snapshots", tags=["city-ingest"])

    @router.post("", status_code=202)
    def post_snapshot(payload: dict = Body(...)):
        _validate_snapshot(payload, now=clock())
        corridor_id = payload["corridor_id"]
        buffered = buffer.append(corridor_id, payload)
        return {"corridor_id": corridor_id, "buffered": buffered}

    @router.get("/{corridor_id}")
    def get_snapshots(corridor_id: str):
        # window_s = span between the oldest and newest buffered
        # timestamp_start (0.0 with fewer than two snapshots).
        snapshots = buffer.window(corridor_id, float("inf"))
        epochs = [
            _parse_timestamp(item["timestamp_start"])
            for item in snapshots
            if item.get("timestamp_start")
        ]
        window_s = max(epochs) - min(epochs) if len(epochs) >= 2 else 0.0
        return {
            "corridor_id": corridor_id,
            "count": buffer.count(corridor_id),
            "window_s": window_s,
        }

    return router
