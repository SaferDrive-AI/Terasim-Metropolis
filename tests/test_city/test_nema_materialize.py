"""NEMA materialize: nema_plan.v1 pattern -> SUMO <tlLogic type="NEMA">.

The core (expand_dual_ring_static) is net-free — driven by synthetic LinkMovement — so
these tests validate the generation logic (params, maxDur = split-yellow-red,
per-phase yellow/red, state binding) without needing a SUMO net.

Names avoid the ``sumo``/``simulation`` substrings so conftest does not
auto-mark them requires_sumo.
"""

from __future__ import annotations

import json

import pytest
from lxml import etree as ET

from terasim_city.plans.nema_materialize import expand_dual_ring_static, write_nema_additional
from terasim_city.plans.signal_groups import LinkMovement
from tests.fixtures.nema_plan_files import MAIN_1ST_PLAN, plan_intersection

# One controlled link per main_1st phase movement (link_index -> movement).
_LINKS = (
    LinkMovement(0, "EB-left"),
    LinkMovement(1, "WB-through"),
    LinkMovement(2, "SB-left"),
    LinkMovement(3, "NB-through"),
    LinkMovement(4, "WB-left"),
    LinkMovement(5, "EB-through"),
    LinkMovement(6, "NB-left"),
    LinkMovement(7, "SB-through"),
)
_N_LINKS = 8


def _main_1st_intersection_and_pattern():
    """The synthetic 8-phase main_1st plan: phase 3 sits in ring 1."""
    inter = plan_intersection(MAIN_1ST_PLAN)
    pat = inter["patterns"][0]  # am_peak, cycle 100
    return inter, pat


def _params(tl) -> dict:
    return {p.get("key"): p.get("value") for p in tl.findall("param")}


def test_ring_phase_without_definition_raises():
    inter, pat = _main_1st_intersection_and_pattern()
    inter = json.loads(json.dumps(inter))  # deep copy
    inter["phases"] = [p for p in inter["phases"] if p["phase"] != 3]  # 3 still in ring1
    with pytest.raises(ValueError, match="ring lists phase 3"):
        expand_dual_ring_static(inter, pat, _LINKS, _N_LINKS, tls_id="J")


def test_write_additional_roundtrips(tmp_path):
    inter, pat = _main_1st_intersection_and_pattern()
    tl = expand_dual_ring_static(inter, pat, _LINKS, _N_LINKS, tls_id="J_main_1st")
    out = write_nema_additional([tl], tmp_path / "nema.add.xml")
    assert out.exists()
    root = ET.parse(str(out)).getroot()
    assert root.tag == "additional"
    tls = root.findall("tlLogic")
    # The production representation is the static dual-ring expansion.
    assert len(tls) == 1 and tls[0].get("type") == "static"
    assert tls[0].get("id") == "J_main_1st"
    # The pattern's identity and (non-zero) offset travel with the program.
    assert tls[0].get("programID") == "am_peak"
    assert pat["offset_s"] > 0
    assert float(tls[0].get("offset")) == pytest.approx(float(pat["offset_s"]))
    # The expansion's phase durations must sum to the pattern cycle.
    total = sum(float(ph.get("duration")) for ph in tls[0].findall("phase"))
    assert total == pytest.approx(float(pat["cycle_s"]))


def test_movement_riding_both_rings_never_flickers_mid_green():
    """A movement served by phases in BOTH rings holds green while either
    serves it. The old last-writer-wins merge let ring 2's clearance stamp a
    ring-1 green link yellow/red mid-window (G->y->r->G within one cycle)."""
    inter = {
        "intersection_id": "X",
        "phases": [
            # Ring 1 serves EB-through for (nearly) the whole cycle.
            {"phase": 1, "movements": ["EB-through"],
             "min_green_s": 5.0, "yellow_s": 3.0, "all_red_s": 1.0},
            # Ring 2 also serves EB-through, but ends much earlier — its
            # clearance overlaps ring 1's green.
            {"phase": 2, "movements": ["WB-through", "EB-through"],
             "min_green_s": 5.0, "yellow_s": 3.0, "all_red_s": 1.0},
        ],
        "rings": {"ring1": [1], "ring2": [2], "coordinated_phases": [1]},
    }
    pattern = {"pattern_id": "p", "cycle_s": 40.0,
               "splits": {"1": 40.0, "2": 20.0}}
    links = (LinkMovement(0, "EB-through"), LinkMovement(1, "WB-through"))

    tl = expand_dual_ring_static(inter, pattern, links, 2, tls_id="J")

    # Ring 1 holds EB-through green for [0, 36); every emitted phase segment
    # starting inside that window must show 'G' on link 0.
    t = 0.0
    for ph in tl.findall("phase"):
        if t < 36.0:
            assert ph.get("state")[0] == "G", (
                f"link 0 (EB-through) shows {ph.get('state')[0]!r} at t={t}: "
                "ring 2's clearance clobbered ring 1's green"
            )
        t += float(ph.get("duration"))
