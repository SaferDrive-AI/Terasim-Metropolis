"""Per-request traffic model: ``traffic_model="cnde"`` runs one optimization under CNDE.

Both optimization POSTs (the synchronous ``/v1/city/optimize`` and the asynchronous
``/v1/city/optimizations``) run the deployment's vehicle behavior unless the request
names ``traffic_model="cnde"``. Then every candidate of that run is simulated under
"cnde", at most TERASIM_CITY_CNDE_MAX_WORKERS of them at once (the run reserves only
that many slots), and the run is recorded and answered as a CNDE run. A deployment
without the CNDE runtime refuses such a request with a 422 naming what is missing; it
never runs SUMO instead. Scheduled runs never ask for it.

No GPU and no CNDE runtime: the runtime check is stubbed. The BatchEvaluator test runs
the real orchestration on the demo corridor with a fake evaluator (needs sumolib).
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import terasim_city.orchestration.corridor_optimization as corridor_optimization
from terasim_city.factories import cnde_behavior
from terasim_city.optimize.base import Budget
from terasim_city.maps import cnde_artifacts
from terasim_service.city.ingest_api import InMemorySnapshotBuffer
from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import create_v1_city_router, make_scheduled_submit
from terasim_service.city.worker_optimize import (
    _build_optimization_config,
    run_city_optimization_job,
)
from tests.fixtures.tmc_templates import baseline_sheet, demo_tmc, write_tmc
from tests.test_city.conftest import REPO_ROOT, requires_sumolib

_REQUEST = {"tmc_json": "v.json", "base_sumocfg": "b.sumocfg"}
_NOW = datetime.fromisoformat("2026-07-01T09:00:00+00:00").timestamp()
_INTERSECTION = {"intersection_id": "oak", "lat": 37.335, "lon": -121.90339}
_MISSING = [
    "torch is not installed",
    "the CNDE checkpoint /app/TeraSim/packages/cnde/assets/nn_weights.pth is missing",
]
_ENV_VARS = (
    "TERASIM_CITY_VEHICLE_BEHAVIOR",
    "TERASIM_CITY_REFINE_TOP_K",
    "TERASIM_CITY_REFINE_BEHAVIOR",
    "TERASIM_CITY_REFINE_MAX_COLLISIONS",
    "TERASIM_CITY_REFINE_N_WORKERS",
    "TERASIM_CITY_CNDE_MAX_WORKERS",
    "TERASIM_CITY_CNDE_MAX_TRIALS",
    "TERASIM_CITY_INLINE_EVAL",
    "TERASIM_CITY_DATA_ROOT",
)

DEMO_CFG = REPO_ROOT / "examples" / "corridor_demo" / "demo.sumocfg"
requires_demo_assets = pytest.mark.skipif(
    not DEMO_CFG.exists(), reason=f"corridor demo assets not present at {DEMO_CFG.parent}"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """None of the deployment knobs are set."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def cnde_runtime(monkeypatch):
    """This deployment has the CNDE runtime and its checkpoint, and every map its artifacts."""
    monkeypatch.setattr(cnde_behavior, "missing_cnde_runtime", lambda: [])
    monkeypatch.setattr(cnde_artifacts, "missing_cnde_map_artifacts", lambda _sumocfg: [])


@pytest.fixture
def no_cnde_runtime(monkeypatch):
    """This deployment has neither."""
    monkeypatch.setattr(cnde_behavior, "missing_cnde_runtime", lambda: list(_MISSING))


def _write_inputs(folder: Path) -> tuple[str, str]:
    folder.mkdir(parents=True, exist_ok=True)
    tmc = folder / "tmc.json"
    tmc.write_text(json.dumps({"corridor_id": "c1"}), encoding="utf-8")
    (folder / "osm.net.xml").write_text("<net/>", encoding="utf-8")
    sumocfg = folder / "osm.sumocfg"
    sumocfg.write_text(
        '<configuration><input><net-file value="osm.net.xml"/></input></configuration>',
        encoding="utf-8",
    )
    return str(tmc), str(sumocfg)


def _snapshot() -> dict:
    counts = {
        direction: {turn: 1 for turn in ("Right", "Straight", "Left")}
        for direction in ("South", "West", "North", "East")
    }
    return {
        "corridor_id": "c1",
        "observation_interval": "10m",
        "timestamp_start": "2026-07-01T08:50:00Z",
        "intersections": [{**_INTERSECTION, "name": "oak", "counts": counts}],
    }


def _result() -> dict:
    return {
        "baseline": {"plan_id": "baseline", "score": 1.0, "moe": {"control_delay_s_per_veh": 30.0}},
        "best": {"plan_id": "bo_0001", "score": 0.8, "moe": {"control_delay_s_per_veh": 24.0}},
    }


def _app(tmp_path: Path, *, corridors=None, max_concurrency: int = 8):
    """Both POSTs over one in-memory store; the async launcher runs its job inline."""
    store = InMemoryCityJobStore(max_concurrency=max_concurrency, lease_ttl=100)
    output_root = tmp_path / "out"
    seen: dict = {"configs": [], "launched": []}
    sumocfg = tmp_path / "cached.sumocfg"
    sumocfg.write_text("<configuration/>", encoding="utf-8")

    def optimize(config, *, progress_callback):
        seen["configs"].append(config)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        (config.output_dir / "nema_plan.json").write_text(
            json.dumps({"schema": "terasim.nema_timing.v1", "intersections": []}),
            encoding="utf-8",
        )
        seen["held"] = [store.try_acquire_slots("probe", n) for n in (max_concurrency, 1)]
        store.release_slot("probe")
        return _result()

    def launch(run_id, request, output_dir):
        seen["launched"].append(request)
        run_city_optimization_job(store, request, run_id, output_dir, opt_fn=optimize)

    buffer = InMemorySnapshotBuffer(clock=lambda: _NOW)
    buffer.append("c1", _snapshot())
    app = FastAPI()
    app.include_router(
        create_v1_city_router(
            store,
            launch,
            output_root=output_root,
            buffer=buffer,
            corridors=corridors,
            direct_optimizer=optimize,
            map_acquirer=lambda _tmc: sumocfg,
        )
    )
    return TestClient(app), store, seen


def _direct(client: TestClient, **fields):
    return client.post(
        "/v1/city/optimize",
        json={"type": "intersection", "corridor_id": "c1", "intersection": _INTERSECTION, **fields},
    )


# --- the config a request builds ------------------------------------------------------


@pytest.mark.parametrize("fields", [{}, {"traffic_model": "sumo"}, {"traffic_model": None}])
def test_a_request_without_cnde_keeps_the_deployment_config(tmp_path, monkeypatch, fields):
    def never(*_args):
        raise AssertionError("only a CNDE request checks the CNDE runtime")

    monkeypatch.setattr(cnde_behavior, "missing_cnde_runtime", never)
    request = dict(_REQUEST, n_workers=8)

    config = _build_optimization_config({**request, **fields}, tmp_path)

    assert config == _build_optimization_config(request, tmp_path)
    assert (config.vehicle_behavior, config.n_workers, config.cnde_max_workers) == (
        "sumo_passive", 8, 1,
    )


def test_a_cnde_request_runs_every_candidate_under_cnde_with_at_most_three_workers(
    tmp_path, monkeypatch, cnde_runtime
):
    def build(n_workers: int):
        request = dict(_REQUEST, n_workers=n_workers, traffic_model="cnde")
        return _build_optimization_config(request, tmp_path)

    config = build(8)
    assert (config.vehicle_behavior, config.n_workers, config.cnde_max_workers) == ("cnde", 3, 3)
    assert build(2).n_workers == 2  # fewer than the limit: as asked

    monkeypatch.setenv("TERASIM_CITY_CNDE_MAX_WORKERS", "5")
    assert (build(8).n_workers, build(8).cnde_max_workers) == (5, 5)


def test_a_deployment_caps_the_trials_of_a_cnde_run_only(tmp_path, monkeypatch, cnde_runtime):
    """One scheduled-config entry serves both: its 48 trials stay for SUMO runs."""
    monkeypatch.setenv("TERASIM_CITY_CNDE_MAX_TRIALS", "24")

    def budget(**fields):
        return _build_optimization_config(dict(_REQUEST, patience=48, **fields), tmp_path).budget

    assert budget(max_trials=48, traffic_model="cnde") == Budget(max_trials=24, patience=48)
    assert budget(max_trials=10, traffic_model="cnde").max_trials == 10  # a cap, not a target
    assert budget(max_trials=48).max_trials == 48
    monkeypatch.delenv("TERASIM_CITY_CNDE_MAX_TRIALS")
    assert budget(max_trials=48, traffic_model="cnde").max_trials == 48  # unset: as asked


@pytest.mark.parametrize(
    "variable", ["TERASIM_CITY_CNDE_MAX_WORKERS", "TERASIM_CITY_CNDE_MAX_TRIALS"]
)
@pytest.mark.parametrize(("value", "message"), [("three", "must be an integer"), ("0", ">= 1")])
def test_an_invalid_cnde_limit_fails_at_config_build(
    tmp_path, monkeypatch, cnde_runtime, variable, value, message
):
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError, match=message):
        _build_optimization_config(dict(_REQUEST, traffic_model="cnde"), tmp_path)
    assert _build_optimization_config(_REQUEST, tmp_path).vehicle_behavior == "sumo_passive"


def test_a_cnde_request_drops_a_refine_stage_under_cnde(tmp_path, monkeypatch, cnde_runtime):
    monkeypatch.setenv("TERASIM_CITY_REFINE_TOP_K", "2")
    monkeypatch.setenv("TERASIM_CITY_REFINE_BEHAVIOR", "cnde")

    default = _build_optimization_config(dict(_REQUEST, n_workers=1), tmp_path)
    cnde = _build_optimization_config(dict(_REQUEST, traffic_model="cnde"), tmp_path)

    assert (default.vehicle_behavior, default.refine_vehicle_behavior) == ("sumo_passive", "cnde")
    # The best plans were already scored under "cnde": nothing to re-score them under.
    assert (cnde.vehicle_behavior, cnde.refine_vehicle_behavior) == ("cnde", None)


def test_an_unknown_traffic_model_is_rejected(tmp_path):
    client, store, seen = _app(tmp_path)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")

    sync = _direct(client, traffic_model="neural")
    async_ = client.post(
        "/v1/city/optimizations",
        json={"tmc_json": tmc, "base_sumocfg": sumocfg, "traffic_model": "neural"},
    )

    assert (sync.status_code, async_.status_code) == (422, 422)
    assert seen == {"configs": [], "launched": []}
    assert store.list_runs() == []


# --- a deployment without the CNDE runtime ---------------------------------------------


def test_a_cnde_request_without_the_runtime_is_a_clear_422_on_both_posts(
    tmp_path, no_cnde_runtime
):
    client, store, seen = _app(tmp_path)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")

    sync = _direct(client, traffic_model="cnde")
    async_ = client.post(
        "/v1/city/optimizations",
        json={"tmc_json": tmc, "base_sumocfg": sumocfg, "traffic_model": "cnde"},
    )

    for response in (sync, async_):
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "traffic_model='cnde' is not available on this deployment" in detail
        assert all(problem in detail for problem in _MISSING)
    # Nothing ran, under SUMO or otherwise: no optimizer call, no worker, no run, no slot.
    assert seen == {"configs": [], "launched": []}
    assert store.list_runs() == []
    assert store.try_acquire_slots("other", 8) is True
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_a_cnde_request_on_a_map_outside_the_store_is_a_clear_422(tmp_path, monkeypatch):
    """The runtime is there, but the named map is no stored package: CNDE has nowhere to
    keep its map artifacts, so every trial would fail. Refused up front, on both POSTs."""
    monkeypatch.setattr(cnde_behavior, "missing_cnde_runtime", lambda: [])
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    corridors = {
        "c1": {"corridor_id": "c1", "base_sumocfg": sumocfg, "corridor": {"tls_ids": ["J1"]}},
    }
    client, store, seen = _app(tmp_path, corridors=corridors)

    sync = _direct(client, traffic_model="cnde")
    async_ = client.post(
        "/v1/city/optimizations",
        json={"tmc_json": tmc, "base_sumocfg": sumocfg, "traffic_model": "cnde"},
    )

    for response in (sync, async_):
        assert response.status_code == 422
        assert f"the corridor map {sumocfg} is not a stored map package" in (
            response.json()["detail"]
        )
    assert seen == {"configs": [], "launched": []}
    assert store.list_runs() == []


# --- what a CNDE run reserves, records and answers --------------------------------------


def test_a_cnde_api_run_reserves_and_runs_only_the_capped_workers(tmp_path, cnde_runtime):
    client, store, seen = _app(tmp_path)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")

    response = client.post(
        "/v1/city/optimizations",
        json={"tmc_json": tmc, "base_sumocfg": sumocfg, "n_workers": 8, "traffic_model": "cnde"},
    )

    assert response.status_code == 202
    (config,) = seen["configs"]
    assert (config.vehicle_behavior, config.n_workers) == ("cnde", 3)
    assert seen["launched"][0]["n_workers"] == 3
    # While it ran, 3 of the 8 slots were held: 5 more were free, 8 were not.
    assert seen["held"] == [False, True]
    record = store.get(response.json()["run_id"])
    assert (record.status, record.traffic_model) == ("completed", "cnde")
    (listed,) = client.get("/v1/city/optimizations").json()
    assert listed["traffic_model"] == "cnde"


def test_a_cnde_direct_run_is_answered_and_recorded_as_cnde(tmp_path, cnde_runtime):
    _tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    corridors = {
        "c1": {"corridor_id": "c1", "base_sumocfg": sumocfg, "corridor": {"tls_ids": ["J1"]},
               "n_workers": 8, "max_trials": 24},
    }
    client, store, seen = _app(tmp_path, corridors=corridors)

    response = _direct(client, traffic_model="cnde")

    assert response.status_code == 200
    assert response.json()["traffic_model"] == "cnde"
    (config,) = seen["configs"]
    # Every candidate of the configured budget runs under CNDE, 3 at a time.
    assert (config.vehicle_behavior, config.n_workers, config.budget.max_trials) == ("cnde", 3, 24)
    assert seen["held"] == [False, True]
    (record,) = store.list_runs()
    assert (record.origin, record.traffic_model) == ("direct", "cnde")


def test_a_default_direct_run_answers_exactly_as_before(tmp_path):
    client, store, seen = _app(tmp_path)

    response = _direct(client)

    assert response.status_code == 200
    assert set(response.json()) == {"nema_plan", "kpis"}
    (config,) = seen["configs"]
    assert config.vehicle_behavior == "sumo_passive"
    (record,) = store.list_runs()
    assert record.traffic_model == "sumo"


# --- scheduled runs ----------------------------------------------------------------------


def test_scheduled_runs_keep_the_deployment_behavior_next_to_a_cnde_request(
    tmp_path, cnde_runtime
):
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    entry = {"corridor_id": "c1", "base_sumocfg": sumocfg, "corridor": {"tls_ids": ["J1"]},
             "n_workers": 8}
    corridors = {"c1": entry}
    pristine = copy.deepcopy(corridors)
    client, store, seen = _app(tmp_path, corridors=corridors)
    launched: list[dict] = []
    submit = make_scheduled_submit(
        store, lambda _run_id, request, _out: launched.append(request),
        output_root=tmp_path / "out",
    )

    assert _direct(client, traffic_model="cnde").status_code == 200
    run_id = submit(entry, Path(tmc))

    # The CNDE request changed neither the shared corridor config nor the process.
    assert corridors == pristine
    assert "TERASIM_CITY_VEHICLE_BEHAVIOR" not in os.environ
    (request,) = launched
    assert request.get("traffic_model") is None
    config = _build_optimization_config(request, tmp_path)
    assert (config.vehicle_behavior, config.n_workers, config.cnde_max_workers) == (
        "sumo_passive", 8, 1,
    )
    record = store.get(run_id)
    assert (record.origin, record.traffic_model) == ("scheduled", "sumo")
    assert store.try_acquire_slots("other", 1) is False  # it holds all 8 slots, as before


# --- the candidates reach the evaluator under CNDE --------------------------------------


def _install_recording_evaluator(monkeypatch, calls: list[dict]) -> None:
    """Replace BatchEvaluator with a fake that records how it was built and scores plans."""

    class _RecordingBatchEvaluator:
        def __init__(self, **kwargs):
            calls.append(
                {key: kwargs.get(key) for key in ("vehicle_behavior", "n_workers", "inline")}
            )

        def evaluate(self, plans):
            results = []
            for index, plan in enumerate(plans):
                delay = 30.0 - index
                results.append(
                    {
                        "plan_id": plan.plan_id,
                        "moe": {
                            "control_delay_s_per_veh": delay,
                            "stops_per_veh": delay / 10.0,
                            "vehicles_total": 100,
                            "vehicles_completed": 100,
                            "vehicles_unfinished": 0,
                            "vehicles_undeparted": 0,
                            "total_delay_s": delay * 100.0,
                            "total_stops": int(delay * 10.0),
                            "mean_time_loss_s": delay,
                            "mean_depart_delay_s": 0.0,
                        },
                        "valid": True,
                        "quality_errors": [],
                        "trial_dir": f"trial_fake/{plan.plan_id}",
                        "kpi": {"quality_gate": {"counts": {"collisions": 0}}},
                    }
                )
            return results

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

    monkeypatch.setattr(corridor_optimization, "BatchEvaluator", _RecordingBatchEvaluator)


@requires_demo_assets
@requires_sumolib
@pytest.mark.parametrize("traffic_model", [None, "cnde"])
def test_the_requested_traffic_model_reaches_the_batch_evaluator(
    tmp_path, monkeypatch, cnde_runtime, traffic_model
):
    monkeypatch.setenv("TERASIM_CITY_REFINE_TOP_K", "1")
    monkeypatch.setenv("TERASIM_CITY_REFINE_BEHAVIOR", "cnde")
    calls: list[dict] = []
    _install_recording_evaluator(monkeypatch, calls)
    request = {
        "tmc_json": str(write_tmc(tmp_path, demo_tmc())),
        "base_sumocfg": str(DEMO_CFG),
        "corridor": {"from": "i1", "to": "i3"},
        "baseline_sheet": str(write_tmc(tmp_path, baseline_sheet(), name="sheet.json")),
        "optimizer": "bayes_offsets",
        "max_trials": 2,
        "patience": 0,
        "sim_end": 120,
        "n_workers": 8,
        "traffic_model": traffic_model,
    }

    config = _build_optimization_config(request, tmp_path / "out")
    result = corridor_optimization.run_corridor_optimization(config)

    assert result["n_trials"] >= 3  # the seed plus the Bayesian trials
    if traffic_model == "cnde":
        # One evaluator for every candidate, under CNDE, three at a time; no refine
        # pass re-scores them under the behavior they were scored with.
        assert calls == [{"vehicle_behavior": "cnde", "n_workers": 3, "inline": None}]
        assert result["traffic_model"] == "cnde"
        assert (result["config"]["vehicle_behavior"], result["config"]["n_workers"]) == ("cnde", 3)
        assert "refine" not in result
    else:
        # As before: the coarse search under SUMO's own model, then the deployment's
        # CNDE refine of the best plan.
        assert [call["vehicle_behavior"] for call in calls] == ["sumo_passive", "cnde"]
        assert calls[0]["n_workers"] == 8
        assert "traffic_model" not in result
