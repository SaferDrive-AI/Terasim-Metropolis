"""Simulated signal metrics of a run: the background job, its states and the endpoint.

GET /v1/city/optimizations/{run_id}/sim_metrics answers 202 {"status": "pending"} while
the metrics are computed, 200 with the corridor_ui.sim_metrics.v1 document once
sim_metrics.json exists, and 404 with a reason when they are disabled or failed. The
computation is injected here (no SUMO); one runtime-marked test runs the real one in its
child process on the demo scenario.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terasim_service.city import sim_metrics_job as job
from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import create_v1_city_router
from terasim_service.city.worker_optimize import run_city_optimization_job

_BASE = "/v1/city/optimizations"
_DOC = {"schema": "corridor_ui.sim_metrics.v1", "corridor_id": "c1", "plans": {}}
REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.delenv("TERASIM_CITY_SIM_METRICS", raising=False)


def _fake_compute(doc=_DOC, seen=None):
    def compute(run_dir, *, run_id, corridor_id):
        if seen is not None:
            seen.append((Path(run_dir), run_id, corridor_id))
        return {**doc, "run_id": run_id}

    return compute


# --- states ------------------------------------------------------------------------


def test_states_of_a_run(tmp_path: Path) -> None:
    state = job.sim_metrics_state

    assert state(tmp_path, "submitted") == job.SimMetricsState("pending")
    assert state(tmp_path, "running") == job.SimMetricsState("pending")
    disabled = state(tmp_path, "running", enabled=False)
    assert disabled.state == "unavailable" and "TERASIM_CITY_SIM_METRICS" in disabled.reason
    failed = state(tmp_path, "failed")
    assert failed.state == "unavailable" and "failed" in failed.reason
    assert state(tmp_path, "lost").state == "unavailable"
    never = state(tmp_path, "completed")
    assert never.state == "unavailable" and "not computed" in never.reason

    job.mark_pending(tmp_path)
    assert state(tmp_path, "completed") == job.SimMetricsState("pending")
    stale = time.time() - job.STALE_AFTER_S - 5
    os.utime(tmp_path / job.SIM_METRICS_PENDING_FILENAME, (stale, stale))
    gone = state(tmp_path, "completed")
    assert gone.state == "unavailable" and "stopped" in gone.reason

    (tmp_path / job.SIM_METRICS_ERROR_FILENAME).write_text(
        json.dumps({"reason": "the run has no trial"}), encoding="utf-8"
    )
    assert state(tmp_path, "completed") == job.SimMetricsState("unavailable", "the run has no trial")

    (tmp_path / job.SIM_METRICS_FILENAME).write_text("{}", encoding="utf-8")
    assert state(tmp_path, "completed") == job.SimMetricsState("ready")
    assert state(tmp_path, "completed", enabled=False) == job.SimMetricsState("ready")


def test_enabled_by_default_and_switched_off_by_the_variable(monkeypatch) -> None:
    assert job.sim_metrics_enabled() is True
    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv("TERASIM_CITY_SIM_METRICS", value)
        assert job.sim_metrics_enabled() is False
    monkeypatch.setenv("TERASIM_CITY_SIM_METRICS", "1")
    assert job.sim_metrics_enabled() is True


# --- the job -----------------------------------------------------------------------


def test_the_job_writes_the_document_holding_one_slot(tmp_path: Path) -> None:
    store = InMemoryCityJobStore(max_concurrency=2, lease_ttl=100)
    store.create("r1", origin="api", corridor_id="c1")
    held = []

    def compute(run_dir, *, run_id, corridor_id):
        held.append(store.holds_slots(job.slot_token(run_id)))
        assert job.sim_metrics_state(run_dir, "completed").state == "pending"
        return {**_DOC, "run_id": run_id, "corridor_id": corridor_id}

    state = job.run_sim_metrics_job(store, "r1", tmp_path, compute=compute)

    assert state == "ready" and held == [True]
    document = json.loads((tmp_path / job.SIM_METRICS_FILENAME).read_text(encoding="utf-8"))
    assert document == {**_DOC, "run_id": "r1", "corridor_id": "c1"}  # corridor from the record
    assert (tmp_path / job.SIM_METRICS_FILENAME).read_text(encoding="utf-8").endswith("}\n")
    assert not (tmp_path / job.SIM_METRICS_PENDING_FILENAME).exists()
    assert not store.holds_slots(job.slot_token("r1"))


def test_a_failing_computation_leaves_its_reason(tmp_path: Path) -> None:
    from terasim_city.evaluation.sim_metrics import SimMetricsUnavailable

    store = InMemoryCityJobStore(max_concurrency=2, lease_ttl=100)

    def compute(run_dir, *, run_id, corridor_id):
        raise SimMetricsUnavailable("the optimized plan 'bo_1' has no trial")

    assert job.run_sim_metrics_job(store, "r1", tmp_path, compute=compute) == "unavailable"
    state = job.sim_metrics_state(tmp_path, "completed")
    assert state == job.SimMetricsState("unavailable", "the optimized plan 'bo_1' has no trial")
    assert not store.holds_slots(job.slot_token("r1"))

    def crash(run_dir, *, run_id, corridor_id):
        raise RuntimeError("boom")

    job.run_sim_metrics_job(store, "r1", tmp_path, compute=crash)
    assert job.sim_metrics_state(tmp_path, "completed").reason == "RuntimeError: boom"


def test_the_job_waits_for_a_free_slot(tmp_path: Path) -> None:
    store = InMemoryCityJobStore(max_concurrency=1, lease_ttl=100)
    assert store.try_acquire_slots("busy", 1)
    threading.Timer(0.3, store.release_slot, args=("busy",)).start()

    started = time.monotonic()
    state = job.run_sim_metrics_job(
        store, "r1", tmp_path, compute=_fake_compute(), poll_interval=0.05, slot_wait_s=10
    )

    assert state == "ready"
    assert time.monotonic() - started >= 0.25


def test_no_free_slot_in_time_is_unavailable_with_the_reason(tmp_path: Path) -> None:
    store = InMemoryCityJobStore(max_concurrency=1, lease_ttl=100)
    assert store.try_acquire_slots("busy", 1)

    state = job.run_sim_metrics_job(
        store, "r1", tmp_path, compute=_fake_compute(), poll_interval=0.05, slot_wait_s=0.2
    )

    assert state == "unavailable"
    assert "no worker slot" in job.sim_metrics_state(tmp_path, "completed").reason


def _sleeping_child(*_args) -> None:  # module level: the spawned child imports it
    time.sleep(60)


def test_a_child_over_its_wall_time_is_stopped(tmp_path: Path) -> None:
    store = InMemoryCityJobStore(max_concurrency=1, lease_ttl=100)

    started = time.monotonic()
    state = job.run_sim_metrics_job(
        store, "r1", tmp_path, timeout_s=1.0, child_target=_sleeping_child
    )

    assert state == "unavailable"
    assert time.monotonic() - started < 30
    reason = job.sim_metrics_state(tmp_path, "completed").reason
    assert "did not finish within 1 s" in reason
    assert not list(tmp_path.glob(".sim_metrics*"))


# --- the endpoint ------------------------------------------------------------------


def _router_client(tmp_path: Path, store) -> TestClient:
    app = FastAPI()
    app.include_router(create_v1_city_router(store, lambda *_a: None, output_root=tmp_path))
    return TestClient(app)


def test_endpoint_state_machine_pending_then_ready(tmp_path: Path) -> None:
    store = InMemoryCityJobStore(max_concurrency=2, lease_ttl=100)
    client = _router_client(tmp_path, store)
    store.create("r1", origin="api")
    assert store.try_acquire_slots("r1", 1)  # a live worker
    store.set_running("r1")
    run_dir = tmp_path / "r1"
    run_dir.mkdir()

    running = client.get(f"{_BASE}/r1/sim_metrics")
    assert running.status_code == 202
    assert running.json() == {"status": "pending"}

    job.mark_pending(run_dir)
    store.set_completed("r1", str(run_dir / "result.json"), {})
    assert client.get(f"{_BASE}/r1/sim_metrics").status_code == 202

    (run_dir / job.SIM_METRICS_FILENAME).write_text(
        json.dumps(_DOC, indent=1) + "\n", encoding="utf-8"
    )
    (run_dir / job.SIM_METRICS_PENDING_FILENAME).unlink()
    ready = client.get(f"{_BASE}/r1/sim_metrics")
    assert ready.status_code == 200
    assert ready.headers["content-type"].startswith("application/json")
    assert ready.content == (run_dir / job.SIM_METRICS_FILENAME).read_bytes()


def test_endpoint_404s_with_the_reason(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryCityJobStore(max_concurrency=2, lease_ttl=100)
    client = _router_client(tmp_path, store)
    store.create("r1", origin="api")
    store.set_completed("r1", "unused", {})
    (tmp_path / "r1").mkdir()

    never = client.get(f"{_BASE}/r1/sim_metrics")
    assert never.status_code == 404 and "not computed" in never.json()["detail"]

    (tmp_path / "r1" / job.SIM_METRICS_ERROR_FILENAME).write_text(
        json.dumps({"reason": "TimeoutError: too slow"}), encoding="utf-8"
    )
    failed = client.get(f"{_BASE}/r1/sim_metrics")
    assert failed.status_code == 404 and failed.json()["detail"] == "TimeoutError: too slow"

    store.create("r2", origin="api")
    store.set_running("r2")  # no lease: its worker died
    lost = client.get(f"{_BASE}/r2/sim_metrics")
    assert lost.status_code == 404

    monkeypatch.setenv("TERASIM_CITY_SIM_METRICS", "0")
    store.create("r3", origin="api")
    assert store.try_acquire_slots("r3", 1)
    disabled = client.get(f"{_BASE}/r3/sim_metrics")
    assert disabled.status_code == 404
    assert "TERASIM_CITY_SIM_METRICS" in disabled.json()["detail"]

    assert client.get(f"{_BASE}/nope/sim_metrics").status_code == 404


# --- wiring: after every completed run ---------------------------------------------


def _inputs(tmp_path: Path) -> tuple[str, str]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    tmc = inputs / "tmc.json"
    tmc.write_text(json.dumps({"corridor_id": "c1"}), encoding="utf-8")
    (inputs / "osm.net.xml").write_text("<net/>", encoding="utf-8")
    sumocfg = inputs / "osm.sumocfg"
    sumocfg.write_text(
        '<configuration><input><net-file value="osm.net.xml"/></input></configuration>',
        encoding="utf-8",
    )
    return str(tmc), str(sumocfg)


def _finishing_opt(output_dir):
    def fake_opt(config, progress_callback):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        result = {"baseline": {"plan_id": "b", "score": 1.0, "moe": {}},
                  "best": {"plan_id": "o", "score": 0.9, "moe": {}}, "trials": []}
        (out / "corridor_optimization_result.json").write_text(json.dumps(result))
        return result

    return fake_opt


def test_an_async_run_computes_its_metrics_after_it_is_recorded_completed(tmp_path: Path):
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    observed = []

    def compute(run_dir, *, run_id, corridor_id):
        record = store.get(run_id)
        observed.append((record.status, (Path(run_dir) / "corridor_optimization_result.json").is_file(),
                         store.holds_slots(run_id), corridor_id))
        return {**_DOC, "run_id": run_id}

    def launch(run_id, request, output_dir):
        run_city_optimization_job(
            store, request, run_id, output_dir, opt_fn=_finishing_opt(output_dir),
            sim_metrics_runner=lambda rid, out: job.run_sim_metrics_job(
                store, rid, Path(out), compute=compute
            ),
        )

    app = FastAPI()
    app.include_router(create_v1_city_router(store, launch, output_root=tmp_path / "out"))
    client = TestClient(app)
    tmc, sumocfg = _inputs(tmp_path)
    run_id = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg}).json()["run_id"]

    # The result was written and the run recorded completed (its slots released)
    # before the metrics were computed.
    assert observed == [("completed", True, False, "c1")]
    response = client.get(f"{_BASE}/{run_id}/sim_metrics")
    assert response.status_code == 200
    assert response.json()["run_id"] == run_id


def test_disabled_metrics_are_not_computed_for_an_async_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TERASIM_CITY_SIM_METRICS", "0")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    ran = []

    def launch(run_id, request, output_dir):
        run_city_optimization_job(
            store, request, run_id, output_dir, opt_fn=_finishing_opt(output_dir),
            sim_metrics_runner=lambda rid, out: ran.append(rid),
        )

    app = FastAPI()
    app.include_router(create_v1_city_router(store, launch, output_root=tmp_path / "out"))
    client = TestClient(app)
    tmc, sumocfg = _inputs(tmp_path)
    run_id = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg}).json()["run_id"]

    assert ran == []
    response = client.get(f"{_BASE}/{run_id}/sim_metrics")
    assert response.status_code == 404
    assert "TERASIM_CITY_SIM_METRICS" in response.json()["detail"]


def test_a_direct_run_launches_its_metrics_after_it_completed(tmp_path: Path) -> None:
    from datetime import datetime

    from terasim_service.city.ingest_api import InMemorySnapshotBuffer

    now = datetime.fromisoformat("2026-07-01T09:00:00+00:00").timestamp()
    buffer = InMemorySnapshotBuffer(clock=lambda: now)
    counts = {d: {t: 1 for t in ("Right", "Straight", "Left")}
              for d in ("South", "West", "North", "East")}
    buffer.append("c1", {
        "corridor_id": "c1", "observation_interval": "10m",
        "timestamp_start": "2026-07-01T08:50:00Z",
        "intersections": [{"intersection_id": "i1", "lat": 37.3, "lon": -121.9, "counts": counts}],
    })
    sumocfg = tmp_path / "cached.sumocfg"
    sumocfg.write_text("<configuration/>", encoding="utf-8")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    launched = []

    def optimize(config, *, progress_callback):
        config.output_dir.mkdir(parents=True, exist_ok=True)
        (config.output_dir / "nema_plan.json").write_text(json.dumps({"intersections": []}))
        return {"baseline": {"plan_id": "b", "score": 1.0, "moe": {}},
                "best": {"plan_id": "o", "score": 0.9, "moe": {}}}

    def launcher(run_id, run_dir):
        record = store.get(run_id)
        launched.append((record.status, store.holds_slots(run_id), Path(run_dir).name))
        job.run_sim_metrics_job(store, run_id, Path(run_dir), compute=_fake_compute())

    app = FastAPI()
    app.include_router(create_v1_city_router(
        store, lambda *_a: None, output_root=tmp_path / "out", buffer=buffer,
        direct_optimizer=optimize, map_acquirer=lambda _tmc: sumocfg,
        sim_metrics_launcher=launcher,
    ))
    client = TestClient(app)
    response = client.post("/v1/city/optimize", json={
        "type": "intersection", "corridor_id": "c1",
        "intersection": {"intersection_id": "i1", "lat": 37.3, "lon": -121.9},
    })
    assert response.status_code == 200

    (record,) = store.list_runs()
    assert launched == [("completed", False, record.run_id)]
    assert client.get(f"{_BASE}/{record.run_id}/sim_metrics").status_code == 200


# --- the real computation, in its child process ------------------------------------


def _sumo_runtime() -> bool:
    import importlib.util
    import shutil

    return shutil.which("sumo") is not None and importlib.util.find_spec("libsumo") is not None


@pytest.mark.integration
@pytest.mark.requires_sumo
@pytest.mark.skipif(not _sumo_runtime(), reason="needs SUMO (sumo binary and libsumo)")
def test_the_real_metrics_of_a_demo_run_in_their_child_process(tmp_path: Path) -> None:
    from terasim_city.optimize.base import Budget
    from terasim_city.orchestration.corridor_optimization import (
        CorridorOptimizationConfig,
        run_corridor_optimization,
    )

    run_dir = tmp_path / "abc123"
    run_corridor_optimization(CorridorOptimizationConfig(
        tmc_json=DEMO_DIR / "placeholder_tmc.json",
        base_sumocfg=DEMO_DIR / "demo_scenario.sumocfg",
        output_dir=run_dir,
        tls_ids=("J1", "J2", "J3"),
        optimizer="exhaustive",
        flow_source="scenario_routes",
        counts_source="scenario_routes",
        seed_from_scenario_programs=True,
        budget=Budget(max_trials=1, patience=0),
        sim_end=600.0,
        n_workers=2,
    ))
    store = InMemoryCityJobStore(max_concurrency=2, lease_ttl=100)
    store.create("abc123", origin="api", corridor_id="demo_scenario")

    state = job.run_sim_metrics_job(store, "abc123", run_dir, timeout_s=300)

    assert state == "ready", job.sim_metrics_state(run_dir, "completed").reason
    doc = json.loads((run_dir / job.SIM_METRICS_FILENAME).read_text(encoding="utf-8"))
    assert doc["schema"] == "corridor_ui.sim_metrics.v1"
    assert (doc["run_id"], doc["corridor_id"]) == ("abc123", "demo_scenario")
    assert list(doc["plans"]) == ["optimized", "baseline"]
    assert set(doc["plans"]["optimized"]["intersections"]) == {"J1", "J2", "J3"}
    assert not list(run_dir.glob(".sim_metrics*"))
