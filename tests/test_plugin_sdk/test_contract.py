"""Result contract, conformance kit and run harness, with stand-in plugins (no simulation)."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest
from terasim_plugin_sdk import (
    API_VERSION,
    ConformanceError,
    CorridorSelection,
    KpiWeights,
    MapInput,
    OperationRequest,
    OperationResult,
    PluginInputError,
    PluginNotFoundError,
    PluginRegistrationError,
    PluginRegistry,
    PluginRunError,
    TrafficSnapshot,
    run_operation,
)
from terasim_plugin_sdk.conformance import (
    check_plugin,
    check_result,
    demo_request,
    progress_problems,
    run_conformance,
    validate_result,
)

from tests.test_plugin_sdk.conftest import DEMO_DIR, needs_demo

pytestmark = needs_demo

TLS = ("J1", "J2", "J3")


def _nema_plan() -> dict:
    rows = [
        {
            "nema_phase": 2,
            "movements": ["EB-through"],
            "split_s": 52.0,
            "green_s": 48.0,
            "yellow_s": 3.0,
            "all_red_s": 1.0,
            "notes": "",
        },
        {
            "nema_phase": 4,
            "movements": ["NB-through"],
            "split_s": 38.0,
            "green_s": 34.0,
            "yellow_s": 3.0,
            "all_red_s": 1.0,
            "notes": "",
        },
    ]
    return {
        "schema": "terasim.nema_timing.v1",
        "generated_at": "2026-10-20T00:00:00+00:00",
        "corridor_id": "demo_arterial",
        "handoff_ready": True,
        "coordination": {"cycle_length_s": 90.0, "reference_intersection": "J1"},
        "intersections": [
            {
                "tls_id": tls,
                "offset_s": 10.0 * index,
                "description": "Fixed",
                "valid_windows": [],
                "phases": copy.deepcopy(rows),
            }
            for index, tls in enumerate(TLS)
        ],
    }


def _kpis(weights: dict) -> dict:
    return {
        "cost_weights": dict(weights),
        "baseline": {
            "cost": 1.0,
            "metrics": {"control_delay_s_per_veh": 30.0, "stops_per_veh": 0.9},
        },
        "optimized": {
            "cost": 0.9,
            "metrics": {"control_delay_s_per_veh": 27.0, "stops_per_veh": 0.8},
            "timing": {"cycle_s": {tls: 90.0 for tls in TLS}},
        },
    }


class FixedAnswerPlugin:
    """Answers every request with the same plan, the way a real plugin hands it back."""

    name = "fixed_answer"
    version = "1.0.0"
    api_version = API_VERSION
    description = "returns a fixed plan"

    def run(self, request, progress=None):
        if progress is not None:
            progress(0, 1, None)
        nema = _nema_plan()
        (request.output_dir / "nema_plan.json").write_text(json.dumps(nema), encoding="utf-8")
        if progress is not None:
            progress(1, 1, "fixed")
        return OperationResult(
            plugin=self.name,
            plugin_version=self.version,
            corridor_id="demo_arterial",
            tls_ids=TLS,
            nema_plan=nema,
            kpis=_kpis(request.weights.to_dict()),
            output_dir=request.output_dir,
            artifacts={"nema_plan": "nema_plan.json"},
        )


def _request(tmp_path: Path, **changes) -> OperationRequest:
    fields = dict(
        map=MapInput.from_sumocfg(DEMO_DIR / "demo.sumocfg"),
        traffic=TrafficSnapshot.from_file(DEMO_DIR / "demo_tmc.json"),
        output_dir=tmp_path / "out",
        corridor=CorridorSelection.between("i1", "i3"),
    )
    fields.update(changes)
    return OperationRequest(**fields)


def _good_payload(tmp_path: Path) -> dict:
    request = _request(tmp_path)
    request.output_dir.mkdir(parents=True)
    return FixedAnswerPlugin().run(request.resolved()).to_dict()


# ---------------------------------------------------------------------------
# validate_result / check_result
# ---------------------------------------------------------------------------


def test_a_conformant_result_has_no_problems(tmp_path):
    payload = _good_payload(tmp_path)
    assert validate_result(payload) == []
    assert validate_result(OperationResult.from_dict(payload), _request(tmp_path)) == []
    check_result(OperationResult.from_dict(payload))


def _break(path: str, value):
    def mutate(payload: dict) -> None:
        target = payload
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[int(key)] if isinstance(target, list) else target[key]
        last = keys[-1]
        if value is _DELETE:
            del target[last]
        elif isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value

    return mutate


_DELETE = object()


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (_break("schema", "terasim.operation_result.v0"), "schema is"),
        (_break("api_version", "2.0"), "api_version"),
        (_break("plugin", "Not A Name"), "plugin must be"),
        (_break("tls_ids", []), "tls_ids"),
        (_break("nema_plan.schema", "terasim.nema_timing.v0"), "nema_plan.schema"),
        (_break("nema_plan.intersections", []), "non-empty list"),
        (_break("nema_plan.intersections.2.tls_id", "J9"), "no intersection for corridor signal"),
        (_break("nema_plan.intersections.0.phases.0.split_s", -5.0), ">= 0"),
        (_break("nema_plan.coordination.cycle_length_s", 0), "cycle_length_s"),
        (_break("kpis.baseline.cost", math.nan), "finite"),
        (_break("kpis.optimized", _DELETE), "kpis.optimized must be"),
        (_break("kpis.optimized.cost", True), "kpis.optimized.cost"),
        (_break("kpis.optimized.cost", 1.5), "worse than kpis.baseline.cost"),
        (_break("kpis.cost_weights", {}), "cost_weights"),
        (_break("kpis.cost_weights.speed", 1.0), "unknown key"),
        (_break("kpis.baseline.metrics.stops_per_veh", None), "stops_per_veh"),
        (_break("artifacts.nema_plan", "../nema_plan.json"), "inside output_dir"),
        (_break("artifacts.trials", "trials"), "does not exist"),
        (_break("artifacts", {}), "must list 'nema_plan'"),
        (_break("unserved_movements", {"J1": ["EB-sideways"]}), "unserved_movements"),
        (_break("details", [1, 2]), "details"),
        (_break("notes", [1]), "notes"),
    ],
)
def test_broken_results_are_reported(tmp_path, mutate, expected):
    payload = _good_payload(tmp_path)
    mutate(payload)
    problems = validate_result(payload)
    assert any(expected in problem for problem in problems), problems
    with pytest.raises(ConformanceError) as caught:
        check_result(payload)
    assert caught.value.problems == tuple(problems)


def test_the_written_nema_plan_must_match_the_result(tmp_path):
    payload = _good_payload(tmp_path)
    payload["nema_plan"]["intersections"][0]["offset_s"] = 33.0
    assert any("differs from the result's nema_plan" in p for p in validate_result(payload))


def test_result_is_checked_against_its_request(tmp_path):
    payload = _good_payload(tmp_path)
    other_weights = _request(tmp_path, weights=KpiWeights({"control_delay": 1.0}))
    assert any("does not echo" in p for p in validate_result(payload, other_weights))
    other_dir = _request(tmp_path, output_dir=tmp_path / "elsewhere")
    assert any("output_dir" in p for p in validate_result(payload, other_dir))
    other_signals = _request(tmp_path, corridor=CorridorSelection.signals("J3", "J2", "J1"))
    assert any("requested signals" in p for p in validate_result(payload, other_signals))


def test_non_json_results_are_reported(tmp_path):
    result = OperationResult.from_dict(_good_payload(tmp_path))
    broken = OperationResult(**{**result.__dict__, "details": {"path": Path("/x")}})
    assert any("not JSON" in p for p in validate_result(broken))


def test_progress_contract():
    assert progress_problems([(0, 3, None), (1, 3, "a"), (3, 3, "b")]) == []
    assert progress_problems([(0, 5, None), (2, 5, "a")]) == []  # a search may stop early
    assert progress_problems([]) == ["the plugin never called progress"]
    assert any("first progress call" in p for p in progress_problems([(1, 3, "a")]))
    assert any(
        "backwards" in p for p in progress_problems([(0, 3, None), (2, 3, "a"), (1, 3, "b")])
    )
    assert any("outside" in p for p in progress_problems([(0, 3, None), (4, 3, "a")]))


def test_check_plugin():
    check_plugin(FixedAnswerPlugin())
    broken = type("Broken", (FixedAnswerPlugin,), {"api_version": "0.1"})()
    with pytest.raises(ConformanceError, match="api_version"):
        check_plugin(broken)


# ---------------------------------------------------------------------------
# run_operation harness
# ---------------------------------------------------------------------------


def test_harness_writes_request_and_result(tmp_path):
    progress = []
    result = run_operation(
        FixedAnswerPlugin(), _request(tmp_path), progress=lambda *call: progress.append(call)
    )
    out = tmp_path / "out"
    assert result.output_dir == out
    assert progress == [(0, 1, None), (1, 1, "fixed")]
    request_file = json.loads((out / "operation_request.json").read_text(encoding="utf-8"))
    assert request_file["schema"] == "terasim.operation_request.v1"
    assert request_file["output_dir"] == str(out)
    assert OperationResult.read(out) == result


def test_conformance_kit_runs_a_plugin_end_to_end(tmp_path):
    request = demo_request(DEMO_DIR, tmp_path / "out")
    result = run_conformance(FixedAnswerPlugin(), request)
    assert result.kpis["cost_weights"] == {"control_delay": 0.7, "stops": 0.3}
    with pytest.raises(PluginInputError, match="demo corridor files not found"):
        demo_request(tmp_path, tmp_path / "out")


def test_conformance_kit_rejects_a_silent_plugin(tmp_path):
    class Silent(FixedAnswerPlugin):
        def run(self, request, progress=None):
            return super().run(request, None)

    with pytest.raises(ConformanceError, match="never called progress"):
        run_conformance(Silent(), demo_request(DEMO_DIR, tmp_path / "out"))


def test_harness_rejects_nonconformant_results_before_writing_them(tmp_path):
    class WrongWeights(FixedAnswerPlugin):
        def run(self, request, progress=None):
            result = super().run(request, progress)
            return OperationResult(**{**result.__dict__, "kpis": _kpis({"stops": 1.0})})

    with pytest.raises(ConformanceError, match="does not echo"):
        run_operation(WrongWeights(), _request(tmp_path))
    assert not (tmp_path / "out" / "operation_result.json").exists()
    run_operation(WrongWeights(), _request(tmp_path), check=False)
    assert (tmp_path / "out" / "operation_result.json").exists()


def test_harness_error_contract(tmp_path):
    class Crashes(FixedAnswerPlugin):
        def run(self, request, progress=None):
            raise KeyError("internal bug")

    class WrongType(FixedAnswerPlugin):
        def run(self, request, progress=None):
            return {"nema_plan": {}}

    class RejectsInput(FixedAnswerPlugin):
        def run(self, request, progress=None):
            raise PluginInputError("corridor too long")

    with pytest.raises(PluginRunError, match="failed: KeyError") as caught:
        run_operation(Crashes(), _request(tmp_path))
    assert isinstance(caught.value.__cause__, KeyError)
    with pytest.raises(PluginRunError, match="not an OperationResult"):
        run_operation(WrongType(), _request(tmp_path))
    with pytest.raises(PluginInputError, match="corridor too long"):
        run_operation(RejectsInput(), _request(tmp_path))
    with pytest.raises(PluginNotFoundError):
        run_operation(
            "no_such_plugin", _request(tmp_path), registry=PluginRegistry(entry_points=False)
        )
    with pytest.raises(PluginRegistrationError):
        run_operation(object(), _request(tmp_path))


def test_missing_inputs_fail_before_the_plugin_runs(tmp_path):
    calls = []

    class Records(FixedAnswerPlugin):
        def run(self, request, progress=None):
            calls.append(request)
            return super().run(request, progress)

    request = _request(tmp_path, map=MapInput.from_sumocfg(tmp_path / "no_map.sumocfg"))
    with pytest.raises(PluginInputError, match="map file not found"):
        run_operation(Records(), request)
    assert calls == [] and not (tmp_path / "out").exists()


def test_progress_exceptions_propagate_unchanged(tmp_path):
    """The caller cancels by raising from its callback: the harness never wraps that."""

    class Stop(Exception):
        pass

    def cancel(done, total, label):
        raise Stop("caller cancelled")

    with pytest.raises(Stop, match="caller cancelled"):
        run_operation(FixedAnswerPlugin(), _request(tmp_path), progress=cancel)
    assert not (tmp_path / "out" / "operation_result.json").exists()
