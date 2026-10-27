"""HTTP surface and tick logic of the TMC microservice (no network, no timers)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from terasim_tmc_service.app import TmcSettings, create_app, run_tick
from terasim_tmc_service.registry import IntersectionRegistry

from .conftest import EXPECTED_COUNTS, I1, WINDOW_END, WINDOW_START

_NOW = datetime(2026, 9, 4, 18, 33, 20, tzinfo=timezone.utc)
_SENSORS = ["i1-cam1", "i1-cam2", "i1-cam3", "i1-cam4"]


class FakeSource:
    def __init__(self, behaviors_by_sensor, *, fail=False):
        self.behaviors_by_sensor = behaviors_by_sensor
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    def fetch(self, sensor_id, timestamp_start, timestamp_end):
        self.calls.append((sensor_id, timestamp_start, timestamp_end))
        if self.fail:
            raise RuntimeError("vss unreachable")
        return list(self.behaviors_by_sensor.get(sensor_id, []))


class FakeInjector:
    def __init__(self):
        self.payloads: list[dict] = []

    def inject(self, payload):
        self.payloads.append(payload)
        return {"corridor_id": payload["corridor_id"], "buffered": len(self.payloads)}


@pytest.fixture
def harness(spec, sample_behaviors):
    registry = IntersectionRegistry(spec)
    source = FakeSource(sample_behaviors)
    injector = FakeInjector()
    app = create_app(spec, registry, source, injector, autostart=False, clock=lambda: _NOW,
                     tick_on_register=False)
    with TestClient(app) as client:
        yield client, registry, source, injector


def test_run_tick_idle_without_registrations(spec, sample_behaviors):
    result = run_tick(spec, IntersectionRegistry(spec), FakeSource(sample_behaviors),
                      FakeInjector(), now=_NOW, settle_lag_s=180.0, last_windows={})
    assert result == {"status": "idle", "reason": "no registered intersections"}


def test_run_tick_injects_then_skips_same_window(spec, sample_behaviors):
    registry = IntersectionRegistry(spec)
    registry.register(**I1)
    source, injector, last = FakeSource(sample_behaviors), FakeInjector(), {}

    first = run_tick(spec, registry, source, injector, now=_NOW, settle_lag_s=180.0,
                     last_windows=last)
    assert first["status"] == "injected"
    assert (first["timestamp_start"], first["timestamp_end"]) == (WINDOW_START, WINDOW_END)
    assert first["buffered"] == 1
    assert first["behaviors"] == {"i1-cam1": 8, "i1-cam2": 2, "i1-cam3": 2, "i1-cam4": 3}
    assert first["counts"]["i1"] == EXPECTED_COUNTS
    assert [call[0] for call in source.calls] == _SENSORS
    assert injector.payloads[0]["intersections"][0]["name"] == I1["name"]

    second = run_tick(spec, registry, source, injector, now=_NOW, settle_lag_s=180.0,
                      last_windows=last)
    assert second["status"] == "skipped" and second["timestamp_start"] == WINDOW_START
    assert len(injector.payloads) == 1


def test_health_and_listing(harness):
    client, registry, _, _ = harness
    health = client.get("/v1/tmc/health").json()
    assert health["status"] == "ok" and health["registered"] == 0
    assert health["corridor_id"] == "demo_arterial" and health["last_tick"] is None

    listing = client.get("/v1/tmc/intersections").json()
    assert listing["corridor_id"] == "demo_arterial"
    assert listing["available"] == [
        {"intersection_id": iid, "lat": 37.335, "lon": lon,
         "sensors": [f"{iid}-cam{n}" for n in range(1, 5)], "registered": False}
        for iid, lon in (("i1", -121.90339), ("i2", -121.9), ("i3", -121.89661))
    ]
    assert listing["registered"] == []


def test_register_endpoint_status_codes(harness):
    client, registry, _, _ = harness
    created = client.post("/v1/tmc/intersections", json=I1)
    assert created.status_code == 201
    assert created.json()["sensors"] == _SENSORS
    available = client.get("/v1/tmc/intersections").json()["available"]
    assert [item["registered"] for item in available] == [True, False, False]

    unknown = client.post("/v1/tmc/intersections", json={**I1, "intersection_id": "oak"})
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == (
        "unknown intersection_id 'oak'; known ids: ['i1', 'i2', 'i3']"
    )

    far = client.post("/v1/tmc/intersections", json={**I1, "lat": 37.35})
    assert far.status_code == 422

    mismatch = client.post("/v1/tmc/intersections", json={**I1, "corridor_id": "other"})
    assert mismatch.status_code == 422


def test_run_once_endpoint(harness):
    client, _, _, injector = harness
    assert client.post("/v1/tmc/run-once").json()["status"] == "idle"
    client.post("/v1/tmc/intersections", json=I1)
    result = client.post("/v1/tmc/run-once").json()
    assert result["status"] == "injected" and result["counts"]["i1"] == EXPECTED_COUNTS
    assert client.get("/v1/tmc/health").json()["last_tick"]["status"] == "injected"
    assert len(injector.payloads) == 1


def test_run_once_reports_upstream_failure(spec, sample_behaviors):
    registry = IntersectionRegistry(spec)
    app = create_app(spec, registry, FakeSource(sample_behaviors, fail=True), FakeInjector(),
                     autostart=False, clock=lambda: _NOW, tick_on_register=False)
    with TestClient(app) as client:
        client.post("/v1/tmc/intersections", json=I1)
        reply = client.post("/v1/tmc/run-once")
    assert reply.status_code == 502 and "vss unreachable" in reply.json()["detail"]


def test_run_once_reports_validation_failure_and_records_it(spec, sample_behaviors):
    class BrokenInjector:
        def inject(self, payload):
            raise ValueError("1 validation error for TrafficSnapshotInput")

    registry = IntersectionRegistry(spec)
    app = create_app(spec, registry, FakeSource(sample_behaviors), BrokenInjector(),
                     autostart=False, clock=lambda: _NOW, tick_on_register=False)
    with TestClient(app) as client:
        client.post("/v1/tmc/intersections", json=I1)
        reply = client.post("/v1/tmc/run-once")
        assert reply.status_code == 500 and "validation" in reply.json()["detail"]
        last = client.get("/v1/tmc/health").json()["last_tick"]
        assert last["status"] == "failed" and "validation" in last["error"]


def test_run_once_upstream_failure_is_recorded_in_health(spec, sample_behaviors):
    registry = IntersectionRegistry(spec)
    app = create_app(spec, registry, FakeSource(sample_behaviors, fail=True), FakeInjector(),
                     autostart=False, clock=lambda: _NOW, tick_on_register=False)
    with TestClient(app) as client:
        client.post("/v1/tmc/intersections", json=I1)
        assert client.post("/v1/tmc/run-once").status_code == 502
        assert client.get("/v1/tmc/health").json()["last_tick"]["status"] == "failed"


def test_autostart_loop_runs_first_tick_and_stops_cleanly(spec, sample_behaviors):
    import time
    registry = IntersectionRegistry(spec)
    registry.register(**I1)
    injector = FakeInjector()
    app = create_app(spec, registry, FakeSource(sample_behaviors), injector, autostart=True,
                     clock=lambda: datetime.now(timezone.utc), tick_on_register=False)
    with TestClient(app) as client:
        deadline = time.monotonic() + 5.0
        last = None
        while time.monotonic() < deadline:
            last = client.get("/v1/tmc/health").json()["last_tick"]
            if last is not None:
                break
            time.sleep(0.05)
        assert last is not None and last["status"] == "injected"
        assert len(injector.payloads) == 1
    assert len(injector.payloads) == 1  # cancelled on shutdown, no further ticks


def test_last_windows_persist_across_app_instances(spec, sample_behaviors, tmp_path):
    windows_file = tmp_path / "w.json"
    injector = FakeInjector()
    app = create_app(spec, IntersectionRegistry(spec), FakeSource(sample_behaviors), injector,
                     autostart=False, clock=lambda: _NOW, windows_file=windows_file,
                     tick_on_register=False)
    with TestClient(app) as client:
        client.post("/v1/tmc/intersections", json=I1)
        assert client.post("/v1/tmc/run-once").json()["status"] == "injected"
    assert json.loads(windows_file.read_text(encoding="utf-8")) == {"demo_arterial": WINDOW_START}

    # A restart re-reads the watermark: the same window must not be injected twice.
    restarted = create_app(spec, IntersectionRegistry(spec), FakeSource(sample_behaviors), injector,
                           autostart=False, clock=lambda: _NOW, windows_file=windows_file,
                           tick_on_register=False)
    with TestClient(restarted) as client:
        client.post("/v1/tmc/intersections", json=I1)
        result = client.post("/v1/tmc/run-once").json()
    assert result["status"] == "skipped" and result["timestamp_start"] == WINDOW_START
    assert len(injector.payloads) == 1


def test_corrupt_windows_file_is_ignored(spec, sample_behaviors, tmp_path):
    windows_file = tmp_path / "w.json"
    windows_file.write_text("not json", encoding="utf-8")
    app = create_app(spec, IntersectionRegistry(spec), FakeSource(sample_behaviors),
                     FakeInjector(), autostart=False, clock=lambda: _NOW,
                     windows_file=windows_file, tick_on_register=False)
    with TestClient(app) as client:
        client.post("/v1/tmc/intersections", json=I1)
        assert client.post("/v1/tmc/run-once").json()["status"] == "injected"
    assert json.loads(windows_file.read_text(encoding="utf-8")) == {"demo_arterial": WINDOW_START}


def test_register_triggers_background_tick(spec, sample_behaviors):
    import time
    injector = FakeInjector()
    app = create_app(spec, IntersectionRegistry(spec), FakeSource(sample_behaviors), injector,
                     autostart=False, clock=lambda: _NOW, tick_on_register=True)
    with TestClient(app) as client:
        assert client.post("/v1/tmc/intersections", json=I1).status_code == 201
        deadline = time.monotonic() + 3.0
        last = None
        while time.monotonic() < deadline:
            last = client.get("/v1/tmc/health").json()["last_tick"]
            if last is not None:
                break
            time.sleep(0.05)
        assert last is not None and last["status"] == "injected"
        assert len(injector.payloads) == 1
        assert client.post("/v1/tmc/run-once").json()["status"] == "skipped"
    assert len(injector.payloads) == 1


def test_settings_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TMC_SPEC_FILE", str(tmp_path / "Metadata.json"))
    monkeypatch.setenv("VSS_BEHAVIOR_URL", "http://vss-host:8081")
    for name in ("TERASIM_CITY_URL", "TMC_REGISTRY_FILE", "TMC_SETTLE_LAG_S",
                 "TMC_AUTOSTART", "TMC_HOST", "TMC_PORT"):
        monkeypatch.delenv(name, raising=False)
    settings = TmcSettings.from_environment()
    assert settings.spec_file == tmp_path / "Metadata.json"
    assert settings.vss_behavior_url == "http://vss-host:8081"
    assert settings.terasim_city_url == "http://127.0.0.1:8000"
    assert settings.settle_lag_s == 180.0 and settings.autostart is True
    assert (settings.host, settings.port) == ("0.0.0.0", 8200)

    monkeypatch.delenv("VSS_BEHAVIOR_URL")
    with pytest.raises(ValueError, match="VSS_BEHAVIOR_URL"):
        TmcSettings.from_environment()
