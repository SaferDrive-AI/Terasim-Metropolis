"""Tests for terasim_city.nema.export (pure unit; no SUMO/Redis)."""

from __future__ import annotations

import json

import pytest

from terasim_city.nema import NEMA_TIMING_SCHEMA, export_nema
from terasim_city.plans.model import (
    CorridorPlan,
    PhaseSpec,
    PlanProvenance,
    SignalPlan,
)


def _stage(green_s, yellow_s, all_red_s, *, nema_phases=(), movements=()):
    """One green + yellow + all_red stage as a list of PhaseSpecs."""
    return [
        PhaseSpec(
            state="GGrr",
            duration_s=green_s,
            kind="green",
            nema_phases=nema_phases,
            movements=movements,
        ),
        PhaseSpec(state="yyrr", duration_s=yellow_s, kind="yellow"),
        PhaseSpec(state="rrrr", duration_s=all_red_s, kind="all_red"),
    ]


def _signal_plan(tls_id, offset_s, *, with_nema=True):
    ew_nema = (2, 6) if with_nema else ()
    ns_nema = (4, 8) if with_nema else ()
    phases = _stage(
        48, 3, 1, nema_phases=ew_nema, movements=("EB-through", "WB-through")
    ) + _stage(34, 3, 1, nema_phases=ns_nema, movements=("NB-through", "SB-through"))
    return SignalPlan(
        tls_id=tls_id, program_id="p", offset_s=offset_s, phases=tuple(phases)
    )


def _corridor_plan(*, with_nema=True):
    return CorridorPlan(
        plan_id="baseline",
        plans=(
            _signal_plan("J1", 0.0, with_nema=with_nema),
            _signal_plan("J2", 20.0, with_nema=with_nema),
            _signal_plan("J3", 40.0, with_nema=with_nema),
        ),
        provenance=PlanProvenance(source="baseline_sheet"),
    )


def test_export_json_schema_and_files(tmp_path):
    plan = _corridor_plan()
    result = export_nema(plan, corridor_id="demo_arterial", out_dir=tmp_path)

    json_path = tmp_path / "nema_plan.json"
    assert json_path.is_file()
    # JSON is the only export artifact; no .txt is written.
    assert not (tmp_path / "nema_plan.txt").exists()
    assert json.loads(json_path.read_text()) == result

    assert set(result) == {
        "schema",
        "generated_at",
        "corridor_id",
        "handoff_ready",
        "coordination",
        "intersections",
    }
    assert result["schema"] == NEMA_TIMING_SCHEMA
    assert result["corridor_id"] == "demo_arterial"
    assert len(result["intersections"]) == 3
    for intersection in result["intersections"]:
        assert set(intersection) == {
            "tls_id",
            "offset_s",
            "description",
            "valid_windows",
            "phases",
        }
        # Static plans carry no schedule of their own: declared valid all week,
        # in the same window shape the stored-plan export uses.
        assert intersection["valid_windows"] == [
            {
                "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                "start": "00:00",
                "end": "24:00",
            }
        ]
        assert intersection["description"] != ""
        for row in intersection["phases"]:
            assert set(row) == {
                "nema_phase",
                "movements",
                "split_s",
                "green_s",
                "yellow_s",
                "all_red_s",
                "notes",
            }


def test_scenario_programs_are_described_as_such(tmp_path):
    plan = CorridorPlan(
        plan_id="scenario_programs",
        plans=_corridor_plan().plans,
        provenance=PlanProvenance(source="sumo_tls"),
    )
    result = export_nema(plan, corridor_id="c", out_dir=tmp_path)
    assert {i["description"] for i in result["intersections"]} == {
        "The scenario's own signal programs"
    }


def test_handoff_ready_false_when_any_green_unnumbered(tmp_path):
    result = export_nema(
        _corridor_plan(with_nema=False), corridor_id="c", out_dir=tmp_path
    )
    assert result["handoff_ready"] is False
    # unnumbered greens export with nema_phase null
    rows = result["intersections"][0]["phases"]
    assert all(row["nema_phase"] is None for row in rows)


def test_split_is_green_plus_yellow_plus_all_red(tmp_path):
    result = export_nema(_corridor_plan(), corridor_id="c", out_dir=tmp_path)
    for intersection in result["intersections"]:
        for row in intersection["phases"]:
            assert row["split_s"] == pytest.approx(
                row["green_s"] + row["yellow_s"] + row["all_red_s"]
            )
    # the first stage of every intersection: 48 + 3 + 1
    first = result["intersections"][0]["phases"][0]
    assert first["green_s"] == 48
    assert first["yellow_s"] == 3
    assert first["all_red_s"] == 1
    assert first["split_s"] == 52


def test_multiple_nema_phases_one_row_per_number_with_note(tmp_path):
    result = export_nema(_corridor_plan(), corridor_id="c", out_dir=tmp_path)
    rows = result["intersections"][0]["phases"]
    # 2 stages x 2 nema numbers each = 4 rows
    assert [row["nema_phase"] for row in rows] == [2, 6, 4, 8]
    assert rows[0]["notes"] and "2+6" in rows[0]["notes"]
    assert rows[0]["split_s"] == rows[1]["split_s"] == 52
    assert rows[0]["movements"] == rows[1]["movements"]


def test_coordination_block(tmp_path):
    plan = _corridor_plan()
    result = export_nema(plan, corridor_id="c", out_dir=tmp_path)
    assert result["coordination"] == {
        "cycle_length_s": plan.common_cycle_s(),
        "reference_intersection": "J1",
    }
    assert result["coordination"]["cycle_length_s"] == pytest.approx(90.0)


def test_cycle_override(tmp_path):
    result = export_nema(
        _corridor_plan(), corridor_id="c", out_dir=tmp_path, cycle_s=120.0
    )
    assert result["coordination"]["cycle_length_s"] == 120.0


def test_other_kind_phases_fold_into_the_clearance_and_splits_fill_the_cycle(tmp_path):
    # The program is a loop: the leading 'other' phase (5 s) and the trailing one
    # (2 s) both sit between the green's yellow and the next start of that green,
    # so they are its clearance (no vehicle movement is green during them).
    phases = (
        PhaseSpec(state="rrrG", duration_s=5, kind="other"),
        PhaseSpec(
            state="GGrr",
            duration_s=40,
            kind="green",
            nema_phases=(2,),
            movements=("EB-through",),
        ),
        PhaseSpec(state="yyrr", duration_s=3, kind="yellow"),
        PhaseSpec(state="rrrr", duration_s=2, kind="other"),
    )
    plan = CorridorPlan(
        plan_id="p",
        plans=(
            SignalPlan(tls_id="J1", program_id="p", offset_s=0.0, phases=phases),
        ),
        provenance=PlanProvenance(source="explicit"),
    )
    result = export_nema(plan, corridor_id="c", out_dir=tmp_path)
    rows = result["intersections"][0]["phases"]
    assert len(rows) == 1  # only the green produces a row
    assert rows[0]["nema_phase"] == 2
    assert rows[0]["green_s"] == 40.0
    assert rows[0]["yellow_s"] == 3.0
    assert rows[0]["all_red_s"] == pytest.approx(7.0)
    assert rows[0]["split_s"] == pytest.approx(50.0)  # the whole cycle
    assert "5 s before its first green" in rows[0]["notes"]
    assert "7 s without any vehicle green" in rows[0]["notes"]
    # The program's first green starts 5 s after the program does.
    assert result["intersections"][0]["offset_s"] == pytest.approx(5.0)
    assert result["handoff_ready"] is True


def _program(tls_id, offset_s, phases):
    return CorridorPlan(
        plan_id="scenario_programs",
        plans=(SignalPlan(tls_id=tls_id, program_id="p", offset_s=offset_s, phases=phases),),
        provenance=PlanProvenance(source="sumo_tls"),
    )


def test_leading_all_red_becomes_the_last_stage_clearance_and_shifts_the_offset(tmp_path):
    # A customer program that opens with a 50 s all-red block (171 s cycle): the
    # old export dropped it, so the splits summed to 121 s.
    phases = (
        PhaseSpec(state="rrrr", duration_s=50, kind="all_red"),
        PhaseSpec(state="GGrr", duration_s=40, kind="green", movements=("EB-through",)),
        PhaseSpec(state="yyrr", duration_s=3, kind="yellow"),
        PhaseSpec(state="rrrr", duration_s=2, kind="all_red"),
        PhaseSpec(state="rrGG", duration_s=60, kind="green", movements=("NB-through",)),
        PhaseSpec(state="rryy", duration_s=4, kind="yellow"),
        PhaseSpec(state="rrrr", duration_s=12, kind="all_red"),
    )
    plan = _program("n32", 150.0, phases)
    result = export_nema(plan, corridor_id="c", out_dir=tmp_path)
    intersection = result["intersections"][0]
    rows = intersection["phases"]

    assert [row["movements"] for row in rows] == [["EB-through"], ["NB-through"]]
    assert [row["split_s"] for row in rows] == [45.0, 126.0]
    assert sum(row["split_s"] for row in rows) == pytest.approx(171.0)
    assert rows[0]["notes"] == ""
    assert rows[1]["green_s"] == 60.0
    assert rows[1]["yellow_s"] == 4.0
    assert rows[1]["all_red_s"] == 62.0  # its own 12 s + the leading 50 s
    assert "50 s before its first green" in rows[1]["notes"]
    # SUMO starts the program at offset_s; its first green starts 50 s later.
    assert intersection["offset_s"] == pytest.approx((150.0 + 50.0) % 171.0)


def test_program_starting_with_green_is_exported_unchanged(tmp_path):
    result = export_nema(_corridor_plan(), corridor_id="c", out_dir=tmp_path)
    j2 = result["intersections"][1]
    concurrent_ew = "concurrent stage: phases 2+6 share one green interval"
    concurrent_ns = "concurrent stage: phases 4+8 share one green interval"
    ew = {"movements": ["EB-through", "WB-through"], "split_s": 52.0, "green_s": 48.0,
          "yellow_s": 3.0, "all_red_s": 1.0, "notes": concurrent_ew}
    ns = {"movements": ["NB-through", "SB-through"], "split_s": 38.0, "green_s": 34.0,
          "yellow_s": 3.0, "all_red_s": 1.0, "notes": concurrent_ns}
    expected = {
        "tls_id": "J2",
        "offset_s": 20.0,
        "description": "Baseline timing sheet",
        "valid_windows": [
            {"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "start": "00:00", "end": "24:00"}
        ],
        "phases": [
            {"nema_phase": 2, **ew},
            {"nema_phase": 6, **ew},
            {"nema_phase": 4, **ns},
            {"nema_phase": 8, **ns},
        ],
    }
    # Byte for byte, key order included.
    assert json.dumps(j2, indent=2) == json.dumps(expected, indent=2)


def test_program_without_a_green_phase_raises_naming_the_tls(tmp_path):
    phases = (
        PhaseSpec(state="rrrr", duration_s=30, kind="all_red"),
        PhaseSpec(state="yyyy", duration_s=3, kind="yellow"),
    )
    with pytest.raises(ValueError, match="'J9'.*no green phase"):
        export_nema(_program("J9", 0.0, phases), corridor_id="c", out_dir=tmp_path)


def test_splits_that_do_not_fill_the_cycle_raise_naming_the_tls():
    from terasim_city.nema.export import _check_splits_fill_cycle

    signal_plan = _signal_plan("J7", 0.0)  # 90 s cycle
    _check_splits_fill_cycle(signal_plan, [52.0, 38.0])
    with pytest.raises(ValueError, match="'J7'.*81.*90"):
        _check_splits_fill_cycle(signal_plan, [52.0, 29.0])


def test_orchestration_records_an_export_the_exporter_refuses(tmp_path):
    from terasim_city.orchestration.corridor_optimization import _export_nema

    phases = (PhaseSpec(state="rrrr", duration_s=30, kind="all_red"),)
    payload, reason = _export_nema(_program("J9", 0.0, phases), corridor_id="c", out_dir=tmp_path)
    assert payload is None
    assert "NEMA export skipped" in reason and "'J9'" in reason
    assert not (tmp_path / "nema_plan.json").exists()


# ---------------------------------------------------------------------------
# export_nema_plan: time-of-day identity on the handoff file
# ---------------------------------------------------------------------------

def test_nema_plan_export_carries_description_and_windows(tmp_path):
    from terasim_city.nema.export import export_nema_plan
    from terasim_city.plans.nema_plan import NemaCorridorPlan, NemaSignalEntry
    from tests.fixtures.nema_plan_files import DEMO_ARTERIAL_PLAN, plan_intersection

    i1 = plan_intersection(DEMO_ARTERIAL_PLAN, "i1")
    plan = NemaCorridorPlan(
        plan_id="p",
        entries=(NemaSignalEntry("J1", i1, i1["patterns"][0]),),
    )
    payload = export_nema_plan(plan, corridor_id="c", out_dir=tmp_path)
    entry = payload["intersections"][0]
    assert entry["description"] == "AM PEAK"
    # i1 weekday schedule: 06:30 am_peak -> 09:30 midday; its weekend rules
    # never select am_peak, so the weekday window is the only one.
    assert entry["valid_windows"] == [
        {"days": ["Mon", "Tue", "Wed", "Thu", "Fri"], "start": "06:30", "end": "09:30"}
    ]
