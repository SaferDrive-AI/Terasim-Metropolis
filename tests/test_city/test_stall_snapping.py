"""snap_stalls_to_vehicle_lanes: bad coordinates must never sink a run.

Runs on the synthetic demo corridor net. A stall coordinate at a signal's
junction centre is the failure mode this guards against: at ``J2``'s centre
``convertRoad`` resolves to the junction-internal connector lane ``:J2_12``,
where no vehicle can be added.
Names avoid ``sumo``/``simulation`` (conftest auto-mark).
"""

from __future__ import annotations

import pytest

from tests.test_city.conftest import DEMO_COORDS, DEMO_NET, requires_sumolib

pytestmark = [requires_sumolib]

# The centre of the J2 junction (TMC intersection i2 sits exactly on it).
JUNCTION_CENTER = {"latitude": DEMO_COORDS["i2"][0], "longitude": DEMO_COORDS["i2"][1]}
# Between the J1 and J2 cross streets, ~165 m south of the arterial: the
# nearest lane is ~150 m away, far outside the 25 m snapping radius.
OFF_NETWORK = {"latitude": 37.33350, "longitude": -121.90170}


def _midblock_point() -> dict:
    """A coordinate derived FROM the net, guaranteed on a regular lane."""
    import sumolib

    net = sumolib.net.readNet(str(DEMO_NET), withInternal=False)
    lane = net.getEdge("EB_2").getLane(1)  # eastbound arterial, J1 -> J2
    shape = lane.getShape()
    mx, my = shape[len(shape) // 2]
    lon, lat = net.convertXY2LonLat(mx, my)
    return {"latitude": lat, "longitude": lon}


def test_junction_center_snaps_to_regular_lane():
    import sumolib

    from terasim_city.evaluation.diagnostics import snap_stalls_to_vehicle_lanes

    snapped = snap_stalls_to_vehicle_lanes(DEMO_NET, [dict(JUNCTION_CENTER)])
    assert len(snapped) == 1
    # The snapped point must now resolve to a REGULAR drivable lane.
    net = sumolib.net.readNet(str(DEMO_NET), withInternal=False)
    x, y = net.convertLonLat2XY(snapped[0]["longitude"], snapped[0]["latitude"])
    lanes = net.getNeighboringLanes(x, y, 3.0)
    assert lanes, "snapped point is not on any lane"
    lane = min(lanes, key=lambda c: c[1])[0]
    assert not lane.getEdge().getID().startswith(":")
    assert lane.allows("passenger")


def test_midblock_point_is_kept():
    from terasim_city.evaluation.diagnostics import snap_stalls_to_vehicle_lanes

    midblock = _midblock_point()
    snapped = snap_stalls_to_vehicle_lanes(DEMO_NET, [dict(midblock)])
    assert len(snapped) == 1
    # Stays in the immediate vicinity (projection, not teleportation).
    assert abs(snapped[0]["latitude"] - midblock["latitude"]) < 0.0005
    assert abs(snapped[0]["longitude"] - midblock["longitude"]) < 0.0005


def test_off_network_point_fails_loud_with_coordinate():
    from terasim_city.evaluation.diagnostics import snap_stalls_to_vehicle_lanes

    # An incident that cannot be placed must FAIL the run (an incident
    # scenario without its incident answers the wrong question) and the
    # message must carry the offending coordinate.
    with pytest.raises(ValueError, match=r"37\.33350"):
        snap_stalls_to_vehicle_lanes(DEMO_NET, [dict(OFF_NETWORK), _midblock_point()])
