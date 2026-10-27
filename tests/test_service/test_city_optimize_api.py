"""Endpoint tests for the v1 corridor-optimization API.

FastAPI TestClient + in-memory store + inline launcher (no Redis, no SUMO, no
subprocess). The inline launcher runs ``run_city_optimization_job``
synchronously inside the POST with a stubbed optimization function, so by the
time submit returns the job has already finished.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import (
    build_v1_production_router,
    create_v1_city_router,
)
from terasim_service.city.worker_optimize import run_city_optimization_job
from terasim_service.constants import (
    ENVIRONMENT_VARIABLE_CITY_LEASE_TTL,
    ENVIRONMENT_VARIABLE_CITY_MAX_CONCURRENCY,
    ENVIRONMENT_VARIABLE_CITY_OUTPUT_ROOT,
)

_BASE = "/v1/city/optimizations"

_RESULT = {
    "schema": "terasim.corridor_optimization.v1",
    "resolved_corridor": [
        {
            "ref": "i1",
            "intersection_id": "i1",
            "lat": 37.335,
            "lon": -121.9,
            "node_id": "J1",
            "tls_id": "J1",
            "match_distance_m": 0.0,
        }
    ],
    "baseline": {"plan_id": "baseline", "score": 1.0, "moe": {}},
    "best": {
        "plan_id": "bo_0003",
        "score": 0.91,
        "moe": {},
        "offsets": {"J1": 0.0, "J2": 12.0},
        "source": "bo_iteration",
        "trial_dir": "/out/trials/trial_0004",
    },
    "trials": [],
    "nema": {"path": "/out/nema_plan.json", "handoff_ready": True},
    "config": {},
    "n_trials": 5,
}
_NEMA = {
    "schema": "terasim.nema_timing.v1",
    "corridor_id": "demo_arterial",
    "handoff_ready": True,
    "intersections": [],
}


def _write_inputs(folder: Path, *, net_ref: str = "osm.net.xml"):
    """Minimal tmc.json + sumocfg (+ referenced net) for path validation."""
    folder.mkdir(parents=True, exist_ok=True)
    tmc = folder / "tmc.json"
    tmc.write_text("{}", encoding="utf-8")
    (folder / "osm.net.xml").write_text("<net/>", encoding="utf-8")
    sumocfg = folder / "osm.sumocfg"
    sumocfg.write_text(
        f'<configuration><input><net-file value="{net_ref}"/></input></configuration>',
        encoding="utf-8",
    )
    return str(tmc), str(sumocfg)


def _completing_launcher(store, captured=None):
    """Inline launcher whose stubbed optimizer writes result + nema files."""

    def launch(run_id, request, output_dir):
        if captured is not None:
            captured.append(request)

        def fake_opt(config, progress_callback):
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            progress_callback(0, 5, None)
            # Mirror the real contract: ledger record BEFORE its callback.
            (out / "trials.jsonl").write_text(
                json.dumps(
                    {"trial": 0, "plan_id": "baseline", "score": 1.0, "valid": True}
                )
                + "\n",
                encoding="utf-8",
            )
            progress_callback(1, 5, "baseline")
            (out / "corridor_optimization_result.json").write_text(
                json.dumps(_RESULT), encoding="utf-8"
            )
            (out / "nema_plan.json").write_text(json.dumps(_NEMA), encoding="utf-8")
            return _RESULT

        run_city_optimization_job(store, request, run_id, output_dir, opt_fn=fake_opt)

    return launch


_SKIPPED_EXPORT = {
    "exported": False,
    "reason": "NEMA export skipped: plan 'scenario_programs' has no common cycle "
    "(J1=66s, J2=46s)",
}


def _skipped_export_launcher(store):
    """Inline launcher whose optimizer finishes but writes no nema_plan.json."""

    def launch(run_id, request, output_dir):
        def fake_opt(config, progress_callback):
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            result = {
                **_RESULT,
                "nema": {"path": str(out / "nema_plan.json"), "handoff_ready": False},
                "nema_export": _SKIPPED_EXPORT,
            }
            (out / "corridor_optimization_result.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            return result

        run_city_optimization_job(store, request, run_id, output_dir, opt_fn=fake_opt)

    return launch


def _failing_launcher(store):
    def launch(run_id, request, output_dir):
        def boom(config, progress_callback):
            raise RuntimeError("kaboom")

        run_city_optimization_job(store, request, run_id, output_dir, opt_fn=boom)

    return launch


def _noop_launcher(store):
    # Holds the slots (never runs/releases) — simulates a job still running.
    def launch(run_id, request, output_dir):
        return None

    return launch


def _client(tmp_path, *, launcher, max_concurrency=3, input_root=None):
    store = InMemoryCityJobStore(max_concurrency=max_concurrency, lease_ttl=100)
    output_root = tmp_path / "out"
    output_root.mkdir(exist_ok=True)
    app = FastAPI()
    app.include_router(
        create_v1_city_router(
            store,
            launcher(store),
            output_root=output_root,
            input_root=input_root,
        )
    )
    return TestClient(app), store, output_root


def test_lifecycle_submit_status_result_nema(tmp_path: Path) -> None:
    client, _store, _out = _client(tmp_path, launcher=_completing_launcher)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")

    resp = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "submitted"
    run_id = body["run_id"]

    status = client.get(f"{_BASE}/{run_id}/status")
    assert status.status_code == 200
    payload = status.json()
    assert set(payload) == {"run_id", "status", "progress", "best_score", "updated_at"}
    assert payload["status"] == "completed"
    assert payload["progress"] == {"done": 1, "total": 5}
    # 0.91 comes from the result's best (the final set_best_score call).
    assert payload["best_score"] == pytest.approx(0.91)
    assert isinstance(payload["updated_at"], str) and payload["updated_at"].endswith("Z")

    result = client.get(f"{_BASE}/{run_id}/result")
    assert result.status_code == 200
    assert result.json() == _RESULT

    nema = client.get(f"{_BASE}/{run_id}/nema")
    assert nema.status_code == 200
    assert nema.json() == _NEMA


def test_submit_reserves_n_workers_slots_429(tmp_path: Path) -> None:
    client, _store, _out = _client(
        tmp_path, launcher=_noop_launcher, max_concurrency=3
    )
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    body = {"tmc_json": tmc, "base_sumocfg": sumocfg, "n_workers": 2}

    first = client.post(_BASE, json=body)
    assert first.status_code == 202  # holds 2 of 3 slots
    second = client.post(_BASE, json=body)
    assert second.status_code == 429  # 2 + 2 > 3

    # A single-slot request still fits in the remaining slot.
    third = client.post(
        _BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg, "n_workers": 1}
    )
    assert third.status_code == 202


def test_submit_n_workers_above_the_deployment_cap_is_422_not_429(tmp_path: Path) -> None:
    """A request that can never get its slots is a configuration error, not a busy service."""
    client, store, _out = _client(tmp_path, launcher=_noop_launcher, max_concurrency=3)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")

    resp = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg, "n_workers": 4})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "TERASIM_CITY_MAX_CONCURRENCY=3" in detail
    assert "n_workers=4" in detail
    assert "retry" not in detail
    assert store.try_acquire_slots("other", 3) is True  # nothing was reserved
    # While those slots are held, a request that fits the cap is busy, not invalid.
    busy = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg, "n_workers": 3})
    assert busy.status_code == 429
    store.release_slot("other")
    # Exactly at the cap the request is accepted.
    ok = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg, "n_workers": 3})
    assert ok.status_code == 202


def test_scheduler_style_submission_above_the_default_cap_is_422(tmp_path: Path) -> None:
    """The scheduler calls submit_optimization directly with its entry as a dict."""
    from fastapi import HTTPException

    from terasim_service.city.common import DEFAULT_MAX_CONCURRENCY
    from terasim_service.city.optimize_api import submit_optimization

    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    store = InMemoryCityJobStore(max_concurrency=DEFAULT_MAX_CONCURRENCY, lease_ttl=100)
    launched: list = []
    with pytest.raises(HTTPException) as caught:
        submit_optimization(
            {
                "tmc_json": tmc,
                "base_sumocfg": sumocfg,
                "n_workers": DEFAULT_MAX_CONCURRENCY + 1,
                "optimizer": "bayes_corridor",
                "stage_mode": "per_approach",
                "search_cycle": True,
            },
            store,
            lambda *args: launched.append(args),
            output_root=tmp_path / "out",
        )
    assert caught.value.status_code == 422
    assert f"TERASIM_CITY_MAX_CONCURRENCY={DEFAULT_MAX_CONCURRENCY}" in caught.value.detail
    assert f"n_workers={DEFAULT_MAX_CONCURRENCY + 1}" in caught.value.detail
    assert launched == []


def test_result_and_nema_409_until_completed(tmp_path: Path) -> None:
    client, store, _out = _client(tmp_path, launcher=_noop_launcher)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    run_id = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg}).json()[
        "run_id"
    ]

    assert client.get(f"{_BASE}/{run_id}/result").status_code == 409
    assert client.get(f"{_BASE}/{run_id}/nema").status_code == 409

    # Completed but the nema file is missing -> 410 (result file too).
    store.set_completed(run_id, "/unused", {"plan_id": "p", "score": 1.0})
    assert client.get(f"{_BASE}/{run_id}/result").status_code == 410
    assert client.get(f"{_BASE}/{run_id}/nema").status_code == 410


def test_run_that_skipped_the_nema_export_completes_and_nema_is_409_with_the_reason(
    tmp_path: Path,
) -> None:
    client, _store, _out = _client(tmp_path, launcher=_skipped_export_launcher)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    run_id = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg}).json()[
        "run_id"
    ]

    # The optimization itself succeeded: completed, with its result ...
    assert client.get(f"{_BASE}/{run_id}/status").json()["status"] == "completed"
    result = client.get(f"{_BASE}/{run_id}/result")
    assert result.status_code == 200
    assert result.json()["nema_export"] == _SKIPPED_EXPORT
    # ... and the missing NEMA plan is explained, not reported as a vanished file.
    nema = client.get(f"{_BASE}/{run_id}/nema")
    assert nema.status_code == 409
    assert nema.json()["detail"] == _SKIPPED_EXPORT["reason"]


def test_failed_run_status_and_result_409(tmp_path: Path) -> None:
    client, _store, _out = _client(tmp_path, launcher=_failing_launcher)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    run_id = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg}).json()[
        "run_id"
    ]

    status = client.get(f"{_BASE}/{run_id}/status").json()
    assert status["status"] == "failed"
    assert status["best_score"] is None

    result = client.get(f"{_BASE}/{run_id}/result")
    assert result.status_code == 409
    assert "kaboom" in result.json()["detail"]


def test_unknown_run_id_404(tmp_path: Path) -> None:
    client, _store, _out = _client(tmp_path, launcher=_completing_launcher)
    assert client.get(f"{_BASE}/nope/status").status_code == 404
    assert client.get(f"{_BASE}/nope/result").status_code == 404
    assert client.get(f"{_BASE}/nope/nema").status_code == 404


def test_extra_field_rejected_422(tmp_path: Path) -> None:
    client, _store, _out = _client(tmp_path, launcher=_completing_launcher)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    resp = client.post(
        _BASE,
        json={"tmc_json": tmc, "base_sumocfg": sumocfg, "totally_unknown": True},
    )
    assert resp.status_code == 422


def test_corridor_extra_field_rejected_422(tmp_path: Path) -> None:
    client, _store, _out = _client(tmp_path, launcher=_completing_launcher)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    resp = client.post(
        _BASE,
        json={
            "tmc_json": tmc,
            "base_sumocfg": sumocfg,
            "corridor": {"from": "i1", "to": "i3", "bogus": 1},
        },
    )
    assert resp.status_code == 422


def test_corridor_from_to_aliases_accepted(tmp_path: Path) -> None:
    captured: list[dict] = []
    client, _store, _out = _client(
        tmp_path, launcher=lambda store: _completing_launcher(store, captured)
    )
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    resp = client.post(
        _BASE,
        json={
            "tmc_json": tmc,
            "base_sumocfg": sumocfg,
            "corridor": {"from": "i1", "to": "i3"},
        },
    )
    assert resp.status_code == 202
    # The launcher receives the model_dump: field names, values from the aliases.
    corridor = captured[0]["corridor"]
    assert corridor["from_ref"] == "i1"
    assert corridor["to_ref"] == "i3"
    assert corridor["tls_ids"] == []


def test_missing_input_files_400(tmp_path: Path) -> None:
    client, _store, _out = _client(tmp_path, launcher=_completing_launcher)
    resp = client.post(
        _BASE,
        json={"tmc_json": str(tmp_path / "nope.json"), "base_sumocfg": "missing"},
    )
    assert resp.status_code == 400


def test_input_root_confinement_400(tmp_path: Path) -> None:
    input_root = tmp_path / "allowed"
    input_root.mkdir()
    client, _store, _out = _client(
        tmp_path, launcher=_completing_launcher, input_root=input_root
    )
    tmc, sumocfg = _write_inputs(tmp_path / "elsewhere")
    resp = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg})
    assert resp.status_code == 400


def test_launch_failure_releases_slots_500(tmp_path: Path) -> None:
    def exploding_launcher(store):
        def launch(run_id, request, output_dir):
            raise RuntimeError("spawn failed")

        return launch

    client, store, _out = _client(
        tmp_path, launcher=exploding_launcher, max_concurrency=2
    )
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    resp = client.post(
        _BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg, "n_workers": 2}
    )
    assert resp.status_code == 500
    # Both slots must be back.
    assert store.try_acquire_slots("other", 2) is True


# --- direct (scheduler) submission path -----------------------------------------


def test_submit_optimization_accepts_a_plain_dict(tmp_path: Path) -> None:
    # The scheduler submits via a direct function call with a request dict
    # (yaml-derived extras + the server-generated aggregated tmc path).
    from terasim_service.city.optimize_api import submit_optimization

    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    output_root = tmp_path / "out"
    output_root.mkdir()
    captured: list[dict] = []
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")

    run_id = submit_optimization(
        {
            "tmc_json": tmc,
            "base_sumocfg": sumocfg,
            "corridor": {"tls_ids": ["J1", "J2", "J3"]},
            "sim_end": 300,
            "max_trials": 6,
            "n_workers": 2,
        },
        store,
        lambda rid, request, out: captured.append(request),
        output_root=output_root,
        input_root=None,
    )

    assert store.get(run_id).status == "submitted"
    request = captured[0]
    assert request["corridor"]["tls_ids"] == ["J1", "J2", "J3"]
    assert request["sim_end"] == 300
    assert request["max_trials"] == 6
    # Two slots are held for the launched job.
    assert store.try_acquire_slots("other", 2) is False
    assert store.try_acquire_slots("other", 1) is True


def test_submit_optimization_dict_with_unknown_key_422(tmp_path: Path) -> None:
    from fastapi import HTTPException

    from terasim_service.city.optimize_api import submit_optimization

    store = InMemoryCityJobStore()
    output_root = tmp_path / "out"
    output_root.mkdir()
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")

    with pytest.raises(HTTPException) as excinfo:
        submit_optimization(
            {"tmc_json": tmc, "base_sumocfg": sumocfg, "bogus_key": "leak"},
            store,
            lambda *a: None,
            output_root=output_root,
        )
    assert excinfo.value.status_code == 422


# --- production wiring (no Redis connection is made) ---------------------------


def test_create_app_city_enabled_mounts_v1_and_snapshots(
    tmp_path: Path, monkeypatch
) -> None:
    import terasim_service.api as api
    from terasim_service.constants import (
        ENVIRONMENT_VARIABLE_CITY_SCHEDULER_ENABLED,
        ENVIRONMENT_VARIABLE_ENABLE_CITY_API,
    )

    monkeypatch.setattr(api, "check_redis_connection", lambda: None)
    monkeypatch.setattr(api, "_city_router_mounted", False)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_ENABLE_CITY_API, "true")
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_CITY_OUTPUT_ROOT, str(tmp_path))
    monkeypatch.delenv(ENVIRONMENT_VARIABLE_CITY_MAX_CONCURRENCY, raising=False)
    monkeypatch.delenv(ENVIRONMENT_VARIABLE_CITY_LEASE_TTL, raising=False)
    monkeypatch.delenv(ENVIRONMENT_VARIABLE_CITY_SCHEDULER_ENABLED, raising=False)

    app = api.create_app()
    paths = _route_paths(app)
    # The legacy single-intersection surface was retired; only /v1 remains.
    assert "/city/signal-evaluations" not in paths
    assert "/v1/city/optimize" in paths
    assert "/v1/city/optimizations" in paths
    assert "/v1/city/optimizations/{run_id}/nema" in paths
    assert "/v1/city/snapshots" in paths
    assert "/v1/city/snapshots/{corridor_id}" in paths
    assert "/v1/city/scheduled/{corridor_id}/latest" in paths


def _route_paths(app) -> set[str]:
    """Every route path on ``app``. FastAPI >= 0.141 keeps an included router
    nested (``_IncludedRouter.original_router``) instead of flattening it into
    ``app.routes``, so the walk descends into those."""
    found: set[str] = set()

    def walk(routes) -> None:
        for route in routes:
            path = getattr(route, "path", None)
            if isinstance(path, str) and path:
                found.add(path)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner.routes)
            elif isinstance(getattr(route, "routes", None), list):
                walk(route.routes)

    walk(app.routes)
    return found


def test_map_reference_escape_400(tmp_path: Path) -> None:
    """Inputs INSIDE the root, but the sumocfg's net-file points outside."""
    input_root = tmp_path / "allowed"
    input_root.mkdir()
    (tmp_path / "escape.net.xml").write_text("<net/>", encoding="utf-8")
    client, _store, _out = _client(
        tmp_path, launcher=_completing_launcher, input_root=input_root
    )
    tmc, sumocfg = _write_inputs(input_root, net_ref="../escape.net.xml")
    resp = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg})
    assert resp.status_code == 400
    assert "outside the allowed root" in resp.json()["detail"]


def _sumocfg_with_routes(folder: Path, route_ref: str) -> str:
    sumocfg = folder / "routes.sumocfg"
    sumocfg.write_text(
        '<configuration><input><net-file value="osm.net.xml"/>'
        f'<route-files value="{route_ref}"/></input></configuration>',
        encoding="utf-8",
    )
    return str(sumocfg)


def test_route_file_escape_400(tmp_path: Path) -> None:
    """The scenario-route sources read and copy the sumocfg's route files: confined too."""
    input_root = tmp_path / "allowed"
    input_root.mkdir()
    (tmp_path / "escape.rou.xml").write_text("<routes/>", encoding="utf-8")
    client, _store, _out = _client(tmp_path, launcher=_completing_launcher, input_root=input_root)
    tmc, _sumocfg = _write_inputs(input_root)
    sumocfg = _sumocfg_with_routes(input_root, "inside.rou.xml,../escape.rou.xml")
    (input_root / "inside.rou.xml").write_text("<routes/>", encoding="utf-8")

    resp = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg})

    assert resp.status_code == 400
    assert "outside the allowed root" in resp.json()["detail"]
    assert "escape.rou.xml" in resp.json()["detail"]


def test_route_files_inside_the_root_are_accepted(tmp_path: Path) -> None:
    input_root = tmp_path / "allowed"
    input_root.mkdir()
    client, _store, _out = _client(tmp_path, launcher=_completing_launcher, input_root=input_root)
    tmc, _sumocfg = _write_inputs(input_root)
    (input_root / "inside.rou.xml").write_text("<routes/>", encoding="utf-8")
    sumocfg = _sumocfg_with_routes(input_root, "inside.rou.xml")

    resp = client.post(_BASE, json={"tmc_json": tmc, "base_sumocfg": sumocfg})

    assert resp.status_code == 202, resp.text


def test_store_config_env_minimums_fail_loud(monkeypatch) -> None:
    from terasim_service.city.common import store_config_from_env

    monkeypatch.setenv(ENVIRONMENT_VARIABLE_CITY_MAX_CONCURRENCY, "0")
    with pytest.raises(ValueError, match="must be >= 1"):
        store_config_from_env()
    monkeypatch.delenv(ENVIRONMENT_VARIABLE_CITY_MAX_CONCURRENCY, raising=False)

    monkeypatch.setenv(ENVIRONMENT_VARIABLE_CITY_LEASE_TTL, "5")
    with pytest.raises(ValueError, match="must be >= 31"):
        store_config_from_env()


def test_create_app_succeeds_without_city_by_default(tmp_path: Path, monkeypatch) -> None:
    import terasim_service.api as api
    from terasim_service.constants import ENVIRONMENT_VARIABLE_ENABLE_CITY_API

    monkeypatch.setattr(api, "check_redis_connection", lambda: None)
    monkeypatch.setattr(api, "_city_router_mounted", False)
    monkeypatch.delenv(ENVIRONMENT_VARIABLE_ENABLE_CITY_API, raising=False)
    monkeypatch.delenv(ENVIRONMENT_VARIABLE_CITY_OUTPUT_ROOT, raising=False)
    # City API is opt-in: create_app must succeed with NO city env set. (It
    # would RuntimeError if it tried to build city routers without
    # OUTPUT_ROOT.) NOTE: api.app is a module-level singleton, so routes
    # mounted by OTHER tests may already be present — do not assert on them.
    assert api.create_app() is api.app


def test_create_app_city_enabled_without_output_root_raises(monkeypatch) -> None:
    import terasim_service.api as api
    from terasim_service.constants import ENVIRONMENT_VARIABLE_ENABLE_CITY_API

    monkeypatch.setattr(api, "check_redis_connection", lambda: None)
    monkeypatch.setattr(api, "_city_router_mounted", False)
    monkeypatch.setenv(ENVIRONMENT_VARIABLE_ENABLE_CITY_API, "true")
    monkeypatch.delenv(ENVIRONMENT_VARIABLE_CITY_OUTPUT_ROOT, raising=False)
    with pytest.raises(RuntimeError, match="OUTPUT_ROOT"):
        api.create_app()


# --- stage / cycle / scenario-count switches -----------------------------------

_SWITCHES = (
    "stage_mode",
    "search_cycle",
    "cycle_min_s",
    "cycle_max_s",
    "flow_source",
    "counts_source",
    "seed_from_scenario_programs",
)


def test_request_accepts_new_switches():
    from terasim_service.city.optimize_api import OptimizationRequest

    req = OptimizationRequest(
        tmc_json="/x/t.json",
        base_sumocfg="/x/b.sumocfg",
        corridor={"tls_ids": ["n34"]},
        optimizer="bayes_corridor",
        stage_mode="per_approach",
        search_cycle=True,
        cycle_min_s=45,
        cycle_max_s=240,
        flow_source="scenario_routes",
        counts_source="scenario_routes",
        seed_from_scenario_programs=True,
    )
    assert req.optimizer == "bayes_corridor" and req.stage_mode == "per_approach"
    dumped = req.model_dump(exclude_unset=True, exclude_none=True)
    assert dumped["counts_source"] == "scenario_routes" and dumped["search_cycle"] is True


def test_request_rejects_unknown_stage_mode():
    from pydantic import ValidationError

    from terasim_service.city.optimize_api import OptimizationRequest

    with pytest.raises(ValidationError) as excinfo:
        OptimizationRequest(tmc_json="/x/t.json", stage_mode="five")
    # rejected by the field's own choices, not just as an unknown field
    assert [error["type"] for error in excinfo.value.errors()] == ["literal_error"]


def test_request_new_switches_default_to_none():
    # None means "server default": that is what lets a scheduled-config entry supply them.
    from terasim_service.city.optimize_api import OptimizationRequest

    req = OptimizationRequest(tmc_json="/x/t.json")
    assert [getattr(req, name) for name in _SWITCHES] == [None] * len(_SWITCHES)
    assert req.optimizer == "auto"


def test_inconsistent_cycle_bounds_are_a_submission_time_422(tmp_path: Path) -> None:
    client, store, _out = _client(tmp_path, launcher=_noop_launcher)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    resp = client.post(
        _BASE,
        json={"tmc_json": tmc, "base_sumocfg": sumocfg, "cycle_min_s": 200, "cycle_max_s": 100},
    )
    assert resp.status_code == 422
    assert "cycle_min_s" in resp.json()["detail"]
    assert store.try_acquire_slots("other", 3) is True  # no slot was reserved for it


def _scenario_counts_corridors(sumocfg: str, **entry_extra) -> dict:
    """A scheduled-config entry whose counts come from the scenario's own routes."""
    return {
        "c1": {
            "corridor_id": "c1",
            "base_sumocfg": sumocfg,
            "corridor": {"tls_ids": ["J1"]},
            "counts_source": "scenario_routes",
            "flow_source": "scenario_routes",
            **entry_extra,
        }
    }


def _submit_corridor_id(tmp_path: Path, request: dict, *, corridors: dict, buffer=None):
    """submit_optimization for a corridor_id request; returns (run_id, launched, output_root)."""
    from terasim_service.city.optimize_api import submit_optimization

    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    output_root = tmp_path / "out"
    output_root.mkdir(exist_ok=True)
    launched: list[dict] = []
    run_id = submit_optimization(
        request,
        store,
        lambda rid, forwarded, out: launched.append(forwarded),
        output_root=output_root,
        input_root=(tmp_path / "inputs").resolve(),
        buffer=buffer,
        corridors=corridors,
        window_s=3600,
    )
    return run_id, launched, output_root


@pytest.mark.parametrize("buffer_wired", [True, False])
def test_corridor_id_with_scenario_counts_skips_empty_buffer(tmp_path, buffer_wired):
    """A scheduled-config entry with counts_source=scenario_routes needs no snapshots."""
    from terasim_city.flow.descriptors import TMCData
    from terasim_service.city.ingest_api import InMemorySnapshotBuffer

    _tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    run_id, launched, output_root = _submit_corridor_id(
        tmp_path,
        {"corridor_id": "c1"},
        corridors=_scenario_counts_corridors(sumocfg),
        # an empty queue, or a deployment with no snapshot buffer at all
        buffer=InMemorySnapshotBuffer() if buffer_wired else None,
    )
    assert run_id and launched
    placeholder = Path(launched[0]["tmc_json"])
    assert placeholder == output_root / run_id / "inputs" / "aggregated_tmc.json"
    payload = json.loads(placeholder.read_text())
    assert payload["corridor_id"] == "c1"
    assert datetime.fromisoformat(payload["timestamp_start"]).tzinfo is not None
    assert TMCData.from_json(placeholder).corridor_id == "c1"  # still a well-formed TMC file
    assert launched[0]["counts_source"] == "scenario_routes"
    assert launched[0]["flow_source"] == "scenario_routes"


def test_corridor_id_request_switches_win_over_the_entry(tmp_path):
    """The entry supplies the defaults; a switch set on the request wins."""
    _tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    _run_id, launched, _out = _submit_corridor_id(
        tmp_path,
        {"corridor_id": "c1", "search_cycle": False},
        corridors=_scenario_counts_corridors(
            sumocfg,
            optimizer="bayes_corridor",
            stage_mode="per_approach",
            search_cycle=True,
            cycle_max_s=240,
        ),
    )
    forwarded = launched[0]
    assert forwarded["optimizer"] == "bayes_corridor"  # entry; the request left it unset
    assert forwarded["stage_mode"] == "per_approach"  # entry
    assert forwarded["cycle_max_s"] == 240.0  # entry
    assert forwarded["search_cycle"] is False  # the request beats the entry's True
    assert forwarded["seed_from_scenario_programs"] is None  # nobody set it: server default


def test_corridor_id_request_counts_source_beats_the_entry(tmp_path):
    """The placeholder decision uses the MERGED counts source: a request that asks for
    tmc counts needs snapshots even though its entry says scenario_routes."""
    from fastapi import HTTPException

    from terasim_service.city.ingest_api import InMemorySnapshotBuffer

    _tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    with pytest.raises(HTTPException) as excinfo:
        _submit_corridor_id(
            tmp_path,
            {"corridor_id": "c1", "counts_source": "tmc"},
            corridors=_scenario_counts_corridors(sumocfg),
            buffer=InMemorySnapshotBuffer(),
        )
    assert excinfo.value.status_code == 409
    assert "no recent traffic data buffered" in excinfo.value.detail


def test_direct_kpis_carry_timing():
    from terasim_service.city.optimize_api import _direct_kpis

    result = {
        "baseline": {
            "score": 1.0,
            "moe": {"control_delay_s_per_veh": 10},
            "timing": {"cycle_s": {"J1": 90.0}},
        },
        "best": {
            "score": 0.9,
            "moe": {"control_delay_s_per_veh": 9},
            "timing": {"cycle_s": {"J1": 100.0}},
        },
    }
    kpis = _direct_kpis(result, {"control_delay": 0.7, "stops": 0.3})
    assert kpis["baseline"]["timing"]["cycle_s"]["J1"] == 90.0
    assert kpis["optimized"]["timing"]["cycle_s"]["J1"] == 100.0
    no_timing = {"baseline": {"score": 1, "moe": {}}, "best": {"score": 1, "moe": {}}}
    assert _direct_kpis(no_timing, {})["optimized"]["timing"] is None


def test_direct_kpis_refine_branch_reports_no_timing():
    # The refine stage re-measures plans under another behavior and records no timing on
    # its entries; the coarse blocks' timing describes the coarse best plan, which need
    # not be the refine winner, so it is not borrowed.
    from terasim_service.city.optimize_api import _direct_kpis

    result = {
        "baseline": {"score": 1.0, "moe": {}, "timing": {"cycle_s": {"J1": 90.0}}},
        "best": {"score": 0.7, "moe": {}, "timing": {"cycle_s": {"J1": 100.0}}},
        "export_source": "refine",
        "refine": {
            "baseline": {"plan_id": "baseline", "score": 0.5, "moe": {"a": 1}},
            "evaluations": [{"plan_id": "bo_0001", "score": 0.4, "moe": {"a": 2}}],
            "winner_plan_id": "bo_0001",
        },
    }
    kpis = _direct_kpis(result, {})
    assert (kpis["baseline"]["cost"], kpis["optimized"]["cost"]) == (0.5, 0.4)
    assert kpis["baseline"]["timing"] is None and kpis["optimized"]["timing"] is None


_TIMING_RESULT = {
    "baseline": {
        "plan_id": "baseline",
        "score": 1.0,
        "moe": {"control_delay_s_per_veh": 30.0},
        "timing": {
            "cycle_s": {"J1": 90.0},
            "offsets_s": {"J1": 0.0},
            "greens_s": {"J1": [40.0, 30.0]},
        },
    },
    "best": {
        "plan_id": "bo_0001",
        "score": 0.8,
        "moe": {"control_delay_s_per_veh": 22.0},
        "timing": {
            "cycle_s": {"J1": 100.0},
            "offsets_s": {"J1": 0.0},
            "greens_s": {"J1": [45.0, 35.0]},
        },
    },
}


def _direct_client(tmp_path: Path, result: dict) -> TestClient:
    """POST /v1/city/optimize backed by a stub optimizer that returns ``result``."""
    from terasim_service.city.ingest_api import InMemorySnapshotBuffer

    now = datetime.fromisoformat("2026-07-01T09:00:00+00:00").timestamp()
    counts = {
        direction: {turn: 1 for turn in ("Right", "Straight", "Left")}
        for direction in ("South", "West", "North", "East")
    }
    buffer = InMemorySnapshotBuffer(clock=lambda: now)
    buffer.append(
        "c1",
        {
            "corridor_id": "c1",
            "observation_interval": "10m",
            "timestamp_start": "2026-07-01T08:50:00Z",
            "intersections": [{**_DIRECT_INTERSECTION, "counts": counts}],
        },
    )
    sumocfg = tmp_path / "cached.sumocfg"
    sumocfg.write_text("<configuration/>", encoding="utf-8")

    def optimize(config, *, progress_callback):
        config.output_dir.mkdir(parents=True, exist_ok=True)
        (config.output_dir / "nema_plan.json").write_text(json.dumps(_NEMA), encoding="utf-8")
        return result

    app = FastAPI()
    app.include_router(
        create_v1_city_router(
            InMemoryCityJobStore(max_concurrency=3, lease_ttl=100),
            lambda *_args: None,
            output_root=tmp_path / "outputs",
            buffer=buffer,
            direct_optimizer=optimize,
            map_acquirer=lambda _tmc: sumocfg,
        )
    )
    return TestClient(app)


_DIRECT_INTERSECTION = {"intersection_id": "i1", "name": "A & B", "lat": 37.3, "lon": -121.9}


@pytest.mark.parametrize("with_timing", [True, False])
def test_optimize_endpoint_carries_timing_only_when_the_result_has_it(tmp_path, with_timing):
    """POST /v1/city/optimize answers with each plan's timing; a result without any
    keeps today's payload (no timing key at all)."""
    result = _TIMING_RESULT
    if not with_timing:
        result = {
            side: {key: value for key, value in block.items() if key != "timing"}
            for side, block in _TIMING_RESULT.items()
        }

    response = _direct_client(tmp_path, result).post(
        "/v1/city/optimize",
        json={"type": "intersection", "corridor_id": "c1", "intersection": _DIRECT_INTERSECTION},
    )

    assert response.status_code == 200
    kpis = response.json()["kpis"]
    if with_timing:
        assert kpis["baseline"]["timing"] == _TIMING_RESULT["baseline"]["timing"]
        assert kpis["optimized"]["timing"] == _TIMING_RESULT["best"]["timing"]
    else:
        assert set(kpis["baseline"]) == {"cost", "metrics"}
        assert set(kpis["optimized"]) == {"cost", "metrics"}


@pytest.mark.parametrize(
    ("unserved", "sent"),
    [
        ({"n69": ["WB-left", "WB-through", "WB-right"]}, True),
        ({}, False),
        (None, False),
    ],
    ids=["some", "none", "absent"],
)
def test_optimize_endpoint_names_unserved_movements_only_when_there_are_some(
    tmp_path, unserved, sent
):
    result = dict(_TIMING_RESULT)
    if unserved is not None:
        result["unserved_movements"] = unserved

    response = _direct_client(tmp_path, result).post(
        "/v1/city/optimize",
        json={"type": "intersection", "corridor_id": "c1", "intersection": _DIRECT_INTERSECTION},
    )

    assert response.status_code == 200
    body = response.json()
    if sent:
        assert body["unserved_movements"] == unserved
    else:
        assert set(body) == {"nema_plan", "kpis"}


@pytest.mark.parametrize(
    ("config", "sent"),
    [
        ({"flow_source": "scenario_routes", "counts_source": "tmc", "max_trials": 6},
         {"flow_source": "scenario_routes", "counts_source": "tmc"}),
        ({"flow_source": "corridor_graph", "counts_source": "scenario_routes"},
         {"flow_source": "corridor_graph", "counts_source": "scenario_routes"}),
        ({"flow_source": "corridor_graph", "counts_source": "tmc"}, None),
        (None, None),
    ],
    ids=["scenario_flows", "scenario_counts", "defaults", "absent"],
)
def test_optimize_endpoint_names_scenario_demand_sources(tmp_path, config, sent):
    result = dict(_TIMING_RESULT)
    if config is not None:
        result["config"] = config

    response = _direct_client(tmp_path, result).post(
        "/v1/city/optimize",
        json={"type": "intersection", "corridor_id": "c1", "intersection": _DIRECT_INTERSECTION},
    )

    assert response.status_code == 200
    body = response.json()
    if sent is None:
        assert set(body) == {"nema_plan", "kpis"}
    else:
        assert body["config"] == sent


def test_the_published_api_schema_names_no_integration_product(tmp_path):
    """Model docstrings become /openapi.json descriptions, which callers read:
    they describe the data generically and name no integration product."""
    client, _store, _out = _client(tmp_path, launcher=_noop_launcher)
    schema = json.dumps(client.app.openapi())
    lowered = schema.lower()
    for name in ("vss", "nvidia", "metropolis", "mcp"):
        assert name not in lowered, name
    assert "stalled-vehicle geometry as reported by the video analytics" in schema


# --- run records: origin, corridor, intersections ----------------------------------


def test_an_api_run_is_recorded_with_origin_api_and_the_tmc_corridor(tmp_path: Path) -> None:
    client, store, _out = _client(tmp_path, launcher=_completing_launcher)
    tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    Path(tmc).write_text(json.dumps({"corridor_id": "demo_arterial"}), encoding="utf-8")

    resp = client.post(
        _BASE,
        json={"tmc_json": tmc, "base_sumocfg": sumocfg, "corridor": {"from": "i1", "to": "i2"}},
    )

    assert resp.status_code == 202
    record = store.get(resp.json()["run_id"])
    assert record.origin == "api"
    assert record.corridor_id == "demo_arterial"
    assert record.intersection_ids == ["i1", "i2"]


def test_a_corridor_id_request_records_its_corridor_and_the_entrys_tls_ids(
    tmp_path: Path,
) -> None:
    """The corridor geometry of a corridor_id request may come from its config entry."""
    from terasim_service.city.optimize_api import submit_optimization

    _tmc, sumocfg = _write_inputs(tmp_path / "inputs")
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    run_id = submit_optimization(
        {"corridor_id": "c1"},
        store,
        lambda *_args: None,
        output_root=tmp_path / "out",
        input_root=(tmp_path / "inputs").resolve(),
        corridors=_scenario_counts_corridors(sumocfg),
    )
    record = store.get(run_id)
    assert (record.origin, record.corridor_id) == ("api", "c1")
    assert record.intersection_ids == ["J1"]
