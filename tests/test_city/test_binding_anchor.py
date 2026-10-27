"""One binding standard: plans and targets bind to the MAP junction's anchor.

The city contract associates TMC counts, uploaded plans and optimization
targets through the map — each coordinate binds to a map signal junction
independently, and comparable candidates are reported as ambiguity instead of
silently picked. These tests pin the two behaviours that used to be wrong:

* plan lookup keyed on the junction's own position (the anchor), not on the
  TMC camera's coordinates;
* ambiguity reported when several signals/plans are comparably close.

Test names deliberately avoid the substrings ``sumo`` and ``simulation`` so
the repo-root conftest does not auto-mark the net-free tests ``requires_sumo``.
"""

from __future__ import annotations

import importlib.util
import math
from datetime import datetime

import pytest

from terasim_city.binding import (
    AmbiguousMatchError,
    is_ambiguous,
    signal_candidates,
)
from terasim_city.plans.assemble import plan_selections
from terasim_city.plans.store import PlanStore

from tests.test_city.conftest import DEMO_NET, DEMO_SUMOCFG
from tests.fixtures.tmc_templates import demo_tmc, write_tmc

WHEN = datetime(2026, 7, 1, 8, 30)

#: 1 degree of longitude at the test latitude, in metres.
LAT0, LON0 = 37.3300, -121.9000
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT0))


def _east_of(lon: float, metres: float) -> float:
    return lon + metres / M_PER_DEG_LON


def _plan_doc(corridor: str, *intersections: dict) -> dict:
    return {
        "schema": "terasim.nema_plan.v1",
        "corridor_id": corridor,
        "intersections": list(intersections),
        "provenance": {"source_files": ["x.pdf"], "handoff_ready": False, "gaps": []},
    }


def _inter(iid: str, lat: float, lon: float) -> dict:
    return {
        "intersection_id": iid,
        "name": iid,
        "lat": lat,
        "lon": lon,
        "phases": [{"phase": 2, "movements": ["EB-through"]},
                   {"phase": 6, "movements": ["WB-through"]}],
        "rings": {"ring1": [2], "ring2": [6], "coordinated_phases": [2, 6]},
        "patterns": [{"pattern_id": "am", "cycle_s": 100.0, "splits": {"2": 50, "6": 50}}],
        "schedule": [],
    }


# ---------------------------------------------------------------------------
# Plan lookup anchors on the junction, not on the TMC camera
# ---------------------------------------------------------------------------

def test_plan_binds_via_junction_anchor_not_tmc_coords(tmp_path):
    """The contract's own example: camera 40 m east, plan 25 m west.

    Plan and camera are 65 m apart — farther than any radius — yet both
    describe the junction between them, so the plan must bind. Deleting the
    anchor-based lookup silently discards every plan whose surveyed position
    sits on the other side of the junction from the camera.
    """
    store = PlanStore(tmp_path)
    store.ingest(_plan_doc("main_st", _inter("city_plan_7", LAT0, _east_of(LON0, -25.0))))

    resolved = [{
        "intersection_id": "cam_1",          # TMC name, unknown to the plan
        "tls_id": "J1",
        "lat": LAT0, "lon": _east_of(LON0, 40.0),   # camera position
        "anchor_lat": LAT0, "anchor_lon": LON0,     # the junction itself
    }]
    rows, _ = plan_selections("main_st", resolved, store, WHEN)
    (sel, sp, _pattern), = rows
    assert sel.source == "nema"
    assert sel.matched_by == "geolocation"
    assert sp.intersection_id == "city_plan_7"

    # Without the anchor (legacy record: camera coordinates only) the very
    # same junction finds nothing — which is the failure mode being fixed.
    legacy = [dict(resolved[0], anchor_lat=None, anchor_lon=None)]
    rows, _ = plan_selections("main_st", legacy, store, WHEN)
    assert rows[0][0].source == "no_plan"


def test_junction_without_tmc_coverage_still_finds_plan(tmp_path):
    """A mid-corridor junction no camera observes must stay plan-addressable."""
    store = PlanStore(tmp_path)
    store.ingest(_plan_doc("main_st", _inter("uncovered_sig", LAT0, _east_of(LON0, 10.0))))

    resolved = [{
        "intersection_id": None, "tls_id": "J2",
        "lat": None, "lon": None,               # no TMC counterpart
        "anchor_lat": LAT0, "anchor_lon": LON0,
    }]
    rows, _ = plan_selections("main_st", resolved, store, WHEN)
    (sel, sp, _pattern), = rows
    assert sel.source == "nema"
    assert sp.intersection_id == "uncovered_sig"


def test_comparable_plans_report_ambiguity(tmp_path):
    """Two plans flanking the query point are reported, not silently picked."""
    store = PlanStore(tmp_path)
    # 50 m apart: distinct records (ingest dedup keeps ≤30 m as one junction).
    store.ingest(_plan_doc("main_st", _inter("east_sig", LAT0, _east_of(LON0, 25.0))))
    store.ingest(_plan_doc("main_st", _inter("west_sig", LAT0, _east_of(LON0, -25.0))))

    with pytest.raises(AmbiguousMatchError, match="east_sig|west_sig"):
        store.find_for("other", "unknown", LAT0, LON0)

    # A clearly nearer plan stays an ordinary match.
    sp = store.find_for("other", "unknown", LAT0, _east_of(LON0, 20.0))
    assert sp.intersection_id == "east_sig"

    # The optimization path must survive the ambiguity: report + fall back to
    # the static seed ladder instead of killing the whole run.
    resolved = [{
        "intersection_id": None, "tls_id": "J3",
        "lat": None, "lon": None,
        "anchor_lat": LAT0, "anchor_lon": LON0,
    }]
    rows, warnings = plan_selections("main_st", resolved, store, WHEN)
    assert rows[0][0].source == "no_plan"
    assert any("ambiguous" in w for w in warnings)


def test_neighbouring_plan_beyond_same_intersection_radius_not_applied(tmp_path):
    """A lone plan 31–60 m away is the NEIGHBOUR's — warn, don't apply.

    Plan lookup has no structural ranking to protect it (unlike TMC
    matching), so a single hit past the same-intersection radius would
    silently time the wrong junction. The verified failure mode: junction B
    has no plan, junction A's plan sits 45 m away, and B would adopt it.
    """
    store = PlanStore(tmp_path)
    store.ingest(_plan_doc("main_st", _inter("junction_a", LAT0, _east_of(LON0, 45.0))))

    resolved = [{
        "intersection_id": None, "tls_id": "J_b",
        "lat": None, "lon": None,
        "anchor_lat": LAT0, "anchor_lon": LON0,
    }]
    rows, warnings = plan_selections("main_st", resolved, store, WHEN)
    assert rows[0][0].source == "no_plan"
    assert any("junction_a" in w and "not applied" in w for w in warnings)


def test_ambiguity_rule():
    assert is_ambiguous(20.0, 25.0)          # too close to call
    assert not is_ambiguous(20.0, 55.0)      # clear winner
    assert not is_ambiguous(0.0, 10.0)       # exactly on the junction


# ---------------------------------------------------------------------------
# Raw-coordinate target snapping reports ambiguity between two signals
# ---------------------------------------------------------------------------

_requires_sumolib = pytest.mark.skipif(
    importlib.util.find_spec("sumolib") is None,
    reason="needs sumolib (install eclipse-sumo or pip install sumolib)",
)


@_requires_sumolib
def test_coordinate_between_two_signals_reports_ambiguity(tmp_path):
    import sumolib

    from terasim_city.corridor.resolve import (
        _node_for_tls,
        _node_to_tls_map,
        resolve_corridor,
    )

    # The demo corridor's J1 and J2: two signals ~300 m apart on the arterial.
    net = sumolib.net.readNet(str(DEMO_NET))
    node_to_tls = _node_to_tls_map(net)
    a = _node_for_tls(net, node_to_tls, "J1")
    b = _node_for_tls(net, node_to_tls, "J2")
    ax, ay = a.getCoord()
    bx, by = b.getCoord()
    lon, lat = net.convertXY2LonLat((ax + bx) / 2, (ay + by) / 2)
    radius = math.hypot(ax - bx, ay - by)  # generous: covers both signals

    # Premise: from the midpoint the two signals are comparably close.
    cands = signal_candidates(net, node_to_tls, lat, lon, radius_m=radius)
    assert len(cands) >= 2
    assert {cands[0].tls_id, cands[1].tls_id} == {"J1", "J2"}
    assert is_ambiguous(cands[0].distance_m, cands[1].distance_m)

    tmc_path = write_tmc(tmp_path, demo_tmc())
    ref = f"{lat},{lon}"
    with pytest.raises(AmbiguousMatchError, match="comparably close"):
        resolve_corridor(
            DEMO_SUMOCFG, tmc_path,
            from_ref=ref, to_ref=ref,
            max_match_distance_m=radius,
        )
