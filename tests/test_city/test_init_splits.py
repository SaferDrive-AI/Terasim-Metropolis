"""Tests for the Webster initial green-split solver.

Webster math is checked against hand-computed values; the corridor-level
builder is exercised on the bundled 3-intersection demo map, with expected
state strings derived from the pinned J1 link-movement mapping (see
``test_signal_groups.py``). The same corridor on the demo network re-built
with sidewalks and signalized pedestrian crossings additionally proves that
generated states match the NET's controlled-link counts (every signal controls
its crossing links beyond the vehicle connections) and that SUMO actually
ACCEPTS the materialized plan — the 'Mismatching phase size' regression that
motivated ``controlled_link_count``; states cut to the vehicle links are
rejected on the same net.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from terasim_city.flow.descriptors import TMCData
from terasim_city.plans.init_splits import (
    DEFAULT_ALL_RED_S,
    DEFAULT_MIN_GREEN_S,
    DEFAULT_YELLOW_S,
    PER_APPROACH_ORDER,
    SATURATION_FLOW_VPH,
    STAGE_MODES,
    WEBSTER_PLAN_ID,
    GroupTiming,
    TurnGroup,
    WebsterSolver,
    build_webster_plan,
    stages_from_counts,
    webster_cycle,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_SUMOCFG = DEMO_DIR / "demo.sumocfg"
DEMO_NET = DEMO_DIR / "demo.net.xml"
DEMO_TMC = DEMO_DIR / "demo_tmc.json"

EW_MOVEMENTS = (
    "EB-left", "EB-through", "EB-right", "WB-left", "WB-through", "WB-right",
)
NS_MOVEMENTS = (
    "NB-left", "NB-through", "NB-right", "SB-left", "SB-through", "SB-right",
)

HAS_SUMOLIB = importlib.util.find_spec("sumolib") is not None

requires_demo_map = pytest.mark.skipif(
    not (HAS_SUMOLIB and DEMO_SUMOCFG.exists() and DEMO_TMC.exists()),
    reason="needs sumolib and the examples/corridor_demo assets",
)


def _ew(volume: float) -> TurnGroup:
    return TurnGroup(movements=EW_MOVEMENTS, volume_vph=volume)


def _ns(volume: float) -> TurnGroup:
    return TurnGroup(movements=NS_MOVEMENTS, volume_vph=volume)


# ---------------------------------------------------------------------------
# TurnGroup / GroupTiming validation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebsterSolver.solve — hand-computed cases
# ---------------------------------------------------------------------------


def test_webster_greens_proportional_to_volumes() -> None:
    # L = 2 * (3 + 1) = 8, effective green = 68 - 8 = 60, split 2:1.
    timings = WebsterSolver().solve([_ew(900.0), _ns(450.0)], 68.0)
    assert [t.green_s for t in timings] == pytest.approx([40.0, 20.0])
    assert timings[0].movements == EW_MOVEMENTS
    assert timings[1].movements == NS_MOVEMENTS


def test_webster_min_green_enforced_with_redistribution() -> None:
    # Effective green = 45 - 8 = 37; the 10 veh/h stage's proportional share
    # (~0.37s) pins to min_green=5, leaving 32 for the heavy stage.
    timings = WebsterSolver().solve([_ew(1000.0), _ns(10.0)], 45.0)
    assert [t.green_s for t in timings] == pytest.approx([32.0, 5.0])


def test_webster_redistribution_keeps_survivors_proportional() -> None:
    # 3 stages, cycle 30: L = 12, effective = 18. Proportional shares are
    # (8.78, 8.78, 0.44); the starved stage pins to 5 and the remaining 13
    # splits evenly between the two equal-volume stages.
    groups = [
        TurnGroup(("EB-through",), 100.0),
        TurnGroup(("WB-through",), 100.0),
        TurnGroup(("NB-through",), 5.0),
    ]
    timings = WebsterSolver().solve(groups, 30.0)
    assert [t.green_s for t in timings] == pytest.approx([6.5, 6.5, 5.0])
    assert sum(t.green_s for t in timings) + 3 * 4.0 == pytest.approx(30.0)


def test_webster_zero_volume_splits_evenly() -> None:
    timings = WebsterSolver().solve([_ew(0.0), _ns(0.0)], 45.0)
    assert [t.green_s for t in timings] == pytest.approx([18.5, 18.5])


def test_webster_infeasible_cycle_raises() -> None:
    # 3 stages need 3*5 green + 3*4 lost = 27s; a 20s cycle cannot fit them.
    groups = [
        TurnGroup(("EB-through",), 100.0),
        TurnGroup(("WB-through",), 100.0),
        TurnGroup(("NB-through",), 100.0),
    ]
    with pytest.raises(ValueError, match="infeasible"):
        WebsterSolver().solve(groups, 20.0)


# ---------------------------------------------------------------------------
# webster_cycle
# ---------------------------------------------------------------------------


def test_webster_cycle_formula() -> None:
    # L = 8, Y = (900 + 450) / 1800 = 0.75 -> C0 = 17 / 0.25 = 68.
    assert webster_cycle([_ew(900.0), _ns(450.0)]) == 68.0


def test_webster_cycle_rounds_to_int() -> None:
    # Y = 1250/1800 -> C0 = 17 / (1 - 0.69444) = 55.63... -> 56.
    assert webster_cycle([_ew(850.0), _ns(400.0)]) == 56.0


def test_webster_cycle_clamps_to_min() -> None:
    # Y = 0.1 -> C0 = 18.9 -> clamped up to 45.
    assert webster_cycle([_ew(90.0), _ns(90.0)]) == 45.0


def test_webster_cycle_caps_flow_ratio_and_clamps_to_max() -> None:
    # Y = (1700 + 700) / 1800 = 1.33 capped at 0.9 -> C0 = 17 / 0.1 = 170.
    saturated = [_ew(1700.0), _ns(700.0)]
    assert webster_cycle(saturated) == 150.0  # default max clamp
    assert webster_cycle(saturated, max_cycle_s=200.0) == 170.0


# ---------------------------------------------------------------------------
# stages_from_counts
# ---------------------------------------------------------------------------


def _demo_counts(intersection_id: str) -> dict:
    tmc = TMCData.from_json(DEMO_TMC)
    for intersection in tmc.intersections:
        if intersection.intersection_id == intersection_id:
            return intersection.counts
    raise AssertionError(f"{intersection_id} not in {DEMO_TMC}")


def test_stages_missing_directions_count_as_zero() -> None:
    stage_ew, stage_ns = stages_from_counts({"EB": {"through": 100, "left": 20}})
    assert stage_ew.volume_vph == pytest.approx(120.0)
    assert stage_ns.volume_vph == pytest.approx(0.0)


def test_stages_reject_unknown_keys_and_negative_counts() -> None:
    with pytest.raises(ValueError, match="unknown direction"):
        stages_from_counts({"NE": {"through": 10}})
    with pytest.raises(ValueError, match="unknown turn"):
        stages_from_counts({"EB": {"uturn": 10}})
    with pytest.raises(ValueError, match="finite and >= 0"):
        stages_from_counts({"EB": {"through": -5}})


# ---------------------------------------------------------------------------
# build_webster_plan on the demo corridor
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def demo_corridor():
    from terasim_city.corridor.resolve import resolve_corridor

    return resolve_corridor(DEMO_SUMOCFG, DEMO_TMC, tls_ids=["J1", "J2", "J3"])


@requires_demo_map
def test_build_webster_plan_demo_structure(demo_corridor) -> None:
    plan = build_webster_plan(DEMO_TMC, demo_corridor, DEMO_NET)

    assert plan.plan_id == WEBSTER_PLAN_ID
    assert plan.provenance.source == "webster_init"
    assert [p.tls_id for p in plan.plans] == ["J1", "J2", "J3"]
    assert all(p.offset_s == 0.0 for p in plan.plans)
    # Each intersection: 2 stages x (green, yellow, all_red).
    assert all(len(p.phases) == 6 for p in plan.plans)
    kinds = [phase.kind for phase in plan.plans[0].phases]
    assert kinds == ["green", "yellow", "all_red", "green", "yellow", "all_red"]


@requires_demo_map
def test_build_webster_plan_common_cycle_is_max_of_intersections(demo_corridor) -> None:
    plan = build_webster_plan(DEMO_TMC, demo_corridor, DEMO_NET)
    per_intersection = [
        webster_cycle(stages_from_counts(_demo_counts(iid)))
        for iid in ("i1", "i2", "i3")
    ]
    assert plan.common_cycle_s() == pytest.approx(max(per_intersection))
    for signal_plan in plan.plans:
        assert signal_plan.cycle_s == pytest.approx(max(per_intersection), abs=1e-9)


@requires_demo_map
def test_build_webster_plan_states_match_link_movements(demo_corridor) -> None:
    from terasim_city.plans.signal_groups import derive_link_movements

    plan = build_webster_plan(DEMO_TMC, demo_corridor, DEMO_NET)
    for signal_plan in plan.plans:
        link_movements = derive_link_movements(DEMO_NET, signal_plan.tls_id)
        state_length = max(lm.link_index for lm in link_movements) + 1
        ew_links = {
            lm.link_index for lm in link_movements if lm.movement in EW_MOVEMENTS
        }
        stage1_green, stage1_yellow, all_red1 = signal_plan.phases[0:3]
        stage2_green = signal_plan.phases[3]
        assert len(stage1_green.state) == state_length
        # Each two-stage green serves both directions of one axis, so its
        # lefts run concurrently with the opposing through: permissive 'g'.
        left_links = {
            lm.link_index
            for lm in link_movements
            if lm.movement.endswith("-left")
        }
        for index in range(state_length):
            expect_green = index in ew_links
            green_char = "g" if index in left_links else "G"
            assert stage1_green.state[index] == (green_char if expect_green else "r")
            assert stage1_yellow.state[index] == ("y" if expect_green else "r")
            assert stage2_green.state[index] == ("r" if expect_green else green_char)
        assert all_red1.state == "r" * state_length
        assert stage1_green.movements == EW_MOVEMENTS
        assert stage2_green.movements == NS_MOVEMENTS


@requires_demo_map
def test_build_webster_plan_missing_tmc_intersection_raises(
    demo_corridor, tmp_path
) -> None:
    payload = json.loads(DEMO_TMC.read_text(encoding="utf-8"))
    payload["intersections"] = [
        inter for inter in payload["intersections"]
        if inter["intersection_id"] != "i3"
    ]
    truncated = tmp_path / "tmc_two_intersections.json"
    truncated.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="no TMC counts"):
        build_webster_plan(truncated, demo_corridor, DEMO_NET)


# ---------------------------------------------------------------------------
# build_webster_plan on signals with pedestrian crossings
# ---------------------------------------------------------------------------

from tests.fixtures.demo_corridor import (  # noqa: E402
    DEMO_ROU,
    build_crossing_net,
    write_sumocfg,
)
from tests.test_city.conftest import requires_netconvert  # noqa: E402

requires_sumo_binary = pytest.mark.skipif(
    shutil.which("sumo") is None, reason="needs the `sumo` binary on PATH"
)


@pytest.fixture(scope="module")
def crossing_corridor(tmp_path_factory):
    """The demo corridor (i1 -> i3) on the demo net re-built with crossings.

    Returns ``(net_file, sumocfg, resolved_corridor)``.
    """
    if not HAS_SUMOLIB:
        pytest.skip("sumolib not importable")
    from terasim_city.corridor.resolve import resolve_corridor

    workdir = tmp_path_factory.mktemp("crossing_corridor")
    net = build_crossing_net(workdir)
    sumocfg = write_sumocfg(workdir, net, route_files=(DEMO_ROU,))
    corridor = resolve_corridor(sumocfg, DEMO_TMC, from_ref="i1", to_ref="i3")
    return net, sumocfg, corridor


def _links_from_xml(net: Path, tls_id: str) -> tuple[set[int], set[int]]:
    """(vehicle, crossing) link indices of one signal, read from the net XML.

    Crossing links lead from a walking area into a crossing (both internal
    ``:``-prefixed lanes); vehicle links leave a normal edge.
    """
    import xml.etree.ElementTree as ET

    vehicle: set[int] = set()
    crossing: set[int] = set()
    for conn in ET.parse(net).getroot().iter("connection"):
        if conn.get("tl") != tls_id:
            continue
        index = int(conn.get("linkIndex"))
        (crossing if conn.get("from").startswith(":") else vehicle).add(index)
    return vehicle, crossing


def _run_sumo(sumocfg: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sumo", "-c", str(sumocfg), "-e", "5", "--no-step-log"],
        capture_output=True,
        text=True,
        timeout=300,
    )


@requires_netconvert
def test_build_webster_plan_states_match_controlled_link_counts_with_crossings(
    crossing_corridor,
) -> None:
    from terasim_city.plans.signal_groups import controlled_link_count

    net, _sumocfg, corridor = crossing_corridor
    plan = build_webster_plan(DEMO_TMC, corridor, net)
    plain = build_webster_plan(DEMO_TMC, corridor, DEMO_NET).by_tls()
    assert [p.tls_id for p in plan.plans] == ["J1", "J2", "J3"]
    for signal_plan in plan.plans:
        vehicle, crossing = _links_from_xml(net, signal_plan.tls_id)
        # Premise: 14 vehicle links, then the four crossings.
        assert vehicle == set(range(14)) and crossing == set(range(14, 18))
        expected = controlled_link_count(net, signal_plan.tls_id)
        assert expected == len(vehicle) + len(crossing) == 18
        # EVERY phase state must have the net's controlled-link length — this
        # is what SUMO validates ('Mismatching phase size').
        assert {len(phase.state) for phase in signal_plan.phases} == {expected}
        for phase, plain_phase in zip(
            signal_plan.phases, plain[signal_plan.tls_id].phases
        ):
            # The crossing links pad red; the vehicle part is the plain net's.
            assert phase.state[14:] == "rrrr"
            assert phase.state[:14] == plain_phase.state


@requires_netconvert
@requires_sumo_binary
@pytest.mark.requires_sumo
def test_crossing_corridor_webster_plan_is_accepted_by_sumo(
    crossing_corridor, tmp_path
) -> None:
    """Materialize the Webster plan and prove SUMO loads it (exit 0).

    Regression test: states sized from the vehicle links only (14 instead of
    the net's 18 controlled links) make SUMO hard-reject the tlLogic with
    'Mismatching phase size' — shown by the truncated control plan.
    """
    from dataclasses import replace

    from terasim_city.plans.materialize import materialize_plan

    net, sumocfg, corridor = crossing_corridor
    plan = build_webster_plan(DEMO_TMC, corridor, net)
    trial_sumocfg = materialize_plan(plan, sumocfg, tmp_path / "trial")
    completed = _run_sumo(trial_sumocfg)
    assert completed.returncode == 0, (
        f"sumo rejected the materialized Webster plan:\n{completed.stderr}"
    )
    assert "Mismatching phase size" not in completed.stderr

    vehicle_only = replace(
        plan,
        plans=tuple(
            replace(
                signal_plan,
                phases=tuple(
                    replace(phase, state=phase.state[:14])
                    for phase in signal_plan.phases
                ),
            )
            for signal_plan in plan.plans
        ),
    )
    rejected = _run_sumo(materialize_plan(vehicle_only, sumocfg, tmp_path / "vehicle_only"))
    assert rejected.returncode != 0
    assert "Mismatching phase size" in rejected.stderr


# ---------------------------------------------------------------------------
# Observation-window scaling: TMC window counts -> hourly volumes
# ---------------------------------------------------------------------------


def test_stages_from_counts_scale_multiplies_volumes() -> None:
    counts = {
        "EB": {"left": 10, "through": 50, "right": 20},
        "WB": {"left": 5, "through": 40, "right": 5},
        "NB": {"left": 2, "through": 8, "right": 2},
        "SB": {"left": 1, "through": 4, "right": 1},
    }
    ew, ns = stages_from_counts(counts, scale=6.0)
    assert ew.volume_vph == pytest.approx(80 * 6.0)  # max(sum EB, sum WB)
    assert ns.volume_vph == pytest.approx(12 * 6.0)


def test_stages_from_counts_rejects_nonpositive_scale() -> None:
    with pytest.raises(ValueError, match="scale"):
        stages_from_counts({"EB": {"through": 1}}, scale=0.0)


@pytest.mark.parametrize("bad_scale", [-1.0, float("nan"), float("inf")])
def test_stages_from_counts_rejects_negative_and_non_finite_scale(
    bad_scale: float,
) -> None:
    with pytest.raises(ValueError, match="scale"):
        stages_from_counts({"EB": {"through": 1}}, scale=bad_scale)


@requires_demo_map
def test_ten_minute_window_matches_scaled_hourly_window(
    demo_corridor, tmp_path
) -> None:
    """A 10-minute payload must yield the same seed as the 6x hourly payload."""
    from tests.fixtures.tmc_templates import demo_tmc, write_tmc

    ten = demo_tmc()
    ten["observation_interval"] = "10m"
    hour = demo_tmc()
    hour["observation_interval"] = "1h"
    for item in hour["intersections"]:
        item["counts"] = {
            direction: {turn: 6 * count for turn, count in turns.items()}
            for direction, turns in item["counts"].items()
        }
    ten_path = write_tmc(tmp_path, ten, name="ten.json")
    hour_path = write_tmc(tmp_path, hour, name="hour.json")

    plan_ten = build_webster_plan(ten_path, demo_corridor, DEMO_NET)
    plan_hour = build_webster_plan(hour_path, demo_corridor, DEMO_NET)

    assert plan_ten.common_cycle_s() == plan_hour.common_cycle_s()
    assert [[p.duration_s for p in sp.phases] for sp in plan_ten.plans] == [
        [p.duration_s for p in sp.phases] for sp in plan_hour.plans
    ]


@requires_demo_map
def test_build_webster_plan_scale_is_3600_over_the_observation_window(
    demo_corridor, tmp_path
) -> None:
    """A 40-minute window reads x1.5. Unsaturated, so the exact factor shows
    in the cycle (the 10-minute test above saturates at the cycle cap)."""
    from tests.fixtures.tmc_templates import demo_tmc, write_tmc

    payload = demo_tmc()
    payload["observation_interval"] = "40m"
    path = write_tmc(tmp_path, payload, name="forty.json")

    plan = build_webster_plan(path, demo_corridor, DEMO_NET)

    # Busiest intersection i2: per 40 min the critical E-W approach carries 740
    # and the critical N-S approach 270 -> x1.5 = 1110 and 405 veh/h,
    # Y = 1515 / 1800, C0 = (1.5 * 8 + 5) / (1 - Y) = 107.4 -> 107.
    assert plan.common_cycle_s() == pytest.approx(107.0)
    assert dict(plan.provenance.params) == {
        "cycle_s": 107.0,
        "count_scale": 1.5,
        "stage_mode": "two_stage",
    }


# ---------------------------------------------------------------------------
# TurnGroup saturation flow + lane-aware Webster math
# ---------------------------------------------------------------------------


def test_turn_group_saturation_defaults_to_one_lane_and_validates() -> None:
    group = TurnGroup(("EB-through",), 900.0)
    assert group.saturation_vph == SATURATION_FLOW_VPH
    assert group.flow_ratio == pytest.approx(0.5)
    assert TurnGroup(("EB-through",), 900.0, saturation_vph=3).saturation_vph == 3.0
    for bad in (0.0, -1800.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="saturation_vph"):
            TurnGroup(("EB-through",), 900.0, saturation_vph=bad)


def test_webster_uses_flow_ratios_per_group() -> None:
    a = TurnGroup(("EB-through",), 1800.0, saturation_vph=3600.0)  # y = 0.5
    b = TurnGroup(("NB-through",), 900.0, saturation_vph=1800.0)  # y = 0.5
    timings = WebsterSolver().solve((a, b), 100.0)
    assert timings[0].green_s == pytest.approx(timings[1].green_s)
    assert webster_cycle((a, b)) == webster_cycle((TurnGroup(("EB-through",), 900.0), b))


def test_webster_cycle_uses_lane_aware_flow_ratios() -> None:
    # y = 2700 / 5400 = 0.5 and 450 / 1800 = 0.25 -> Y = 0.75, L = 8 ->
    # C0 = 17 / 0.25 = 68. Reading the wide stage against a flat 1800 veh/h
    # would give Y = 1.75, capped at 0.9, i.e. the 150 s maximum.
    wide = TurnGroup(("EB-through",), 2700.0, saturation_vph=5400.0)
    narrow = TurnGroup(("NB-through",), 450.0)
    assert webster_cycle([wide, narrow]) == 68.0


# ---------------------------------------------------------------------------
# Per-approach stage mode: one protected stage per approach
# ---------------------------------------------------------------------------


def test_per_approach_gives_every_approach_with_lanes_a_stage_even_without_demand() -> None:
    counts = {
        "EB": {"left": 100, "through": 500, "right": 100},
        "WB": {"left": 0, "through": 0, "right": 0},
        "NB": {"left": 50, "through": 300, "right": 50},
        "SB": {"left": 20, "through": 200, "right": 30},
    }
    # WB carries no demand but has incoming lanes (controlled links), so it still gets
    # its stage (spec G2: a real approach is never left permanently red); all stages
    # follow SB, WB, NB, EB.
    groups = stages_from_counts(
        counts, mode="per_approach", lanes={"EB": 3, "WB": 2, "NB": 2}
    )
    assert [g.movements[0].split("-")[0] for g in groups] == ["SB", "WB", "NB", "EB"]
    by_dir = {g.movements[0].split("-")[0]: g for g in groups}
    assert by_dir["WB"].movements == ("WB-left", "WB-through", "WB-right")
    assert by_dir["WB"].volume_vph == 0.0
    assert by_dir["WB"].saturation_vph == 2 * SATURATION_FLOW_VPH
    assert by_dir["EB"].movements == ("EB-left", "EB-through", "EB-right")
    assert by_dir["EB"].volume_vph == 700
    assert by_dir["EB"].saturation_vph == 3 * SATURATION_FLOW_VPH
    assert by_dir["NB"].saturation_vph == 2 * SATURATION_FLOW_VPH
    assert by_dir["SB"].saturation_vph == SATURATION_FLOW_VPH  # no lane info -> 1 lane
    # The solver holds the empty approach at the minimum green.
    timings = WebsterSolver().solve(groups, 90.0)
    assert timings[1].movements[0] == "WB-left"
    assert timings[1].green_s == DEFAULT_MIN_GREEN_S


def test_per_approach_skips_an_approach_with_neither_lanes_nor_demand() -> None:
    counts = {"EB": {"through": 500}, "WB": {"through": 0}, "NB": {"through": 300}}
    groups = stages_from_counts(counts, mode="per_approach", lanes={"EB": 2, "NB": 1})
    assert [g.movements[0].split("-")[0] for g in groups] == ["NB", "EB"]


def test_per_approach_without_demand_raises() -> None:
    with pytest.raises(ValueError, match="no approach"):
        stages_from_counts({"EB": {"through": 0}}, mode="per_approach")


def test_stage_mode_validated() -> None:
    with pytest.raises(ValueError):
        stages_from_counts({"EB": {"through": 1}}, mode="three_stage")
    assert STAGE_MODES == ("two_stage", "per_approach")
    assert PER_APPROACH_ORDER == ("SB", "WB", "NB", "EB")


def test_per_approach_scale_converts_window_counts_to_hourly() -> None:
    counts = {"EB": {"left": 10, "through": 50, "right": 20}, "NB": {"through": 8}}
    groups = stages_from_counts(counts, scale=6.0, mode="per_approach")
    assert [g.volume_vph for g in groups] == pytest.approx([8 * 6.0, 80 * 6.0])  # NB, EB


def test_per_approach_lane_counts_floor_at_one_lane() -> None:
    groups = stages_from_counts(
        {"EB": {"through": 100}}, mode="per_approach", lanes={"EB": 0}
    )
    assert groups[0].saturation_vph == SATURATION_FLOW_VPH


def test_two_stage_groups_keep_the_default_saturation_flow() -> None:
    # The lane map only matters to per_approach; two_stage stays numerically
    # what it always was.
    counts = {"EB": {"through": 900}, "NB": {"through": 450}}
    for groups in (
        stages_from_counts(counts),
        stages_from_counts(counts, lanes={"EB": 3, "NB": 2}),
    ):
        assert [g.saturation_vph for g in groups] == [SATURATION_FLOW_VPH] * 2
        assert [g.volume_vph for g in groups] == [900.0, 450.0]


# ---------------------------------------------------------------------------
# build_webster_plan(stage_mode="per_approach") on the demo corridor
# ---------------------------------------------------------------------------


@requires_demo_map
def test_build_webster_plan_per_approach_protects_lefts(demo_corridor) -> None:
    plan = build_webster_plan(
        DEMO_TMC, demo_corridor, DEMO_NET, stage_mode="per_approach"
    )
    for signal_plan in plan.plans:
        greens = [p for p in signal_plan.phases if p.kind == "green"]
        assert 1 <= len(greens) <= 4
        for phase in greens:
            assert "g" not in phase.state, phase.state  # protected: no permissive lefts
            directions = {m.split("-")[0] for m in phase.movements}
            assert len(directions) == 1
    assert ("stage_mode", "per_approach") in plan.provenance.params


@requires_demo_map
def test_build_webster_plan_rejects_unknown_stage_mode(demo_corridor) -> None:
    with pytest.raises(ValueError, match="stage_mode"):
        build_webster_plan(DEMO_TMC, demo_corridor, DEMO_NET, stage_mode="three_stage")


@requires_demo_map
def test_build_webster_plan_per_approach_orders_stages_and_honours_cycle_bounds(
    demo_corridor,
) -> None:
    # Demand on all four approaches -> four stages per TLS, SB, WB, NB, EB.
    # The unbounded cycle is ~81 s, so both clamps below actually bite.
    floor = build_webster_plan(
        DEMO_TMC, demo_corridor, DEMO_NET, stage_mode="per_approach",
        cycle_min_s=100.0, cycle_max_s=120.0,
    )
    ceiling = build_webster_plan(
        DEMO_TMC, demo_corridor, DEMO_NET, stage_mode="per_approach",
        cycle_min_s=45.0, cycle_max_s=60.0,
    )
    assert floor.common_cycle_s() == pytest.approx(100.0)
    assert ceiling.common_cycle_s() == pytest.approx(60.0)
    assert dict(floor.provenance.params)["cycle_s"] == 100.0
    for signal_plan in floor.plans:
        order = [
            phase.movements[0].split("-")[0]
            for phase in signal_plan.phases
            if phase.kind == "green"
        ]
        assert order == list(PER_APPROACH_ORDER)
        assert len(signal_plan.phases) == 12  # 4 stages x (green, yellow, all_red)


@requires_demo_map
def test_build_webster_plan_per_approach_greens_follow_lane_aware_flow_ratios(
    demo_corridor,
) -> None:
    plan = build_webster_plan(
        DEMO_TMC, demo_corridor, DEMO_NET, stage_mode="per_approach"
    )
    j2 = plan.by_tls()["J2"]
    counts = _demo_counts("i2")
    # The demo net gives every junction two lanes on the E-W approaches and one
    # on the N-S approaches (see test_signal_groups.py).
    lanes = {"EB": 2, "WB": 2, "NB": 1, "SB": 1}
    ratio = {
        d: sum(counts[d].values()) / (lanes[d] * SATURATION_FLOW_VPH)
        for d in PER_APPROACH_ORDER
    }
    effective_s = j2.cycle_s - 4 * (DEFAULT_YELLOW_S + DEFAULT_ALL_RED_S)
    expected = [
        effective_s * ratio[d] / sum(ratio.values()) for d in PER_APPROACH_ORDER
    ]
    greens = [p.duration_s for p in j2.phases if p.kind == "green"]
    assert greens == pytest.approx(expected)


@requires_demo_map
def test_build_webster_plan_per_approach_serves_a_zero_count_approach_at_min_green(
    demo_corridor, tmp_path
) -> None:
    payload = json.loads(DEMO_TMC.read_text(encoding="utf-8"))
    i2 = next(item for item in payload["intersections"] if item["intersection_id"] == "i2")
    i2["counts"]["North"] = {turn: 0 for turn in i2["counts"]["North"]}
    tmc_path = tmp_path / "zero_north.json"
    tmc_path.write_text(json.dumps(payload), encoding="utf-8")
    (empty,) = [
        direction
        for direction, turns in TMCData.from_json(tmc_path).intersections[1].counts.items()
        if not sum(turns.values())
    ]

    plan = build_webster_plan(tmc_path, demo_corridor, DEMO_NET, stage_mode="per_approach")

    j2 = plan.by_tls()["J2"]
    greens = [phase for phase in j2.phases if phase.kind == "green"]
    assert [g.movements[0].split("-")[0] for g in greens] == list(PER_APPROACH_ORDER)
    (stage,) = [g for g in greens if g.movements[0].startswith(empty + "-")]
    assert stage.duration_s == DEFAULT_MIN_GREEN_S
    # The stage really turns that approach's links green.
    from terasim_city.plans.signal_groups import derive_link_movements

    links = [lm for lm in derive_link_movements(DEMO_NET, "J2") if lm.movement.startswith(empty + "-")]
    assert links and all(stage.state[lm.link_index] == "G" for lm in links)


@requires_demo_map
def test_build_webster_plan_per_approach_lane_counts_honour_overrides(
    demo_corridor, tmp_path
) -> None:
    # Re-labelling J1 link 7 (a left off the first EB lane) as NB-left makes
    # that lane serve NB too: NB then counts two incoming lanes.
    overrides = tmp_path / "signal_groups.json"
    overrides.write_text(json.dumps({"J1": {"7": "NB-left"}}), encoding="utf-8")
    plan = build_webster_plan(
        DEMO_TMC, demo_corridor, DEMO_NET, stage_mode="per_approach",
        overrides_path=overrides,
    )
    j1 = plan.by_tls()["J1"]
    counts = _demo_counts("i1")
    lanes = {"EB": 2, "WB": 2, "NB": 2, "SB": 1}
    ratio = {
        d: sum(counts[d].values()) / (lanes[d] * SATURATION_FLOW_VPH)
        for d in PER_APPROACH_ORDER
    }
    effective_s = j1.cycle_s - 4 * (DEFAULT_YELLOW_S + DEFAULT_ALL_RED_S)
    expected = [
        effective_s * ratio[d] / sum(ratio.values()) for d in PER_APPROACH_ORDER
    ]
    greens = [p.duration_s for p in j1.phases if p.kind == "green"]
    assert greens == pytest.approx(expected)
