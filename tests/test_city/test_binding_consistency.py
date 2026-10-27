"""Flow demand and signal control must bind to the SAME junctions.

The three-signal demo corridor is the fixture, with each TMC point moved
15-21 m off its junction the way a roadside camera sits: for every TMC
intersection, the junction the flow matcher injects demand at must be exactly
the junction whose traffic light the corridor resolver hands to the
optimizer. Divergence here means "cars poured at junction X, signals tuned at
junction Y" — silently wrong optimization.

Test names deliberately avoid the substrings ``sumo`` and ``simulation`` so
the repo-root conftest does not auto-mark them ``requires_sumo``.
"""

from __future__ import annotations

import math

import pytest

from terasim_city.corridor import resolve_corridor
from terasim_city.flow.descriptors import TMCData
from terasim_city.flow.network.matcher import map_intersections

from tests.fixtures.tmc_templates import demo_tmc, write_tmc
from tests.test_city.conftest import (
    DEMO_COORDS,
    DEMO_INTERSECTION_TLS,
    DEMO_SUMOCFG,
    requires_sumolib,
)

pytestmark = [requires_sumolib]

#: The corridor's TMC intersections, west to east.
CORRIDOR_IDS = ["i1", "i2", "i3"]

#: Camera position relative to its junction, metres (east, north).
CAMERA_OFFSET_M = {"i1": (0.0, 20.0), "i2": (-15.0, 0.0), "i3": (10.0, -18.0)}
_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON = _M_PER_DEG_LAT * math.cos(math.radians(DEMO_COORDS["i1"][0]))


def _camera(iid: str) -> tuple[float, float]:
    """(lat, lon) of the TMC point: the junction shifted by its camera offset."""
    lat, lon = DEMO_COORDS[iid]
    east, north = CAMERA_OFFSET_M[iid]
    return round(lat + north / _M_PER_DEG_LAT, 7), round(lon + east / _M_PER_DEG_LON, 7)


def _raw(iid: str) -> str:
    """A raw ``"lat,lon"`` ref at the TMC intersection's own (camera) coordinates."""
    lat, lon = _camera(iid)
    return f"{lat},{lon}"


@pytest.fixture(scope="module")
def demo_tmc_path(tmp_path_factory):
    payload = demo_tmc()
    for item in payload["intersections"]:
        item["lat"], item["lon"] = _camera(item["intersection_id"])
    return write_tmc(tmp_path_factory.mktemp("demo_tmc"), payload)


@pytest.fixture(scope="module")
def demo_net():
    import sumolib

    from terasim_city.scenario.sumocfg_io import parse_sumocfg

    info = parse_sumocfg(DEMO_SUMOCFG)
    return sumolib.net.readNet(str(info.net_file))


@pytest.fixture(scope="module")
def flow_side(demo_net, demo_tmc_path):
    """The flow pipeline's binding: iid -> matched junction node."""
    tmc = TMCData.from_json(demo_tmc_path)
    results = map_intersections(demo_net, tmc, max_dist_m=60.0)
    return {
        r["intersection_id"]: r for r in results if r["status"] == "matched"
    }


@pytest.fixture(scope="module")
def corridor(demo_tmc_path):
    """The optimizer's binding: i1 -> i3 resolved corridor."""
    return resolve_corridor(
        DEMO_SUMOCFG, demo_tmc_path, from_ref="i1", to_ref="i3"
    )


def test_corridor_resolves_all_three_in_order(corridor):
    ids = [item.intersection_id for item in corridor.intersections]
    assert ids == CORRIDOR_IDS, f"corridor order/coverage wrong: {ids}"
    # Three DISTINCT signals, no duplicates, each the one its TMC point sits on.
    assert corridor.tls_ids == tuple(DEMO_INTERSECTION_TLS[iid] for iid in CORRIDOR_IDS)
    assert len(set(corridor.tls_ids)) == 3


def test_flow_and_signal_bind_to_identical_junctions(flow_side, corridor):
    """The core contract: demand node == control node, for all 3."""
    assert set(flow_side) == set(CORRIDOR_IDS)
    for item in corridor.intersections:
        record = flow_side[item.intersection_id]
        assert item.node_id == record["matched_node_id"], (
            f"{item.intersection_id}: demand is injected at junction "
            f"{record['matched_node_id']!r} but the optimizer controls the "
            f"signal at junction {item.node_id!r} — bindings diverged"
        )
        assert item.match_distance_m == pytest.approx(
            float(record["distance_to_node_m"]), abs=0.01
        )
        # ...and that junction is the one the camera actually looks at.
        assert item.match_distance_m == pytest.approx(
            math.hypot(*CAMERA_OFFSET_M[item.intersection_id]), abs=0.5
        )


def test_raw_coordinate_endpoints_resolve_the_same_three_intersections(demo_tmc_path):
    """Core raw "lat,lon" refs resolve the same corridor path as named refs."""

    resolved = resolve_corridor(
        DEMO_SUMOCFG,
        demo_tmc_path,
        from_ref=_raw("i1"),
        to_ref=_raw("i3"),
    )

    assert [item.intersection_id for item in resolved.intersections] == CORRIDOR_IDS


def test_single_raw_coordinate_endpoint_resolves_one_intersection(demo_tmc_path):
    coordinate = _raw("i1")

    resolved = resolve_corridor(
        DEMO_SUMOCFG,
        demo_tmc_path,
        from_ref=coordinate,
        to_ref=coordinate,
    )

    assert [item.intersection_id for item in resolved.intersections] == ["i1"]
