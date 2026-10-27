"""GET /v1/city/optimizations/{run_id}/baseline: the run's baseline plan as NEMA timing.

A run seeded from stored NEMA plans returns that stored plan (as the run used it), any
other run the export of its baseline static plan; both in the terasim.nema_timing.v1
shape of /nema. The copy is written at completion (baseline_nema.json) and served from
the file; it is rebuilt from the baseline trial when missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from terasim_city.nema.export import export_nema, export_nema_plan
from terasim_city.plans.model import CorridorPlan, PhaseSpec, PlanProvenance, SignalPlan
from terasim_city.plans.nema_plan import NemaCorridorPlan, NemaSignalEntry
from terasim_service.city.job_store import InMemoryCityJobStore
from terasim_service.city.optimize_api import create_v1_city_router
from terasim_service.city.run_outputs import (
    BASELINE_NEMA_FILENAME,
    BaselineUnavailable,
    build_baseline_nema,
    persist_baseline_nema,
)
from terasim_service.city.worker_optimize import run_city_optimization_job

_BASE = "/v1/city/optimizations"


def _static_plan(cycles=(66.0, 66.0), *, source="sumo_tls", green=True) -> CorridorPlan:
    plans = []
    for index, cycle in enumerate(cycles, start=1):
        half = (cycle - 6.0) / 2.0
        phases = (
            PhaseSpec("GGrr", half, "green" if green else "other",
                      movements=("EB-through", "WB-through")),
            PhaseSpec("yyrr", 3.0, "yellow"),
            PhaseSpec("rrGG", half, "green" if green else "other",
                      movements=("NB-through", "SB-through")),
            PhaseSpec("rryy", 3.0, "yellow"),
        )
        plans.append(SignalPlan(f"J{index}", "alt_fixed", 0.0, phases))
    return CorridorPlan("scenario_programs", tuple(plans), PlanProvenance(source=source))


def _nema_plan() -> NemaCorridorPlan:
    intersection = {
        "intersection_id": "main_1st",
        "name": "A St @ B St",
        "phases": [
            {"phase": 2, "movements": ["EB-through"], "yellow_s": 3.5, "all_red_s": 1.5},
            {"phase": 4, "movements": ["NB-through"], "yellow_s": 3.0, "all_red_s": 1.0},
        ],
        "patterns": [
            {"pattern_id": "midday", "description": "MIDDAY", "cycle_s": 90,
             "offset_s": 12, "splits": {"2": 50, "4": 40}},
        ],
        "schedule": [
            {"days": ["Mon", "Tue"], "start": "09:30", "pattern_id": "midday"},
            {"days": ["Mon", "Tue"], "start": "15:00", "pattern_id": "free"},
        ],
    }
    entry = NemaSignalEntry("J1", intersection, dict(intersection["patterns"][0]))
    return NemaCorridorPlan("stored_nema", (entry,), PlanProvenance(source="stored_nema"))


def _run_dir(root: Path, plan, *, seed_source: str, corridor_id: str = "c1") -> tuple[Path, dict]:
    """A finished run's directory: its baseline trial (plan.json), result and NEMA export."""
    run_dir = root / "run1"
    trial = run_dir / "trials" / "trial_0000"
    trial.mkdir(parents=True)
    (trial / "plan.json").write_text(json.dumps(plan.to_json_dict()), encoding="utf-8")
    tmc = run_dir / "inputs" / "aggregated_tmc.json"
    tmc.parent.mkdir(parents=True)
    tmc.write_text(json.dumps({"corridor_id": corridor_id}), encoding="utf-8")
    result = {
        "seed": {"source": seed_source, "stored_plans_used": seed_source == "stored_nema"},
        "baseline": {"plan_id": plan.plan_id, "score": 1.0, "moe": {}},
        "best": {"plan_id": "bo_0001", "score": 0.9, "moe": {}, "trial_dir": str(trial)},
        "trials": [{"plan_id": plan.plan_id, "trial_dir": str(trial), "valid": True}],
        "config": {"tmc_json": str(tmc), "tls_ids": []},
    }
    (run_dir / "corridor_optimization_result.json").write_text(json.dumps(result), encoding="utf-8")
    return run_dir, result


def _without_stamp(doc: dict) -> dict:
    return {key: value for key, value in doc.items() if key != "generated_at"}


# --- the builder -------------------------------------------------------------------


def test_a_static_baseline_is_the_loop_aware_export_of_its_plan(tmp_path: Path) -> None:
    plan = _static_plan()
    run_dir, result = _run_dir(tmp_path, plan, seed_source="sumo_tls")

    doc = build_baseline_nema(run_dir, result)

    expected = export_nema(plan, corridor_id="c1", out_dir=tmp_path / "expected")
    assert _without_stamp(doc) == _without_stamp(expected)
    assert doc["schema"] == "terasim.nema_timing.v1"
    assert all(row["nema_phase"] is None for i in doc["intersections"] for row in i["phases"])
    # The run's own nema_plan.json is never touched by the export.
    assert not (run_dir / "nema_plan.json").exists()


def test_a_stored_nema_baseline_is_the_stored_plan_the_run_used(tmp_path: Path) -> None:
    plan = _nema_plan()
    run_dir, result = _run_dir(tmp_path, plan, seed_source="stored_nema")

    doc = build_baseline_nema(run_dir, result)

    expected = export_nema_plan(plan, corridor_id="c1", out_dir=tmp_path / "expected")
    assert _without_stamp(doc) == _without_stamp(expected)
    (intersection,) = doc["intersections"]
    assert intersection["pattern_id"] == "midday"
    assert intersection["valid_windows"] == [
        {"days": ["Mon", "Tue"], "start": "09:30", "end": "15:00"}
    ]
    assert [row["nema_phase"] for row in intersection["phases"]] == [2, 4]


def test_signals_without_a_common_cycle_are_exported_one_by_one(tmp_path: Path) -> None:
    plan = _static_plan(cycles=(66.0, 90.0))
    run_dir, result = _run_dir(tmp_path, plan, seed_source="sumo_tls")

    doc = build_baseline_nema(run_dir, result)

    assert doc["coordination"]["cycle_length_s"] == 90.0
    assert [i["cycle_s"] for i in doc["intersections"]] == [66.0, 90.0]
    for intersection in doc["intersections"]:
        assert sum(row["split_s"] for row in intersection["phases"]) == pytest.approx(
            intersection["cycle_s"]
        )


def test_a_baseline_that_cannot_be_expressed_says_why(tmp_path: Path) -> None:
    plan = _static_plan(green=False)  # no green phase at all
    run_dir, result = _run_dir(tmp_path, plan, seed_source="sumo_tls")

    with pytest.raises(BaselineUnavailable, match="no green phase"):
        build_baseline_nema(run_dir, result)


def test_a_missing_baseline_trial_is_unavailable(tmp_path: Path) -> None:
    run_dir, result = _run_dir(tmp_path, _static_plan(), seed_source="sumo_tls")
    (run_dir / "trials" / "trial_0000" / "plan.json").unlink()

    with pytest.raises(BaselineUnavailable, match="scenario_programs"):
        build_baseline_nema(run_dir, result)


def test_persisting_writes_the_copy_and_never_raises(tmp_path: Path) -> None:
    run_dir, result = _run_dir(tmp_path, _static_plan(), seed_source="sumo_tls")
    persist_baseline_nema(run_dir, result)
    assert json.loads((run_dir / BASELINE_NEMA_FILENAME).read_text())["corridor_id"] == "c1"

    broken = tmp_path / "broken"
    broken.mkdir()
    persist_baseline_nema(broken, {"baseline": {"plan_id": "x"}})  # logged, not raised
    assert not (broken / BASELINE_NEMA_FILENAME).exists()


# --- the endpoint ------------------------------------------------------------------


def _client(tmp_path: Path, plan, *, seed_source: str):
    """A router whose inline worker finishes with a run whose baseline is ``plan``."""
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    output_root = tmp_path / "out"
    output_root.mkdir()
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

    def launch(run_id, request, output_dir):
        def fake_opt(config, progress_callback):
            out = Path(output_dir)
            trial = out / "trials" / "trial_0000"
            trial.mkdir(parents=True)
            (trial / "plan.json").write_text(json.dumps(plan.to_json_dict()), encoding="utf-8")
            result = {
                "seed": {"source": seed_source},
                "baseline": {"plan_id": plan.plan_id, "score": 1.0, "moe": {}},
                "best": {"plan_id": plan.plan_id, "score": 1.0, "moe": {}},
                "trials": [{"plan_id": plan.plan_id, "trial_dir": str(trial)}],
                "config": {"tmc_json": str(tmc)},
            }
            (out / "corridor_optimization_result.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            return result

        run_city_optimization_job(store, request, run_id, output_dir, opt_fn=fake_opt)

    app = FastAPI()
    app.include_router(create_v1_city_router(store, launch, output_root=output_root))
    client = TestClient(app)
    response = client.post(_BASE, json={"tmc_json": str(tmc), "base_sumocfg": str(sumocfg)})
    assert response.status_code == 202, response.text
    return client, store, output_root / response.json()["run_id"]


@pytest.mark.parametrize(
    ("plan", "seed_source"),
    [(_static_plan(), "sumo_tls"), (_nema_plan(), "stored_nema")],
    ids=["static", "stored_nema"],
)
def test_the_endpoint_serves_the_copy_written_at_completion(tmp_path, plan, seed_source):
    client, _store, run_dir = _client(tmp_path, plan, seed_source=seed_source)
    assert (run_dir / BASELINE_NEMA_FILENAME).is_file()  # written when the run completed

    response = client.get(f"{_BASE}/{run_dir.name}/baseline")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"run_id", "plan_id", "source", "nema_plan"}
    assert (body["run_id"], body["plan_id"], body["source"]) == (
        run_dir.name, plan.plan_id, seed_source,
    )
    assert body["nema_plan"] == json.loads((run_dir / BASELINE_NEMA_FILENAME).read_text())
    assert body["nema_plan"]["schema"] == "terasim.nema_timing.v1"
    assert body["nema_plan"]["corridor_id"] == "c1"


def test_a_missing_copy_is_rebuilt_from_the_baseline_trial(tmp_path: Path) -> None:
    client, _store, run_dir = _client(tmp_path, _static_plan(), seed_source="webster_init")
    (run_dir / BASELINE_NEMA_FILENAME).unlink()

    response = client.get(f"{_BASE}/{run_dir.name}/baseline")

    assert response.status_code == 200
    assert response.json()["source"] == "webster_init"
    assert (run_dir / BASELINE_NEMA_FILENAME).is_file()


def test_a_baseline_that_cannot_be_expressed_is_404_with_the_reason(tmp_path: Path) -> None:
    client, _store, run_dir = _client(tmp_path, _static_plan(green=False), seed_source="sumo_tls")
    assert not (run_dir / BASELINE_NEMA_FILENAME).exists()

    response = client.get(f"{_BASE}/{run_dir.name}/baseline")

    assert response.status_code == 404
    assert "no green phase" in response.json()["detail"]


def test_the_baseline_is_409_until_the_run_completes_and_404_for_unknown_runs(tmp_path):
    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    app = FastAPI()
    app.include_router(create_v1_city_router(store, lambda *_a: None, output_root=tmp_path))
    client = TestClient(app)
    store.create("r1", origin="api")
    store.set_running("r1")

    running = client.get(f"{_BASE}/r1/baseline")
    assert running.status_code == 409
    assert "not finished" in running.json()["detail"]

    store.set_failed("r1", "boom")
    failed = client.get(f"{_BASE}/r1/baseline")
    assert failed.status_code == 409
    assert "boom" in failed.json()["detail"]

    assert client.get(f"{_BASE}/nope/baseline").status_code == 404


def test_a_direct_run_gets_its_baseline_copy_too(tmp_path: Path) -> None:
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
    plan = _static_plan()

    def optimize(config, *, progress_callback):
        out = config.output_dir
        trial = out / "trials" / "trial_0000"
        trial.mkdir(parents=True)
        (trial / "plan.json").write_text(json.dumps(plan.to_json_dict()), encoding="utf-8")
        (out / "nema_plan.json").write_text(
            json.dumps({"schema": "terasim.nema_timing.v1", "corridor_id": "c1",
                        "intersections": []}),
            encoding="utf-8",
        )
        return {
            "seed": {"source": "sumo_tls"},
            "baseline": {"plan_id": plan.plan_id, "score": 1.0, "moe": {}},
            "best": {"plan_id": plan.plan_id, "score": 1.0, "moe": {}},
            "trials": [{"plan_id": plan.plan_id, "trial_dir": str(trial)}],
        }

    store = InMemoryCityJobStore(max_concurrency=3, lease_ttl=100)
    app = FastAPI()
    app.include_router(create_v1_city_router(
        store, lambda *_a: None, output_root=tmp_path / "out", buffer=buffer,
        direct_optimizer=optimize, map_acquirer=lambda _tmc: sumocfg,
    ))
    client = TestClient(app)
    response = client.post("/v1/city/optimize", json={
        "type": "intersection", "corridor_id": "c1",
        "intersection": {"intersection_id": "i1", "lat": 37.3, "lon": -121.9},
    })
    assert response.status_code == 200, response.text

    (record,) = store.list_runs()
    baseline = client.get(f"{_BASE}/{record.run_id}/baseline")
    assert baseline.status_code == 200
    assert baseline.json()["source"] == "sumo_tls"
    assert baseline.json()["nema_plan"]["corridor_id"] == "c1"
    assert (tmp_path / "out" / record.run_id / BASELINE_NEMA_FILENAME).is_file()


def test_the_copy_is_as_readable_as_the_optimizers_files(tmp_path: Path) -> None:
    import os
    import stat

    run_dir, result = _run_dir(tmp_path, _static_plan(), seed_source="sumo_tls")
    previous = os.umask(0o022)
    try:
        persist_baseline_nema(run_dir, result)
    finally:
        os.umask(previous)
    mode = stat.S_IMODE((run_dir / BASELINE_NEMA_FILENAME).stat().st_mode)
    assert mode == 0o644
    assert not [p for p in run_dir.iterdir() if p.name.startswith(".")]
