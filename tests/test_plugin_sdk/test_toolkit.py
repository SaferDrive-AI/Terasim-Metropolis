"""Toolkit helpers on the demo corridor: scenario, maps, baseline plans (no SUMO run)."""

from __future__ import annotations

import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from terasim_plugin_sdk import (
    CorridorSelection,
    CurrentPlan,
    MapInput,
    OperationRequest,
    PluginInputError,
    TrafficSnapshot,
)
from terasim_plugin_sdk.toolkit import (
    baseline_plan,
    ingest_map,
    kpi_metrics,
    plan_timing,
    prepare_scenario,
    resolve_map,
)

from tests.test_plugin_sdk.conftest import (
    DEMO_DIR,
    DEMO_INTERSECTIONS,
    needs_demo,
    write_demo_nema_plan,
)

pytestmark = [
    needs_demo,
    pytest.mark.skipif(importlib.util.find_spec("sumolib") is None, reason="needs sumolib"),
]


def _request(tmp_path: Path, **changes) -> OperationRequest:
    fields = dict(
        map=MapInput.from_sumocfg(DEMO_DIR / "demo.sumocfg"),
        traffic=TrafficSnapshot.from_file(DEMO_DIR / "demo_tmc.json"),
        output_dir=tmp_path / "out",
        corridor=CorridorSelection.between("i1", "i3"),
        sim_end=300.0,
        seed=7,
    )
    fields.update(changes)
    return OperationRequest(**fields).resolved()


@pytest.fixture
def scenario(tmp_path):
    return prepare_scenario(_request(tmp_path))


def test_prepare_scenario_builds_one_shared_scenario(scenario, tmp_path):
    out = tmp_path / "out"
    assert scenario.corridor_id == "demo_arterial"
    assert scenario.tls_ids == ("J1", "J2", "J3")
    assert scenario.output_dir == out
    assert scenario.sumocfg == out / "scenario" / "simulation.sumocfg"
    assert scenario.net_file.name == "demo.net.xml"
    assert (out / "scenario" / "routes.rou.xml").is_file()
    config = ET.parse(scenario.sumocfg).getroot()
    values = {child.tag: child.get("value") for section in config for child in section}
    assert values["time-to-teleport"] == "-1"  # congestion is measured, never teleported away
    assert values["seed"] == "7"
    assert float(values["end"]) == 300.0


def test_prepare_scenario_writes_an_in_memory_snapshot(tmp_path):
    payload = json.loads((DEMO_DIR / "demo_tmc.json").read_text(encoding="utf-8"))
    built = prepare_scenario(_request(tmp_path, traffic=TrafficSnapshot.from_payload(payload)))
    assert built.tmc_json == tmp_path / "out" / "inputs" / "tmc.json"
    assert json.loads(built.tmc_json.read_text(encoding="utf-8")) == payload


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"corridor": CorridorSelection.signals("J9")}, "J9"),
        ({"vehicle_behavior": "teleporting_cars"}, "unknown vehicle_behavior"),
        ({"map": MapInput.from_sumocfg(Path("/nonexistent/demo.sumocfg"))}, "map file not found"),
    ],
)
def test_prepare_scenario_input_errors(tmp_path, changes, message):
    with pytest.raises(PluginInputError, match=message):
        prepare_scenario(_request(tmp_path, **changes))
    with pytest.raises(PluginInputError, match="flow_source"):
        prepare_scenario(_request(tmp_path), flow_source="loop_detectors")


def test_baseline_plan_for_every_current_plan_kind(scenario, tmp_path):
    from terasim_city.plans.nema_plan import NemaCorridorPlan

    webster, notes = baseline_plan(scenario, None)
    assert (webster.plan_id, webster.provenance.source, notes) == (
        "webster_init",
        "webster_init",
        (),
    )

    sheet, notes = baseline_plan(
        scenario, CurrentPlan.timing_sheet(DEMO_DIR / "baseline_sheet.json")
    )
    assert sheet.plan_id == "baseline" and notes == ()
    assert plan_timing(sheet)["offsets_s"] == {"J1": 0.0, "J2": 20.0, "J3": 40.0}
    assert plan_timing(sheet)["cycle_s"] == {"J1": 90.0, "J2": 90.0, "J3": 90.0}

    programs, _ = baseline_plan(scenario, CurrentPlan.scenario_programs())
    assert programs.provenance.source == "sumo_tls"

    document = write_demo_nema_plan(tmp_path / "plans")
    nema, notes = baseline_plan(scenario, CurrentPlan.nema_plan(document))
    assert isinstance(nema, NemaCorridorPlan) and notes == ()
    assert [entry.tls_id for entry in nema.entries] == ["J1", "J2", "J3"]
    assert nema.programs() == {"J1": "am_peak", "J2": "am_peak", "J3": "am_peak"}
    store = tmp_path / "out" / "inputs" / "plan_store"
    assert (store / "plans" / "demo_arterial" / "i2" / "plan.json").is_file()

    from_store, notes = baseline_plan(
        scenario, CurrentPlan.plan_store(store, plan_time="2026-07-02T08:30:00")
    )
    assert isinstance(from_store, NemaCorridorPlan) and notes == ()


def test_a_nema_plan_missing_a_corridor_signal_falls_back_with_a_note(scenario, tmp_path):
    document = write_demo_nema_plan(tmp_path / "plans", intersections=DEMO_INTERSECTIONS[:2])
    plan, notes = baseline_plan(scenario, CurrentPlan.nema_plan(document))
    assert plan.plan_id == "webster_init"
    assert len(notes) == 1 and "not used" in notes[0] and "J3" in notes[0]


def test_malformed_current_plans_are_input_errors(scenario, tmp_path):
    bad = tmp_path / "bad_plan.json"
    bad.write_text(
        json.dumps({"schema": "terasim.nema_plan.v1", "intersections": [{}]}), encoding="utf-8"
    )
    with pytest.raises(PluginInputError, match="NEMA plan"):
        baseline_plan(scenario, CurrentPlan.nema_plan(bad))
    sheet = json.loads((DEMO_DIR / "baseline_sheet.json").read_text(encoding="utf-8"))
    sheet["intersections"] = sheet["intersections"][:1]  # covers i1 only
    partial = tmp_path / "partial_sheet.json"
    partial.write_text(json.dumps(sheet), encoding="utf-8")
    with pytest.raises(PluginInputError, match="timing sheet"):
        baseline_plan(scenario, CurrentPlan.timing_sheet(partial))


def test_maps_from_the_store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    map_id = ingest_map(DEMO_DIR / "demo.sumocfg", data_root=store)
    assert len(map_id) == 12
    assert ingest_map(DEMO_DIR / "demo.sumocfg", data_root=store) == map_id  # content-addressed

    by_id = resolve_map(MapInput.from_store(map_id, data_root=store))
    assert by_id.base_sumocfg == (DEMO_DIR / "demo.sumocfg").resolve()
    # A prebuilt package stays where it is: only its manifest lives in the store.
    assert (by_id.map_id, by_id.map_dir) == (map_id, DEMO_DIR.resolve())
    assert (store / "maps" / map_id / "map_package.json").is_file()

    tmc = TrafficSnapshot.from_file(DEMO_DIR / "demo_tmc.json").load()
    covering = resolve_map(MapInput.from_store(data_root=store), tmc)
    assert covering.map_id == map_id

    with pytest.raises(PluginInputError, match="no stored map with id"):
        resolve_map(MapInput.from_store("ffffffffffff", data_root=store))
    far_away = TrafficSnapshot.from_payload(
        {
            "corridor_id": "elsewhere",
            "observation_interval": "1h",
            "intersections": [{"intersection_id": "x", "lat": 10.0, "lon": 10.0, "counts": {}}],
        }
    ).load()
    with pytest.raises(PluginInputError, match="no stored map covers"):
        resolve_map(MapInput.from_store(data_root=store), far_away)
    monkeypatch.delenv("TERASIM_CITY_DATA_ROOT", raising=False)
    with pytest.raises(PluginInputError, match="TERASIM_CITY_DATA_ROOT"):
        resolve_map(MapInput.from_store(map_id))
    not_a_map = tmp_path / "notes.txt"
    not_a_map.write_text("hello", encoding="utf-8")
    with pytest.raises(PluginInputError, match="cannot store map"):
        ingest_map(not_a_map, data_root=store)


def test_a_stored_map_drives_a_scenario(tmp_path):
    store = tmp_path / "store"
    map_id = ingest_map(DEMO_DIR / "demo.sumocfg", data_root=store)
    built = prepare_scenario(_request(tmp_path, map=MapInput.from_store(map_id, data_root=store)))
    assert built.map.map_id == map_id
    assert built.tls_ids == ("J1", "J2", "J3")


def test_kpi_metric_names():
    assert kpi_metrics() == {"control_delay": "control_delay_s_per_veh", "stops": "stops_per_veh"}
