"""Tests for terasim_city.plans.signal_groups.

Classification ground truth comes from the corridor demo map, read
independently from ``demo.net.xml`` with ElementTree: the demo edges are named
after their travel direction (``EB_*``, ``WB_*``, ``NB_J*``, ``SB_J*``) and
every controlled connection carries an explicit ``dir`` attribute, so the
expected movement per link index can be derived without going through the
code under test.

The demo network re-built with sidewalks and signalized pedestrian crossings
(``tests/fixtures/demo_corridor.build_crossing_net``) covers the
controlled-link-count contract: its signals control crossing links beyond the
vehicle connections, so full state strings must be sized via
``controlled_link_count`` (the net's own tlLogic state length) and padded red
past the vehicle links.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from terasim_city.plans.signal_groups import (
    LinkMovement,
    approach_lane_counts,
    build_state,
    controlled_link_count,
    derive_link_movements,
    load_overrides,
    yield_permissive_lefts,
)
from tests.fixtures.demo_corridor import build_crossing_net
from tests.test_city.conftest import requires_netconvert, requires_sumolib

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_NET = REPO_ROOT / "examples" / "corridor_demo" / "demo.net.xml"

TLS_IDS = ("J1", "J2", "J3")

_DIR_TO_TURN = {"s": "through", "l": "left", "r": "right", "t": "left"}
_EDGE_PREFIX_TO_CARDINAL = {"EB": "EB", "WB": "WB", "NB": "NB", "SB": "SB"}


def _ground_truth(tls_id: str) -> dict[int, str]:
    """Expected {link_index: movement} straight from the net XML."""
    truth: dict[int, str] = {}
    for conn in ET.parse(DEMO_NET).getroot().iter("connection"):
        if conn.get("tl") != tls_id:
            continue
        from_edge = conn.get("from")
        cardinal = _EDGE_PREFIX_TO_CARDINAL[from_edge.split("_")[0]]
        turn = _DIR_TO_TURN[conn.get("dir")]
        truth[int(conn.get("linkIndex"))] = f"{cardinal}-{turn}"
    assert truth, f"no controlled connections found for {tls_id}"
    return truth


def _ground_truth_lane_counts(tls_id: str) -> dict[str, int]:
    """Expected {approach: distinct incoming lanes} straight from the net XML."""
    lanes: dict[str, set[tuple[str, str]]] = {}
    for conn in ET.parse(DEMO_NET).getroot().iter("connection"):
        if conn.get("tl") != tls_id:
            continue
        from_edge = conn.get("from")
        cardinal = _EDGE_PREFIX_TO_CARDINAL[from_edge.split("_")[0]]
        lanes.setdefault(cardinal, set()).add((from_edge, conn.get("fromLane")))
    assert lanes, f"no controlled connections found for {tls_id}"
    return {cardinal: len(ids) for cardinal, ids in lanes.items()}


# ---------------------------------------------------------------------------
# derive_link_movements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tls_id", TLS_IDS)
def test_derive_matches_net_ground_truth(tls_id: str) -> None:
    derived = derive_link_movements(DEMO_NET, tls_id)
    assert {lm.link_index: lm.movement for lm in derived} == _ground_truth(tls_id)
    # Sorted by link index, one entry per controlled link.
    indices = [lm.link_index for lm in derived]
    assert indices == sorted(set(indices))


def test_derive_unknown_tls_raises_with_available_ids() -> None:
    with pytest.raises(ValueError, match=r"nope.*J1"):
        derive_link_movements(DEMO_NET, "nope")


def test_derive_missing_net_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        derive_link_movements(DEMO_NET.parent / "does_not_exist.net.xml", "J1")


# ---------------------------------------------------------------------------
# approach_lane_counts
# ---------------------------------------------------------------------------


def test_approach_lane_counts_match_distinct_incoming_lanes() -> None:
    import sumolib

    lanes = approach_lane_counts(DEMO_NET, "J1")
    assert lanes and all(n >= 1 for n in lanes.values())
    assert set(lanes) <= {"EB", "WB", "NB", "SB"}
    assert set(lanes) == {
        lm.movement.split("-")[0] for lm in derive_link_movements(DEMO_NET, "J1")
    }
    net = sumolib.net.readNet(str(DEMO_NET))
    distinct = {
        in_lane.getID() for in_lane, _out, _idx in net.getTLS("J1").getConnections()
    }
    assert sum(lanes.values()) == len(distinct)


@pytest.mark.parametrize("tls_id", TLS_IDS)
def test_approach_lane_counts_match_net_ground_truth(tls_id: str) -> None:
    counts = approach_lane_counts(DEMO_NET, tls_id)
    assert counts == _ground_truth_lane_counts(tls_id)
    # The demo junctions: two lanes on each E-W approach, one on each N-S one.
    assert counts == {"EB": 2, "WB": 2, "NB": 1, "SB": 1}


def test_approach_lane_counts_overrides_win_over_derived_classification() -> None:
    # J1 link 7 leaves the first EB lane (which also serves link 8, EB-through).
    # Re-labelling it NB-left makes that lane count towards NB as well.
    counts = approach_lane_counts(DEMO_NET, "J1", overrides={7: "NB-left"})
    assert counts == {"EB": 2, "WB": 2, "NB": 2, "SB": 1}


def test_approach_lane_counts_unknown_tls_raises_with_available_ids() -> None:
    with pytest.raises(ValueError, match=r"nope.*J1"):
        approach_lane_counts(DEMO_NET, "nope")


# ---------------------------------------------------------------------------
# overrides
# ---------------------------------------------------------------------------


def test_overrides_win_over_derived_classification() -> None:
    derived = {
        lm.link_index: lm.movement
        for lm in derive_link_movements(DEMO_NET, "J1", overrides={7: "NB-left"})
    }
    assert derived[7] == "NB-left"
    assert derived[8] == "EB-through"  # untouched links keep their classification


def test_override_for_uncontrolled_link_index_raises() -> None:
    with pytest.raises(ValueError, match=r"\[99\]"):
        derive_link_movements(DEMO_NET, "J1", overrides={99: "NB-left"})


def test_load_overrides_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "signal_groups.json"
    path.write_text(json.dumps({"J1": {"7": "NB-left", "8": "SB-through"}}))
    assert load_overrides(path) == {"J1": {7: "NB-left", 8: "SB-through"}}


def test_load_overrides_rejects_bad_movement(tmp_path: Path) -> None:
    path = tmp_path / "signal_groups.json"
    path.write_text(json.dumps({"J1": {"7": "EB-uturn"}}))
    with pytest.raises(ValueError, match="EB-uturn"):
        load_overrides(path)


def test_load_overrides_rejects_non_integer_index(tmp_path: Path) -> None:
    path = tmp_path / "signal_groups.json"
    path.write_text(json.dumps({"J1": {"seven": "NB-left"}}))
    with pytest.raises(ValueError, match="seven"):
        load_overrides(path)


# ---------------------------------------------------------------------------
# build_state
# ---------------------------------------------------------------------------

EW_MOVEMENTS = (
    "EB-left", "EB-through", "EB-right",
    "WB-left", "WB-through", "WB-right",
)


def test_build_state_greens_exactly_the_selected_movements() -> None:
    link_movements = derive_link_movements(DEMO_NET, "J1")
    state = build_state(link_movements, EW_MOVEMENTS)
    expected_green = {
        lm.link_index for lm in link_movements if lm.movement in EW_MOVEMENTS
    }
    for lm in link_movements:
        assert state[lm.link_index] == ("G" if lm.link_index in expected_green else "r")
    assert state == "GGGGrrrGGGGrrr"


def test_build_state_permissive_uses_lowercase_g() -> None:
    link_movements = derive_link_movements(DEMO_NET, "J1")
    state = build_state(link_movements, EW_MOVEMENTS, protected=False)
    assert set(state) == {"g", "r"}
    assert state == "ggggrrrggggrrr"


def test_build_state_missing_link_indices_render_red() -> None:
    state = build_state(
        [LinkMovement(2, "EB-through"), LinkMovement(5, "NB-left")],
        {"EB-through"},
    )
    assert state == "rrGrrr"


def test_build_state_rejects_unknown_green_movement() -> None:
    with pytest.raises(ValueError, match="EB-straight"):
        build_state([LinkMovement(0, "EB-through")], {"EB-straight"})


def test_link_movement_validates_inputs() -> None:
    with pytest.raises(ValueError, match="NB-uturn"):
        LinkMovement(0, "NB-uturn")
    with pytest.raises(ValueError, match=">= 0"):
        LinkMovement(-1, "NB-left")


# ---------------------------------------------------------------------------
# controlled_link_count + n_links on signals with pedestrian crossings
# ---------------------------------------------------------------------------
# The demo net re-built with sidewalks and crossings: every signal controls
# MORE links than it has vehicle connections — its four pedestrian crossings
# occupy the trailing link indices (14..17, after the 14 vehicle links). SUMO
# rejects any program whose state length differs from the net's
# controlled-link count, so full states must be sized via
# controlled_link_count and padded red beyond the vehicle links.


@pytest.fixture(scope="module")
def crossing_net(tmp_path_factory) -> Path:
    """The demo network with sidewalks and signalized pedestrian crossings."""
    return build_crossing_net(tmp_path_factory.mktemp("crossing_net"))


def _links_by_kind(net: Path, tls_id: str) -> tuple[set[int], set[int]]:
    """(vehicle, crossing) link indices of one signal, straight from the net XML.

    Crossing links lead from a walking area into a crossing — both internal
    lanes (``:J1_w0`` -> ``:J1_c3``) — vehicle links from a normal edge.
    """
    vehicle: set[int] = set()
    crossing: set[int] = set()
    for conn in ET.parse(net).getroot().iter("connection"):
        if conn.get("tl") != tls_id:
            continue
        index = int(conn.get("linkIndex"))
        (crossing if conn.get("from").startswith(":") else vehicle).add(index)
    return vehicle, crossing


def _program_state_lengths(net: Path, tls_id: str) -> set[int]:
    """State lengths of the net's own program(s) for ``tls_id``."""
    return {
        len(phase.get("state"))
        for tl in ET.parse(net).getroot().iter("tlLogic")
        if tl.get("id") == tls_id
        for phase in tl.iter("phase")
    }


@requires_sumolib
@requires_netconvert
@pytest.mark.parametrize("tls_id", TLS_IDS)
def test_controlled_link_count_includes_crossing_links(crossing_net: Path, tls_id: str) -> None:
    vehicle, crossing = _links_by_kind(crossing_net, tls_id)
    # Premise: the 14 vehicle links come first, the four crossings trail them.
    assert vehicle == set(range(14))
    assert crossing == set(range(14, 18))

    n_links = controlled_link_count(crossing_net, tls_id)
    assert n_links == len(vehicle) + len(crossing) == 18
    # Exactly the state length the net's own program carries.
    assert _program_state_lengths(crossing_net, tls_id) == {n_links}
    # The rebuild keeps the vehicle links and their classification unchanged.
    assert derive_link_movements(crossing_net, tls_id) == derive_link_movements(
        DEMO_NET, tls_id
    )


def test_controlled_link_count_demo_net_matches_dense_links() -> None:
    # The demo net has no crossings: count == number of vehicle links.
    assert controlled_link_count(DEMO_NET, "J1") == 14


def test_controlled_link_count_missing_tls_raises_naming_net_and_tls() -> None:
    with pytest.raises(ValueError, match=r"nope"):
        controlled_link_count(DEMO_NET, "nope")
    with pytest.raises(ValueError, match=r"demo\.net\.xml"):
        controlled_link_count(DEMO_NET, "nope")


@requires_sumolib
@requires_netconvert
def test_build_state_n_links_pads_crossings_red(crossing_net: Path) -> None:
    link_movements = derive_link_movements(crossing_net, "J1")
    n_links = controlled_link_count(crossing_net, "J1")
    vehicle, crossing = _links_by_kind(crossing_net, "J1")
    assert n_links == 18
    # sumolib reports only the 14 vehicle connections (indices 0..13).
    assert len(link_movements) == 14
    assert {lm.link_index for lm in link_movements} == vehicle
    assert max(lm.link_index for lm in link_movements) == 13

    state = build_state(link_movements, EW_MOVEMENTS, n_links=n_links)
    assert len(state) == 18
    for lm in link_movements:
        expected = "G" if lm.movement in EW_MOVEMENTS else "r"
        assert state[lm.link_index] == expected
    # The pedestrian-crossing indices (beyond the vehicle links) pad red.
    assert "".join(state[i] for i in sorted(crossing)) == "rrrr"
    assert state[14:] == "rrrr"
    # Same vehicle part as on the plain demo net (no crossings).
    assert state[:14] == build_state(derive_link_movements(DEMO_NET, "J1"), EW_MOVEMENTS)


def test_build_state_n_links_smaller_than_link_index_raises() -> None:
    with pytest.raises(ValueError, match="n_links"):
        build_state(
            [LinkMovement(2, "EB-through"), LinkMovement(5, "NB-left")],
            {"EB-through"},
            n_links=4,
        )


def test_build_state_n_links_exactly_max_plus_one_matches_default() -> None:
    movements = [LinkMovement(2, "EB-through"), LinkMovement(5, "NB-left")]
    assert build_state(movements, {"EB-through"}, n_links=6) == build_state(
        movements, {"EB-through"}
    )


def test_yield_permissive_lefts_demotes_only_conflicting_lefts() -> None:
    """A left green concurrently with its opposing through must yield ('g');
    a protected left (opposing through red) and the throughs keep 'G'."""
    movements = [
        LinkMovement(0, "EB-left"),
        LinkMovement(1, "EB-through"),
        LinkMovement(2, "WB-through"),
        LinkMovement(3, "NB-left"),
    ]
    # EB-left faces a green WB-through -> permissive 'g'; NB-left's opposing
    # (SB) through is red -> protected 'G'; throughs untouched.
    assert yield_permissive_lefts("GGGG", movements) == "gGGG"
    # Opposing through red -> the left stays protected.
    assert yield_permissive_lefts("GGrr", movements) == "GGrr"
