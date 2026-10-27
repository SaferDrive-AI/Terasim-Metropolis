"""TERASIM_CITY_OSM_DOWNLOAD=0: maps ship as files, OpenStreetMap is never downloaded.

Every path that could fetch OpenStreetMap data selects stored maps only: the
synchronous optimize endpoint and the asynchronous submit (also the scheduler's) answer
422 naming the setting, before capacity is reserved or a run is recorded, when no
stored map covers the request; the worker records the run as failed with the same
reason. A network guard fails the test on any outbound connection attempt (socket,
requests, urllib), so "no download" is proven, not assumed. Stored (shipped or
registered) maps keep working, and the default keeps today's downloads.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from terasim_city.flow.descriptors import TMCData
from terasim_city.maps import MapStore
from terasim_service.city.ingest_api import InMemorySnapshotBuffer
from terasim_service.city.job_store import FAILED, InMemoryCityJobStore
from terasim_service.city.map_acquisition import (
    DisabledOsmDownloader,
    MapAcquirer,
    OsmBoundingBox,
    OsmDownloadDisabled,
    OverpassOsmDownloader,
    acquire_map_for_tmc,
    osm_download_enabled,
)
from terasim_service.city.optimize_api import create_v1_city_router, make_scheduled_submit
from terasim_service.city.scheduler import SubmissionRejected
from terasim_service.city.worker_optimize import run_city_optimization_job

_NOW = datetime.fromisoformat("2026-07-01T09:00:00+00:00").timestamp()
_CORRIDOR = "park_ave"
_OAK = (37.335, -121.90339)
_CEDAR = (37.335, -121.89661)
#: A shipped map around both intersections (lon/lat: west, south, east, north).
_COVERING = "-121.95,37.30,-121.88,37.35"
#: A shipped map somewhere else.
_ELSEWHERE = "-122.50,37.70,-122.40,37.80"
_SETTING = "TERASIM_CITY_OSM_DOWNLOAD=0"
_NO_MAP = "no shipped or registered map covers this area"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list:
    """Fail on any outbound connection; the list records attempts (asserted empty)."""
    attempts: list = []

    def refuse(*args, **kwargs):
        attempts.append(args[:2])
        raise AssertionError(f"outbound network request attempted: {args[:2]!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr("requests.post", refuse)
    monkeypatch.setattr("requests.get", refuse)
    monkeypatch.setattr("requests.sessions.Session.request", refuse)
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    return attempts


@pytest.fixture
def downloads_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """TERASIM_CITY_OSM_DOWNLOAD=0 and an empty map store as TERASIM_CITY_DATA_ROOT."""
    monkeypatch.setenv("TERASIM_CITY_OSM_DOWNLOAD", "0")
    data_root = tmp_path / "store"
    monkeypatch.setenv("TERASIM_CITY_DATA_ROOT", str(data_root))
    return data_root


def _ship_map(data_root: Path, tmp_path: Path, boundary: str, name: str = "shipped") -> Path:
    """Register a prebuilt map (as an operator does with POST /v1/city/maps); its base_sumocfg."""
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "plain.net.xml").write_text(
        f'<net><location origBoundary="{boundary}" '
        'projParameter="+proj=utm +zone=10 +units=m"/></net>',
        encoding="utf-8",
    )
    (root / "empty.rou.xml").write_text("<routes/>\n", encoding="utf-8")
    config = root / "plain.sumocfg"
    config.write_text(
        '<configuration><input><net-file value="plain.net.xml"/>'
        '<route-files value="empty.rou.xml"/></input></configuration>\n',
        encoding="utf-8",
    )
    stored = MapStore(data_root).ingest(config)
    return Path(stored.package.base_sumocfg).resolve()


def _snapshot() -> dict:
    turns = ("Right", "Straight", "Left")
    counts = {d: {t: 1 for t in turns} for d in ("South", "West", "North", "East")}
    return {
        "corridor_id": _CORRIDOR,
        "observation_interval": "10m",
        "timestamp_start": "2026-07-01T08:50:00Z",
        "intersections": [
            {"intersection_id": "oak", "name": "Park Ave & Oak St",
             "lat": _OAK[0], "lon": _OAK[1], "counts": counts},
            {"intersection_id": "cedar", "name": "Park Ave & Cedar St",
             "lat": _CEDAR[0], "lon": _CEDAR[1], "counts": counts},
        ],
    }


def _client(tmp_path: Path):
    """The v1 router as production builds it: no injected map acquirer."""
    jobs = InMemoryCityJobStore(max_concurrency=4, lease_ttl=100)
    buffer = InMemorySnapshotBuffer(clock=lambda: _NOW)
    buffer.append(_CORRIDOR, _snapshot())
    seen: dict = {"launched": []}

    def optimize(config, *, progress_callback):
        seen["config"] = config
        config.output_dir.mkdir(parents=True, exist_ok=True)
        (config.output_dir / "nema_plan.json").write_text(
            json.dumps({"schema": "terasim.nema_timing.v1", "intersections": []}), encoding="utf-8"
        )
        return {
            "baseline": {"plan_id": "baseline", "score": 1.0,
                         "moe": {"control_delay_s_per_veh": 30.0}},
            "best": {"plan_id": "bo_0001", "score": 0.9, "moe": {"control_delay_s_per_veh": 27.0}},
        }

    app = FastAPI()
    app.include_router(create_v1_city_router(
        jobs, lambda run_id, request, out: seen["launched"].append(request),
        output_root=tmp_path / "out", buffer=buffer, direct_optimizer=optimize,
    ))
    return TestClient(app), jobs, seen


_DIRECT = {
    "type": "intersection",
    "corridor_id": _CORRIDOR,
    "intersection": {"intersection_id": "oak", "lat": _OAK[0], "lon": _OAK[1]},
}


# ---------------------------------------------------------------------- the switch


@pytest.mark.parametrize("value, enabled", [
    (None, True), ("1", True), ("true", True), ("", True),
    ("0", False), ("false", False), ("No", False), (" off ", False),
])
def test_the_setting(monkeypatch, value, enabled) -> None:
    if value is None:
        monkeypatch.delenv("TERASIM_CITY_OSM_DOWNLOAD", raising=False)
    else:
        monkeypatch.setenv("TERASIM_CITY_OSM_DOWNLOAD", value)
    assert osm_download_enabled() is enabled


def test_the_network_guard_would_catch_a_download(tmp_path, no_network) -> None:
    """The guard is effective: the real Overpass downloader trips it."""
    with pytest.raises(AssertionError, match="outbound network request"):
        OverpassOsmDownloader().download(
            OsmBoundingBox(37.3, -121.95, 37.35, -121.88), tmp_path / "x.osm"
        )
    assert len(no_network) == 1


def test_the_default_still_downloads_from_overpass(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TERASIM_CITY_OSM_DOWNLOAD", raising=False)
    monkeypatch.setenv("TERASIM_CITY_DATA_ROOT", str(tmp_path / "store"))
    assert isinstance(MapAcquirer.from_environment().downloader, OverpassOsmDownloader)


def test_disabled_acquirer_never_goes_online(downloads_off, tmp_path, no_network) -> None:
    acquirer = MapAcquirer.from_environment()
    assert isinstance(acquirer.downloader, DisabledOsmDownloader)
    tmc = tmp_path / "tmc.json"
    tmc.write_text(json.dumps(_snapshot()), encoding="utf-8")

    with pytest.raises(OsmDownloadDisabled) as excinfo:
        acquirer(tmc)

    message = str(excinfo.value)
    assert _NO_MAP in message and _SETTING in message
    assert "lat 37.33" in message and "lon -121.9" in message  # the area it needed
    assert no_network == []
    assert MapStore(downloads_off).list_ids() == []  # nothing was cached either


def test_disabled_downloader_refuses_without_writing(tmp_path, no_network) -> None:
    destination = tmp_path / "download.osm"
    with pytest.raises(OsmDownloadDisabled, match=_SETTING):
        DisabledOsmDownloader().download(OsmBoundingBox(37.3, -121.95, 37.35, -121.88), destination)
    assert not destination.exists() and no_network == []


def test_shipped_maps_keep_working(downloads_off, tmp_path, no_network) -> None:
    _ship_map(downloads_off, tmp_path, _ELSEWHERE, name="elsewhere")
    covering = _ship_map(downloads_off, tmp_path, _COVERING)
    tmc = tmp_path / "tmc.json"
    tmc.write_text(json.dumps(_snapshot()), encoding="utf-8")

    assert Path(MapAcquirer.from_environment()(tmc)).resolve() == covering

    class FailIfCalled:
        def download(self, bbox, destination):
            raise AssertionError("a covered area must not ask the downloader")

    _map_id, package = acquire_map_for_tmc(
        TMCData.from_json(tmc), MapStore(downloads_off), FailIfCalled()
    )
    assert Path(package.base_sumocfg).resolve() == covering
    assert no_network == []


# ---------------------------------------------------------------------- synchronous endpoint


def test_direct_request_without_a_covering_map_is_422_before_anything_starts(
    downloads_off, tmp_path, no_network
) -> None:
    _ship_map(downloads_off, tmp_path, _ELSEWHERE, name="elsewhere")
    client, jobs, seen = _client(tmp_path)

    response = client.post("/v1/city/optimize", json=_DIRECT)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert _NO_MAP in detail and _SETTING in detail
    assert "config" not in seen                      # the optimizer never ran
    assert jobs.list_runs() == []                    # no run recorded
    assert jobs.try_acquire_slots("probe", 4)        # no slot held
    out = tmp_path / "out"
    assert not out.exists() or list(out.iterdir()) == []
    assert no_network == []


def test_direct_request_on_a_shipped_map_runs(downloads_off, tmp_path, no_network) -> None:
    covering = _ship_map(downloads_off, tmp_path, _COVERING)
    client, jobs, seen = _client(tmp_path)

    response = client.post("/v1/city/optimize", json=_DIRECT)

    assert response.status_code == 200, response.text
    assert Path(seen["config"].base_sumocfg).resolve() == covering
    (record,) = jobs.list_runs()
    assert record.status == "completed"
    assert no_network == []


def test_an_injected_acquirer_without_downloads_is_a_422_too(tmp_path, no_network) -> None:
    """The backstop at the original acquisition point (downloads allowed by the setting,
    but the injected acquirer cannot download): still a 422 with the reason, no network."""
    jobs = InMemoryCityJobStore(max_concurrency=4, lease_ttl=100)
    buffer = InMemorySnapshotBuffer(clock=lambda: _NOW)
    buffer.append(_CORRIDOR, _snapshot())
    app = FastAPI()
    app.include_router(create_v1_city_router(
        jobs, lambda *_a: None, output_root=tmp_path / "out", buffer=buffer,
        direct_optimizer=lambda *_a, **_k: pytest.fail("the optimizer must not run"),
        map_acquirer=MapAcquirer(MapStore(tmp_path / "store"), DisabledOsmDownloader()),
    ))
    response = TestClient(app).post("/v1/city/optimize", json=_DIRECT)
    assert response.status_code == 422 and _SETTING in response.json()["detail"]
    (record,) = jobs.list_runs()
    assert record.status == FAILED and _SETTING in record.error
    assert no_network == []


# ------------------------------------------------------------- async submit, scheduler, worker


def test_async_submit_without_a_covering_map_is_422_and_launches_nothing(
    downloads_off, tmp_path, no_network
) -> None:
    client, jobs, seen = _client(tmp_path)

    response = client.post("/v1/city/optimizations", json={"corridor_id": _CORRIDOR})

    assert response.status_code == 422
    assert _NO_MAP in response.json()["detail"] and _SETTING in response.json()["detail"]
    assert seen["launched"] == [] and jobs.list_runs() == []
    assert jobs.try_acquire_slots("probe", 4)
    assert no_network == []


def test_async_submit_on_a_shipped_map_launches(downloads_off, tmp_path, no_network) -> None:
    _ship_map(downloads_off, tmp_path, _COVERING)
    client, jobs, seen = _client(tmp_path)

    response = client.post("/v1/city/optimizations", json={"corridor_id": _CORRIDOR})

    assert response.status_code == 202, response.text
    (forwarded,) = seen["launched"]
    assert forwarded.get("base_sumocfg") is None  # the worker selects the stored map itself
    assert no_network == []


def test_the_scheduler_hears_the_reason(downloads_off, tmp_path, no_network) -> None:
    jobs = InMemoryCityJobStore(max_concurrency=4, lease_ttl=100)
    launched: list = []
    submit = make_scheduled_submit(
        jobs, lambda *a: launched.append(a), output_root=tmp_path / "out"
    )
    aggregated = tmp_path / "aggregated_tmc.json"
    aggregated.write_text(json.dumps(_snapshot()), encoding="utf-8")

    with pytest.raises(SubmissionRejected) as excinfo:
        submit({"corridor_id": _CORRIDOR}, aggregated)

    assert excinfo.value.status_code == 422 and _SETTING in excinfo.value.detail
    assert launched == [] and no_network == []


def test_the_worker_records_the_reason_and_stays_offline(
    downloads_off, tmp_path, no_network
) -> None:
    jobs = InMemoryCityJobStore(max_concurrency=4, lease_ttl=100)
    jobs.create("r1")
    assert jobs.try_acquire_slots("r1", 2)
    tmc = tmp_path / "tmc.json"
    tmc.write_text(json.dumps(_snapshot()), encoding="utf-8")

    run_city_optimization_job(
        jobs, {"tmc_json": str(tmc)}, "r1", tmp_path / "out" / "r1",
        opt_fn=lambda *_a, **_k: pytest.fail("the optimizer must not run"),
    )

    record = jobs.get("r1")
    assert record.status == FAILED
    assert _NO_MAP in record.error and _SETTING in record.error
    assert jobs.try_acquire_slots("probe", 4)  # slots released
    assert no_network == []
