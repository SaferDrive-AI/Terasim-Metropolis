"""Tests for the corridor optimization refine stage.

All FAST: the corridor pipeline (map resolution, TMC flow, sumolib parsing)
runs for real against the demo assets, but every simulated evaluation is a
fake driven by a per-plan/per-behavior lookup — ``BatchEvaluator`` itself is
monkeypatched, so no SUMO process or GPU/CNDE runtime is ever touched.

Test names deliberately avoid the repo-root auto-marker substrings ("sumo",
"simulation" — see tests/conftest.py's collection hook), matching the sibling
test_corridor_optimization.py module.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest

import terasim_city.orchestration.corridor_optimization as corridor_optimization
from terasim_city.corridor.resolve import resolve_corridor
from terasim_city.evaluation.moe import KpiConfig, score_moe
from terasim_city.optimize.base import Budget
from terasim_city.orchestration.corridor_optimization import (
    CorridorOptimizationConfig,
    run_corridor_optimization,
)
from tests.fixtures.tmc_templates import baseline_sheet, demo_tmc, write_tmc
from tests.test_city.conftest import REPO_ROOT, requires_sumolib

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_CFG = DEMO_DIR / "demo.sumocfg"

requires_demo_assets = pytest.mark.skipif(
    not DEMO_CFG.exists(),
    reason=f"corridor demo assets not present at {DEMO_DIR} "
    "(run examples/corridor_demo/build_demo_map.py)",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _synthetic_moe(delay: float) -> dict:
    return {
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
    }


def _install_fake_evaluator(
    monkeypatch,
    score_fn,
    calls: list[dict],
    *,
    invalid: "set[tuple[str, str]] | None" = None,
) -> None:
    """Replace ``BatchEvaluator`` with a fake recording constructor kwargs.

    ``score_fn(vehicle_behavior, plan_dict) -> (delay, counts)`` drives the
    synthetic MOE + kpi.json quality-gate counts for every evaluated plan.
    Patches the class where ``corridor_optimization`` imported it, so this
    covers BOTH the coarse construction and the refine stage's own
    construction with the exact same fake.

    ``invalid``: a set of ``(vehicle_behavior, plan_id)`` pairs that should
    come back as an EVALUATOR-level failure (``valid: False``, no ``moe``)
    instead of calling ``score_fn`` — for testing the "this specific
    evaluation itself failed" path (as opposed to a valid-but-gated one).
    """
    invalid_pairs = set(invalid or ())

    class _FakeBatchEvaluator:
        def __init__(self, **kwargs):
            calls.append(
                {
                    "vehicle_behavior": kwargs.get("vehicle_behavior"),
                    "inline": kwargs.get("inline"),
                    "n_workers": kwargs.get("n_workers"),
                    "map_dir": kwargs.get("map_dir"),
                }
            )
            self._behavior = kwargs.get("vehicle_behavior")

        def evaluate(self, plans):
            results = []
            for plan in plans:
                plan_dict = plan.to_json_dict()
                plan_id = plan_dict["plan_id"]
                if (self._behavior, plan_id) in invalid_pairs:
                    results.append(
                        {
                            "plan_id": plan_id,
                            "valid": False,
                            "error": "synthetic evaluator failure (test fixture)",
                            "trial_dir": f"trial_fake/{self._behavior}/{plan_id}",
                        }
                    )
                    continue
                delay, counts = score_fn(self._behavior, plan_dict)
                results.append(
                    {
                        "plan_id": plan_id,
                        "moe": _synthetic_moe(float(delay)),
                        "valid": True,
                        "quality_errors": [],
                        "trial_dir": f"trial_fake/{self._behavior}/{plan_id}",
                        "kpi": {"quality_gate": {"counts": dict(counts)}},
                    }
                )
            return results

        def close(self) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self.close()

    monkeypatch.setattr(corridor_optimization, "BatchEvaluator", _FakeBatchEvaluator)


def _install_fake_evaluator_env(monkeypatch, delay_fn, plans: "list[dict] | None" = None):
    """Coarse-only fake matching test_corridor_optimization.py's pattern.

    Used ONLY by the refine-disabled test, to prove the disabled path is
    untouched by anything this module adds (no BatchEvaluator class patch,
    no refine-aware plumbing at all — just the inline-eval env var + a
    patched ``evaluate_plan_task``, exactly like the pre-existing suite).

    When ``plans`` is given, every plan dict handed to the evaluator is appended
    to it, so a test can derive expectations from what was actually evaluated.
    """
    from terasim_city.optimize.batch import INLINE_EVAL_ENV_VAR

    monkeypatch.setenv(INLINE_EVAL_ENV_VAR, "1")

    def fake_evaluate_plan_task(task: dict) -> dict:
        plan = task["plan"]
        if plans is not None:
            plans.append(plan)
        offsets = {p["tls_id"]: float(p["offset_s"]) for p in plan["plans"]}
        delay = float(delay_fn(plan["plan_id"], offsets))
        return {
            "plan_id": plan["plan_id"],
            "moe": _synthetic_moe(delay),
            "valid": True,
            "quality_errors": [],
            "trial_dir": task["trial_dir"],
            "kpi": {},
        }

    monkeypatch.setattr(
        "terasim_city.optimize.batch.evaluate_plan_task", fake_evaluate_plan_task
    )


def _read_ledger(output_dir: Path) -> list[dict]:
    lines = (output_dir / "trials.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _offset_corridor_config(tmp_path: Path, **overrides) -> CorridorOptimizationConfig:
    """A 3-TLS bayes_offsets config; overrides layer on top of common fields."""
    fields = dict(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=DEMO_CFG,
        output_dir=tmp_path / "out",
        from_ref="i1",
        to_ref="i3",
        baseline_sheet=write_tmc(tmp_path, baseline_sheet(), name="baseline_sheet.json"),
        optimizer="bayes_offsets",
        budget=Budget(max_trials=3, patience=0),
        sim_end=120.0,
        seed=42,
        n_workers=3,
    )
    fields.update(overrides)
    return CorridorOptimizationConfig(**fields)


# ---------------------------------------------------------------------------
# 1. refine disabled (default) -> full deep-equality pin against an
#    explicit expected dict
# ---------------------------------------------------------------------------

def _one_tls_demo_copy(tmp_path: Path) -> Path:
    """Copy the demo map and give J2 a second program (>1 seed source)."""
    import copy
    import shutil
    from xml.etree import ElementTree as ET

    demo_copy = tmp_path / "demo_map"
    shutil.copytree(DEMO_DIR, demo_copy)
    tls_path = demo_copy / "demo.tls.xml"
    tree = ET.parse(tls_path)
    root = tree.getroot()
    j2 = next(node for node in root.findall("tlLogic") if node.attrib["id"] == "J2")
    alt = copy.deepcopy(j2)
    alt.set("programID", "alt_b")
    for phase, duration in zip(alt.findall("phase"), ("40", "3", "20", "3")):
        phase.set("duration", duration)
    root.append(alt)
    tree.write(tls_path)
    return demo_copy / "demo.sumocfg"


@requires_demo_assets
@requires_sumolib
def test_refine_disabled_result_matches_explicit_expected_dict(tmp_path, monkeypatch):
    # Single deterministic seed (Webster, no BO trials at all under
    # "exhaustive"): every field of the result is then computable from first
    # principles, with NO dependency on optuna/BO sampling.
    evaluated_plans: list[dict] = []
    _install_fake_evaluator_env(
        monkeypatch, lambda _plan_id, _offsets: 20.0, evaluated_plans
    )

    tmc_json = write_tmc(tmp_path, demo_tmc())
    base_sumocfg = _one_tls_demo_copy(tmp_path)
    output_dir = tmp_path / "out"
    cfg = CorridorOptimizationConfig(
        tmc_json=tmc_json,
        base_sumocfg=base_sumocfg,
        output_dir=output_dir,
        tls_ids=("J2",),
        optimizer="exhaustive",
        sim_end=60.0,
        n_workers=2,
    )
    assert cfg.refine_top_k == 0
    assert cfg.refine_vehicle_behavior is None

    result = run_corridor_optimization(cfg)

    # Independently re-derive every value the fake scenario determines using
    # the SAME trusted primitives run_corridor_optimization itself calls
    # (resolve_corridor, score_moe, _synthetic_moe) -- NOT by re-reading
    # `result` -- so this is a genuine deep-equality pin, not a tautology.
    # Values with no independent derivation available (the Webster-computed
    # offset, resolved_corridor's exact fields) are golden literals captured
    # from a known-good run of this exact fixture.
    corridor = resolve_corridor(base_sumocfg, tmc_json, tls_ids=("J2",))
    moe = _synthetic_moe(20.0)
    score = score_moe(moe, moe, KpiConfig())
    trial_dir = str(output_dir / "trials" / "trial_0000")
    # `timing` is derived from the one plan the evaluator was actually handed (the
    # Webster seed), never from `result`: its cycle, offset and greens.
    assert len(evaluated_plans) == 1
    seed_plan = evaluated_plans[0]["plans"][0]
    timing = {
        "cycle_s": {"J2": sum(p["duration_s"] for p in seed_plan["phases"])},
        "offsets_s": {"J2": seed_plan["offset_s"]},
        "greens_s": {
            "J2": [p["duration_s"] for p in seed_plan["phases"] if p["kind"] == "green"]
        },
    }

    expected = {
        "schema": corridor_optimization.RESULT_SCHEMA,
        "seed": {"source": "webster_init", "stored_plans_used": False},
        "resolved_corridor": [
            dataclasses.asdict(item) for item in corridor.intersections
        ],
        "baseline": {
            "plan_id": "webster_init",
            "score": score,
            "moe": moe,
            "timing": timing,
        },
        "best": {
            "plan_id": "webster_init",
            "score": score,
            "moe": moe,
            "offsets": {"J2": 0.0},  # golden: Webster's computed offset
            "timing": timing,
            "source": "webster_init",
            "trial_dir": trial_dir,
        },
        "trials": [
            {
                "trial": 0,
                "plan_id": "webster_init",
                "source": "webster_init",
                "score": score,
                "valid": True,
                "moe": moe,
                "offsets": {"J2": 0.0},
                "trial_dir": trial_dir,
            }
        ],
        "nema": {
            "path": str(output_dir / corridor_optimization.NEMA_JSON_FILENAME),
            "handoff_ready": False,  # golden: this program is not NEMA-numbered
        },
        "nema_export": {"exported": True},
        "unserved_movements": {},  # golden: the Webster seed serves every movement
        "search": {
            "ran": False,  # golden: exhaustive evaluates the seed plans only
            "reason": "optimizer 'exhaustive' evaluates the seed plans only",
            "warm_starts": {"used": [], "skipped": []},
        },
        "config": {
            "tmc_json": str(tmc_json),
            "base_sumocfg": str(base_sumocfg),
            "map_id": None,
            "output_dir": str(output_dir),
            "from_ref": None,
            "to_ref": None,
            "tls_ids": ["J2"],
            "baseline_sheet": None,
            "stall_data": None,
            "plan_data_root": None,
            "plan_time": None,
            "optimizer": "exhaustive",
            "max_trials": Budget().max_trials,
            "patience": Budget().patience,
            "kpi_weights": KpiConfig().to_dict(),
            "sim_end": 60.0,
            "seed": 42,
            "n_workers": 2,
            "vehicle_behavior": "sumo_passive",
            "signal_groups_overrides": None,
            "flow_source": "corridor_graph",
            "counts_source": "tmc",
            "seed_from_scenario_programs": False,
            "stage_mode": "two_stage",
            "search_cycle": False,
            "cycle_min_s": 45.0,
            "cycle_max_s": 150.0,
        },
        "n_trials": 1,
    }

    assert result == expected
    assert "refine" not in result
    assert "export_source" not in result

    ledger = _read_ledger(cfg.output_dir)
    assert ledger == result["trials"]
    assert all("stage" not in record for record in ledger)


# ---------------------------------------------------------------------------
# 2. unregistered refine behavior -> ValueError at config-build time
# ---------------------------------------------------------------------------

def test_unregistered_refine_behavior_rejected_at_config_time(tmp_path):
    with pytest.raises(ValueError, match="refine_vehicle_behavior"):
        CorridorOptimizationConfig(
            tmc_json=write_tmc(tmp_path, demo_tmc()),
            base_sumocfg=tmp_path / "does_not_matter.sumocfg",
            output_dir=tmp_path / "out",
            refine_top_k=2,
            refine_vehicle_behavior="not_a_registered_behavior",
        )
    # Never got far enough to touch the filesystem/output_dir: the failure is
    # at config construction, not mid-run.
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------
# 3. refine reverses the coarse ranking -> winner = refine argmin
# ---------------------------------------------------------------------------

# Deterministic regardless of what offsets optuna's TPE sampler picks: BO
# warm-starts with the seed (consuming trial 0), so the first ask() batch is
# always numbered bo_0001.. (verified against optuna's actual behavior, not
# assumed) - see optimize/bayes_base.py TpeSearchBase.ask/warm_start.
_COARSE_DELAY = {"baseline": 50.0, "bo_0001": 5.0, "bo_0002": 20.0, "bo_0003": 30.0}
_REFINE_DELAY = {"baseline": 100.0, "bo_0001": 80.0, "bo_0002": 5.0, "bo_0003": 50.0}


def _reversal_score_fn(behavior, plan_dict):
    delay = (_REFINE_DELAY if behavior == "cnde" else _COARSE_DELAY)[
        plan_dict["plan_id"]
    ]
    return delay, {"collisions": 0, "emergency_stops": 0, "teleports": 0}


@requires_demo_assets
@requires_sumolib
def test_refine_stage_reverses_coarse_ranking_and_wins(tmp_path, monkeypatch):
    calls: list[dict] = []
    _install_fake_evaluator(monkeypatch, _reversal_score_fn, calls)

    cfg = _offset_corridor_config(
        tmp_path, refine_top_k=10, refine_vehicle_behavior="cnde"
    )
    result = run_corridor_optimization(cfg)

    # Coarse winner is untouched by the refine stage: it's whichever plan the
    # _COARSE_DELAY table favors ("bo_0001", delay 5.0 vs 20/30/50).
    assert result["best"]["plan_id"] == "bo_0001"

    refine = result["refine"]
    assert refine["behavior"] == "cnde"
    assert refine["top_k"] == 10
    assert refine["refine_max_collisions"] is None
    assert refine["n_workers"] == 1  # default: sequential, inline
    assert refine["candidates_evaluated"] == 4  # baseline + 3 distinct BO trials
    assert refine["executed"] is True
    assert refine["invalid"] == []
    assert refine["baseline"]["plan_id"] == "baseline"
    assert refine["baseline"]["moe"] == _synthetic_moe(100.0)

    # Refine winner is the argmin of the REFINE table ("bo_0002", delay 5.0),
    # not the coarse winner: the reversal actually happened.
    assert refine["winner_plan_id"] == "bo_0002"
    assert refine["winner_plan_id"] != result["best"]["plan_id"]

    refined_ids = {e["plan_id"] for e in refine["evaluations"]}
    assert refined_ids == {"bo_0001", "bo_0002", "bo_0003"}
    # The full MOE is recorded alongside the scalar score, not just the
    # ratio, so a consumer can source real KPIs per entry.
    winner_entry = next(
        e for e in refine["evaluations"] if e["plan_id"] == "bo_0002"
    )
    assert winner_entry["moe"] == _synthetic_moe(5.0)

    # export_source reflects that the refine winner was actually exported.
    assert result["export_source"] == "refine"

    # nema_plan.json switched to the refine winner, not the coarse winner.
    nema_payload = json.loads(
        (cfg.output_dir / "nema_plan.json").read_text(encoding="utf-8")
    )
    winner_j2_offset = next(
        t["offsets"]["J2"] for t in result["trials"] if t["plan_id"] == "bo_0002"
    )
    exported_j2 = next(
        i["offset_s"] for i in nema_payload["intersections"] if i["tls_id"] == "J2"
    )
    assert exported_j2 == pytest.approx(winner_j2_offset)

    # trials.jsonl carries the refine evaluations tagged, coarse rows untouched.
    ledger = _read_ledger(cfg.output_dir)
    assert ledger == result["trials"]
    coarse_rows = [r for r in ledger if "stage" not in r]
    refine_rows = [r for r in ledger if r.get("stage") == "refine"]
    assert {r["plan_id"] for r in coarse_rows} == {
        "baseline", "bo_0001", "bo_0002", "bo_0003",
    }
    assert {r["plan_id"] for r in refine_rows} == {
        "baseline", "bo_0001", "bo_0002", "bo_0003",
    }

    # BatchEvaluator recording: coarse construction, then a SEPARATE refine
    # construction -- inline via the constructor arg (not an env var), single
    # worker: the refine_n_workers=1 default, regardless of cfg.n_workers=3.
    assert len(calls) == 2
    assert calls[0]["vehicle_behavior"] == "sumo_passive"
    assert calls[0]["inline"] is None
    assert calls[0]["n_workers"] == 3
    assert calls[1]["vehicle_behavior"] == "cnde"
    assert calls[1]["inline"] is True
    assert calls[1]["n_workers"] == 1
    assert calls[1]["map_dir"] == calls[0]["map_dir"]


def _mixed_cycle_demo_copy(tmp_path: Path) -> Path:
    """The demo map whose J2 runs a 46 s cycle while J1 and J3 keep 66 s (a customer's programs)."""
    import shutil
    from xml.etree import ElementTree as ET

    customer_map = tmp_path / "customer_map"
    shutil.copytree(DEMO_DIR, customer_map)
    tls_path = customer_map / "demo.tls.xml"
    tree = ET.parse(tls_path)
    j2 = next(node for node in tree.getroot().findall("tlLogic") if node.attrib["id"] == "J2")
    for phase in j2.findall("phase"):
        if phase.attrib["duration"] == "30":
            phase.set("duration", "20")
    tree.write(tls_path)
    return customer_map / "demo.sumocfg"


@requires_demo_assets
@requires_sumolib
def test_refine_winner_without_a_common_cycle_skips_the_export_but_not_the_run(
    tmp_path, monkeypatch, caplog
):
    # Coarse: Webster beats the customer's plan. Refine ("cnde"): the customer's plan wins.
    delays = {
        ("sumo_passive", "scenario_programs"): 60.0, ("sumo_passive", "webster_init"): 40.0,
        ("cnde", "scenario_programs"): 30.0, ("cnde", "webster_init"): 50.0,
    }

    def score_fn(behavior, plan_dict):
        return delays[(behavior, plan_dict["plan_id"])], {
            "collisions": 0, "emergency_stops": 0, "teleports": 0,
        }

    _install_fake_evaluator(monkeypatch, score_fn, [])
    cfg = CorridorOptimizationConfig(
        tmc_json=write_tmc(tmp_path, demo_tmc()),
        base_sumocfg=_mixed_cycle_demo_copy(tmp_path),
        output_dir=tmp_path / "out",
        tls_ids=("J1", "J2", "J3"),
        optimizer="exhaustive",
        seed_from_scenario_programs=True,
        refine_top_k=1,
        refine_vehicle_behavior="cnde",
        sim_end=60.0,
        n_workers=1,
    )
    with caplog.at_level(logging.WARNING, logger=corridor_optimization.logger.name):
        result = run_corridor_optimization(cfg)

    assert result["best"]["plan_id"] == "webster_init"
    assert result["refine"]["winner_plan_id"] == "scenario_programs"
    # The plan picked for export is the refine winner. It has no common cycle, so nothing
    # is written.
    assert result["export_source"] == "refine"
    export = result["nema_export"]
    assert export["exported"] is False
    assert all(text in export["reason"] for text in ("scenario_programs", "J2=46s", "J1=66s"))
    assert not (cfg.output_dir / "nema_plan.json").exists()
    assert any("NEMA export" in r.getMessage() for r in caplog.records)
    assert (cfg.output_dir / "corridor_optimization_result.json").exists()


# ---------------------------------------------------------------------------
# 4. top-K slot budget excludes the baseline
# ---------------------------------------------------------------------------

# Baseline ranks #1 by COARSE score here (delay 1.0, better than every BO
# trial) -- it must NOT consume one of refine_top_k's slots meant for
# genuine alternatives.
_SLOT_COARSE_DELAY = {"baseline": 1.0, "bo_0001": 5.0, "bo_0002": 20.0, "bo_0003": 30.0}


def _slot_score_fn(behavior, plan_dict):
    plan_id = plan_dict["plan_id"]
    delay = (_REFINE_DELAY if behavior == "cnde" else _SLOT_COARSE_DELAY)[plan_id]
    return delay, {"collisions": 0, "emergency_stops": 0, "teleports": 0}


@requires_demo_assets
@requires_sumolib
def test_refine_top_k_does_not_let_baseline_consume_a_slot(tmp_path, monkeypatch):
    calls: list[dict] = []
    _install_fake_evaluator(monkeypatch, _slot_score_fn, calls)

    cfg = _offset_corridor_config(
        tmp_path, refine_top_k=2, refine_vehicle_behavior="cnde"
    )
    result = run_corridor_optimization(cfg)

    # Sanity: baseline really is the coarse winner (would have consumed a
    # slot under the pre-fix ranking-with-baseline-included behavior).
    assert result["best"]["plan_id"] == "baseline"

    refine = result["refine"]
    assert refine["top_k"] == 2
    # baseline + 2 genuine alternatives, NOT baseline + 1.
    assert refine["candidates_evaluated"] == 3
    refined_ids = {e["plan_id"] for e in refine["evaluations"]}
    assert refined_ids == {"bo_0001", "bo_0002"}
    assert "bo_0003" not in refined_ids
    assert refine["baseline"]["plan_id"] == "baseline"


# ---------------------------------------------------------------------------
# 5. validity gate excludes the would-be winner -> falls back to next-best
# ---------------------------------------------------------------------------

_GATE_REFINE_DELAY = {"baseline": 100.0, "bo_0001": 80.0, "bo_0002": 5.0, "bo_0003": 50.0}
_GATE_REFINE_COLLISIONS = {"baseline": 0, "bo_0001": 0, "bo_0002": 999, "bo_0003": 0}


def _gate_score_fn(behavior, plan_dict):
    plan_id = plan_dict["plan_id"]
    if behavior == "cnde":
        delay = _GATE_REFINE_DELAY[plan_id]
        collisions = _GATE_REFINE_COLLISIONS[plan_id]
    else:
        delay = _COARSE_DELAY[plan_id]
        collisions = 0
    return delay, {"collisions": collisions, "emergency_stops": 0, "teleports": 0}


@requires_demo_assets
@requires_sumolib
def test_refine_validity_gate_excludes_and_falls_back_to_next_best(
    tmp_path, monkeypatch
):
    calls: list[dict] = []
    _install_fake_evaluator(monkeypatch, _gate_score_fn, calls)

    cfg = _offset_corridor_config(
        tmp_path,
        refine_top_k=10,
        refine_vehicle_behavior="cnde",
        refine_max_collisions=10,
    )
    result = run_corridor_optimization(cfg)
    refine = result["refine"]
    assert refine["refine_max_collisions"] == 10

    # "bo_0002" would win under the raw refine scores (delay 5.0, the
    # lowest) but its collision count (999) exceeds refine_max_collisions=10.
    invalid_ids = {e["plan_id"]: e for e in refine["invalid"]}
    assert "bo_0002" in invalid_ids
    assert invalid_ids["bo_0002"]["collisions"] == 999
    assert "refine_max_collisions" in invalid_ids["bo_0002"]["reason"]

    # Winner falls back to the next-best VALID candidate: "bo_0003" (delay
    # 50.0), not "bo_0001" (80.0) and not the excluded "bo_0002" (5.0).
    assert refine["winner_plan_id"] == "bo_0003"
    assert result["export_source"] == "refine"

    # The excluded evaluation still reports its own score/moe -- gating is
    # about winner eligibility, not about hiding the safety surface.
    assert invalid_ids["bo_0002"]["score"] is not None
    assert invalid_ids["bo_0002"]["moe"] == _synthetic_moe(5.0)


# ---------------------------------------------------------------------------
# 6. refine baseline itself fails -> total fallback to the coarse winner
# ---------------------------------------------------------------------------

@requires_demo_assets
@requires_sumolib
def test_refine_baseline_failure_falls_back_to_coarse_winner(
    tmp_path, monkeypatch, caplog
):
    calls: list[dict] = []
    # The refine-stage baseline re-evaluation itself fails (evaluator-level,
    # e.g. a crashed trial) -- ONLY under "cnde": the coarse baseline
    # evaluation (behavior "sumo_passive") must stay valid so the run can
    # establish its coarse baseline/winner normally.
    _install_fake_evaluator(
        monkeypatch,
        _reversal_score_fn,
        calls,
        invalid={("cnde", "baseline")},
    )

    cfg = _offset_corridor_config(
        tmp_path, refine_top_k=10, refine_vehicle_behavior="cnde"
    )
    with caplog.at_level(
        logging.WARNING, logger="terasim_city.orchestration.corridor_optimization"
    ):
        result = run_corridor_optimization(cfg)

    assert any(
        "baseline" in record.message and "no valid evaluation" in record.message
        for record in caplog.records
    )

    refine = result["refine"]
    assert refine["executed"] is True
    # Every candidate (baseline + all 3 alternatives) is unscoreable and
    # therefore invalid: the ratio denominator (baseline_refine_moe) never
    # existed. Baseline appears in BOTH "baseline" and "invalid" (by design
    # -- see _run_refine_stage's docstring); the alternatives appear in both
    # "evaluations" and "invalid".
    assert refine["baseline"]["plan_id"] == "baseline"
    assert refine["baseline"]["valid"] is False
    assert {e["plan_id"] for e in refine["invalid"]} == {
        "baseline", "bo_0001", "bo_0002", "bo_0003",
    }
    assert {e["plan_id"] for e in refine["evaluations"]} == {
        "bo_0001", "bo_0002", "bo_0003",
    }
    assert refine["winner_plan_id"] is None

    # No usable refine winner -> the coarse winner is what actually got
    # exported, and export_source records that this was a fallback.
    coarse_winner_id = result["best"]["plan_id"]
    assert result["export_source"] == "coarse"

    nema_payload = json.loads(
        (cfg.output_dir / "nema_plan.json").read_text(encoding="utf-8")
    )
    coarse_winner_j2_offset = next(
        t["offsets"]["J2"]
        for t in result["trials"]
        if t["plan_id"] == coarse_winner_id and "stage" not in t
    )
    exported_j2 = next(
        i["offset_s"] for i in nema_payload["intersections"] if i["tls_id"] == "J2"
    )
    assert exported_j2 == pytest.approx(coarse_winner_j2_offset)


# ---------------------------------------------------------------------------
# 7. refine_n_workers: 1 (default) keeps the inline single-process evaluator;
#    > 1 hands BatchEvaluator a real spawn pool of exactly that size. Nothing
#    downstream of the evaluator depends on the mode.
# ---------------------------------------------------------------------------

def test_refine_n_workers_defaults_to_one_and_is_validated(tmp_path):
    cfg = _offset_corridor_config(
        tmp_path, refine_top_k=1, refine_vehicle_behavior="cnde"
    )
    assert cfg.refine_n_workers == 1

    # Coerced like n_workers (env/CLI plumbing may hand over a string).
    cfg = _offset_corridor_config(
        tmp_path,
        refine_top_k=1,
        refine_vehicle_behavior="cnde",
        refine_n_workers="3",
    )
    assert cfg.refine_n_workers == 3

    for bad in (0, -1):
        with pytest.raises(ValueError, match="refine_n_workers must be >= 1"):
            _offset_corridor_config(
                tmp_path,
                refine_top_k=1,
                refine_vehicle_behavior="cnde",
                refine_n_workers=bad,
            )
    # Config-time failures: nothing ever touched the output dir.
    assert not (tmp_path / "out").exists()


@requires_demo_assets
@requires_sumolib
@pytest.mark.parametrize("refine_n_workers", [2, 8])
def test_refine_n_workers_above_one_builds_a_pooled_evaluator(
    tmp_path, monkeypatch, refine_n_workers
):
    calls: list[dict] = []
    _install_fake_evaluator(monkeypatch, _reversal_score_fn, calls)

    cfg = _offset_corridor_config(
        tmp_path,
        refine_top_k=10,
        refine_vehicle_behavior="cnde",
        refine_n_workers=refine_n_workers,
    )
    result = run_corridor_optimization(cfg)

    # The coarse construction is untouched by the knob; the refine
    # construction is a real pool (inline=False) of EXACTLY the requested
    # size -- 8 workers for 4 candidates is deliberately not capped (idle
    # workers are harmless; a silent cap would hide the configured value).
    assert len(calls) == 2
    assert calls[0]["vehicle_behavior"] == "sumo_passive"
    assert calls[0]["inline"] is None
    assert calls[0]["n_workers"] == 3
    assert calls[1]["vehicle_behavior"] == "cnde"
    assert calls[1]["inline"] is False
    assert calls[1]["n_workers"] == refine_n_workers
    assert calls[1]["map_dir"] == calls[0]["map_dir"]

    # Audit trail in the refine block ...
    refine = result["refine"]
    assert refine["n_workers"] == refine_n_workers
    # ... and everything downstream of the evaluator is mode-independent:
    # same candidate set, same winner, same export as the sequential run in
    # test_refine_stage_reverses_coarse_ranking_and_wins.
    assert refine["candidates_evaluated"] == 4
    assert refine["baseline"]["plan_id"] == "baseline"
    assert refine["winner_plan_id"] == "bo_0002"
    assert result["export_source"] == "refine"
    assert result["best"]["plan_id"] == "bo_0001"


@requires_demo_assets
@requires_sumolib
def test_refine_result_is_identical_across_modes_given_equal_evaluations(
    tmp_path, monkeypatch
):
    """With the evaluator returning the same per-plan results, the ONLY
    differences between a sequential and a pooled refine run are the
    recorded ``n_workers`` and the output paths: ledger rows, refine block,
    winner, ``export_source`` and the exported NEMA offsets are all
    mode-independent (the real-runtime counterpart of this claim is that a
    candidate's tripinfo is byte-identical whichever process evaluates it)."""
    calls: list[dict] = []
    _install_fake_evaluator(monkeypatch, _reversal_score_fn, calls)

    results: dict[int, dict] = {}
    exported: dict[int, dict] = {}
    for n_workers in (1, 2):
        out_dir = tmp_path / f"out_{n_workers}"
        cfg = _offset_corridor_config(
            tmp_path,
            output_dir=out_dir,
            refine_top_k=10,
            refine_vehicle_behavior="cnde",
            refine_n_workers=n_workers,
        )
        results[n_workers] = run_corridor_optimization(cfg)
        exported[n_workers] = json.loads(
            (out_dir / "nema_plan.json").read_text(encoding="utf-8")
        )

    assert results[1]["refine"]["n_workers"] == 1
    assert results[2]["refine"]["n_workers"] == 2

    def _normalized(result: dict) -> dict:
        clone = json.loads(json.dumps(result))
        clone["config"]["output_dir"] = "<out>"
        clone["nema"]["path"] = "<out>/nema_plan.json"
        clone["refine"]["n_workers"] = "<mode>"
        return clone

    assert _normalized(results[1]) == _normalized(results[2])
    assert [i["offset_s"] for i in exported[1]["intersections"]] == [
        i["offset_s"] for i in exported[2]["intersections"]
    ]
    # Both runs constructed the refine evaluator once, differing only in mode.
    refine_calls = [c for c in calls if c["vehicle_behavior"] == "cnde"]
    assert [(c["inline"], c["n_workers"]) for c in refine_calls] == [
        (True, 1),
        (False, 2),
    ]
