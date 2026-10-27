"""Seed plans built from a scenario's <tlLogic> programs."""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

from terasim_city.plans.signal_groups import derive_link_movements
from terasim_city.plans.sumo_programs import _phase_kind, load_scenario_programs
from tests.test_city.conftest import REPO_ROOT, requires_sumolib

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"


@requires_sumolib
def test_loads_demo_programs_with_kinds_and_movements():
    plan = load_scenario_programs(DEMO_DIR / "demo.sumocfg", ["J1", "J2", "J3"])
    assert plan.plan_id == "scenario_programs" and plan.provenance.source == "sumo_tls"
    assert [p.tls_id for p in plan.plans] == ["J1", "J2", "J3"]
    for sp in plan.plans:
        kinds = [ph.kind for ph in sp.phases]
        assert "green" in kinds and "yellow" in kinds
        for ph in sp.phases:
            if ph.kind == "green":
                assert ph.movements and all("-" in m for m in ph.movements)
            assert ph.duration_s > 0


@requires_sumolib
def test_last_definition_wins_and_fractional_durations_load(tmp_path):
    """Additional-file programs override the net's; a 4.50 s duration parses."""
    import shutil
    shutil.copytree(DEMO_DIR, tmp_path / "demo")
    tls_path = tmp_path / "demo" / "demo.tls.xml"
    text = tls_path.read_text(encoding="utf-8")
    first_duration = text.split('duration="', 1)[1].split('"', 1)[0]
    text = text.replace(f'duration="{first_duration}"', 'duration="4.50"', 1)
    tls_path.write_text(text, encoding="utf-8")
    plan = load_scenario_programs(tmp_path / "demo" / "demo.sumocfg", ["J1"])
    assert any(ph.duration_s == 4.5 for ph in plan.plans[0].phases)


@requires_sumolib
def test_unknown_tls_fails_clearly():
    with pytest.raises(ValueError, match="no <tlLogic>"):
        load_scenario_programs(DEMO_DIR / "demo.sumocfg", ["does-not-exist"])


# ---------------------------------------------------------------------------
# What the program loader reads, and in which order
# ---------------------------------------------------------------------------

EAST_WEST = {f"{c}-{t}" for c in ("EB", "WB") for t in ("left", "through", "right")}
NORTH_SOUTH = {f"{c}-{t}" for c in ("NB", "SB") for t in ("left", "through", "right")}


def _demo_copy(tmp_path: Path) -> Path:
    """A private copy of the demo scenario; returns the copy's directory."""
    shutil.copytree(DEMO_DIR, tmp_path / "demo")
    return tmp_path / "demo"


def _set_additional_files(sumocfg: Path, files: str) -> None:
    text = sumocfg.read_text(encoding="utf-8")
    assert 'value="demo.tls.xml"' in text
    sumocfg.write_text(text.replace('value="demo.tls.xml"', f'value="{files}"'), encoding="utf-8")


@requires_sumolib
def test_demo_programs_are_read_as_written():
    sumocfg = DEMO_DIR / "demo.sumocfg"
    plan = load_scenario_programs(sumocfg, ["J1", "J2", "J3"])
    assert plan.provenance.params == (
        ("base_sumocfg", str(sumocfg)),
        ("program_types", "J1:static,J2:static,J3:static"),
    )
    for signal_plan in plan.plans:
        # demo.tls.xml's alt_fixed program (30/3/30/3), not the net's own 42/3/42/3 program "0"
        assert signal_plan.program_id.endswith("alt_fixed") and signal_plan.offset_s == 0.0
        assert [(ph.kind, ph.duration_s, ph.state) for ph in signal_plan.phases] == [
            ("green", 30.0, "GGGgrrrGGGgrrr"),
            ("yellow", 3.0, "yyyyrrryyyyrrr"),
            ("green", 30.0, "rrrrGGgrrrrGGg"),
            ("yellow", 3.0, "rrrryyyrrrryyy"),
        ]
        east_west, north_south = signal_plan.phases[0].movements, signal_plan.phases[2].movements
        # each movement is listed once, however many lanes serve it
        assert len(east_west) == len(set(east_west)) and set(east_west) == EAST_WEST
        assert len(north_south) == len(set(north_south)) and set(north_south) == NORTH_SOUTH
        assert signal_plan.phases[1].movements == signal_plan.phases[3].movements == ()


@requires_sumolib
def test_net_program_is_used_when_no_additional_file_defines_one(tmp_path):
    demo = _demo_copy(tmp_path)
    sumocfg = demo / "demo.sumocfg"
    sumocfg.write_text(
        sumocfg.read_text(encoding="utf-8").replace('<additional-files value="demo.tls.xml"/>', ""),
        encoding="utf-8",
    )
    assert "additional-files" not in sumocfg.read_text(encoding="utf-8")
    (signal_plan,) = load_scenario_programs(sumocfg, ["J1"]).plans
    assert [ph.duration_s for ph in signal_plan.phases] == [42.0, 3.0, 42.0, 3.0]
    assert signal_plan.program_id.endswith("0")


@requires_sumolib
@pytest.mark.parametrize(
    ("files", "j1_durations", "j1_offset"),
    [
        ("demo.tls.xml,override.tls.xml", [20.0, 4.0, 20.0, 4.0], 7.0),
        ("override.tls.xml,demo.tls.xml", [30.0, 3.0, 30.0, 3.0], 0.0),
    ],
)
def test_the_later_additional_file_wins(tmp_path, files, j1_durations, j1_offset):
    demo = _demo_copy(tmp_path)
    (demo / "override.tls.xml").write_text(
        """<additional>
  <tlLogic id="J1" type="static" programID="late" offset="7">
    <phase duration="20" state="GGGgrrrGGGgrrr"/>
    <phase duration="4" state="yyyyrrryyyyrrr"/>
    <phase duration="20" state="rrrrGGgrrrrGGg"/>
    <phase duration="4" state="rrrryyyrrrryyy"/>
  </tlLogic>
</additional>
""",
        encoding="utf-8",
    )
    _set_additional_files(demo / "demo.sumocfg", files)
    j1, j2 = load_scenario_programs(demo / "demo.sumocfg", ["J1", "J2"]).plans
    assert [ph.duration_s for ph in j1.phases] == j1_durations and j1.offset_s == j1_offset
    assert [ph.duration_s for ph in j2.phases] == [30.0, 3.0, 30.0, 3.0]  # only demo.tls.xml has J2


@requires_sumolib
@pytest.mark.parametrize(("offset", "wrapped"), [("70", 4.0), ("-6", 60.0), ("66", 0.0)])
def test_offset_is_wrapped_into_the_cycle(tmp_path, offset, wrapped):
    demo = _demo_copy(tmp_path)
    tls_path = demo / "demo.tls.xml"
    text = tls_path.read_text(encoding="utf-8")
    tls_path.write_text(text.replace('offset="0"', f'offset="{offset}"', 1), encoding="utf-8")
    (signal_plan,) = load_scenario_programs(demo / "demo.sumocfg", ["J1"]).plans
    assert signal_plan.cycle_s == 66.0 and signal_plan.offset_s == wrapped


@requires_sumolib
def test_signal_group_overrides_relabel_the_movements(tmp_path):
    through_links = [
        lm.link_index
        for lm in derive_link_movements(DEMO_DIR / "demo.net.xml", "J2")
        if lm.movement == "EB-through"
    ]
    overrides = tmp_path / "signal_groups.json"
    overrides.write_text(
        json.dumps({"J2": {str(link): "EB-left" for link in through_links}}), encoding="utf-8"
    )
    plan = load_scenario_programs(
        DEMO_DIR / "demo.sumocfg", ["J1", "J2"], overrides_path=overrides
    )
    j1, j2 = plan.plans
    assert "EB-through" in j1.phases[0].movements
    assert "EB-through" not in j2.phases[0].movements and "EB-left" in j2.phases[0].movements


@pytest.mark.parametrize(
    ("state", "kind"),
    [
        ("GGGgrrrGGGgrrr", "green"),
        ("rrrgrr", "green"),
        ("yyyyrrryyyyrrr", "yellow"),
        ("GGyyrr", "yellow"),  # still yellow somewhere: a transition, not a green stage
        ("rrrrrr", "all_red"),
        ("rrssoO", "all_red"),
        ("rruuuu", "all_red"),
        ("rrxxrr", "other"),
    ],
)
def test_phase_kind_follows_the_state_string(state, kind):
    assert _phase_kind(state) == kind


# ---------------------------------------------------------------------------
# Programs that are not fixed-time are flattened, loudly
# ---------------------------------------------------------------------------

def _rewrite_tls(demo: Path, tls_id: str, edit) -> None:
    """Apply ``edit(<tlLogic> text)`` to one program of the copy's demo.tls.xml."""
    tls_path = demo / "demo.tls.xml"
    text = tls_path.read_text(encoding="utf-8")
    start = text.index(f'<tlLogic id="{tls_id}"')
    end = text.index("</tlLogic>", start)
    tls_path.write_text(text[:start] + edit(text[start:end]) + text[end:], encoding="utf-8")


def _actuated(block: str) -> str:
    assert 'type="static"' in block and 'duration="30"' in block
    return block.replace('type="static"', 'type="actuated"').replace(
        'duration="30"', 'duration="30" minDur="10" maxDur="45"'
    )


@requires_sumolib
def test_actuated_program_is_flattened_with_a_warning_and_its_type_recorded(tmp_path, caplog):
    demo = _demo_copy(tmp_path)
    _rewrite_tls(demo, "J2", _actuated)
    with caplog.at_level(logging.WARNING, logger="terasim_city.plans.sumo_programs"):
        plan = load_scenario_programs(demo / "demo.sumocfg", ["J1", "J2", "J3"])

    # One entry for the whole plan, in TLS order, next to the scenario it came from.
    assert dict(plan.provenance.params)["program_types"] == "J1:static,J2:actuated,J3:static"
    # Only the actuated TLS is reported, and the warning says what happened to its program.
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "J2" in warning and "actuated" in warning and "fixed-time" in warning
    # It really is the fixed-time reading: the nominal durations, minDur/maxDur dropped.
    assert [(ph.kind, ph.duration_s) for ph in plan.plans[1].phases] == [
        ("green", 30.0), ("yellow", 3.0), ("green", 30.0), ("yellow", 3.0),
    ]


@requires_sumolib
def test_a_missing_type_means_static_and_the_types_follow_the_requested_order(tmp_path, caplog):
    demo = _demo_copy(tmp_path)
    _rewrite_tls(demo, "J1", lambda block: block.replace(' type="static"', ""))
    with caplog.at_level(logging.WARNING, logger="terasim_city.plans.sumo_programs"):
        plan = load_scenario_programs(demo / "demo.sumocfg", ["J3", "J1"])
    assert dict(plan.provenance.params)["program_types"] == "J3:static,J1:static"
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


@requires_sumolib
def test_a_phase_that_turns_only_crossings_green_is_a_fixed_other_phase(tmp_path, monkeypatch):
    """An exclusive pedestrian phase serves no vehicle movement: it is kept at its duration
    ("other", like an all-red interval) instead of becoming a searchable green stage."""
    from terasim_city.plans import sumo_programs

    demo = _demo_copy(tmp_path)
    # J1's link 13 stands in for a pedestrian crossing: no vehicle movement uses it.
    real = sumo_programs.derive_link_movements

    def without_link_13(net_file, tls_id, overrides=None):
        links = real(net_file, tls_id, overrides=overrides)
        return tuple(lm for lm in links if not (tls_id == "J1" and lm.link_index == 13))

    monkeypatch.setattr(sumo_programs, "derive_link_movements", without_link_13)
    _rewrite_tls(
        demo,
        "J1",
        lambda block: block.replace(
            '<phase duration="3" state="rrrryyyrrrryyy"/>',
            '<phase duration="3" state="rrrryyyrrrryyy"/>\n'
            '    <phase duration="12" state="rrrrrrrrrrrrrG"/>',
        ),
    )

    plan = load_scenario_programs(demo / "demo.sumocfg", ["J1"])

    phases = plan.plans[0].phases
    assert [(ph.kind, ph.duration_s) for ph in phases] == [
        ("green", 30.0), ("yellow", 3.0), ("green", 30.0), ("yellow", 3.0), ("other", 12.0),
    ]
    assert phases[-1].movements == ()
    # The north-south stage that also greens the stand-in link still serves its vehicles.
    assert phases[2].movements
