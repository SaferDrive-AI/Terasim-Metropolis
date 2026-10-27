"""GET /v1/city/optimizations: every run of every origin, newest first.

Scheduled runs (the scheduler's submit callback), API runs (POST
/v1/city/optimizations) and direct runs (the synchronous POST /v1/city/optimize) all
appear, with their status, KPI summary (derived from the result like the synchronous
answer), NEMA export, report and simulated-metrics state.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terasim_service.city import run_report
from terasim_service.city import sim_metrics_job as metrics
from terasim_service.city.ingest_api import InMemorySnapshotBuffer
from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import create_v1_city_router, make_scheduled_submit
from terasim_service.city.worker_optimize import run_city_optimization_job

_BASE = "/v1/city/optimizations"
_NOW = datetime.fromisoformat("2026-10-06T16:54:10+00:00").timestamp()
_ITEM_KEYS = {
    "run_id", "corridor_id", "origin", "status", "submitted_at", "completed_at",
    "intersection_ids", "traffic_model", "kpis", "nema_exported", "has_report", "sim_metrics",
}


def _moe(delay: float, stops: float, vehicles: int = 412) -> dict:
    return {"control_delay_s_per_veh": delay, "stops_per_veh": stops,
            "vehicles_total": vehicles, "completion_ratio": 0.98}


def _result(corridor: str = "main_st") -> dict:
    return {
        "resolved_corridor": [
            {"ref": "main_1st", "intersection_id": "main_1st", "lat": 37.335, "lon": -121.90339,
             "node_id": "n1", "tls_id": "J1", "match_distance_m": 3.0},
        ],
        "seed": {"source": "stored_nema"},
        "baseline": {"plan_id": "stored_nema", "score": 1.0, "moe": _moe(31.33, 0.70)},
        "best": {"plan_id": "bo_0007", "score": 0.927, "moe": _moe(29.05, 0.66)},
        "trials": [],
        "config": {"kpi_weights": {"control_delay": 0.7, "stops": 0.3}},
    }


class _Clock:
    def __init__(self) -> None:
        self._ticks = itertools.count()

    def __call__(self) -> float:
        return _NOW + next(self._ticks)


def _write_run(out: Path, result: dict, *, nema: bool = True) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "corridor_optimization_result.json").write_text(json.dumps(result), encoding="utf-8")
    if nema:
        (out / "nema_plan.json").write_text(
            json.dumps({"schema": "terasim.nema_timing.v1", "intersections": [{"tls_id": "J1"}]}),
            encoding="utf-8",
        )


def _inputs(tmp_path: Path) -> tuple[str, str]:
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    tmc = inputs / "tmc.json"
    tmc.write_text(json.dumps({"corridor_id": "main_st"}), encoding="utf-8")
    (inputs / "osm.net.xml").write_text("<net/>", encoding="utf-8")
    sumocfg = inputs / "osm.sumocfg"
    sumocfg.write_text(
        '<configuration><input><net-file value="osm.net.xml"/></input></configuration>',
        encoding="utf-8",
    )
    return str(tmc), str(sumocfg)


def _app(tmp_path: Path, store=None):
    store = store or InMemoryCityJobStore(max_concurrency=8, lease_ttl=100, wall_clock=_Clock())
    output_root = tmp_path / "out"

    def launch(run_id, request, output_dir):
        def fake_opt(config, progress_callback):
            _write_run(Path(output_dir), _result())
            return _result()

        run_city_optimization_job(store, request, run_id, output_dir, opt_fn=fake_opt)

    buffer = InMemorySnapshotBuffer(clock=lambda: _NOW)
    counts = {d: {t: 1 for t in ("Right", "Straight", "Left")}
              for d in ("South", "West", "North", "East")}
    buffer.append("main_st", {
        "corridor_id": "main_st", "observation_interval": "10m",
        "timestamp_start": "2026-10-06T16:40:00Z",
        "intersections": [{"intersection_id": "main_1st", "lat": 37.335, "lon": -121.90339,
                           "counts": counts}],
    })
    sumocfg = tmp_path / "cached.sumocfg"
    sumocfg.write_text("<configuration/>", encoding="utf-8")

    def optimize(config, *, progress_callback):
        _write_run(config.output_dir, _result())
        return _result()

    app = FastAPI()
    app.include_router(create_v1_city_router(
        store, launch, output_root=output_root, buffer=buffer,
        direct_optimizer=optimize, map_acquirer=lambda _tmc: sumocfg,
    ))
    return TestClient(app), store, output_root, launch


def test_runs_of_every_origin_are_listed_newest_first(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_report, "renderer_unavailable_reason", lambda: None)
    client, store, output_root, launch = _app(tmp_path)
    tmc, sumocfg = _inputs(tmp_path)

    # 1. scheduled: the scheduler's submit callback
    submit = make_scheduled_submit(store, launch, output_root=output_root)
    scheduled = submit(
        {"corridor_id": "main_st", "base_sumocfg": sumocfg,
         "corridor": {"tls_ids": ["J1"]}},
        Path(tmc),
    )
    # 2. api: POST /v1/city/optimizations
    api = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg}).json()["run_id"]
    # 3. direct: the synchronous POST /v1/city/optimize
    direct = client.post("/v1/city/optimize", json={
        "type": "intersection", "corridor_id": "main_st",
        "intersection": {"intersection_id": "main_1st", "lat": 37.335, "lon": -121.90339},
    })
    assert direct.status_code == 200

    response = client.get(_BASE)

    assert response.status_code == 200
    runs = response.json()
    assert [run["origin"] for run in runs] == ["direct", "api", "scheduled"]
    assert runs[1]["run_id"] == api and runs[2]["run_id"] == scheduled
    for run in runs:
        assert set(run) == _ITEM_KEYS
        assert run["corridor_id"] == "main_st"
        assert run["status"] == "completed"
        assert run["submitted_at"].endswith("Z") and run["completed_at"].endswith("Z")
        assert run["submitted_at"] <= run["completed_at"]
        assert run["intersection_ids"] == ["main_1st"]  # the resolved corridor's
        assert run["traffic_model"] == "sumo"  # none of them asked for another
        assert run["kpis"] == {
            "baseline": {"cost": 1.0, "control_delay_s_per_veh": 31.33,
                         "stops_per_veh": 0.7, "vehicles_total": 412},
            "optimized": {"cost": 0.927, "control_delay_s_per_veh": 29.05,
                          "stops_per_veh": 0.66, "vehicles_total": 412},
        }
        assert run["nema_exported"] is True
        assert run["has_report"] is True
        assert run["sim_metrics"] == "unavailable"  # none computed in these runs
    # Every run's per-run endpoints answer.
    for run in runs:
        assert client.get(f"{_BASE}/{run['run_id']}/result").status_code == 200
        assert client.get(f"{_BASE}/{run['run_id']}/nema").status_code == 200


def test_states_while_queued_running_lost_and_failed(tmp_path: Path) -> None:
    store = InMemoryCityJobStore(max_concurrency=8, lease_ttl=100, wall_clock=_Clock())
    client, *_rest = _app(tmp_path, store)
    store.create("queued", origin="api", corridor_id="c", intersection_ids=["a", "b"])
    store.try_acquire_slots("queued", 1)
    store.create("running", origin="scheduled", corridor_id="c")
    store.try_acquire_slots("running", 1)
    store.set_running("running")
    store.create("lost", origin="direct", corridor_id="c")
    store.set_running("lost")  # no lease: its worker died
    store.create("failed", origin="api", corridor_id="c")
    store.set_failed("failed", "boom")

    runs = {run["run_id"]: run for run in client.get(_BASE).json()}

    assert {key: run["status"] for key, run in runs.items()} == {
        "queued": "queued", "running": "running", "lost": "lost", "failed": "failed",
    }
    assert runs["queued"]["intersection_ids"] == ["a", "b"]  # as requested, until resolved
    for key in ("queued", "running", "lost"):
        assert runs[key]["completed_at"] is None
    assert runs["failed"]["completed_at"] is not None
    for run in runs.values():
        assert run["kpis"] is None
        assert run["nema_exported"] is False and run["has_report"] is False
    assert runs["queued"]["sim_metrics"] == "pending"
    assert runs["running"]["sim_metrics"] == "pending"
    assert runs["lost"]["sim_metrics"] == "unavailable"
    assert runs["failed"]["sim_metrics"] == "unavailable"


def test_metrics_and_report_states_follow_the_run_files(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryCityJobStore(max_concurrency=8, lease_ttl=100, wall_clock=_Clock())
    client, _store, output_root, _launch = _app(tmp_path, store)
    for run_id in ("ready", "pending", "nonema"):
        store.create(run_id, origin="api", corridor_id="c")
        _write_run(output_root / run_id, _result(), nema=run_id != "nonema")
        store.set_completed(run_id, "unused", {})
    (output_root / "ready" / metrics.SIM_METRICS_FILENAME).write_text("{}")
    (output_root / "ready" / run_report.REPORT_FILENAME).write_bytes(b"%PDF")
    metrics.mark_pending(output_root / "pending")
    monkeypatch.setattr(run_report, "renderer_unavailable_reason", lambda: "no reportlab")

    runs = {run["run_id"]: run for run in client.get(_BASE).json()}

    assert runs["ready"]["sim_metrics"] == "ready" and runs["ready"]["has_report"] is True
    assert runs["pending"]["sim_metrics"] == "pending"
    assert runs["pending"]["has_report"] is False  # no renderer, nothing rendered yet
    assert runs["nonema"]["nema_exported"] is False and runs["nonema"]["has_report"] is False


def test_limit_and_corridor_filter(tmp_path: Path) -> None:
    store = InMemoryCityJobStore(max_concurrency=8, lease_ttl=100, wall_clock=_Clock())
    client, *_rest = _app(tmp_path, store)
    for index in range(205):
        store.create(f"r{index:03d}", origin="api", corridor_id="a" if index % 2 else "b")

    assert len(client.get(_BASE).json()) == 20  # default
    five = client.get(_BASE, params={"limit": 5}).json()
    assert [run["run_id"] for run in five] == ["r204", "r203", "r202", "r201", "r200"]
    assert len(client.get(_BASE, params={"limit": 500}).json()) == 200  # bounded
    only_a = client.get(_BASE, params={"corridor_id": "a", "limit": 3}).json()
    assert [run["run_id"] for run in only_a] == ["r203", "r201", "r199"]
    assert client.get(_BASE, params={"corridor_id": "none"}).json() == []
    assert client.get(_BASE, params={"limit": 0}).status_code == 422


def test_unreadable_results_list_without_kpis(tmp_path: Path) -> None:
    store = InMemoryCityJobStore(max_concurrency=8, lease_ttl=100, wall_clock=_Clock())
    client, _store, output_root, _launch = _app(tmp_path, store)
    store.create("broken", origin="api", corridor_id="c", intersection_ids=["x"])
    (output_root / "broken").mkdir(parents=True)
    (output_root / "broken" / "corridor_optimization_result.json").write_text("{not json")
    store.set_completed("broken", "unused", {})
    store.create("legacy")  # created without an origin: not part of the listing

    (run,) = client.get(_BASE).json()

    assert run["run_id"] == "broken" and run["kpis"] is None
    assert run["intersection_ids"] == ["x"]


def test_a_refine_run_lists_the_exported_plans_kpis(tmp_path: Path) -> None:
    store = InMemoryCityJobStore(max_concurrency=8, lease_ttl=100, wall_clock=_Clock())
    client, _store, output_root, _launch = _app(tmp_path, store)
    result = _result()
    result["export_source"] = "refine"
    result["refine"] = {
        "baseline": {"plan_id": "stored_nema", "score": 1.0, "moe": _moe(40.0, 1.0)},
        "evaluations": [{"plan_id": "bo_0002", "score": 0.8, "moe": _moe(32.0, 0.8)}],
        "winner_plan_id": "bo_0002",
    }
    store.create("r1", origin="scheduled", corridor_id="c")
    _write_run(output_root / "r1", result)
    store.set_completed("r1", "unused", {})

    (run,) = client.get(_BASE).json()

    assert run["kpis"]["baseline"]["control_delay_s_per_veh"] == 40.0
    assert run["kpis"]["optimized"] == {"cost": 0.8, "control_delay_s_per_veh": 32.0,
                                        "stops_per_veh": 0.8, "vehicles_total": 412}


def test_the_listing_is_documented_with_its_model(tmp_path: Path) -> None:
    client, *_rest = _app(tmp_path)
    schema = client.app.openapi()
    listing = schema["paths"]["/v1/city/optimizations"]["get"]
    ref = listing["responses"]["200"]["content"]["application/json"]["schema"]
    assert ref["type"] == "array"
    item = schema["components"]["schemas"][ref["items"]["$ref"].rsplit("/", 1)[-1]]
    assert set(item["properties"]) == _ITEM_KEYS
    assert item.get("additionalProperties") is False
    new_paths = [f"/v1/city/optimizations/{{run_id}}/{p}"
                 for p in ("baseline", "sim_metrics", "report.pdf")]
    for path in new_paths:
        assert path in schema["paths"]
    # What these endpoints publish stays product-neutral: no integration or product names.
    published = [listing, *(schema["paths"][p] for p in new_paths)] + [
        model for name, model in schema["components"]["schemas"].items()
        if name in {"OptimizationRunSummary", "RunKpis", "RunKpiSummary",
                    "BaselinePlanResponse", "SimMetricsDocument", "SimMetricsPending"}
    ]
    text = json.dumps(published).lower()
    for name in ("vss", "nvidia", "metropolis", "atspm", "mcp"):
        assert name not in text, name
