"""Request and result types: validation and JSON round trips (no simulation)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from terasim_plugin_sdk import (
    PLAN_KINDS,
    CorridorSelection,
    CurrentPlan,
    KpiWeights,
    MapInput,
    OperationRequest,
    OperationResult,
    PluginInputError,
    TrafficSnapshot,
)

from tests.test_plugin_sdk.conftest import DEMO_DIR, needs_demo, write_demo_nema_plan


def _request(tmp_path: Path, **changes) -> OperationRequest:
    fields = dict(
        map=MapInput.from_sumocfg(DEMO_DIR / "demo.sumocfg"),
        traffic=TrafficSnapshot.from_file(DEMO_DIR / "demo_tmc.json"),
        output_dir=tmp_path / "out",
        corridor=CorridorSelection.between("i1", "i3"),
    )
    fields.update(changes)
    return OperationRequest(**fields)


# ---------------------------------------------------------------------------
# KPI weights
# ---------------------------------------------------------------------------


def test_default_weights_match_the_core_and_the_service():
    from terasim_city.evaluation.moe import KpiConfig

    weights = KpiWeights()
    assert weights.to_dict() == {"control_delay": 0.7, "stops": 0.3}
    assert weights.to_kpi_config() == KpiConfig()


@pytest.mark.parametrize(
    "given",
    [
        {"control_delay": 1},
        {"stops": 0.25, "control_delay": 0.75},
        (("control_delay", 0.5), ("stops", 0.5)),
    ],
)
def test_weights_accept_mappings_and_pairs(given):
    weights = KpiWeights(given)
    assert weights.to_dict() == {key: float(value) for key, value in dict(given).items()}
    assert KpiWeights.from_dict(weights.to_dict()) == weights


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"control_delay": True}, "must be a number"),
        ({"control_delay": "0.7"}, "must be a number"),
        ({"control_delay": math.nan}, "must be finite"),
        ({"control_delay": math.inf}, "must be finite"),
        ({"control_delay": 0.0}, "must be > 0"),
        ({"control_delay": -1.0}, "must be > 0"),
        ({"travel_time": 1.0}, "unknown weight key"),
        ({}, "must not be empty"),
        ((("stops", 0.5), ("stops", 0.5)), "duplicate"),
    ],
)
def test_bad_weights_are_input_errors(bad, message):
    with pytest.raises(PluginInputError, match=message):
        KpiWeights(bad)


def test_weights_parse_json_and_key_value_text():
    assert KpiWeights.parse('{"control_delay": 0.8, "stops": 0.2}').to_dict() == {
        "control_delay": 0.8,
        "stops": 0.2,
    }
    assert KpiWeights.parse("control_delay=0.6, stops=0.4").to_dict() == {
        "control_delay": 0.6,
        "stops": 0.4,
    }
    for bad in ("control_delay", "control_delay=fast", "{not json", "stops=-1"):
        with pytest.raises(PluginInputError):
            KpiWeights.parse(bad)


# ---------------------------------------------------------------------------
# Map, traffic, corridor, current plan
# ---------------------------------------------------------------------------


def test_map_input_forms():
    assert MapInput.from_sumocfg("a/demo.sumocfg").kind == "sumocfg"
    assert MapInput.from_store("0123456789ab", data_root="/data").kind == "store"
    assert MapInput.from_store().kind == "auto"
    with pytest.raises(PluginInputError, match="not both"):
        MapInput(sumocfg=Path("a.sumocfg"), map_id="0123456789ab")
    with pytest.raises(PluginInputError):
        MapInput(map_id="")


def test_traffic_snapshot_takes_a_file_or_a_payload(tmp_path):
    with pytest.raises(PluginInputError, match="exactly one"):
        TrafficSnapshot()
    payload = json.loads((DEMO_DIR / "demo_tmc.json").read_text(encoding="utf-8"))
    snapshot = TrafficSnapshot.from_payload(payload)
    payload["corridor_id"] = "changed after the fact"
    assert snapshot.payload["corridor_id"] == "demo_arterial"  # a copy was taken
    written = snapshot.write_to(tmp_path / "inputs")
    assert written == tmp_path / "inputs" / "tmc.json"
    assert snapshot.load().corridor_id == "demo_arterial"
    assert [i.intersection_id for i in TrafficSnapshot.from_file(written).load().intersections] == [
        "i1",
        "i2",
        "i3",
    ]


def test_invalid_counts_are_input_errors(tmp_path):
    with pytest.raises(PluginInputError, match="invalid TMC data"):
        TrafficSnapshot.from_payload({"corridor_id": "x", "observation_interval": "1h"}).load()
    with pytest.raises(PluginInputError, match="not found"):
        TrafficSnapshot.from_file(tmp_path / "missing.json").load()
    with pytest.raises(PluginInputError, match="JSON-compatible"):
        TrafficSnapshot.from_payload({"corridor_id": math.nan})


def test_corridor_selection_rules():
    assert CorridorSelection.signals("J1", "J2").tls_ids == ("J1", "J2")
    assert CorridorSelection.between("i1", "i3").to_dict() == {
        "from_ref": "i1",
        "to_ref": "i3",
        "tls_ids": [],
    }
    for kwargs, message in (
        ({"from_ref": "i1"}, "together"),
        ({"from_ref": "i1", "to_ref": "i3", "tls_ids": ("J1",)}, "not both"),
        ({"tls_ids": ("J1", "J1")}, "duplicates"),
        ({"tls_ids": "J1"}, "not a string"),
    ):
        with pytest.raises(PluginInputError, match=message):
            CorridorSelection(**kwargs)


def test_current_plan_kinds_and_validation(tmp_path):
    assert PLAN_KINDS == ("timing_sheet", "nema_plan", "plan_store", "scenario_programs")
    assert CurrentPlan.scenario_programs().path is None
    assert CurrentPlan.nema_plan("p.json", plan_time="2026-07-01T08:00:00Z").plan_time
    for args, message in (
        (("weekly",), "must be one of"),
        (("timing_sheet",), "needs a path"),
        (("scenario_programs", Path("x")), "takes no path"),
        (("timing_sheet", Path("s.json"), "2026-07-01T08:00:00"), "NEMA kinds"),
        (("nema_plan", Path("p.json"), "tomorrow morning"), "ISO 8601"),
    ):
        with pytest.raises(PluginInputError, match=message):
            CurrentPlan(*args)
    with pytest.raises(PluginInputError, match="not found"):
        CurrentPlan.timing_sheet(tmp_path / "missing.json").check()


@needs_demo
def test_current_plan_detect_reads_the_schema(tmp_path):
    assert CurrentPlan.detect(DEMO_DIR / "baseline_sheet.json").kind == "timing_sheet"
    nema = CurrentPlan.detect(write_demo_nema_plan(tmp_path), plan_time="2026-07-01T08:00:00")
    assert (nema.kind, nema.plan_time) == ("nema_plan", "2026-07-01T08:00:00")
    assert CurrentPlan.detect(tmp_path).kind == "plan_store"
    assert CurrentPlan.detect("scenario").kind == "scenario_programs"
    other = tmp_path / "other.json"
    other.write_text('{"schema": "something.else"}', encoding="utf-8")
    with pytest.raises(PluginInputError, match="schema"):
        CurrentPlan.detect(other)
    with pytest.raises(PluginInputError, match="not found"):
        CurrentPlan.detect(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"sim_end": 0}, "sim_end"),
        ({"sim_end": math.inf}, "sim_end"),
        ({"sim_end": True}, "sim_end"),
        ({"seed": -1}, "seed"),
        ({"n_workers": 0}, "n_workers"),
        ({"n_workers": 1.5}, "n_workers"),
        ({"vehicle_behavior": ""}, "vehicle_behavior"),
        ({"params": {"x": math.nan}}, "params"),
        ({"params": {"x": object()}}, "params"),
        ({"weights": {"stops": 1.0}}, "KpiWeights"),
    ],
)
def test_request_validation(tmp_path, changes, message):
    with pytest.raises(PluginInputError, match=message):
        _request(tmp_path, **changes)


@needs_demo
def test_request_json_round_trip_and_absolute_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    request = OperationRequest(
        map=MapInput.from_sumocfg(DEMO_DIR / "demo.sumocfg"),
        traffic=TrafficSnapshot.from_file(DEMO_DIR / "demo_tmc.json"),
        output_dir=Path("relative-out"),
        corridor=CorridorSelection.signals("J1", "J2"),
        current_plan=CurrentPlan.timing_sheet(DEMO_DIR / "baseline_sheet.json"),
        weights=KpiWeights({"control_delay": 1.0}),
        sim_end=600,
        seed=7,
        n_workers=1,
        params={"optimizer": "exhaustive", "nested": {"a": [1, 2]}},
    )
    payload = request.to_dict()
    assert payload["schema"] == "terasim.operation_request.v1"
    assert OperationRequest.from_dict(json.loads(json.dumps(payload))) == request
    resolved = request.resolved()
    assert resolved.output_dir == tmp_path / "relative-out"
    assert resolved.output_dir.is_absolute() and resolved.map.sumocfg.is_absolute()
    assert resolved.inputs_dir == tmp_path / "relative-out" / "inputs"


def test_missing_map_is_an_input_error(tmp_path):
    request = _request(tmp_path, map=MapInput.from_sumocfg(tmp_path / "missing.sumocfg"))
    with pytest.raises(PluginInputError, match="map file not found"):
        request.check_inputs()


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


def test_result_round_trip_and_service_response(tmp_path):
    result = OperationResult(
        plugin="demo",
        plugin_version="1.0",
        corridor_id="demo_arterial",
        tls_ids=["J1"],
        nema_plan={"schema": "terasim.nema_timing.v1"},
        kpis={"cost_weights": {"stops": 1.0}},
        output_dir=tmp_path,
        artifacts={"nema_plan": "nema_plan.json"},
        notes=["a note"],
    )
    written = result.write()
    assert written == tmp_path / "operation_result.json"
    again = OperationResult.read(tmp_path)
    assert again == result
    assert again.tls_ids == ("J1",)
    assert result.response() == {
        "nema_plan": {"schema": "terasim.nema_timing.v1"},
        "kpis": {"cost_weights": {"stops": 1.0}},
    }
    with_unserved = OperationResult.from_dict(
        {**result.to_dict(), "unserved_movements": {"J1": ["EB-left"]}}
    )
    assert with_unserved.response()["unserved_movements"] == {"J1": ["EB-left"]}
