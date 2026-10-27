"""Tests for terasim_city.plans.baseline_sheet on the corridor demo assets."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from terasim_city.plans.baseline_sheet import load_timing_sheet
from terasim_city.plans.signal_groups import derive_link_movements

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_NET = DEMO_DIR / "demo.net.xml"
DEMO_SHEET = DEMO_DIR / "baseline_sheet.json"


def _write_sheet(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "sheet.json"
    path.write_text(json.dumps(payload))
    return path


@pytest.fixture()
def demo_sheet_payload() -> dict:
    return json.loads(DEMO_SHEET.read_text())


# ---------------------------------------------------------------------------
# Happy path on the bundled demo sheet
# ---------------------------------------------------------------------------


def test_demo_sheet_loads_baseline_corridor_plan() -> None:
    plan = load_timing_sheet(DEMO_SHEET, net_file=DEMO_NET)
    assert plan.plan_id == "baseline"
    assert plan.provenance.source == "baseline_sheet"
    assert [p.tls_id for p in plan.plans] == ["J1", "J2", "J3"]
    assert [p.offset_s for p in plan.plans] == [0.0, 20.0, 40.0]
    for signal_plan in plan.plans:
        assert signal_plan.program_id == "baseline"
        assert signal_plan.cycle_s == pytest.approx(90.0)
    assert plan.common_cycle_s() == pytest.approx(90.0)


def test_demo_sheet_phase_expansion_structure() -> None:
    plan = load_timing_sheet(DEMO_SHEET, net_file=DEMO_NET)
    for signal_plan in plan.plans:
        # 2 sheet stages x (green, yellow, all_red), none zero-duration.
        assert [p.kind for p in signal_plan.phases] == [
            "green", "yellow", "all_red",
            "green", "yellow", "all_red",
        ]
        assert [p.duration_s for p in signal_plan.phases] == [48, 3, 1, 34, 3, 1]
        greens = [p for p in signal_plan.phases if p.kind == "green"]
        assert greens[0].nema_phases == (2, 6)
        assert greens[1].nema_phases == (4, 8)


def test_demo_sheet_green_states_drive_the_right_links() -> None:
    """Cross-check every green state against derive_link_movements."""
    plan = load_timing_sheet(DEMO_SHEET, net_file=DEMO_NET)
    for signal_plan in plan.plans:
        link_movements = derive_link_movements(DEMO_NET, signal_plan.tls_id)
        length = max(lm.link_index for lm in link_movements) + 1
        for phase in signal_plan.phases:
            assert len(phase.state) == length
            if phase.kind == "all_red":
                assert phase.state == "r" * length
                continue
            expected_char = "G" if phase.kind == "green" else "y"
            served = set(phase.movements)
            expected = {
                lm.link_index for lm in link_movements if lm.movement in served
            }
            for lm in link_movements:
                want = expected_char if lm.link_index in expected else "r"
                direction, _, turn = lm.movement.partition("-")
                opposing = {"EB": "WB", "WB": "EB", "NB": "SB", "SB": "NB"}[direction]
                if (
                    want == "G"
                    and turn == "left"
                    and f"{opposing}-through" in served
                ):
                    # A left green concurrently with its opposing through is
                    # permissive and must yield.
                    want = "g"
                assert phase.state[lm.link_index] == want, (
                    f"{signal_plan.tls_id} {phase.name}: link {lm.link_index} "
                    f"({lm.movement}) is {phase.state[lm.link_index]!r}, want {want!r}"
                )


# ---------------------------------------------------------------------------
# tls_id mapping
# ---------------------------------------------------------------------------


def test_null_tls_id_requires_mapping(tmp_path: Path, demo_sheet_payload: dict) -> None:
    payload = copy.deepcopy(demo_sheet_payload)
    for entry in payload["intersections"]:
        entry["tls_id"] = None
    path = _write_sheet(tmp_path, payload)
    with pytest.raises(ValueError, match="tls_by_intersection"):
        load_timing_sheet(path, net_file=DEMO_NET)


def test_null_tls_id_resolved_via_mapping(tmp_path: Path, demo_sheet_payload: dict) -> None:
    payload = copy.deepcopy(demo_sheet_payload)
    for entry in payload["intersections"]:
        entry["tls_id"] = None
    path = _write_sheet(tmp_path, payload)
    plan = load_timing_sheet(
        path,
        net_file=DEMO_NET,
        tls_by_intersection={"i1": "J1", "i2": "J2", "i3": "J3"},
    )
    assert [p.tls_id for p in plan.plans] == ["J1", "J2", "J3"]


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_bad_cycle_sum_raises(tmp_path: Path, demo_sheet_payload: dict) -> None:
    payload = copy.deepcopy(demo_sheet_payload)
    payload["intersections"][1]["phases"][0]["green_s"] = 50  # 92 != 90
    path = _write_sheet(tmp_path, payload)
    with pytest.raises(ValueError, match=r"i2.*92.*90"):
        load_timing_sheet(path, net_file=DEMO_NET)


def test_unknown_movement_raises(tmp_path: Path, demo_sheet_payload: dict) -> None:
    payload = copy.deepcopy(demo_sheet_payload)
    payload["intersections"][0]["phases"][0]["movements"][0] = "EB-straight"
    path = _write_sheet(tmp_path, payload)
    with pytest.raises(ValueError, match="EB-straight"):
        load_timing_sheet(path, net_file=DEMO_NET)


def test_wrong_schema_raises(tmp_path: Path, demo_sheet_payload: dict) -> None:
    payload = copy.deepcopy(demo_sheet_payload)
    payload["schema"] = "terasim.timing_sheet.v2"
    path = _write_sheet(tmp_path, payload)
    with pytest.raises(ValueError, match="terasim.timing_sheet.v1"):
        load_timing_sheet(path, net_file=DEMO_NET)


def test_missing_green_s_raises(tmp_path: Path, demo_sheet_payload: dict) -> None:
    payload = copy.deepcopy(demo_sheet_payload)
    del payload["intersections"][0]["phases"][0]["green_s"]
    path = _write_sheet(tmp_path, payload)
    with pytest.raises(ValueError, match="green_s"):
        load_timing_sheet(path, net_file=DEMO_NET)


# ---------------------------------------------------------------------------
# Overrides + zero-duration expansion
# ---------------------------------------------------------------------------


def test_signal_group_overrides_file_honored(
    tmp_path: Path, demo_sheet_payload: dict
) -> None:
    # Reclassify J1 link 8 (an EB-through lane) as NB-through: it must go red
    # in the EW stage and green in the NS stage.
    overrides_path = tmp_path / "signal_groups.json"
    overrides_path.write_text(json.dumps({"J1": {"8": "NB-through"}}))
    plan = load_timing_sheet(
        DEMO_SHEET, net_file=DEMO_NET, overrides_path=overrides_path
    )
    j1 = plan.by_tls()["J1"]
    ew_green, ns_green = j1.phases[0], j1.phases[3]
    assert ew_green.state[8] == "r"
    assert ns_green.state[8] == "G"
    # Untouched J2 keeps the derived classification.
    j2 = plan.by_tls()["J2"]
    assert j2.phases[0].state[8] == "G"


def test_zero_duration_specs_are_skipped(tmp_path: Path, demo_sheet_payload: dict) -> None:
    payload = copy.deepcopy(demo_sheet_payload)
    for entry in payload["intersections"]:
        stage1 = entry["phases"][0]
        stage1["all_red_s"] = 0
        stage1["green_s"] = 49  # keep the 90 s cycle closed
    path = _write_sheet(tmp_path, payload)
    plan = load_timing_sheet(path, net_file=DEMO_NET)
    for signal_plan in plan.plans:
        assert [p.kind for p in signal_plan.phases] == [
            "green", "yellow",
            "green", "yellow", "all_red",
        ]
        assert signal_plan.cycle_s == pytest.approx(90.0)
