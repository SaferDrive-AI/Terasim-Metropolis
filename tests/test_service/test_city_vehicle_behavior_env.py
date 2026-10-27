"""Deployment-level env-var pass-through of the CNDE/refine knobs.

Five env vars (TERASIM_CITY_VEHICLE_BEHAVIOR / _REFINE_TOP_K /
_REFINE_BEHAVIOR / _REFINE_MAX_COLLISIONS / _REFINE_N_WORKERS) are deliberately NOT part of
OptimizationRequest/DirectOptimizationRequest — quality/fidelity knobs are
ops-controlled, not caller-controllable. They are read
by worker_optimize._build_optimization_config and must fail loud AT
SUBMISSION TIME (before any slot is reserved or worker launched), never as a
mid-run worker crash.

Also covers optimize_api._direct_kpis's export_source branching:
"optimized"/"baseline" must come from the refine winner/refine baseline when
the result carries export_source == "refine", and stay on the coarse-stage
result["best"]/result["baseline"] sourcing otherwise.

No network, no GPU, no SUMO.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from terasim_city.optimize.batch import INLINE_EVAL_ENV_VAR
from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import (
    _direct_kpis,
    create_v1_city_router,
    submit_optimization,
)
from terasim_service.city.worker_optimize import _build_optimization_config

_REQUEST = {"tmc_json": "v.json", "base_sumocfg": "b.sumocfg"}

_ENV_VARS = (
    "TERASIM_CITY_VEHICLE_BEHAVIOR",
    "TERASIM_CITY_REFINE_TOP_K",
    "TERASIM_CITY_REFINE_BEHAVIOR",
    "TERASIM_CITY_REFINE_MAX_COLLISIONS",
    "TERASIM_CITY_REFINE_N_WORKERS",
    "TERASIM_CITY_DATA_ROOT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Deterministic baseline: none of the deployment knobs are set."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _write_inputs(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    tmc = folder / "tmc.json"
    tmc.write_text("{}", encoding="utf-8")
    (folder / "osm.net.xml").write_text("<net/>", encoding="utf-8")
    sumocfg = folder / "osm.sumocfg"
    sumocfg.write_text(
        '<configuration><input><net-file value="osm.net.xml"/></input></configuration>',
        encoding="utf-8",
    )
    return str(tmc), str(sumocfg)


# --- _build_optimization_config: env vars populate config fields -----------


def test_env_vars_populate_config_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TERASIM_CITY_VEHICLE_BEHAVIOR", "cnde")
    monkeypatch.setenv("TERASIM_CITY_REFINE_TOP_K", "3")
    monkeypatch.setenv("TERASIM_CITY_REFINE_BEHAVIOR", "cnde")
    monkeypatch.setenv("TERASIM_CITY_REFINE_MAX_COLLISIONS", "2")
    monkeypatch.setenv("TERASIM_CITY_REFINE_N_WORKERS", "4")
    # n_workers=1: _REQUEST omits it, and DEFAULT_N_WORKERS=2 would trip
    # the coarse-path cnde/multi-worker guard below -- this test
    # is about field population, not that guard (covered separately by
    # test_cnde_vehicle_behavior_with_default_n_workers_fails_at_config_build).
    request = dict(_REQUEST, n_workers=1)

    config = _build_optimization_config(request, tmp_path)

    assert config.vehicle_behavior == "cnde"
    assert config.refine_top_k == 3
    assert config.refine_vehicle_behavior == "cnde"
    assert config.refine_max_collisions == 2
    assert config.refine_n_workers == 4


def test_refine_behavior_env_empty_string_means_no_refine(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TERASIM_CITY_REFINE_BEHAVIOR", "")
    config = _build_optimization_config(_REQUEST, tmp_path)
    assert config.refine_vehicle_behavior is None


# --- no env vars set: byte-identical to the configuration defaults ---------


def test_no_env_vars_config_matches_the_configuration_defaults(tmp_path: Path) -> None:
    from terasim_city.orchestration.corridor_optimization import (
        CorridorOptimizationConfig,
    )

    config = _build_optimization_config(_REQUEST, tmp_path)

    assert config.vehicle_behavior == "sumo_passive"
    assert config.refine_top_k == 0
    assert config.refine_vehicle_behavior is None
    assert config.refine_max_collisions is None
    assert config.refine_n_workers == 1

    # Full field-by-field pin: a config built with none of the five
    # deployment fields passed at all (i.e. every dataclass default) must be
    # identical to what _build_optimization_config produces with no env vars set.
    reference = CorridorOptimizationConfig(
        tmc_json=Path(_REQUEST["tmc_json"]),
        base_sumocfg=Path(_REQUEST["base_sumocfg"]),
        output_dir=tmp_path,
        n_workers=2,  # _build_optimization_config's own DEFAULT_N_WORKERS,
        # distinct from CorridorOptimizationConfig's own dataclass default (4)
        # — unrelated to the env vars, so pinned explicitly here.
    )
    assert config == reference


# --- fail-loud at submission time -------------------------------------------


def test_unregistered_vehicle_behavior_fails_at_config_build(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TERASIM_CITY_VEHICLE_BEHAVIOR", "not_a_real_behavior")
    with pytest.raises(ValueError, match="not a registered vehicle behavior"):
        _build_optimization_config(_REQUEST, tmp_path)


def test_non_integer_refine_top_k_fails_at_config_build(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TERASIM_CITY_REFINE_TOP_K", "not-an-int")
    with pytest.raises(ValueError, match="must be an integer"):
        _build_optimization_config(_REQUEST, tmp_path)


def test_non_integer_refine_max_collisions_fails_at_config_build(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TERASIM_CITY_REFINE_MAX_COLLISIONS", "abc")
    with pytest.raises(ValueError, match="must be an integer"):
        _build_optimization_config(_REQUEST, tmp_path)


def test_negative_refine_max_collisions_fails_at_config_build(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TERASIM_CITY_REFINE_MAX_COLLISIONS", "-1")
    with pytest.raises(ValueError, match=">= 0"):
        _build_optimization_config(_REQUEST, tmp_path)


def test_negative_refine_top_k_fails_at_config_build(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TERASIM_CITY_REFINE_TOP_K", "-1")
    with pytest.raises(ValueError, match=">= 0"):
        _build_optimization_config(_REQUEST, tmp_path)


# --- TERASIM_CITY_REFINE_N_WORKERS (refine-stage process count) ------------


def test_non_integer_refine_n_workers_fails_at_config_build(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TERASIM_CITY_REFINE_N_WORKERS", "two")
    with pytest.raises(ValueError, match="must be an integer"):
        _build_optimization_config(_REQUEST, tmp_path)


def test_zero_refine_n_workers_fails_at_config_build(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TERASIM_CITY_REFINE_N_WORKERS", "0")
    with pytest.raises(ValueError, match=">= 1"):
        _build_optimization_config(_REQUEST, tmp_path)


def test_refine_n_workers_needs_no_inline_eval_env(
    tmp_path: Path, monkeypatch
) -> None:
    """refine_n_workers > 1 is the refine stage's own opt-in to concurrent
    CNDE processes: it must NOT trip the coarse-path cnde/multi-worker guard
    (which reads vehicle_behavior, not refine_vehicle_behavior) and must not
    need TERASIM_CITY_INLINE_EVAL, which the refine stage ignores."""
    monkeypatch.delenv(INLINE_EVAL_ENV_VAR, raising=False)
    monkeypatch.setenv("TERASIM_CITY_REFINE_TOP_K", "3")
    monkeypatch.setenv("TERASIM_CITY_REFINE_BEHAVIOR", "cnde")
    monkeypatch.setenv("TERASIM_CITY_REFINE_N_WORKERS", "4")

    config = _build_optimization_config(_REQUEST, tmp_path)

    assert config.vehicle_behavior == "sumo_passive"
    assert config.n_workers == 2  # DEFAULT_N_WORKERS: the coarse pool
    assert config.refine_vehicle_behavior == "cnde"
    assert config.refine_n_workers == 4


def test_invalid_refine_n_workers_rejected_before_submit_optimization_starts_job(
    tmp_path: Path, monkeypatch
) -> None:
    """submit_optimization: the new knob rides the same validate-first path
    as the other four -- a client-visible 422 BEFORE any slot/job is created."""
    monkeypatch.setenv("TERASIM_CITY_REFINE_N_WORKERS", "0")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    output_root = tmp_path / "out"
    output_root.mkdir()
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    launched: list = []

    with pytest.raises(HTTPException) as excinfo:
        submit_optimization(
            {"tmc_json": tmc, "base_sumocfg": sumocfg},
            store,
            lambda *args: launched.append(args),
            output_root=output_root,
        )

    assert excinfo.value.status_code == 422
    assert "TERASIM_CITY_REFINE_N_WORKERS" in str(excinfo.value.detail)
    assert launched == []  # the worker was never launched
    # No slot was reserved either.
    assert store.try_acquire_slots("other", 3) is True


def test_invalid_refine_n_workers_rejected_via_http_submit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TERASIM_CITY_REFINE_N_WORKERS", "not-an-int")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    output_root = tmp_path / "out"
    output_root.mkdir()
    launched: list = []

    def launcher(run_id, request, out_dir):
        launched.append(run_id)

    app = FastAPI()
    app.include_router(
        create_v1_city_router(store, launcher, output_root=output_root)
    )
    client = TestClient(app)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")

    resp = client.post(
        "/v1/city/optimizations",
        json={"tmc_json": tmc, "base_sumocfg": sumocfg},
    )

    assert resp.status_code == 422
    assert "TERASIM_CITY_REFINE_N_WORKERS" in resp.json()["detail"]
    assert launched == []


def test_cnde_vehicle_behavior_with_default_n_workers_fails_at_config_build(
    tmp_path: Path, monkeypatch
) -> None:
    """TERASIM_CITY_VEHICLE_BEHAVIOR=cnde rides the
    request's default n_workers (2, > 1) straight into the coarse spawn pool
    unless inline evaluation is forced. Rides the same
    CorridorOptimizationConfig.__post_init__ funnel as the other three env
    vars, so this fails at config-build time too."""
    monkeypatch.delenv(INLINE_EVAL_ENV_VAR, raising=False)
    monkeypatch.setenv("TERASIM_CITY_VEHICLE_BEHAVIOR", "cnde")
    with pytest.raises(ValueError, match="n_workers"):
        _build_optimization_config(_REQUEST, tmp_path)


def test_cnde_vehicle_behavior_allowed_with_explicit_single_worker(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv(INLINE_EVAL_ENV_VAR, raising=False)
    monkeypatch.setenv("TERASIM_CITY_VEHICLE_BEHAVIOR", "cnde")
    request = dict(_REQUEST, n_workers=1)

    config = _build_optimization_config(request, tmp_path)

    assert config.vehicle_behavior == "cnde"
    assert config.n_workers == 1


def test_cnde_vehicle_behavior_multi_worker_allowed_with_inline_eval_env(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TERASIM_CITY_VEHICLE_BEHAVIOR", "cnde")
    monkeypatch.setenv(INLINE_EVAL_ENV_VAR, "1")

    config = _build_optimization_config(_REQUEST, tmp_path)

    assert config.vehicle_behavior == "cnde"
    assert config.n_workers == 2  # DEFAULT_N_WORKERS, unused once inline


def test_cnde_multi_worker_rejected_before_submit_optimization_starts_job(
    tmp_path: Path, monkeypatch
) -> None:
    """submit_optimization: client-visible 422 BEFORE any slot/job is created."""
    monkeypatch.delenv(INLINE_EVAL_ENV_VAR, raising=False)
    monkeypatch.setenv("TERASIM_CITY_VEHICLE_BEHAVIOR", "cnde")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    output_root = tmp_path / "out"
    output_root.mkdir()
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    launched: list = []

    with pytest.raises(HTTPException) as excinfo:
        submit_optimization(
            {"tmc_json": tmc, "base_sumocfg": sumocfg},
            store,
            lambda *args: launched.append(args),
            output_root=output_root,
        )

    assert excinfo.value.status_code == 422
    assert launched == []  # the worker was never launched
    # No slot was reserved either.
    assert store.try_acquire_slots("other", 3) is True


def test_cnde_multi_worker_rejected_via_http_submit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv(INLINE_EVAL_ENV_VAR, raising=False)
    monkeypatch.setenv("TERASIM_CITY_VEHICLE_BEHAVIOR", "cnde")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    output_root = tmp_path / "out"
    output_root.mkdir()
    launched: list = []

    def launcher(run_id, request, out_dir):
        launched.append(run_id)

    app = FastAPI()
    app.include_router(
        create_v1_city_router(store, launcher, output_root=output_root)
    )
    client = TestClient(app)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")

    resp = client.post(
        "/v1/city/optimizations",
        json={"tmc_json": tmc, "base_sumocfg": sumocfg},
    )

    assert resp.status_code == 422
    assert launched == []


def test_unregistered_vehicle_behavior_rejected_before_submit_optimization_starts_job(
    tmp_path: Path, monkeypatch
) -> None:
    """submit_optimization: client-visible 422 BEFORE any slot/job is created."""
    monkeypatch.setenv("TERASIM_CITY_VEHICLE_BEHAVIOR", "not_a_real_behavior")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    output_root = tmp_path / "out"
    output_root.mkdir()
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    launched: list = []

    with pytest.raises(HTTPException) as excinfo:
        submit_optimization(
            {"tmc_json": tmc, "base_sumocfg": sumocfg},
            store,
            lambda *args: launched.append(args),
            output_root=output_root,
        )

    assert excinfo.value.status_code == 422
    assert launched == []  # the worker was never launched
    # No slot was reserved either.
    assert store.try_acquire_slots("other", 3) is True


def test_unregistered_vehicle_behavior_rejected_via_http_submit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TERASIM_CITY_VEHICLE_BEHAVIOR", "not_a_real_behavior")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    output_root = tmp_path / "out"
    output_root.mkdir()
    launched: list = []

    def launcher(run_id, request, out_dir):
        launched.append(run_id)

    app = FastAPI()
    app.include_router(
        create_v1_city_router(store, launcher, output_root=output_root)
    )
    client = TestClient(app)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")

    resp = client.post(
        "/v1/city/optimizations",
        json={"tmc_json": tmc, "base_sumocfg": sumocfg},
    )

    assert resp.status_code == 422
    assert launched == []


def test_direct_optimization_rejects_unregistered_deployment_behavior(
    tmp_path: Path, monkeypatch
) -> None:
    """run_direct_optimization: the direct-path injection point also fails loud."""
    from terasim_service.city.ingest_api import InMemorySnapshotBuffer

    monkeypatch.setenv("TERASIM_CITY_VEHICLE_BEHAVIOR", "not_a_real_behavior")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    buffer = InMemorySnapshotBuffer(clock=lambda: 1751360400.0)
    buffer.append(
        "park_ave",
        {
            "corridor_id": "park_ave",
            "observation_interval": "10m",
            "timestamp_start": "2026-07-01T08:50:00Z",
            "intersections": [
                {
                    "intersection_id": "oak",
                    "name": "oak",
                    "lat": 37.335,
                    "lon": -121.90339,
                    "counts": {
                        direction: {turn: 1 for turn in ("Right", "Straight", "Left")}
                        for direction in ("South", "West", "North", "East")
                    },
                }
            ],
        },
    )
    sumocfg = tmp_path / "cached.sumocfg"
    sumocfg.write_text("<configuration/>", encoding="utf-8")
    reached_optimizer = []

    def acquire_map(tmc_json: Path) -> Path:
        return sumocfg

    def optimize(config, *, progress_callback):
        reached_optimizer.append(True)
        raise AssertionError("optimizer must never run for an invalid deployment config")

    app = FastAPI()
    app.include_router(
        create_v1_city_router(
            store,
            lambda *_args: None,
            output_root=tmp_path / "outputs",
            buffer=buffer,
            direct_optimizer=optimize,
            map_acquirer=acquire_map,
        )
    )
    client = TestClient(app)

    resp = client.post(
        "/v1/city/optimize",
        json={
            "type": "intersection",
            "corridor_id": "park_ave",
            "intersection": {
                "intersection_id": "oak",
                "lat": 37.335,
                "lon": -121.90339,
            },
        },
    )

    assert resp.status_code == 422
    assert reached_optimizer == []


def test_direct_optimization_validates_before_slot_acquisition_and_map_download(
    tmp_path: Path, monkeypatch
) -> None:
    """Deployment-config validation runs BEFORE
    store.try_acquire_slots and BEFORE map_acquirer — a misconfigured
    deployment must not burn a concurrency slot or trigger a (possibly
    expensive) OSM download before 422ing. Mirrors submit_optimization's
    documented ordering (validate -> then acquire slots -> then the worker's
    own map download; see its comment at the corresponding call site).
    """
    from terasim_service.city.ingest_api import InMemorySnapshotBuffer

    monkeypatch.setenv("TERASIM_CITY_VEHICLE_BEHAVIOR", "not_a_real_behavior")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    buffer = InMemorySnapshotBuffer(clock=lambda: 1751360400.0)
    buffer.append(
        "park_ave",
        {
            "corridor_id": "park_ave",
            "observation_interval": "10m",
            "timestamp_start": "2026-07-01T08:50:00Z",
            "intersections": [
                {
                    "intersection_id": "oak",
                    "name": "oak",
                    "lat": 37.335,
                    "lon": -121.90339,
                    "counts": {
                        direction: {turn: 1 for turn in ("Right", "Straight", "Left")}
                        for direction in ("South", "West", "North", "East")
                    },
                }
            ],
        },
    )

    slot_calls: list = []
    orig_try_acquire_slots = store.try_acquire_slots

    def spy_try_acquire_slots(*args, **kwargs):
        slot_calls.append((args, kwargs))
        return orig_try_acquire_slots(*args, **kwargs)

    store.try_acquire_slots = spy_try_acquire_slots

    map_calls: list = []

    def acquire_map(tmc_json: Path) -> Path:
        map_calls.append(tmc_json)
        raise AssertionError(
            "map must never be acquired before deployment-config validation"
        )

    def optimize(config, *, progress_callback):
        raise AssertionError(
            "optimizer must never run for an invalid deployment config"
        )

    app = FastAPI()
    app.include_router(
        create_v1_city_router(
            store,
            lambda *_args: None,
            output_root=tmp_path / "outputs",
            buffer=buffer,
            direct_optimizer=optimize,
            map_acquirer=acquire_map,
        )
    )
    client = TestClient(app)

    # No base_sumocfg anywhere in this request/config -> had validation come
    # later, map_acquirer would have been invoked (and a slot reserved)
    # before ever reaching config validation.
    resp = client.post(
        "/v1/city/optimize",
        json={
            "type": "intersection",
            "corridor_id": "park_ave",
            "intersection": {
                "intersection_id": "oak",
                "lat": 37.335,
                "lon": -121.90339,
            },
        },
    )

    assert resp.status_code == 422
    assert map_calls == []
    assert slot_calls == []
    # No slot was left held either: full capacity is still free.
    assert store.try_acquire_slots("other", 3) is True


# --- _direct_kpis: export_source branching ----------------------------------


_REFINE_RESULT = {
    "baseline": {
        "plan_id": "baseline",
        "score": 1.0,
        "moe": {"control_delay_s_per_veh": 40.0},
    },
    "best": {
        "plan_id": "bo_0003",
        "score": 0.7,
        "moe": {"control_delay_s_per_veh": 18.0},
    },
    "export_source": "refine",
    "refine": {
        "behavior": "cnde",
        "top_k": 2,
        "refine_max_collisions": None,
        "candidates_evaluated": 3,
        "executed": True,
        "baseline": {
            "plan_id": "baseline",
            "score": 1.0,
            "valid": True,
            "moe": {"control_delay_s_per_veh": 31.0, "stops_per_veh": 1.6},
            "collisions": 0,
            "emergency_stops": 1,
            "trial_dir": "/out/trials_refine/baseline",
        },
        "evaluations": [
            {
                "plan_id": "bo_0003",
                "score": 0.95,
                "valid": True,
                "moe": {"control_delay_s_per_veh": 29.0, "stops_per_veh": 1.4},
                "collisions": 0,
                "emergency_stops": 0,
                "trial_dir": "/out/trials_refine/bo_0003",
            },
            {
                "plan_id": "bo_0007",
                "score": 0.80,
                "valid": True,
                "moe": {"control_delay_s_per_veh": 25.0, "stops_per_veh": 1.1},
                "collisions": 0,
                "emergency_stops": 0,
                "trial_dir": "/out/trials_refine/bo_0007",
            },
        ],
        "winner_plan_id": "bo_0007",
        "invalid": [],
    },
}


def test_direct_kpis_sources_refine_winner_when_export_source_is_refine() -> None:
    kpis = _direct_kpis(_REFINE_RESULT, {"control_delay": 1.0})

    # Baseline comes from the REFINE-stage baseline, not the coarse one.
    assert kpis["baseline"]["cost"] == 1.0
    assert kpis["baseline"]["metrics"] == {
        "control_delay_s_per_veh": 31.0,
        "stops_per_veh": 1.6,
    }
    # Optimized comes from the refine winner (bo_0007), NOT result["best"]
    # (bo_0003/0.7), which is a different, coarse-stage plan.
    assert kpis["optimized"]["cost"] == 0.80
    assert kpis["optimized"]["metrics"] == {
        "control_delay_s_per_veh": 25.0,
        "stops_per_veh": 1.1,
    }


def test_direct_kpis_refine_winner_may_be_the_baseline_plan() -> None:
    result = {
        "baseline": {"plan_id": "baseline", "score": 1.0, "moe": {}},
        "best": {"plan_id": "bo_0003", "score": 0.7, "moe": {}},
        "export_source": "refine",
        "refine": {
            "baseline": {
                "plan_id": "baseline",
                "score": 0.5,
                "valid": True,
                "moe": {"control_delay_s_per_veh": 12.0},
                "collisions": 0,
                "emergency_stops": 0,
                "trial_dir": "x",
            },
            "evaluations": [
                {
                    "plan_id": "bo_0003",
                    "score": 0.9,
                    "valid": True,
                    "moe": {"control_delay_s_per_veh": 20.0},
                    "collisions": 3,
                    "emergency_stops": 2,
                    "trial_dir": "y",
                },
            ],
            "winner_plan_id": "baseline",
        },
    }

    kpis = _direct_kpis(result, {})

    assert kpis["baseline"]["cost"] == 0.5
    assert kpis["optimized"]["cost"] == 0.5
    assert kpis["optimized"]["metrics"] == {"control_delay_s_per_veh": 12.0}


def test_direct_kpis_legacy_sourcing_when_export_source_absent() -> None:
    result = {
        "baseline": {"plan_id": "baseline", "score": 1.0, "moe": {"a": 1}},
        "best": {"plan_id": "bo_0003", "score": 0.7, "moe": {"a": 2}},
    }

    kpis = _direct_kpis(result, {"w": 1.0})

    assert kpis["baseline"] == {"cost": 1.0, "metrics": {"a": 1}, "timing": None}
    assert kpis["optimized"] == {"cost": 0.7, "metrics": {"a": 2}, "timing": None}


def test_direct_kpis_legacy_sourcing_when_export_source_is_coarse() -> None:
    """Refine ran but fell back — export_source == "coarse" also stays legacy."""
    result = {
        "baseline": {"plan_id": "baseline", "score": 1.0, "moe": {"a": 1}},
        "best": {"plan_id": "bo_0003", "score": 0.7, "moe": {"a": 2}},
        "export_source": "coarse",
        "refine": _REFINE_RESULT["refine"],
    }

    kpis = _direct_kpis(result, {"w": 1.0})

    assert kpis["baseline"] == {"cost": 1.0, "metrics": {"a": 1}, "timing": None}
    assert kpis["optimized"] == {"cost": 0.7, "metrics": {"a": 2}, "timing": None}


# --- worker completion metadata: export_source branching -------------------


def _store_with_slots(run_id: str, n: int = 2) -> InMemoryCityJobStore:
    store = InMemoryCityJobStore(max_concurrency=4, lease_ttl=100)
    store.create(run_id)
    assert store.try_acquire_slots(run_id, n) is True
    return store


def test_worker_completion_sources_refine_winner_when_export_source_is_refine(
    tmp_path: Path,
) -> None:
    from terasim_service.city.job_store import COMPLETED
    from terasim_service.city.worker_optimize import run_city_optimization_job

    store = _store_with_slots("r1", 2)

    def fake_opt(config, progress_callback):
        out = Path(config.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        return _REFINE_RESULT

    run_city_optimization_job(store, _REQUEST, "r1", tmp_path, opt_fn=fake_opt)

    rec = store.get("r1")
    assert rec.status == COMPLETED
    # bo_0007 (the refine winner) — NOT result["best"] (bo_0003/0.7).
    assert rec.best == {"plan_id": "bo_0007", "score": 0.80}
    assert rec.best_score == pytest.approx(0.80)


def test_worker_completion_uses_best_when_export_source_absent(
    tmp_path: Path,
) -> None:
    from terasim_service.city.job_store import COMPLETED
    from terasim_service.city.worker_optimize import run_city_optimization_job

    store = _store_with_slots("r1", 2)
    result = {"best": {"plan_id": "bo_0002", "score": 0.9}}

    def fake_opt(config, progress_callback):
        out = Path(config.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        return result

    run_city_optimization_job(store, _REQUEST, "r1", tmp_path, opt_fn=fake_opt)

    rec = store.get("r1")
    assert rec.status == COMPLETED
    assert rec.best == {"plan_id": "bo_0002", "score": 0.9}
    assert rec.best_score == pytest.approx(0.9)


def test_worker_completion_fails_loud_when_refine_winner_plan_id_unmatched(
    tmp_path: Path,
) -> None:
    """A winner_plan_id that matches nothing in the refine
    block's baseline/evaluations entries is a contract violation, not a
    silent-fallback-to-result["best"] opportunity — _exported_summary
    raises (mirroring optimize_api._direct_kpis's identical failure mode),
    so the job fails loud instead of quietly reporting the coarse plan's
    score as if it were the actually-exported refine winner's."""
    from terasim_service.city.job_store import FAILED
    from terasim_service.city.worker_optimize import run_city_optimization_job

    store = _store_with_slots("r1", 2)
    result = {
        "baseline": {"plan_id": "baseline", "score": 1.0, "moe": {}},
        "best": {"plan_id": "bo_0003", "score": 0.7, "moe": {}},
        "export_source": "refine",
        "refine": {
            "baseline": {"plan_id": "baseline", "score": 0.5, "moe": {}},
            "evaluations": [{"plan_id": "bo_0003", "score": 0.9, "moe": {}}],
            "winner_plan_id": "no_such_plan_id",
        },
    }

    def fake_opt(config, progress_callback):
        out = Path(config.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        return result

    run_city_optimization_job(store, _REQUEST, "r1", tmp_path, opt_fn=fake_opt)

    rec = store.get("r1")
    assert rec.status == FAILED
    assert "no_such_plan_id" in (rec.error or "")
