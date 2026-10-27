"""Synchronous VSS-facing city optimization contract.

The VSS agent has already resolved natural language to TMC coordinates.  This
surface therefore accepts only structured targets, executes the optimization
inside the request, and returns the NEMA plan plus KPI comparison directly. The
run is also recorded like an asynchronous one (origin ``direct``): its directory
stays under the output root and the status/result/nema endpoints serve it.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terasim_service.city.ingest_api import InMemorySnapshotBuffer
from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import create_v1_city_router


_NOW = datetime.fromisoformat("2026-07-01T09:00:00+00:00").timestamp()
_ENDPOINT = "/v1/city/optimize"


def _snapshot() -> dict:
    counts = {
        direction: {turn: 1 for turn in ("Right", "Straight", "Left")}
        for direction in ("South", "West", "North", "East")
    }
    return {
        "corridor_id": "park_ave",
        "observation_interval": "10m",
        "timestamp_start": "2026-07-01T08:50:00Z",
        "intersections": [
            {
                "intersection_id": "oak",
                "name": "Park Ave & Oak St",
                "lat": 37.335,
                "lon": -121.90339,
                "counts": counts,
            },
            {
                "intersection_id": "cedar",
                "name": "Park Ave & Cedar St",
                "lat": 37.335,
                "lon": -121.89661,
                "counts": counts,
            },
        ],
    }


def _client(
    tmp_path: Path,
    *,
    skipped_export: "str | None" = None,
    max_concurrency: int = 3,
    failure: "Exception | None" = None,
):
    """``skipped_export``: the reason the fake optimizer gives for writing no NEMA plan;
    ``failure``: an exception the fake optimizer raises instead of finishing."""
    store = InMemoryCityJobStore(max_concurrency=max_concurrency, lease_ttl=100)
    buffer = InMemorySnapshotBuffer(clock=lambda: _NOW)
    buffer.append("park_ave", _snapshot())
    captured: dict = {}
    sumocfg = tmp_path / "cached.sumocfg"
    sumocfg.write_text("<configuration/>", encoding="utf-8")

    def acquire_map(tmc_json: Path) -> Path:
        captured["map_tmc"] = json.loads(Path(tmc_json).read_text(encoding="utf-8"))
        return sumocfg

    def optimize(config, *, progress_callback):
        captured["config"] = config
        captured["output_dir"] = config.output_dir
        config.output_dir.mkdir(parents=True, exist_ok=True)
        progress_callback(1, 1, "bo_0001")
        if failure is not None:
            raise failure
        nema = {
            "schema": "terasim.nema_timing.v1",
            "corridor_id": "park_ave",
            "handoff_ready": True,
            "intersections": [],
        }
        if skipped_export is None:
            (config.output_dir / "nema_plan.json").write_text(
                json.dumps(nema), encoding="utf-8"
            )
        result = {
            "baseline": {
                "plan_id": "baseline",
                "score": 1.0,
                "moe": {
                    "control_delay_s_per_veh": 30.0,
                    "stops_per_veh": 1.5,
                },
            },
            "best": {
                "plan_id": "bo_0001",
                "score": 0.8,
                "moe": {
                    "control_delay_s_per_veh": 22.0,
                    "stops_per_veh": 1.2,
                },
            },
        }
        if skipped_export is not None:
            result["nema_export"] = {"exported": False, "reason": skipped_export}
        return result

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
    return TestClient(app), store, captured


def test_single_intersection_returns_nema_and_kpis_and_records_the_run(
    tmp_path: Path,
) -> None:
    client, store, captured = _client(tmp_path)

    response = client.post(
        _ENDPOINT,
        json={
            "type": "intersection",
            "corridor_id": "park_ave",
            "intersection": {
                "intersection_id": "oak",
                "name": "Park Ave & Oak St",
                "lat": 37.335,
                "lon": -121.90339,
            },
            "cost": {
                "weights": {"control_delay": 0.6, "stops": 0.4}
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "nema_plan": {
            "schema": "terasim.nema_timing.v1",
            "corridor_id": "park_ave",
            "handoff_ready": True,
            "intersections": [],
        },
        "kpis": {
            "cost_weights": {"control_delay": 0.6, "stops": 0.4},
            "baseline": {
                "cost": 1.0,
                "metrics": {
                    "control_delay_s_per_veh": 30.0,
                    "stops_per_veh": 1.5,
                },
            },
            "optimized": {
                "cost": 0.8,
                "metrics": {
                    "control_delay_s_per_veh": 22.0,
                    "stops_per_veh": 1.2,
                },
            },
        },
    }
    config = captured["config"]
    assert config.from_ref == "oak"
    assert config.to_ref == "oak"
    assert config.kpi.to_dict() == {"control_delay": 0.6, "stops": 0.4}
    # The run is kept and recorded like an asynchronous one, origin "direct".
    (record,) = store.list_runs()
    assert record.origin == "direct"
    assert record.status == "completed"
    assert record.corridor_id == "park_ave"
    assert record.intersection_ids == ["oak"]
    assert record.completed_at is not None
    assert captured["output_dir"] == (tmp_path / "outputs" / record.run_id).resolve()
    assert captured["output_dir"].is_dir()
    assert not store.holds_slots(record.run_id)  # the lease was released

    base = f"/v1/city/optimizations/{record.run_id}"
    status = client.get(f"{base}/status")
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert status.json()["best_score"] == pytest.approx(0.8)
    result = client.get(f"{base}/result")
    assert result.status_code == 200
    assert result.json()["best"]["plan_id"] == "bo_0001"
    nema = client.get(f"{base}/nema")
    assert nema.status_code == 200
    assert nema.json() == response.json()["nema_plan"]


def test_best_plan_without_a_nema_export_is_a_409_with_the_reason(tmp_path: Path) -> None:
    reason = "NEMA export skipped: plan 'scenario_programs' has no common cycle (J1=66s, J2=46s)"
    client, store, captured = _client(tmp_path, skipped_export=reason)

    response = client.post(
        _ENDPOINT,
        json={
            "type": "intersection",
            "corridor_id": "park_ave",
            "intersection": {
                "intersection_id": "oak",
                "name": "Park Ave & Oak St",
                "lat": 37.335,
                "lon": -121.90339,
            },
        },
    )

    # Nothing to return as a plan: a clear, explained error rather than an unhandled
    # FileNotFoundError. The optimization itself finished, so the run is recorded as
    # completed, and its /nema answers with the same reason.
    assert response.status_code == 409
    assert response.json()["detail"] == reason
    (record,) = store.list_runs()
    assert (record.origin, record.status) == ("direct", "completed")
    assert captured["output_dir"].is_dir()
    nema = client.get(f"/v1/city/optimizations/{record.run_id}/nema")
    assert nema.status_code == 409
    assert nema.json()["detail"] == reason


def test_corridor_uses_separate_from_and_to_lat_lon(tmp_path: Path) -> None:
    client, _store, captured = _client(tmp_path)

    response = client.post(
        _ENDPOINT,
        json={
            "type": "corridor",
            "corridor_id": "park_ave",
            "corridor": {
                "from": {
                    "intersection_id": "oak",
                    "name": "Park Ave & Oak St",
                    "lat": 37.335,
                    "lon": -121.90339,
                },
                "to": {
                    "intersection_id": "cedar",
                    "name": "Park Ave & Cedar St",
                    "lat": 37.335,
                    "lon": -121.89661,
                },
            },
        },
    )

    assert response.status_code == 200
    config = captured["config"]
    assert config.from_ref == "oak"
    assert config.to_ref == "cedar"
    assert config.kpi.to_dict() == {"control_delay": 0.7, "stops": 0.3}


def test_target_shape_must_match_type(tmp_path: Path) -> None:
    client, _store, _captured = _client(tmp_path)

    response = client.post(
        _ENDPOINT,
        json={
            "type": "intersection",
            "corridor_id": "park_ave",
            "corridor": {
                "from": {
                    "intersection_id": "oak",
                    "lat": 37.335,
                    "lon": -121.90339,
                },
                "to": {
                    "intersection_id": "cedar",
                    "lat": 37.335,
                    "lon": -121.89661,
                },
            },
        },
    )

    assert response.status_code == 422


def test_target_is_not_compared_with_buffered_tmc_before_optimizer(
    tmp_path: Path,
) -> None:
    client, _store, captured = _client(tmp_path)

    response = client.post(
        _ENDPOINT,
        json={
            "type": "intersection",
            "corridor_id": "park_ave",
            "intersection": {
                "intersection_id": "vss-selected-id",
                "name": "VSS-selected target",
                "lat": 37.0,
                "lon": -121.0,
            },
        },
    )

    assert response.status_code == 200
    assert captured["map_tmc"]["corridor_id"] == "park_ave"
    assert captured["config"].from_ref == "vss-selected-id"
    assert captured["config"].to_ref == "vss-selected-id"


def test_vss_contract_does_not_accept_internal_optimizer_controls(tmp_path: Path) -> None:
    client, _store, _captured = _client(tmp_path)
    body = {
        "type": "intersection",
        "corridor_id": "park_ave",
        "intersection": {
            "intersection_id": "oak",
            "lat": 37.335,
            "lon": -121.90339,
        },
        "optimizer": "auto",
        "max_trials": 20,
    }

    response = client.post(_ENDPOINT, json=body)

    assert response.status_code == 422


@pytest.mark.parametrize("bad_weight", [True, "1.0"])
def test_cost_weights_reject_coercible_non_numbers_before_work_starts(
    tmp_path: Path, bad_weight
) -> None:
    client, _store, captured = _client(tmp_path)

    response = client.post(
        _ENDPOINT,
        json={
            "type": "intersection",
            "corridor_id": "park_ave",
            "intersection": {
                "intersection_id": "oak",
                "lat": 37.335,
                "lon": -121.90339,
            },
            "cost": {
                "weights": {"control_delay": bad_weight, "stops": 0.3}
            },
        },
    )

    assert response.status_code == 422
    assert "map_tmc" not in captured
    assert "config" not in captured


def test_cost_weights_reject_non_finite_values() -> None:
    from terasim_service.city.optimize_api import DirectOptimizationRequest

    for bad_weight in (math.inf, -math.inf, math.nan):
        with pytest.raises(ValueError, match="finite"):
            DirectOptimizationRequest.model_validate(
                {
                    "type": "intersection",
                    "corridor_id": "park_ave",
                    "intersection": {
                        "intersection_id": "oak",
                        "lat": 37.335,
                        "lon": -121.90339,
                    },
                    "cost": {
                        "weights": {
                            "control_delay": bad_weight,
                            "stops": 0.3,
                        }
                    },
                }
            )


def test_capacity_is_reserved_before_map_acquisition(tmp_path: Path) -> None:
    """A request that must return 429 cannot download/convert a map first."""

    client, store, captured = _client(tmp_path)
    assert store.try_acquire_slots("already-running", 3)

    response = client.post(
        _ENDPOINT,
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

    assert response.status_code == 429
    assert "map_tmc" not in captured


def test_n_workers_above_the_deployment_cap_is_422_before_map_acquisition(
    tmp_path: Path,
) -> None:
    """The default request needs 2 worker slots; a deployment capped at 1 can never run it."""

    client, _store, captured = _client(tmp_path, max_concurrency=1)

    response = client.post(
        _ENDPOINT,
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

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "n_workers=2" in detail
    assert "TERASIM_CITY_MAX_CONCURRENCY=1" in detail
    assert "map_tmc" not in captured


_INTERSECTION_BODY = {
    "type": "intersection",
    "corridor_id": "park_ave",
    "intersection": {
        "intersection_id": "oak",
        "lat": 37.335,
        "lon": -121.90339,
    },
}


def test_a_failing_optimization_is_recorded_as_failed(tmp_path: Path) -> None:
    client, store, captured = _client(tmp_path, failure=RuntimeError("SUMO crashed"))

    with pytest.raises(RuntimeError, match="SUMO crashed"):
        client.post(_ENDPOINT, json=_INTERSECTION_BODY)

    (record,) = store.list_runs()
    assert (record.origin, record.status) == ("direct", "failed")
    assert "SUMO crashed" in record.error
    assert record.completed_at is not None
    assert not store.holds_slots(record.run_id)
    assert captured["output_dir"].is_dir()  # kept for inspection, like an async run


def test_a_request_rejected_before_it_starts_leaves_no_run_behind(tmp_path: Path) -> None:
    client, store, captured = _client(tmp_path)
    assert store.try_acquire_slots("already-running", 3)

    response = client.post(_ENDPOINT, json=_INTERSECTION_BODY)

    assert response.status_code == 429
    assert store.list_runs() == []
    outputs = tmp_path / "outputs"
    assert not outputs.exists() or list(outputs.iterdir()) == []


def test_a_corridor_run_records_both_endpoints(tmp_path: Path) -> None:
    client, store, _captured = _client(tmp_path)

    response = client.post(
        _ENDPOINT,
        json={
            "type": "corridor",
            "corridor_id": "park_ave",
            "corridor": {
                "from": {"intersection_id": "oak", "lat": 37.335, "lon": -121.90339},
                "to": {"intersection_id": "cedar", "lat": 37.335, "lon": -121.89661},
            },
        },
    )

    assert response.status_code == 200
    (record,) = store.list_runs()
    assert record.intersection_ids == ["oak", "cedar"]
