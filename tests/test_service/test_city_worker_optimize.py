"""Unit tests for the corridor-optimization worker (status + lease + best_score).

Drives the pure ``run_city_optimization_job`` directly with an in-memory store
and a stubbed optimization function — no Redis, no SUMO, no subprocess.
``_build_optimization_config`` constructs a real CorridorOptimizationConfig
(no I/O), so the request only needs the path fields.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from terasim_service.city.job_store import COMPLETED, FAILED, InMemoryCityJobStore
from terasim_service.city.common import LeaseLost
from terasim_service.city.worker_optimize import (
    _build_optimization_config,
    run_city_optimization_job,
)

_REQUEST = {"tmc_json": "v.json", "base_sumocfg": "b.sumocfg"}


def _store_with_slots(run_id: str, n: int = 2) -> InMemoryCityJobStore:
    """Simulate the HTTP parent: create the record and acquire n slots."""
    store = InMemoryCityJobStore(max_concurrency=4, lease_ttl=100)
    store.create(run_id)
    assert store.try_acquire_slots(run_id, n) is True
    return store


def _write_ledger_line(out: Path, plan_id: str, score, valid: bool = True) -> None:
    with (out / "trials.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"plan_id": plan_id, "score": score, "valid": valid}) + "\n"
        )


def test_happy_path_completed_best_and_best_score(tmp_path: Path) -> None:
    store = _store_with_slots("r1", 2)
    result = {"best": {"plan_id": "bo_0002", "score": 0.9}}

    def fake_opt(config, progress_callback):
        out = Path(config.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        progress_callback(0, 3, None)
        _write_ledger_line(out, "baseline", 1.0)
        progress_callback(1, 3, "baseline")
        _write_ledger_line(out, "bo_0002", 0.9)
        progress_callback(2, 3, "bo_0002")
        return result

    run_city_optimization_job(store, _REQUEST, "r1", tmp_path, opt_fn=fake_opt)

    rec = store.get("r1")
    assert rec.status == COMPLETED
    assert rec.best == {"plan_id": "bo_0002", "score": 0.9}
    assert rec.best_score == pytest.approx(0.9)
    assert (rec.done, rec.total) == (2, 3)
    assert rec.result_path.endswith("corridor_optimization_result.json")
    # All slots released -> a full-cap acquire succeeds.
    assert store.try_acquire_slots("other", 4) is True


def test_best_score_tracks_running_minimum_from_ledger(tmp_path: Path) -> None:
    store = _store_with_slots("r1")
    seen: list = []

    def fake_opt(config, progress_callback):
        out = Path(config.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        _write_ledger_line(out, "p1", 1.0)
        progress_callback(1, 4, "p1")
        seen.append(store.get("r1").best_score)
        _write_ledger_line(out, "p2", 0.8)
        progress_callback(2, 4, "p2")
        seen.append(store.get("r1").best_score)
        _write_ledger_line(out, "p3", 0.95)  # worse — must NOT overwrite
        progress_callback(3, 4, "p3")
        seen.append(store.get("r1").best_score)
        _write_ledger_line(out, "p4", None, valid=False)  # invalid — ignored
        progress_callback(4, 4, "p4")
        seen.append(store.get("r1").best_score)
        return {"best": {"plan_id": "p2", "score": 0.8}}

    run_city_optimization_job(store, _REQUEST, "r1", tmp_path, opt_fn=fake_opt)

    assert seen == [1.0, 0.8, 0.8, 0.8]
    assert store.get("r1").best_score == pytest.approx(0.8)


def test_opt_failure_marks_failed_and_releases(tmp_path: Path) -> None:
    store = _store_with_slots("r1", 2)

    def boom(config, progress_callback):
        raise RuntimeError("kaboom")

    run_city_optimization_job(store, _REQUEST, "r1", tmp_path, opt_fn=boom)

    rec = store.get("r1")
    assert rec.status == FAILED
    assert "kaboom" in rec.error
    assert store.try_acquire_slots("other", 4) is True  # released even on failure


def test_worker_acquires_and_reuses_map_when_request_has_no_base_sumocfg(
    tmp_path: Path,
) -> None:
    store = _store_with_slots("r1", 2)
    tmc_json = tmp_path / "aggregated_tmc.json"
    tmc_json.write_text("{}", encoding="utf-8")
    cached_sumocfg = tmp_path / "store" / "maps" / "cached" / "base.sumocfg"
    cached_sumocfg.parent.mkdir(parents=True)
    cached_sumocfg.write_text("<configuration/>", encoding="utf-8")
    acquired: list[Path] = []

    def acquire_map(path: Path) -> Path:
        acquired.append(path)
        return cached_sumocfg

    def fake_opt(config, progress_callback):
        assert config.base_sumocfg == cached_sumocfg.resolve()
        return {"best": {"plan_id": "baseline", "score": 1.0}}

    run_city_optimization_job(
        store,
        {"tmc_json": str(tmc_json)},
        "r1",
        tmp_path / "out",
        opt_fn=fake_opt,
        map_acquirer=acquire_map,
    )

    assert acquired == [tmc_json]
    assert store.get("r1").status == COMPLETED


def test_lease_lost_before_start_skips_opt(tmp_path: Path) -> None:
    store = _store_with_slots("r1", 2)
    store.release_slot("r1")  # parent's lease gone before the worker starts
    called = []

    def fake_opt(config, progress_callback):
        called.append(True)
        return {"best": {}}

    run_city_optimization_job(store, _REQUEST, "r1", tmp_path, opt_fn=fake_opt)

    rec = store.get("r1")
    assert rec.status == FAILED
    assert "before worker start" in rec.error
    assert called == []


class _FlakyRenewStore(InMemoryCityJobStore):
    """Startup renew succeeds; every later (heartbeat) renew reports lease lost."""

    def __init__(self) -> None:
        super().__init__(max_concurrency=4, lease_ttl=100)
        self._renew_calls = 0

    def renew_slot(self, run_id: str) -> bool:
        self._renew_calls += 1
        return self._renew_calls == 1  # True once (startup), then False


def test_heartbeat_lease_loss_stops_run_via_callback(tmp_path: Path) -> None:
    store = _FlakyRenewStore()
    store.create("r1")
    assert store.try_acquire_slots("r1", 2) is True

    def fake_opt(config, progress_callback):
        # Report progress until the heartbeat marks the lease lost and the
        # callback raises LeaseLost. Bounded so a bug can't hang the test.
        for _ in range(500):
            progress_callback(0, 1, None)
            time.sleep(0.01)
        return {"best": {}}

    run_city_optimization_job(
        store, _REQUEST, "r1", tmp_path, heartbeat_interval=0.001, opt_fn=fake_opt
    )

    rec = store.get("r1")
    assert rec.status == FAILED
    assert "lease lost" in rec.error


class _RaiseOnSetRunning(InMemoryCityJobStore):
    def set_running(self, run_id: str) -> None:
        raise RuntimeError("redis down at set_running")


def test_store_error_at_start_still_releases_slots(tmp_path: Path) -> None:
    store = _RaiseOnSetRunning(max_concurrency=4, lease_ttl=100)
    store.create("r1")
    assert store.try_acquire_slots("r1", 3) is True
    calls: list[int] = []

    run_city_optimization_job(
        store, _REQUEST, "r1", tmp_path,
        opt_fn=lambda c, progress_callback: calls.append(1) or {"best": {}},
    )

    assert calls == []  # never reached the optimizer
    assert store.try_acquire_slots("other", 4) is True  # slots released


# --- request -> config mapping (server defaults) -------------------------------


def test_build_config_explicit_null_sim_end_uses_default(tmp_path: Path) -> None:
    config = _build_optimization_config({**_REQUEST, "sim_end": None}, tmp_path)
    assert config.sim_end == 1800.0


def test_build_config_corridor_field_names_and_aliases(tmp_path: Path) -> None:
    by_field = _build_optimization_config(
        {**_REQUEST, "corridor": {"from_ref": "i1", "to_ref": "i3", "tls_ids": []}},
        tmp_path,
    )
    assert (by_field.from_ref, by_field.to_ref) == ("i1", "i3")

    by_alias = _build_optimization_config(
        {**_REQUEST, "corridor": {"from": "i1", "to": "i3"}}, tmp_path
    )
    assert (by_alias.from_ref, by_alias.to_ref) == ("i1", "i3")

    by_tls = _build_optimization_config(
        {**_REQUEST, "corridor": {"tls_ids": ["J1", "J2"]}}, tmp_path
    )
    assert by_tls.tls_ids == ("J1", "J2")


def test_build_config_full_request(tmp_path: Path) -> None:
    request = {
        **_REQUEST,
        "baseline_sheet": "sheet.json",
        "stall_data": "stalls.json",
        "optimizer": "bayes_offsets",
        "max_trials": 8,
        "patience": 3,
        "kpi_weights": {"control_delay": 1.0},
        "sim_end": 300,
        "seed": 7,
        "n_workers": 4,
    }
    config = _build_optimization_config(request, tmp_path)
    assert config.baseline_sheet == Path("sheet.json")
    assert config.stall_data == Path("stalls.json")
    assert config.optimizer == "bayes_offsets"
    assert config.budget.max_trials == 8
    assert config.budget.patience == 3
    assert config.kpi.to_dict() == {"control_delay": 1.0}
    assert config.sim_end == 300.0
    assert config.seed == 7
    assert config.n_workers == 4


# --- stage / cycle / scenario-count switches ------------------------------------


def test_build_config_forwards_new_switches(tmp_path):
    tmc = tmp_path / "t.json"
    tmc.write_text("{}")
    cfg = _build_optimization_config(
        {
            "tmc_json": str(tmc),
            "base_sumocfg": str(tmp_path / "b.sumocfg"),
            "corridor": {"tls_ids": ["J1", "J2"]},
            "optimizer": "bayes_corridor",
            "stage_mode": "per_approach",
            "search_cycle": True,
            "cycle_min_s": 45,
            "cycle_max_s": 240,
            "flow_source": "scenario_routes",
            "counts_source": "scenario_routes",
            "seed_from_scenario_programs": True,
        },
        tmp_path / "out",
    )
    assert cfg.optimizer == "bayes_corridor" and cfg.stage_mode == "per_approach"
    assert cfg.search_cycle is True and cfg.cycle_max_s == 240.0
    assert cfg.flow_source == "scenario_routes" and cfg.counts_source == "scenario_routes"
    assert cfg.seed_from_scenario_programs is True


def test_build_config_defaults_unchanged(tmp_path):
    tmc = tmp_path / "t.json"
    tmc.write_text("{}")
    cfg = _build_optimization_config({"tmc_json": str(tmc)}, tmp_path / "out")
    assert (
        cfg.stage_mode,
        cfg.search_cycle,
        cfg.flow_source,
        cfg.counts_source,
        cfg.seed_from_scenario_programs,
    ) == ("two_stage", False, "corridor_graph", "tmc", False)


def test_build_config_none_switches_keep_the_config_defaults(tmp_path):
    # What the API forwards for a request that set none of them (model_dump keeps the Nones).
    from terasim_city.orchestration.corridor_optimization import CorridorOptimizationConfig

    forwarded = {
        "tmc_json": "t.json",
        "stage_mode": None,
        "search_cycle": None,
        "cycle_min_s": None,
        "cycle_max_s": None,
        "flow_source": None,
        "counts_source": None,
        "seed_from_scenario_programs": None,
    }
    cfg = _build_optimization_config(forwarded, tmp_path)
    defaults = CorridorOptimizationConfig(
        tmc_json=Path("t.json"), base_sumocfg=None, output_dir=tmp_path
    )
    for name in (
        "stage_mode",
        "search_cycle",
        "cycle_min_s",
        "cycle_max_s",
        "flow_source",
        "counts_source",
        "seed_from_scenario_programs",
    ):
        assert getattr(cfg, name) == getattr(defaults, name), name


@pytest.mark.parametrize(
    "switches, message",
    [
        ({"stage_mode": "five"}, "stage_mode"),
        ({"cycle_min_s": 200.0, "cycle_max_s": 100.0}, "cycle_min_s"),
        ({"counts_source": "scenario_routes"}, "tls_ids"),  # the entry names no signals
    ],
)
def test_build_config_hands_new_switches_to_the_config_validation(tmp_path, switches, message):
    # The request model only constrains the types; the config rejects inconsistent values,
    # which the API turns into a submission-time 422.
    with pytest.raises(ValueError, match=message):
        _build_optimization_config({**_REQUEST, **switches}, tmp_path)
