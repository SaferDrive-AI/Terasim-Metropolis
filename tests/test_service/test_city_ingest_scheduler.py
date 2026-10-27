"""Tests for snapshot ingestion (buffer + endpoints) and the city scheduler.

Pure in-process: in-memory buffer with an injected clock, FastAPI TestClient,
and a submit spy for the scheduler — no Redis, no SUMO. The scheduler happy
path uses the shared demo TMC template (the hour-level equivalent of the
corridor demo snapshots) and verifies the aggregated file round-trips through
TMCData.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terasim_service.city import ingest_api
from terasim_service.city.ingest_api import (
    FUTURE_SKEW_TOLERANCE_S,
    InMemorySnapshotBuffer,
    RedisSnapshotBuffer,
    create_snapshot_router,
)
from terasim_service.city.scheduler import CityScheduler, load_scheduled_config

from tests.fixtures.tmc_templates import demo_snapshots

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_DIR = _REPO_ROOT / "examples" / "corridor_demo"

_BASE = "/v1/city/snapshots"


def _epoch(iso: str) -> float:
    """Epoch of an ISO timestamp under the ingest contract: naive == UTC."""
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _snapshot(corridor_id: str = "c1", timestamp: str = "2026-07-01T08:00:00") -> dict:
    return {
        "corridor_id": corridor_id,
        "timestamp_start": timestamp,
        "observation_interval": "10min",
        "intersections": [
            {
                "intersection_id": "i1",
                "lat": 37.0,
                "lon": -121.0,
                "counts": {"East": {"Straight": 10}},
            }
        ],
    }


def _load_demo_snapshots() -> list[dict]:
    # The six distinct 10-minute corridor demo snapshots (corridor_id
    # demo_arterial, i1/i2/i3); aggregating them exercises the summing path.
    return demo_snapshots()


# --- buffer ---------------------------------------------------------------------


def test_buffer_append_count_and_window_filtering() -> None:
    now = _epoch("2026-07-01T09:00:00")
    buffer = InMemorySnapshotBuffer(clock=lambda: now)

    assert buffer.append("c1", _snapshot(timestamp="2026-07-01T07:00:00")) == 1
    assert buffer.append("c1", _snapshot(timestamp="2026-07-01T08:30:00")) == 2
    assert buffer.append("c1", _snapshot(timestamp="2026-07-01T08:50:00")) == 3
    assert buffer.count("c1") == 3
    assert buffer.count("other") == 0

    # Window of 1h from 09:00 keeps 08:30 and 08:50, drops 07:00.
    recent = buffer.window("c1", 3600)
    assert [s["timestamp_start"] for s in recent] == [
        "2026-07-01T08:30:00",
        "2026-07-01T08:50:00",
    ]
    # A huge window returns everything; a tiny one nothing.
    assert len(buffer.window("c1", 10 * 3600)) == 3
    assert buffer.window("c1", 60) == []
    assert buffer.window("unknown", 3600) == []


class _FakeRedisPipeline:
    """Small transactional facade covering the Redis commands used by the buffer."""

    def __init__(self, client) -> None:
        self._client = client
        self._commands: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self._commands.append((name, args, kwargs))
            return self

        return queue

    def execute(self) -> list:
        return [
            getattr(self._client, name)(*args, **kwargs)
            for name, args, kwargs in self._commands
        ]


class _FakeRedis:
    """In-process Redis subset with both legacy-list and sorted-set semantics."""

    def __init__(self) -> None:
        self._lists: dict[str, list[str]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._expirations: dict[str, int] = {}

    def pipeline(self):
        return _FakeRedisPipeline(self)

    def rpush(self, key: str, value: str) -> int:
        values = self._lists.setdefault(key, [])
        values.append(value)
        return len(values)

    def ltrim(self, key: str, start: int, stop: int) -> bool:
        values = self._lists.setdefault(key, [])
        start = max(0, len(values) + start) if start < 0 else start
        stop = len(values) + stop if stop < 0 else stop
        self._lists[key] = values[start : stop + 1]
        return True

    def llen(self, key: str) -> int:
        return len(self._lists.get(key, ()))

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        values = self._lists.get(key, [])
        stop = len(values) + stop if stop < 0 else stop
        return values[start : stop + 1]

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        values = self._zsets.setdefault(key, {})
        added = sum(member not in values for member in mapping)
        values.update(mapping)
        return added

    def zremrangebyscore(self, key: str, minimum, maximum) -> int:
        values = self._zsets.setdefault(key, {})
        lower = float("-inf") if minimum == "-inf" else float(minimum)
        exclusive = isinstance(maximum, str) and maximum.startswith("(")
        upper = float(maximum[1:] if exclusive else maximum)
        removed = [
            member
            for member, score in values.items()
            if lower <= score < upper or (not exclusive and lower <= score <= upper)
        ]
        for member in removed:
            del values[member]
        return len(removed)

    def zremrangebyrank(self, key: str, start: int, stop: int) -> int:
        values = self._zsets.setdefault(key, {})
        ordered = sorted(values, key=lambda member: (values[member], member))
        start = max(0, len(ordered) + start) if start < 0 else start
        stop = len(ordered) + stop if stop < 0 else stop
        removed = ordered[start : stop + 1]
        for member in removed:
            del values[member]
        return len(removed)

    def zrangebyscore(self, key: str, minimum, maximum) -> list[str]:
        values = self._zsets.get(key, {})
        lower = float("-inf") if minimum == "-inf" else float(minimum)
        upper = float("inf") if maximum == "+inf" else float(maximum)
        return [
            member
            for member in sorted(values, key=lambda item: (values[item], item))
            if lower <= values[member] <= upper
        ]

    def zcard(self, key: str) -> int:
        return len(self._zsets.get(key, ()))

    def expire(self, key: str, ttl: int) -> bool:
        self._expirations[key] = ttl
        return True


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_snapshot_buffers_enforce_two_hour_timestamp_retention(backend: str) -> None:
    now = [_epoch("2026-07-01T10:00:00")]
    if backend == "memory":
        buffer = InMemorySnapshotBuffer(clock=lambda: now[0])
    else:
        buffer = RedisSnapshotBuffer(_FakeRedis(), clock=lambda: now[0])

    # Arrival order does not define retention: the stale snapshot arrives last.
    assert buffer.append("c1", _snapshot(timestamp="2026-07-01T09:30:00")) == 1
    assert buffer.append("c1", _snapshot(timestamp="2026-07-01T08:00:00")) == 2
    assert buffer.append("c1", _snapshot(timestamp="2026-07-01T07:59:59")) == 2
    assert [item["timestamp_start"] for item in buffer.window("c1", 10 * 3600)] == [
        "2026-07-01T08:00:00",
        "2026-07-01T09:30:00",
    ]

    # Retention advances with wall time even when no new snapshot is appended.
    now[0] += 1
    assert buffer.count("c1") == 1
    assert [item["timestamp_start"] for item in buffer.window("c1", float("inf"))] == [
        "2026-07-01T09:30:00"
    ]


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_exact_snapshot_retransmission_is_idempotent(backend: str) -> None:
    now = _epoch("2026-07-01T09:00:00")
    if backend == "memory":
        buffer = InMemorySnapshotBuffer(clock=lambda: now)
    else:
        buffer = RedisSnapshotBuffer(_FakeRedis(), clock=lambda: now)
    payload = _snapshot(timestamp="2026-07-01T08:50:00")

    assert buffer.append("c1", payload) == 1
    assert buffer.append("c1", payload) == 1
    assert buffer.window("c1", 3600) == [payload]


def test_redis_buffer_uses_versioned_key_and_retention_ttl() -> None:
    now = _epoch("2026-07-01T10:00:00")
    client = _FakeRedis()
    buffer = RedisSnapshotBuffer(client, clock=lambda: now)

    assert buffer.append("c1", _snapshot(timestamp="2026-07-01T09:30:00")) == 1

    key = "city_snap_v2:c1"
    assert ingest_api.REDIS_SNAPSHOT_KEY_PREFIX == "city_snap_v2"
    assert set(client._zsets) == {key}
    assert client._expirations == {key: int(ingest_api.SNAPSHOT_RETENTION_S)}


# --- Redis-backed buffer (self-skipped without a reachable Redis) ---------------


def _redis_available() -> bool:
    try:
        import redis

        from terasim_service.utils.base import redis_connection_kwargs_from_env

        client = redis.Redis(
            **redis_connection_kwargs_from_env(), socket_connect_timeout=0.5
        )
        client.ping()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _redis_available(), reason="Redis not available")
def test_redis_buffer_append_window_count_and_cap() -> None:
    import uuid

    import redis

    from terasim_service.utils.base import redis_connection_kwargs_from_env

    client = redis.Redis(**redis_connection_kwargs_from_env())
    prefix = f"test_city_snap_{uuid.uuid4().hex}"
    now = _epoch("2026-07-01T09:00:00")
    buffer = RedisSnapshotBuffer(client, key_prefix=prefix, max_keep=2, clock=lambda: now)
    try:
        assert buffer.append("c1", _snapshot(timestamp="2026-07-01T07:00:00")) == 1
        assert buffer.append("c1", _snapshot(timestamp="2026-07-01T08:30:00")) == 2
        # max_keep=2: the third append evicts the oldest.
        assert buffer.append("c1", _snapshot(timestamp="2026-07-01T08:50:00")) == 2
        assert buffer.count("c1") == 2
        recent = buffer.window("c1", 3600)
        assert [s["timestamp_start"] for s in recent] == [
            "2026-07-01T08:30:00",
            "2026-07-01T08:50:00",
        ]
        assert buffer.window("c1", 60) == []
    finally:
        for key in client.scan_iter(f"{prefix}:*"):
            client.delete(key)


# --- endpoints ------------------------------------------------------------------


def _client(buffer=None, clock=None):
    if clock is None:
        now = _epoch("2026-07-01T09:00:00")

        def fixed_clock() -> float:
            return now

        clock = fixed_clock
    buffer = buffer if buffer is not None else InMemorySnapshotBuffer(clock=clock)
    app = FastAPI()
    app.include_router(create_snapshot_router(buffer, clock=clock))
    return TestClient(app), buffer


def test_post_snapshot_202_and_buffered_count() -> None:
    client, _buffer = _client()
    first = client.post(_BASE, json=_snapshot())
    assert first.status_code == 202
    assert first.json() == {"corridor_id": "c1", "buffered": 1}
    second = client.post(_BASE, json=_snapshot(timestamp="2026-07-01T08:10:00"))
    assert second.json() == {"corridor_id": "c1", "buffered": 2}


def test_parse_timestamp_accepts_tmc_utc_z_suffix() -> None:
    """Production TMC timestamps use the RFC 3339 ``Z`` UTC suffix."""
    from terasim_service.city.ingest_api import _parse_timestamp

    assert _parse_timestamp("2026-07-11T16:16:17.000Z") == _parse_timestamp(
        "2026-07-11T16:16:17.000+00:00"
    )


def test_post_snapshot_rejects_unknown_tmc_movement() -> None:
    now = _epoch("2026-07-01T09:00:00")
    client, buffer = _client(
        buffer=InMemorySnapshotBuffer(clock=lambda: now), clock=lambda: now
    )
    payload = _snapshot(timestamp="2026-07-01T08:00:00")
    payload["intersections"][0]["counts"] = {
        "Southeast": {"Straight": 10}
    }

    response = client.post(_BASE, json=payload)

    assert response.status_code == 422
    assert "unknown direction" in response.json()["detail"]
    assert buffer.count("c1") == 0


def test_post_snapshot_tz_aware_timestamp_converted_to_utc() -> None:
    now = _epoch("2026-07-01T08:30:00")
    client, _buffer = _client(
        buffer=InMemorySnapshotBuffer(clock=lambda: now), clock=lambda: now
    )
    # 08:00 UTC expressed naive and as 01:20 Pacific (= 08:20 UTC).
    assert client.post(_BASE, json=_snapshot(timestamp="2026-07-01T08:00:00")).status_code == 202
    reply = client.post(_BASE, json=_snapshot(timestamp="2026-07-01T01:20:00-07:00"))
    assert reply.status_code == 202
    body = client.get(f"{_BASE}/c1").json()
    assert body["count"] == 2
    assert body["window_s"] == pytest.approx(1200.0)  # 08:00Z .. 08:20Z


def test_post_snapshot_far_future_timestamp_422_clock_skew() -> None:
    now = _epoch("2026-07-01T08:00:00")
    client, buffer = _client(
        buffer=InMemorySnapshotBuffer(clock=lambda: now), clock=lambda: now
    )
    # 600 s ahead of the injected clock: beyond the 300 s tolerance.
    assert FUTURE_SKEW_TOLERANCE_S == 300.0
    reply = client.post(_BASE, json=_snapshot(timestamp="2026-07-01T08:10:00"))
    assert reply.status_code == 422
    assert "clock skew" in reply.json()["detail"]
    assert buffer.count("c1") == 0  # rejected payloads are not buffered
    # Within tolerance (240 s ahead) is accepted.
    accepted = client.post(_BASE, json=_snapshot(timestamp="2026-07-01T08:04:00"))
    assert accepted.status_code == 202


def test_get_snapshots_count_and_window_span() -> None:
    client, _buffer = _client()
    empty = client.get(f"{_BASE}/c1").json()
    assert empty == {"corridor_id": "c1", "count": 0, "window_s": 0.0}

    client.post(_BASE, json=_snapshot(timestamp="2026-07-01T08:00:00"))
    client.post(_BASE, json=_snapshot(timestamp="2026-07-01T08:20:00"))
    body = client.get(f"{_BASE}/c1").json()
    assert body["count"] == 2
    assert body["window_s"] == pytest.approx(1200.0)


# --- scheduled config -----------------------------------------------------------


_DEMO_SCHEDULED_CONFIG_YAML = """\
corridors:
  demo_arterial:
    base_sumocfg: examples/corridor_demo/demo.sumocfg
    corridor:
      tls_ids: [J1, J2, J3]
    baseline_sheet: examples/corridor_demo/baseline_sheet_uncoordinated.json
    sim_end: 600
    max_trials: 6
    n_workers: 2
"""


def _write_demo_scheduled_config(directory: Path) -> Path:
    """Hardcoded copy of examples/corridor_demo/scheduled_config.yaml."""
    path = Path(directory) / "scheduled_config.yaml"
    path.write_text(_DEMO_SCHEDULED_CONFIG_YAML, encoding="utf-8")
    return path


def test_load_scheduled_config_demo_yaml(tmp_path: Path) -> None:
    corridors = load_scheduled_config(_write_demo_scheduled_config(tmp_path))
    assert set(corridors) == {"demo_arterial"}
    entry = corridors["demo_arterial"]
    assert entry["corridor_id"] == "demo_arterial"
    assert entry["corridor"] == {"tls_ids": ["J1", "J2", "J3"]}
    assert entry["base_sumocfg"].endswith("demo.sumocfg")
    # Extra keys are forwarded verbatim into the request dict.
    assert entry["max_trials"] == 6
    assert entry["sim_end"] == 600
    assert entry["n_workers"] == 2
    assert entry["baseline_sheet"].endswith("baseline_sheet_uncoordinated.json")


def test_load_scheduled_config_from_to_list_normalized(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "corridors:\n  c1:\n    base_sumocfg: a.sumocfg\n    corridor: [i1, i3]\n",
        encoding="utf-8",
    )
    corridors = load_scheduled_config(path)
    assert corridors["c1"]["corridor"] == {"from": "i1", "to": "i3"}


def test_load_scheduled_config_missing_file() -> None:
    with pytest.raises(ValueError, match="not found"):
        load_scheduled_config("/nope/cfg.yaml")


# --- scheduler ------------------------------------------------------------------


def _demo_corridor_cfg() -> dict:
    return {
        "corridor_id": "demo_arterial",
        "base_sumocfg": str(_DEMO_DIR / "demo.sumocfg"),
        "corridor": {"tls_ids": ["J1", "J2", "J3"]},
        "sim_end": 300,
        "max_trials": 6,
        "n_workers": 2,
    }


def test_scheduler_tick_aggregates_and_submits_once(tmp_path: Path) -> None:
    snapshots = _load_demo_snapshots()
    # Clock one hour after the snapshot start: the hour-long demo TMC window
    # falls inside the 2h lookback.
    now = _epoch(snapshots[0]["timestamp_start"]) + 3600
    buffer = InMemorySnapshotBuffer(clock=lambda: now)
    for snap in snapshots:
        buffer.append("demo_arterial", snap)

    calls: list[tuple[dict, Path]] = []

    async def main() -> None:
        stop = asyncio.Event()

        def submit(corridor_cfg: dict, aggregated_path: Path):
            calls.append((corridor_cfg, aggregated_path))
            stop.set()  # end the run loop after this tick
            return "run_abc"

        scheduler = CityScheduler(
            buffer,
            submit,
            {"demo_arterial": _demo_corridor_cfg()},
            output_root=tmp_path,
            cadence_s=0.01,
            window_s=7200,
            clock=lambda: now,
        )
        await asyncio.wait_for(scheduler.run(stop), timeout=5)

    asyncio.run(main())

    assert len(calls) == 1  # exactly one submission for the corridor
    corridor_cfg, aggregated_path = calls[0]
    assert corridor_cfg["corridor_id"] == "demo_arterial"
    assert corridor_cfg["corridor"] == {"tls_ids": ["J1", "J2", "J3"]}
    assert corridor_cfg["max_trials"] == 6  # extras forwarded

    # The aggregated file lands under output_root/scheduled/<corridor>/<ts>/
    # and is a valid TMC payload covering all six 10-min snapshots.
    assert aggregated_path.name == "aggregated_tmc.json"
    relative = aggregated_path.relative_to(tmp_path)
    assert relative.parts[0] == "scheduled"
    assert relative.parts[1] == "demo_arterial"

    from terasim_city.flow.descriptors import TMCData

    tmc = TMCData.from_json(aggregated_path)
    assert tmc.corridor_id == "demo_arterial"
    assert tmc.observation_seconds() == pytest.approx(3600.0)
    assert sorted(i.intersection_id for i in tmc.intersections) == ["i1", "i2", "i3"]
    # Counts are summed across the snapshots.
    by_id = {i.intersection_id: i for i in tmc.intersections}
    total_east_straight = sum(
        snap["intersections"][0]["counts"]["East"]["Straight"]
        for snap in snapshots
    )
    assert by_id["i1"].counts["EB"]["through"] == total_east_straight


def test_scheduler_empty_window_skips_submit(tmp_path: Path) -> None:
    buffer = InMemorySnapshotBuffer(clock=lambda: 0.0)
    calls: list = []

    scheduler = CityScheduler(
        buffer,
        lambda cfg, path: calls.append((cfg, path)) or "run_x",
        {"demo_arterial": _demo_corridor_cfg()},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=3600,
    )
    asyncio.run(scheduler.tick())

    assert calls == []
    assert not (tmp_path / "scheduled").exists()  # nothing written either


def test_scheduler_overlap_guard_skips_tick(tmp_path: Path) -> None:
    now = _epoch("2026-07-01T08:30:00")
    buffer = InMemorySnapshotBuffer(clock=lambda: now)
    buffer.append("demo_arterial", _snapshot("demo_arterial"))
    calls: list = []

    scheduler = CityScheduler(
        buffer,
        lambda cfg, path: calls.append(cfg) or "run_x",
        {"demo_arterial": _demo_corridor_cfg()},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=7200,
        clock=lambda: now,
    )

    async def main() -> None:
        async with scheduler._lock:  # simulate a still-running previous tick
            await scheduler.tick()
        assert calls == []  # skipped, not queued
        await scheduler.tick()  # lock released -> the next tick submits
        assert len(calls) == 1

    asyncio.run(main())


def test_scheduler_submit_exception_is_contained(tmp_path: Path) -> None:
    now = _epoch("2026-07-01T08:30:00")
    buffer = InMemorySnapshotBuffer(clock=lambda: now)
    buffer.append("c1", _snapshot("c1"))
    buffer.append("c2", _snapshot("c2"))
    calls: list = []

    def submit(cfg, path):
        if cfg["corridor_id"] == "c1":
            raise RuntimeError("boom")
        calls.append(cfg["corridor_id"])
        return "run_x"

    cfg_c1 = {**_demo_corridor_cfg(), "corridor_id": "c1"}
    cfg_c2 = {**_demo_corridor_cfg(), "corridor_id": "c2"}
    scheduler = CityScheduler(
        buffer,
        submit,
        {"c1": cfg_c1, "c2": cfg_c2},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=7200,
        clock=lambda: now,
    )
    asyncio.run(scheduler.tick())

    assert calls == ["c2"]  # c1's failure did not stop c2


def test_create_app_scheduler_enabled_registers_task_handlers(
    tmp_path: Path, monkeypatch
) -> None:
    import terasim_service.api as api
    from terasim_service.constants import (
        ENVIRONMENT_VARIABLE_CITY_CADENCE_S,
        ENVIRONMENT_VARIABLE_CITY_OUTPUT_ROOT,
        ENVIRONMENT_VARIABLE_CITY_SCHEDULED_CONFIG,
        ENVIRONMENT_VARIABLE_CITY_SCHEDULER_ENABLED,
        ENVIRONMENT_VARIABLE_CITY_WINDOW_S,
        ENVIRONMENT_VARIABLE_ENABLE_CITY_API,
    )

    monkeypatch.setattr(api, "check_redis_connection", lambda: None)
    monkeypatch.setattr(api, "_city_router_mounted", False)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_ENABLE_CITY_API, "1")
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_CITY_OUTPUT_ROOT, str(tmp_path))
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_CITY_SCHEDULER_ENABLED, "1")
    monkeypatch.setenv(
        ENVIRONMENT_VARIABLE_CITY_SCHEDULED_CONFIG,
        str(_write_demo_scheduled_config(tmp_path)),
    )
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_CITY_CADENCE_S, "45")
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_CITY_WINDOW_S, "7200")

    before = len(api._startup_hooks)
    app = api.create_app()
    # The scheduler registered a startup hook (task creation is deferred to the
    # app lifespan; no event loop exists here). Starlette 1.x dropped
    # router.on_startup, so the hooks live in api._startup_hooks.
    assert len(api._startup_hooks) == before + 1
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/v1/city/scheduled/{corridor_id}/latest" in paths


def test_create_app_scheduler_enabled_without_config_raises(
    tmp_path: Path, monkeypatch
) -> None:
    import terasim_service.api as api
    from terasim_service.constants import (
        ENVIRONMENT_VARIABLE_CITY_OUTPUT_ROOT,
        ENVIRONMENT_VARIABLE_CITY_SCHEDULED_CONFIG,
        ENVIRONMENT_VARIABLE_CITY_SCHEDULER_ENABLED,
        ENVIRONMENT_VARIABLE_ENABLE_CITY_API,
    )

    monkeypatch.setattr(api, "check_redis_connection", lambda: None)
    monkeypatch.setattr(api, "_city_router_mounted", False)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_ENABLE_CITY_API, "1")
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_CITY_OUTPUT_ROOT, str(tmp_path))
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_CITY_SCHEDULER_ENABLED, "1")
    monkeypatch.delenv(ENVIRONMENT_VARIABLE_CITY_SCHEDULED_CONFIG, raising=False)

    with pytest.raises(RuntimeError, match="SCHEDULED_CONFIG"):
        api.create_app()


def test_scheduler_rejects_bad_construction(tmp_path: Path) -> None:
    buffer = InMemorySnapshotBuffer()
    with pytest.raises(ValueError):
        CityScheduler(buffer, lambda c, p: None, [], output_root=tmp_path,
                      cadence_s=1, window_s=1)
    with pytest.raises(ValueError):
        CityScheduler(buffer, lambda c, p: None, {}, output_root=tmp_path,
                      cadence_s=0, window_s=1)
    with pytest.raises(ValueError):
        CityScheduler(buffer, lambda c, p: None, {}, output_root=tmp_path,
                      cadence_s=1, window_s=0)


# --- scenario-count entries: no snapshots needed ---------------------------------


def _scenario_corridor_cfg(corridor_id: str) -> dict:
    """A scheduled entry whose counts come from the scenario's own route files."""
    return {
        **_demo_corridor_cfg(),
        "corridor_id": corridor_id,
        "counts_source": "scenario_routes",
        "flow_source": "scenario_routes",
    }


def _tmc_corridor_cfg(corridor_id: str) -> dict:
    return {**_demo_corridor_cfg(), "corridor_id": corridor_id}


def test_scheduler_fires_scenario_counts_entry_without_snapshots(
    tmp_path: Path, caplog
) -> None:
    now = _epoch("2026-07-01T08:30:00")
    buffer = InMemorySnapshotBuffer(clock=lambda: now)  # nothing buffered at all
    calls: list[tuple[dict, Path]] = []

    scheduler = CityScheduler(
        buffer,
        lambda cfg, path: calls.append((cfg, path)) or "run_x",
        {"scn": _scenario_corridor_cfg("scn"), "tmc_c": _tmc_corridor_cfg("tmc_c")},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=3600,
        clock=lambda: now,
    )
    with caplog.at_level(logging.INFO, logger="terasim_service.city.scheduler"):
        asyncio.run(scheduler.tick())

    # Exactly one submission: the scenario-count entry, with its whole config.
    assert [cfg["corridor_id"] for cfg, _path in calls] == ["scn"]
    cfg, path = calls[0]
    assert cfg["counts_source"] == "scenario_routes" and cfg["flow_source"] == "scenario_routes"
    # Its demand file is laid out like any scheduled run's and is a placeholder: the
    # worker reads the corridor id and the time of day (the scheduler's "now") from it.
    assert path.relative_to(tmp_path).parts[:2] == ("scheduled", "scn")
    assert path.name == "aggregated_tmc.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["corridor_id"] == "scn"
    assert payload["timestamp_start"] == "2026-07-01T08:30:00+00:00"

    from terasim_city.flow.descriptors import TMCData

    assert TMCData.from_json(path).corridor_id == "scn"  # still a well-formed TMC file
    # The snapshot entry is skipped with the usual log line and writes nothing.
    messages = [record.getMessage() for record in caplog.records]
    assert "corridor tmc_c: no snapshots in the last 3600 s; skipping tick" in messages
    assert not any("corridor scn: no snapshots" in message for message in messages)
    assert not (tmp_path / "scheduled" / "tmc_c").exists()


def test_scheduler_scenario_counts_entry_fires_once_when_snapshots_exist(
    tmp_path: Path,
) -> None:
    now = _epoch("2026-07-01T08:30:00")
    buffer = InMemorySnapshotBuffer(clock=lambda: now)
    buffer.append("scn", _snapshot("scn"))
    buffer.append("tmc_c", _snapshot("tmc_c"))
    calls: list[tuple[dict, Path]] = []

    scheduler = CityScheduler(
        buffer,
        lambda cfg, path: calls.append((cfg, path)) or "run_x",
        {"scn": _scenario_corridor_cfg("scn"), "tmc_c": _tmc_corridor_cfg("tmc_c")},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=7200,
        clock=lambda: now,
    )
    asyncio.run(scheduler.tick())

    # One submission per entry, none doubled for the scenario-count one.
    assert sorted(cfg["corridor_id"] for cfg, _path in calls) == ["scn", "tmc_c"]
    by_id = {cfg["corridor_id"]: path for cfg, path in calls}
    # The scenario-count entry ignores the buffered window (its counts are not TMC counts);
    # the snapshot entry still gets its aggregate.
    scn = json.loads(by_id["scn"].read_text(encoding="utf-8"))
    tmc = json.loads(by_id["tmc_c"].read_text(encoding="utf-8"))
    assert [item["intersection_id"] for item in scn["intersections"]] == ["placeholder"]
    assert [item["intersection_id"] for item in tmc["intersections"]] == ["i1"]


def test_scheduler_scenario_counts_entry_ignores_an_unusable_window(tmp_path: Path) -> None:
    now = _epoch("2026-07-01T08:30:00")
    buffer = InMemorySnapshotBuffer(clock=lambda: now)
    # Two snapshots of different corridors: aggregating them raises.
    for corridor in ("scn", "tmc_c"):
        buffer.append(corridor, _snapshot("a", "2026-07-01T08:00:00"))
        buffer.append(corridor, _snapshot("b", "2026-07-01T08:10:00"))
    calls: list[str] = []

    scheduler = CityScheduler(
        buffer,
        lambda cfg, path: calls.append(cfg["corridor_id"]) or "run_x",
        {"tmc_c": _tmc_corridor_cfg("tmc_c"), "scn": _scenario_corridor_cfg("scn")},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=7200,
        clock=lambda: now,
    )
    asyncio.run(scheduler.tick())

    # The snapshot entry fails (contained, logged); the scenario-count entry never looks.
    assert calls == ["scn"]


def test_scheduler_scenario_counts_entry_follows_the_cadence(tmp_path: Path) -> None:
    now = _epoch("2026-07-01T08:30:00")
    cadence_s = 0.05
    stamps: list[float] = []

    async def main() -> float:
        stop = asyncio.Event()

        def submit(cfg: dict, path: Path):
            stamps.append(time.monotonic())
            if len(stamps) == 2:
                stop.set()  # end the loop after the second tick
            return "run_x"

        scheduler = CityScheduler(
            InMemorySnapshotBuffer(clock=lambda: now),
            submit,
            {"scn": _scenario_corridor_cfg("scn")},
            output_root=tmp_path,
            cadence_s=cadence_s,
            window_s=3600,
            clock=lambda: now,
        )
        started = time.monotonic()
        await asyncio.wait_for(scheduler.run(stop), timeout=2)
        return started

    started = asyncio.run(main())

    # One submission per tick: none before the first cadence wait, and the next one a
    # full cadence later (it does not fire continuously just because no window gates it).
    assert len(stamps) == 2
    assert stamps[0] - started >= cadence_s * 0.9
    assert stamps[1] - stamps[0] >= cadence_s * 0.9


def test_scheduler_scenario_placeholder_matches_the_corridor_id_path(tmp_path: Path) -> None:
    # A scheduled run and a corridor_id request hand the worker the same placeholder file.
    from terasim_service.city.optimize_api import (
        OptimizationRequest,
        _resolve_corridor_request,
    )

    now = _epoch("2026-07-01T08:30:00")
    paths: list[Path] = []
    scheduler = CityScheduler(
        InMemorySnapshotBuffer(clock=lambda: now),
        lambda cfg, path: paths.append(path) or "run_x",
        {"scn": _scenario_corridor_cfg("scn")},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=3600,
        clock=lambda: now,
    )
    asyncio.run(scheduler.tick())
    resolved = _resolve_corridor_request(
        OptimizationRequest(corridor_id="scn", counts_source="scenario_routes"),
        tmp_path / "inputs",
        None,
        None,
        3600,
    )

    def shape(path) -> dict:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        payload.pop("timestamp_start")  # both are "now": the value differs, the field does not
        return payload

    assert shape(paths[0]) == shape(resolved.tmc_json)


# --- rejected submissions: the log names the real status and reason -------------------


def test_scheduler_logs_the_status_and_detail_of_a_rejected_submission(
    tmp_path: Path, caplog
) -> None:
    from terasim_service.city.scheduler import SubmissionRejected

    now = _epoch("2026-07-01T08:30:00")

    def submit(cfg, path):
        raise SubmissionRejected(422, "n_workers=4 exceeds the cap (TERASIM_CITY_MAX_CONCURRENCY=3)")

    scheduler = CityScheduler(
        InMemorySnapshotBuffer(clock=lambda: now),
        submit,
        {"scn": _scenario_corridor_cfg("scn")},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=3600,
        clock=lambda: now,
    )
    with caplog.at_level(logging.INFO, logger="terasim_service.city.scheduler"):
        asyncio.run(scheduler.tick())

    messages = [record.getMessage() for record in caplog.records]
    assert (
        "corridor scn: submission rejected (HTTP 422): n_workers=4 exceeds the cap "
        "(TERASIM_CITY_MAX_CONCURRENCY=3); retrying next tick"
    ) in messages
    assert not any("rejected (capacity)" in message for message in messages)
    rejected = [r for r in caplog.records if "submission rejected" in r.getMessage()]
    assert [r.levelno for r in rejected] == [logging.ERROR]  # permanent: needs a config fix


def _production_submit(store, tmp_path: Path, recorded: list):
    from terasim_service.city.optimize_api import make_scheduled_submit

    return make_scheduled_submit(
        store,
        lambda run_id, request, output_dir: None,  # holds its slots, like a running worker
        output_root=tmp_path / "out",
        on_submitted=lambda corridor_id, run_id: recorded.append((corridor_id, run_id)),
    )


def test_scheduler_production_submit_logs_422_for_n_workers_above_the_cap(
    tmp_path: Path, caplog
) -> None:
    from terasim_service.city.job_store import InMemoryCityJobStore

    now = _epoch("2026-07-01T08:30:00")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    recorded: list = []
    scheduler = CityScheduler(
        InMemorySnapshotBuffer(clock=lambda: now),
        _production_submit(store, tmp_path, recorded),
        {"scn": {**_scenario_corridor_cfg("scn"), "n_workers": 4}},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=3600,
        clock=lambda: now,
    )
    with caplog.at_level(logging.INFO, logger="terasim_service.city.scheduler"):
        asyncio.run(scheduler.tick())

    rejected = [r.getMessage() for r in caplog.records if "submission rejected" in r.getMessage()]
    assert len(rejected) == 1
    assert rejected[0].startswith("corridor scn: submission rejected (HTTP 422): ")
    assert "n_workers=4" in rejected[0] and "TERASIM_CITY_MAX_CONCURRENCY=3" in rejected[0]
    assert recorded == []


def test_scheduler_production_submit_logs_429_when_the_slots_are_busy(
    tmp_path: Path, caplog
) -> None:
    from terasim_service.city.job_store import InMemoryCityJobStore

    now = _epoch("2026-07-01T08:30:00")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    assert store.try_acquire_slots("already-running", 2)
    recorded: list = []
    scheduler = CityScheduler(
        InMemorySnapshotBuffer(clock=lambda: now),
        _production_submit(store, tmp_path, recorded),
        {"scn": _scenario_corridor_cfg("scn")},  # n_workers 2: fits the cap, not the free slots
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=3600,
        clock=lambda: now,
    )
    with caplog.at_level(logging.INFO, logger="terasim_service.city.scheduler"):
        asyncio.run(scheduler.tick())
    rejected = [r for r in caplog.records if "submission rejected" in r.getMessage()]
    assert len(rejected) == 1
    assert rejected[0].getMessage().startswith("corridor scn: submission rejected (HTTP 429): ")
    assert rejected[0].levelno == logging.WARNING  # transient: the next tick retries
    assert recorded == []

    store.release_slot("already-running")
    asyncio.run(scheduler.tick())
    assert [corridor for corridor, _run in recorded] == ["scn"]


# --- scenario-count entries: skip a tick on unchanged inputs -------------------------


def _own_scenario(tmp_path: Path) -> Path:
    """A private copy of the demo scenario (its sumocfg and every file it names)."""
    scenario = tmp_path / "scenario"
    scenario.mkdir()
    for name in ("demo.sumocfg", "demo.net.xml", "demo.rou.xml", "demo.tls.xml"):
        (scenario / name).write_bytes((_DEMO_DIR / name).read_bytes())
    return scenario / "demo.sumocfg"


class _Runs:
    """A submit spy that hands out run ids, plus the run-status lookup the scheduler polls."""

    def __init__(self) -> None:
        self.submitted: list[dict] = []
        self.status: dict[str, str] = {}

    def submit(self, cfg: dict, path: Path) -> str:
        run_id = f"run_{len(self.submitted)}"
        self.submitted.append(cfg)
        self.status[run_id] = "submitted"
        return run_id

    def complete_all(self) -> None:
        for run_id in self.status:
            self.status[run_id] = "completed"


def _fingerprint_scheduler(tmp_path: Path, runs: _Runs, cfg: dict) -> CityScheduler:
    now = _epoch("2026-07-01T08:30:00")
    return CityScheduler(
        InMemorySnapshotBuffer(clock=lambda: now),
        runs.submit,
        {cfg["corridor_id"]: cfg},
        output_root=tmp_path / "out",
        cadence_s=0.01,
        window_s=3600,
        clock=lambda: now,
        run_status=runs.status.get,
    )


def test_scenario_entry_skips_a_tick_when_its_inputs_are_unchanged_since_a_completed_run(
    tmp_path: Path, caplog
) -> None:
    runs = _Runs()
    cfg = {**_scenario_corridor_cfg("scn"), "base_sumocfg": str(_own_scenario(tmp_path))}
    scheduler = _fingerprint_scheduler(tmp_path, runs, cfg)

    asyncio.run(scheduler.tick())
    assert len(runs.submitted) == 1

    runs.complete_all()
    with caplog.at_level(logging.INFO, logger="terasim_service.city.scheduler"):
        asyncio.run(scheduler.tick())
    assert len(runs.submitted) == 1  # skipped
    skipped = [r.getMessage() for r in caplog.records if "unchanged" in r.getMessage()]
    assert skipped == [
        "corridor scn: inputs unchanged since run run_0 completed; skipping tick "
        "(set rerun_unchanged: true to run on every tick)"
    ]


@pytest.mark.parametrize("status", ["submitted", "running"])
def test_scenario_entry_skips_while_a_run_on_the_same_inputs_is_in_progress(
    tmp_path: Path, caplog, status
) -> None:
    """A run that outlasts the cadence never gets a duplicate submission."""
    runs = _Runs()
    cfg = {**_scenario_corridor_cfg("scn"), "base_sumocfg": str(_own_scenario(tmp_path))}
    scheduler = _fingerprint_scheduler(tmp_path, runs, cfg)

    asyncio.run(scheduler.tick())
    runs.status["run_0"] = status
    with caplog.at_level(logging.INFO, logger="terasim_service.city.scheduler"):
        asyncio.run(scheduler.tick())
        asyncio.run(scheduler.tick())

    assert len(runs.submitted) == 1
    assert [r.getMessage() for r in caplog.records if "in progress" in r.getMessage()] == [
        "corridor scn: run run_0 on these inputs is still in progress; skipping tick"
    ] * 2
    # ... and once it completed, the inputs still being unchanged, it is not redone either.
    runs.complete_all()
    asyncio.run(scheduler.tick())
    assert len(runs.submitted) == 1


@pytest.mark.parametrize("status", ["failed", "lost", None])
def test_scenario_entry_reruns_after_a_run_that_did_not_finish(tmp_path: Path, status) -> None:
    runs = _Runs()
    cfg = {**_scenario_corridor_cfg("scn"), "base_sumocfg": str(_own_scenario(tmp_path))}
    scheduler = _fingerprint_scheduler(tmp_path, runs, cfg)

    asyncio.run(scheduler.tick())
    runs.status["run_0"] = status
    asyncio.run(scheduler.tick())

    assert len(runs.submitted) == 2


def test_scenario_entry_with_changed_inputs_runs_while_the_old_run_is_in_progress(
    tmp_path: Path,
) -> None:
    runs = _Runs()
    cfg = {**_scenario_corridor_cfg("scn"), "base_sumocfg": str(_own_scenario(tmp_path))}
    scheduler = _fingerprint_scheduler(tmp_path, runs, cfg)

    asyncio.run(scheduler.tick())
    runs.status["run_0"] = "running"
    cfg["max_trials"] = 12
    asyncio.run(scheduler.tick())
    assert len(runs.submitted) == 2
    # Back to the first inputs: their run is still in progress, so nothing new.
    cfg["max_trials"] = 6
    asyncio.run(scheduler.tick())
    assert len(runs.submitted) == 2


def test_run_status_reports_a_run_whose_worker_is_gone_as_lost() -> None:
    """A worker killed before it could record failure leaves its job "running" forever;
    its lease lapses, and the scheduler's lookup reports it as lost (so it reruns)."""
    from terasim_service.city.job_store import InMemoryCityJobStore
    from terasim_service.city.optimize_api import make_run_status

    now = [0.0]
    store = InMemoryCityJobStore(max_concurrency=4, lease_ttl=100, clock=lambda: now[0])
    run_status = make_run_status(store)
    store.create("r1")
    assert store.try_acquire_slots("r1", 2)
    assert run_status("r1") == "submitted"
    store.set_running("r1")
    assert run_status("r1") == "running"
    now[0] = 99.0
    assert run_status("r1") == "running"  # the lease still holds
    now[0] = 101.0
    assert run_status("r1") == "lost"  # nobody renewed it
    store.set_completed("r1", "/unused", {"plan_id": "p"})
    assert run_status("r1") == "completed"  # a finished run needs no lease
    assert run_status("unknown") is None


def test_scenario_entry_runs_again_when_a_route_file_changes(tmp_path: Path) -> None:
    runs = _Runs()
    sumocfg = _own_scenario(tmp_path)
    cfg = {**_scenario_corridor_cfg("scn"), "base_sumocfg": str(sumocfg)}
    scheduler = _fingerprint_scheduler(tmp_path, runs, cfg)

    asyncio.run(scheduler.tick())
    runs.complete_all()
    asyncio.run(scheduler.tick())
    assert len(runs.submitted) == 1  # unchanged: skipped

    routes = sumocfg.parent / "demo.rou.xml"
    routes.write_text(routes.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    asyncio.run(scheduler.tick())
    assert len(runs.submitted) == 2  # the route file changed: runs again


def test_scenario_entry_runs_again_when_its_config_changes_or_the_run_failed(
    tmp_path: Path,
) -> None:
    runs = _Runs()
    cfg = {**_scenario_corridor_cfg("scn"), "base_sumocfg": str(_own_scenario(tmp_path))}
    scheduler = _fingerprint_scheduler(tmp_path, runs, cfg)

    asyncio.run(scheduler.tick())
    runs.status["run_0"] = "failed"
    asyncio.run(scheduler.tick())
    assert len(runs.submitted) == 2  # a failed run is retried

    runs.complete_all()
    cfg["max_trials"] = 12  # the entry changed
    asyncio.run(scheduler.tick())
    assert len(runs.submitted) == 3


def test_rerun_unchanged_runs_on_every_tick(tmp_path: Path) -> None:
    runs = _Runs()
    cfg = {
        **_scenario_corridor_cfg("scn"),
        "base_sumocfg": str(_own_scenario(tmp_path)),
        "rerun_unchanged": True,
    }
    scheduler = _fingerprint_scheduler(tmp_path, runs, cfg)

    for _ in range(3):
        asyncio.run(scheduler.tick())
        runs.complete_all()
    assert len(runs.submitted) == 3


def test_snapshot_entries_ignore_the_fingerprint(tmp_path: Path) -> None:
    now = _epoch("2026-07-01T08:30:00")
    buffer = InMemorySnapshotBuffer(clock=lambda: now)
    buffer.append("tmc_c", _snapshot("tmc_c"))
    runs = _Runs()
    scheduler = CityScheduler(
        buffer,
        runs.submit,
        {"tmc_c": _tmc_corridor_cfg("tmc_c")},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=7200,
        clock=lambda: now,
        run_status=runs.status.get,
    )
    for _ in range(2):
        asyncio.run(scheduler.tick())
        runs.complete_all()
    assert len(runs.submitted) == 2  # same window, completed run: still submits


def test_rerun_unchanged_is_never_forwarded_into_the_request(tmp_path: Path) -> None:
    from terasim_service.city.job_store import InMemoryCityJobStore
    from terasim_service.city.optimize_api import make_scheduled_submit

    launched: list[dict] = []
    submit = make_scheduled_submit(
        InMemoryCityJobStore(max_concurrency=3, lease_ttl=100),
        lambda run_id, request, output_dir: launched.append(request),
        output_root=tmp_path / "out",
    )
    cfg = {**_scenario_corridor_cfg("scn"), "rerun_unchanged": True}
    scheduler = CityScheduler(
        InMemorySnapshotBuffer(),
        submit,
        {"scn": cfg},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=3600,
    )
    asyncio.run(scheduler.tick())
    assert len(launched) == 1 and "rerun_unchanged" not in launched[0]


def test_load_scheduled_config_validates_rerun_unchanged(tmp_path: Path) -> None:
    config = tmp_path / "scheduled.yaml"
    config.write_text(
        "corridors:\n"
        "  scn:\n"
        f"    base_sumocfg: {_DEMO_DIR / 'demo.sumocfg'}\n"
        "    corridor: {tls_ids: [J1]}\n"
        "    rerun_unchanged: true\n",
        encoding="utf-8",
    )
    assert load_scheduled_config(config)["scn"]["rerun_unchanged"] is True
    config.write_text(config.read_text(encoding="utf-8").replace("true", "'yes'"), encoding="utf-8")
    with pytest.raises(ValueError, match="rerun_unchanged"):
        load_scheduled_config(config)


def test_scheduler_submissions_are_recorded_with_origin_scheduled(tmp_path: Path) -> None:
    from terasim_service.city.job_store import InMemoryCityJobStore
    from terasim_service.city.optimize_api import make_scheduled_submit

    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    recorded: list = []
    submit = make_scheduled_submit(
        store,
        lambda run_id, request, output_dir: None,
        output_root=tmp_path / "out",
        on_submitted=lambda corridor_id, run_id: recorded.append((corridor_id, run_id)),
    )
    scheduler = CityScheduler(
        InMemorySnapshotBuffer(),
        submit,
        {"scn": _scenario_corridor_cfg("scn")},
        output_root=tmp_path,
        cadence_s=0.01,
        window_s=3600,
    )
    asyncio.run(scheduler.tick())

    ((corridor_id, run_id),) = recorded
    record = store.get(run_id)
    assert (record.origin, record.corridor_id) == ("scheduled", "scn")
    assert record.intersection_ids == list(_scenario_corridor_cfg("scn")["corridor"]["tls_ids"])
