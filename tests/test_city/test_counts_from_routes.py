"""Turning-movement counts derived from a scenario's route file."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Sequence
from xml.etree import ElementTree as ET

import pytest
import sumolib

from terasim_city.flow.counts_from_routes import derive_tmc_from_routes
from terasim_city.flow.descriptors import TMCData
from terasim_city.plans.signal_groups import controlled_link_count, derive_link_movements
from tests.fixtures.demo_corridor import DEMO_COORDS, build_crossing_net, write_sumocfg
from tests.fixtures.tmc_templates import demo_tmc
from tests.test_city.conftest import REPO_ROOT, requires_netconvert, requires_sumolib

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_NET = DEMO_DIR / "demo.net.xml"
LOGGER = "terasim_city.flow.counts_from_routes"
NO_DEMAND = "no demand in the route files reaches any of the requested signals within the horizon"


def _connections_of(tls_id: str):
    net = sumolib.net.readNet(str(DEMO_NET))
    pairs = {}
    movements = {lm.link_index: lm.movement for lm in derive_link_movements(DEMO_NET, tls_id)}
    for in_lane, out_lane, index in net.getTLS(tls_id).getConnections():
        pairs[(in_lane.getEdge().getID(), out_lane.getEdge().getID())] = movements[int(index)]
    return pairs


def _write_scenario(
    tmp_path: Path,
    routes_xml: str,
    end: int | str | None = 3600,
    *,
    begin: int = 0,
    more_routes_xml: Sequence[str] = (),
) -> Path:
    """A base sumocfg on the demo net.

    ``end=None`` leaves out the window's end: with a ``begin`` the ``<time>`` has a start only,
    without one there is no ``<time>`` at all.
    """
    route_files = []
    for index, text in enumerate((routes_xml, *more_routes_xml)):
        path = tmp_path / ("demand.rou.xml" if index == 0 else f"demand{index}.rou.xml")
        path.write_text(text, encoding="utf-8")
        route_files.append(path)
    if end is not None:
        window = f'<time><begin value="{begin}"/><end value="{end}"/></time>'
    elif begin:
        window = f'<time><begin value="{begin}"/></time>'
    else:
        window = ""
    cfg = tmp_path / "base.sumocfg"
    cfg.write_text(
        f'<configuration><input><net-file value="{DEMO_NET}"/>'
        f'<route-files value="{",".join(str(path) for path in route_files)}"/></input>'
        f"{window}</configuration>",
        encoding="utf-8",
    )
    return cfg


def _east_through_j1(flow_attributes: str) -> str:
    """Route text with one flow along EB_1 -> EB_2, J1's eastbound through movement."""
    return (
        '<routes><route id="r" edges="EB_1 EB_2"/>'
        f'<flow id="f" route="r" {flow_attributes}/></routes>'
    )


def _counts(payload: dict, index: int = 0) -> dict:
    return payload["intersections"][index]["counts"]


@requires_sumolib
def test_counts_follow_flow_rates_and_movement_labels(tmp_path):
    pairs = _connections_of("J1")
    (a, b), movement = next(iter(pairs.items()))
    direction, turn = movement.split("-")
    tmc_dir = {"EB": "East", "WB": "West", "NB": "North", "SB": "South"}[direction]
    tmc_turn = {"left": "Left", "through": "Straight", "right": "Right"}[turn]
    routes = f"""<routes>
  <route id="r" edges="{a} {b}"/>
  <flow id="f1" route="r" begin="0" end="1800" vehsPerHour="240"/>
  <flow id="f2" route="r" begin="0" end="1800" period="30"/>
  <vehicle id="v1" route="r" depart="5"/>
  <trip id="t1" from="{a}" to="{b}" depart="6"/>
</routes>"""
    cfg = _write_scenario(tmp_path, routes, end=3600)
    payload = derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo")
    TMCData.from_payload(payload)  # must be a valid TMC document
    inter = payload["intersections"][0]
    assert inter["intersection_id"] == "J1" and payload["observation_interval"] == "1h"
    # 240 vph + 3600/30 + one vehicle + one trip (routed a -> b) over a 3600 s horizon
    assert inter["counts"][tmc_dir][tmc_turn] == 240 + 120 + 1 + 1
    assert -90 < inter["lat"] < 90 and -180 < inter["lon"] < 180


@requires_sumolib
def test_unreferenced_connections_count_zero(tmp_path):
    cfg = _write_scenario(tmp_path, _east_through_j1('begin="0" end="3600" vehsPerHour="60"'))
    payload = derive_tmc_from_routes(cfg, ["J1", "J2"], corridor_id="demo")
    assert [i["intersection_id"] for i in payload["intersections"]] == ["J1", "J2"]
    j1, j2 = _counts(payload, 0), _counts(payload, 1)
    assert j1["East"]["Straight"] == 60 and sum(v for d in j1.values() for v in d.values()) == 60
    assert all(v == 0 for d in j2.values() for v in d.values())  # no route crosses J2


def test_non_georeferenced_net_fails_clearly(tmp_path):
    """A net without a usable geo-reference is rejected with a clear message."""
    net_text = DEMO_NET.read_text(encoding="utf-8")
    net_text = re.sub(r'projParameter="[^"]*"', 'projParameter="!"', net_text, count=1)
    (tmp_path / "flat.net.xml").write_text(net_text, encoding="utf-8")
    cfg = tmp_path / "base.sumocfg"
    (tmp_path / "demand.rou.xml").write_text("<routes/>", encoding="utf-8")
    cfg.write_text(
        f'<configuration><input><net-file value="{tmp_path / "flat.net.xml"}"/>'
        f'<route-files value="{tmp_path / "demand.rou.xml"}"/></input></configuration>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="georeferenc"):
        derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo")


# ---------------------------------------------------------------------------
# Further cases
# ---------------------------------------------------------------------------

# Every signalised movement of J1 as (route edges, TMC direction, TMC turn). The edges are named
# after their travel direction (EB_*, WB_*, NB_J1_*, SB_J1_*; *_1 arrives at J1, the next leaves
# it), so the expected cell follows from the geometry alone: a north-bound vehicle that joins
# the east-bound street turns right, one that joins the west-bound street turns left, and so on.
J1_MOVEMENTS = [
    ("EB_1 SB_J1_2", "East", "Right"),
    ("EB_1 EB_2", "East", "Straight"),
    ("EB_1 NB_J1_2", "East", "Left"),
    ("NB_J1_1 EB_2", "North", "Right"),
    ("NB_J1_1 NB_J1_2", "North", "Straight"),
    ("NB_J1_1 WB_4", "North", "Left"),
    ("SB_J1_1 WB_4", "South", "Right"),
    ("SB_J1_1 SB_J1_2", "South", "Straight"),
    ("SB_J1_1 EB_2", "South", "Left"),
    ("WB_3 NB_J1_2", "West", "Right"),
    ("WB_3 WB_4", "West", "Straight"),
    ("WB_3 SB_J1_2", "West", "Left"),
]


@requires_sumolib
def test_every_movement_of_a_signal_lands_in_its_own_cell(tmp_path):
    rates = {edges: 100 + 10 * k for k, (edges, _, _) in enumerate(J1_MOVEMENTS)}
    body = "\n".join(
        f'<route id="r{k}" edges="{edges}"/>'
        f'<flow id="f{k}" route="r{k}" begin="0" end="3600" vehsPerHour="{rates[edges]}"/>'
        for k, (edges, _, _) in enumerate(J1_MOVEMENTS)
    )
    cfg = _write_scenario(tmp_path, f"<routes>{body}</routes>")
    counts = _counts(derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo"))
    expected = {
        direction: {turn: 0 for turn in ("Left", "Straight", "Right")}
        for direction in ("East", "West", "North", "South")
    }
    for edges, direction, turn in J1_MOVEMENTS:
        expected[direction][turn] = rates[edges]
    assert counts == expected


@requires_sumolib
def test_a_route_through_the_corridor_counts_at_every_signal_it_crosses(tmp_path):
    routes = """<routes>
  <route id="eb" edges="EB_1 EB_2 EB_3 EB_4"/>
  <route id="wb" edges="WB_1 WB_2 WB_3 WB_4"/>
  <flow id="f_eb" route="eb" begin="0" end="3600" vehsPerHour="300"/>
  <flow id="f_wb" route="wb" begin="0" end="3600" vehsPerHour="200"/>
</routes>"""
    cfg = _write_scenario(tmp_path, routes)
    payload = derive_tmc_from_routes(cfg, ["J1", "J2", "J3"], corridor_id="demo")
    for index in range(3):
        counts = _counts(payload, index)
        assert counts["East"] == {"Left": 0, "Straight": 300, "Right": 0}
        assert counts["West"] == {"Left": 0, "Straight": 200, "Right": 0}
        assert counts["North"] == counts["South"] == {"Left": 0, "Straight": 0, "Right": 0}


@requires_sumolib
@pytest.mark.parametrize(
    ("rate_attributes", "expected_vph"),
    [
        ('begin="0" end="1800" vehsPerHour="240"', 240),
        ('begin="0" end="1800" perHour="240"', 240),  # SUMO's newer spelling of vehsPerHour
        ('begin="0" end="1800" period="30"', 120),
        ('begin="0" end="1800" period="13"', 277),  # 276.9 veh/h: rounded, not truncated
        ('begin="0" end="1800" period="exp(0.1)"', 360),  # Poisson arrivals, 0.1 veh/s
        ('begin="0" end="1800" probability="0.05"', 180),
        ('begin="0" end="1800" number="60"', 120),
        ('number="30"', 30),  # no times: the flow spans the whole 3600 s window
    ],
)
def test_every_flow_rate_spelling_counts(tmp_path, rate_attributes, expected_vph):
    cfg = _write_scenario(tmp_path, _east_through_j1(rate_attributes))
    payload = derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo")
    assert _counts(payload)["East"]["Straight"] == expected_vph


@requires_sumolib
@pytest.mark.parametrize(
    ("begin", "end", "n_vehicles", "expected_vph"),
    [
        (0, 3600, 5, 5),
        (0, 1800, 3, 6),  # three vehicles in half an hour: six per hour
        (3600, 7200, 2, 2),  # the window is end - begin, not end
        (0, None, 4, 4),  # no <time> at all: one hour
        (3600, None, 4, 4),  # a start but no end: one hour from the start
        (0, -1, 4, 4),  # SUMO's own "no end" (-1) is no window either
    ],
)
def test_single_vehicles_are_weighted_by_the_scenario_window(
    tmp_path, begin, end, n_vehicles, expected_vph
):
    vehicles = "".join(
        f'<vehicle id="v{i}" route="r" depart="{begin + i}"/>' for i in range(n_vehicles)
    )
    routes = f'<routes><route id="r" edges="EB_1 EB_2"/>{vehicles}</routes>'
    cfg = _write_scenario(tmp_path, routes, end, begin=begin)
    payload = derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo")
    assert _counts(payload)["East"]["Straight"] == expected_vph


@requires_sumolib
def test_a_number_flow_without_times_runs_from_zero_to_the_window_end(tmp_path):
    cfg = _write_scenario(tmp_path, _east_through_j1('number="30"'), end=7200, begin=3600)
    payload = derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo")
    # 30 vehicles over [0, 7200) s: 15 veh/h, running through the whole window [3600, 7200)
    assert _counts(payload)["East"]["Straight"] == 15


@requires_sumolib
def test_demand_is_summed_over_route_files_and_every_way_to_name_a_route(tmp_path, caplog):
    definitions = """<routes>
  <!-- definitions only: SUMO lets later route files use these routes -->
  <vType id="car"/>
  <route id="r" edges="EB_1 EB_2"/>
  <person id="p" depart="0"><walk edges="EB_1 EB_2"/></person>
</routes>"""
    demand = """<routes>
  <flow id="by_reference" route="r" begin="0" end="3600" vehsPerHour="100"/>
  <vehicle id="nested_in_vehicle" depart="0"><route edges="EB_1 EB_2"/></vehicle>
  <flow id="nested_in_flow" begin="0" end="3600" vehsPerHour="50"><route edges="EB_1 EB_2"/></flow>
</routes>"""
    cfg = _write_scenario(tmp_path, definitions, more_routes_xml=[demand])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        payload = derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo")
    assert _counts(payload)["East"]["Straight"] == 100 + 1 + 50  # the person is not traffic
    assert not [record for record in caplog.records if record.name == LOGGER]  # nor skipped demand


@requires_sumolib
def test_demand_that_cannot_be_counted_is_skipped_with_one_warning_per_kind(tmp_path, caplog):
    routes = """<routes>
  <route id="r" edges="EB_1 EB_2"/>
  <trip id="t1" from="EB_1" to="NOWHERE" depart="1"/>
  <trip id="t2" from="NOWHERE" to="EB_2" depart="2"/>
  <trip id="t3" depart="3"/>
  <flow id="lost" route="ghost" begin="0" end="3600" vehsPerHour="500"/>
  <flow id="open_ended" route="r" begin="0" end="never" vehsPerHour="500"/>
  <vehicle id="parked" route="r" depart="triggered"/>
  <flow id="kept" route="r" begin="0" end="3600" vehsPerHour="10"/>
</routes>"""
    cfg = _write_scenario(tmp_path, routes)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        payload = derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo")
    assert _counts(payload)["East"]["Straight"] == 10
    messages = [record.getMessage() for record in caplog.records if record.name == LOGGER]
    assert len(messages) == 3  # one per kind of element, however many there are
    trips = next(message for message in messages if "<trip>" in message)
    assert "3 <trip>" in trips and "t1" in trips and "no path" in trips and "no route" in trips
    flows = next(message for message in messages if "<flow>" in message)
    assert "2 <flow>" in flows and "lost" in flows and "open_ended" in flows
    assert "no route" in flows and "not a time" in flows
    vehicles = next(message for message in messages if "<vehicle>" in message)
    assert "1 <vehicle>" in vehicles and "parked" in vehicles and "not a time" in vehicles


@requires_sumolib
def test_a_signal_no_route_crosses_is_reported(tmp_path, caplog):
    cfg = _write_scenario(tmp_path, _east_through_j1('begin="0" end="3600" vehsPerHour="60"'))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        payload = derive_tmc_from_routes(cfg, ["J1", "J3"], corridor_id="demo")
    assert _counts(payload, 1)["East"]["Straight"] == 0
    messages = [record.getMessage() for record in caplog.records if record.name == LOGGER]
    assert len(messages) == 1 and "J3" in messages[0] and "J1" not in messages[0]


@requires_sumolib
def test_overrides_reclassify_the_links_they_name(tmp_path):
    through_links = [
        lm.link_index
        for lm in derive_link_movements(DEMO_NET, "J1")
        if lm.movement == "EB-through"
    ]
    cfg = _write_scenario(tmp_path, _east_through_j1('begin="0" end="3600" vehsPerHour="300"'))
    plain = _counts(derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo"))
    moved = _counts(
        derive_tmc_from_routes(
            cfg,
            ["J1"],
            corridor_id="demo",
            overrides={"J1": {link: "NB-left" for link in through_links}},
        )
    )
    assert (plain["East"]["Straight"], plain["North"]["Left"]) == (300, 0)
    assert (moved["East"]["Straight"], moved["North"]["Left"]) == (0, 300)


@requires_sumolib
def test_payload_carries_the_signals_position_and_the_requested_metadata(tmp_path):
    corridor = (
        '<routes><route id="eb" edges="EB_1 EB_2 EB_3 EB_4"/>'
        '<flow id="f" route="eb" begin="0" end="3600" vehsPerHour="100"/></routes>'
    )
    cfg = _write_scenario(tmp_path, corridor)
    payload = derive_tmc_from_routes(
        cfg, ["J1", "J2", "J3"], corridor_id="my_corridor", timestamp_start="2026-07-01T08:00:00"
    )
    assert payload["corridor_id"] == "my_corridor"
    assert payload["timestamp_start"] == "2026-07-01T08:00:00"
    assert json.loads(json.dumps(payload)) == payload
    # The demo's own TMC was cut from these very junctions (its i1..i3 are J1..J3).
    for derived, reference in zip(payload["intersections"], demo_tmc()["intersections"]):
        assert derived["lat"] == pytest.approx(reference["lat"], abs=1e-6)
        assert derived["lon"] == pytest.approx(reference["lon"], abs=1e-6)
    assert TMCData.from_payload(payload).observation_seconds() == 3600.0
    default = derive_tmc_from_routes(cfg, ["J1"], corridor_id="x")
    assert default["timestamp_start"] == "1970-01-01T00:00:00"


@requires_sumolib
def test_unknown_signal_fails_clearly(tmp_path):
    cfg = _write_scenario(tmp_path, "<routes/>")
    with pytest.raises(ValueError, match="NOPE"):
        derive_tmc_from_routes(cfg, ["NOPE"], corridor_id="demo")


@requires_sumolib
def test_base_without_route_files_fails_clearly(tmp_path):
    cfg = tmp_path / "base.sumocfg"
    cfg.write_text(
        f'<configuration><input><net-file value="{DEMO_NET}"/></input></configuration>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="route-files"):
        derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo")


# ---------------------------------------------------------------------------
# The counting window, concurrent demand and routed trips
# ---------------------------------------------------------------------------

# J1's eastbound, westbound and northbound through movements.
_J1_ROUTES = (
    '<route id="eb" edges="EB_1 EB_2"/><route id="wb" edges="WB_3 WB_4"/>'
    '<route id="nb" edges="NB_J1_1 NB_J1_2"/>'
)


def _derive_j1(tmp_path: Path, body: str, *, end: int | str | None = 1800, **kwargs) -> dict:
    """J1's counts for the three through routes above plus ``body``, in a ``[0, end)`` scenario."""
    cfg = _write_scenario(tmp_path, f"<routes>{_J1_ROUTES}{body}</routes>", end)
    return _counts(derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo", **kwargs))


@requires_sumolib
def test_flows_that_run_one_after_another_do_not_add_up(tmp_path):
    periods = "".join(
        f'<flow id="p{k}" route="eb" begin="{600 * k}" end="{600 * (k + 1)}" vehsPerHour="600"/>'
        for k in range(3)
    )
    assert _derive_j1(tmp_path, periods)["East"]["Straight"] == 600  # not 1800


@requires_sumolib
def test_the_count_is_the_peak_of_the_flows_running_at_the_same_time(tmp_path):
    counts = _derive_j1(
        tmp_path,
        """<flow id="a" route="eb" begin="0" end="1200" vehsPerHour="300"/>
           <flow id="b" route="eb" begin="600" end="1800" vehsPerHour="200"/>
           <flow id="c" route="eb" begin="1500" end="1800" vehsPerHour="100"/>""",
    )
    # 300 | 500 (a and b) | 200 (b) | 300 (b and c): the busiest moment is 600-1200 s
    assert counts["East"]["Straight"] == 500


@requires_sumolib
def test_demand_outside_the_horizon_is_not_counted(tmp_path):
    counts = _derive_j1(
        tmp_path,
        """<flow id="now" route="eb" begin="0" end="1800" vehsPerHour="100"/>
           <flow id="later" route="wb" begin="3600" end="7200" vehsPerHour="500"/>
           <vehicle id="late" route="nb" depart="3600"/>""",
    )
    assert counts["East"]["Straight"] == 100
    assert counts["West"]["Straight"] == 0 and counts["North"]["Straight"] == 0


@requires_sumolib
@pytest.mark.parametrize(("depart", "expected_vph"), [(0, 2), (1799.5, 2), (1800, 0)])
def test_a_vehicle_counts_when_it_departs_inside_the_half_open_window(
    tmp_path, depart, expected_vph
):
    counts = _derive_j1(
        tmp_path,
        '<flow id="f" route="eb" begin="0" end="1800" vehsPerHour="100"/>'
        f'<vehicle id="v" route="nb" depart="{depart}"/>',
    )
    assert counts["North"]["Straight"] == expected_vph  # one vehicle in 1800 s is 2 veh/h


@requires_sumolib
def test_flows_that_only_touch_the_window_are_not_counted(tmp_path):
    counts = _derive_j1(
        tmp_path,
        """<flow id="before" route="eb" begin="0" end="900" vehsPerHour="100"/>
           <flow id="inside" route="wb" begin="900" end="1800" vehsPerHour="200"/>
           <flow id="after" route="nb" begin="1800" end="2700" vehsPerHour="300"/>""",
        horizon_s=(900, 1800),
    )
    assert counts["East"]["Straight"] == 0
    assert counts["West"]["Straight"] == 200
    assert counts["North"]["Straight"] == 0


@requires_sumolib
def test_number_flows_spread_over_their_own_window_not_the_clipped_one(tmp_path):
    counts = _derive_j1(
        tmp_path,
        """<flow id="inside" route="eb" begin="0" end="900" number="90"/>
           <flow id="half_in" route="wb" begin="900" end="2700" number="60"/>
           <flow id="after" route="nb" begin="5400" number="300"/>""",
    )
    assert counts["East"]["Straight"] == 360  # 90 vehicles in 900 s
    assert counts["West"]["Straight"] == 120  # 60 vehicles in 1800 s, of which 900 s are inside
    assert counts["North"]["Straight"] == 0  # starts after the horizon: dropped, not 300 in "1 s"


@requires_sumolib
def test_a_number_and_period_flow_stops_after_its_vehicles(tmp_path):
    counts = _derive_j1(
        tmp_path,
        """<flow id="burst" route="eb" begin="0" number="10" period="30"/>
           <flow id="then" route="eb" begin="300" end="600" vehsPerHour="120"/>""",
    )
    # 10 vehicles every 30 s end at 300 s, when the next flow starts: they never overlap
    assert counts["East"]["Straight"] == 120


@requires_sumolib
@pytest.mark.parametrize(("begin", "end"), [(600, 300), (5400, 3600)])
def test_a_flow_that_ends_before_it_begins_fails_clearly(tmp_path, begin, end):
    flow = f'<flow id="backwards" route="eb" begin="{begin}" end="{end}" vehsPerHour="100"/>'
    with pytest.raises(ValueError, match=r"demand\.rou\.xml: .*backwards"):
        _derive_j1(tmp_path, flow)


@requires_sumolib
def test_the_horizon_argument_overrides_the_scenario_window(tmp_path):
    body = """<flow id="first" route="eb" begin="0" end="1800" vehsPerHour="600"/>
              <flow id="second" route="wb" begin="1800" end="3600" vehsPerHour="300"/>"""

    def east_west(**kwargs):
        counts = _derive_j1(tmp_path, body, end=3600, **kwargs)
        return counts["East"]["Straight"], counts["West"]["Straight"]

    assert east_west() == (600, 300)  # the sumocfg's own 0-3600 s window
    assert east_west(horizon_s=(0, 1800)) == (600, 0)
    assert east_west(horizon_s=(1800, 3600)) == (0, 300)


@requires_sumolib
@pytest.mark.parametrize("horizon", [(1800, 1800), (1800, 0)])
def test_an_empty_or_inverted_horizon_fails_clearly(tmp_path, horizon):
    flow = '<flow id="f" route="eb" begin="0" end="1800" vehsPerHour="100"/>'
    with pytest.raises(ValueError, match="horizon_s"):
        _derive_j1(tmp_path, flow, horizon_s=horizon)


@requires_sumolib
def test_period_shaped_demand_counts_the_period_inside_the_horizon(tmp_path):
    """A period-shaped route file in miniature: three periods per route, a 0-1800 s scenario."""
    counts = _derive_j1(
        tmp_path,
        """<flow id="eb_1" route="eb" begin="0" end="1800" vehsPerHour="600"/>
           <flow id="eb_2" route="eb" begin="1800" end="5400" vehsPerHour="300"/>
           <flow id="eb_3" route="eb" begin="5400" end="7200" vehsPerHour="150"/>
           <flow id="wb_1" route="wb" begin="0" end="1800" vehsPerHour="400"/>
           <flow id="wb_2" route="wb" begin="1800" end="5400" vehsPerHour="200"/>
           <flow id="wb_3" route="wb" begin="5400" end="7200" vehsPerHour="100"/>""",
    )
    assert (counts["East"]["Straight"], counts["West"]["Straight"]) == (600, 400)


@requires_sumolib
def test_clock_times_are_read_like_seconds(tmp_path):
    counts = _derive_j1(
        tmp_path,
        """<flow id="am" route="eb" begin="00:10:00" end="00:20:00" vehsPerHour="100"/>
           <flow id="later" route="wb" begin="01:00:00" end="02:00:00" vehsPerHour="500"/>
           <vehicle id="v" route="nb" depart="00:00:30"/>""",
    )
    assert counts["East"]["Straight"] == 100
    assert counts["West"]["Straight"] == 0  # 01:00:00 is 3600 s: after the horizon
    assert counts["North"]["Straight"] == 2  # one vehicle in 1800 s


@requires_sumolib
def test_the_scenario_window_may_be_written_as_clock_times(tmp_path):
    flows = """<flow id="a" route="eb" begin="0" end="1800" vehsPerHour="100"/>
               <flow id="b" route="wb" begin="1800" end="3600" vehsPerHour="100"/>"""
    counts = _derive_j1(tmp_path, flows, end="00:30:00")
    assert (counts["East"]["Straight"], counts["West"]["Straight"]) == (100, 0)


@requires_sumolib
def test_no_signal_to_count_at_fails_clearly(tmp_path):
    cfg = _write_scenario(tmp_path, _east_through_j1('begin="0" end="3600" vehsPerHour="60"'))
    with pytest.raises(ValueError, match="tls_ids is empty"):
        derive_tmc_from_routes(cfg, [], corridor_id="demo")


@requires_sumolib
@pytest.mark.parametrize(
    "body",
    [
        "",
        '<flow id="later" route="eb" begin="3600" end="7200" vehsPerHour="500"/>',
        '<vehicle id="late" route="eb" depart="3600"/>',
    ],
)
def test_no_demand_at_any_requested_signal_fails_clearly(tmp_path, body):
    with pytest.raises(ValueError, match=NO_DEMAND + r" \[0, 1800\) s"):
        _derive_j1(tmp_path, body)


@requires_sumolib
def test_demand_elsewhere_does_not_help_when_every_requested_signal_is_empty(tmp_path):
    cfg = _write_scenario(tmp_path, _east_through_j1('begin="0" end="3600" vehsPerHour="60"'))
    with pytest.raises(ValueError, match=NO_DEMAND + r" \[0, 3600\) s \(signals: \['J2', 'J3'\]"):
        derive_tmc_from_routes(cfg, ["J2", "J3"], corridor_id="demo")  # the route crosses J1 only


@requires_sumolib
def test_a_trip_scenario_is_routed_and_counted(tmp_path):
    """randomTrips-style demand: <trip from to depart> with no route edges at all."""
    corridor = "".join(
        f'<trip id="c{k}" depart="{k * 20}.00" from="EB_1" to="EB_4"/>' for k in range(30)
    )
    side = "".join(
        f'<trip id="s{k}" depart="{k * 20 + 5}.00" from="NB_J1_0" to="EB_3"/>' for k in range(10)
    )
    cfg = _write_scenario(tmp_path, f"<routes>{corridor}{side}</routes>", 600)
    payload = derive_tmc_from_routes(cfg, ["J1", "J2", "J3"], corridor_id="demo")
    j1, j2, j3 = (_counts(payload, index) for index in range(3))
    # each trip stands for 3600 / 600 = 6 veh/h
    assert j1["East"]["Straight"] == 180 and j1["North"]["Right"] == 60
    assert j2["East"]["Straight"] == 240  # the corridor trips and the side-street trips that joined
    assert j3["East"]["Straight"] == 180


@requires_sumolib
def test_demand_outside_the_window_is_not_even_routed(tmp_path, monkeypatch):
    calls = []
    real = sumolib.net.Net.getShortestPath

    def counting(self, from_edge, to_edge, *args, **kwargs):
        calls.append((from_edge.getID(), to_edge.getID()))
        return real(self, from_edge, to_edge, *args, **kwargs)

    monkeypatch.setattr(sumolib.net.Net, "getShortestPath", counting)
    counts = _derive_j1(
        tmp_path,
        """<flow id="now" route="eb" begin="0" end="1800" vehsPerHour="100"/>
           <trip id="late_trip" depart="5000" from="EB_1" to="EB_4"/>
           <flow id="late_flow" from="NB_J1_0" to="EB_3" begin="3600" end="7200" vehsPerHour="50"/>
           <trip id="early_trip" depart="10" from="WB_3" to="WB_4"/>""",
    )
    assert calls == [("WB_3", "WB_4")]  # only the trip that departs inside the window is routed
    assert counts["West"]["Straight"] == 2 and counts["East"]["Straight"] == 100


@requires_sumolib
def test_a_flow_given_by_from_and_to_is_routed(tmp_path):
    flow = '<flow id="f" from="EB_1" to="EB_4" begin="0" end="3600" vehsPerHour="120"/>'
    cfg = _write_scenario(tmp_path, f"<routes>{flow}</routes>")
    payload = derive_tmc_from_routes(cfg, ["J1", "J2", "J3"], corridor_id="demo")
    assert [_counts(payload, index)["East"]["Straight"] for index in range(3)] == [120, 120, 120]


@requires_sumolib
def test_the_via_edges_of_a_trip_shape_its_route(tmp_path):
    # EB_1 -> NB_J1_2 (a left turn into the side street), round its dead end, and back through J1
    trip = '<trip id="t" depart="0" from="EB_1" via="NB_J1_2" to="EB_3"/>'
    cfg = _write_scenario(tmp_path, f"<routes>{trip}</routes>")
    payload = derive_tmc_from_routes(cfg, ["J1", "J2"], corridor_id="demo")
    j1, j2 = _counts(payload, 0), _counts(payload, 1)
    assert (j1["East"]["Left"], j1["South"]["Left"], j2["East"]["Straight"]) == (1, 1, 1)
    assert j1["East"]["Straight"] == 0  # not the direct EB_1 -> EB_2


@requires_sumolib
def test_trips_are_routed_once_per_pair_of_edges(tmp_path, monkeypatch):
    calls = []
    real = sumolib.net.Net.getShortestPath

    def counting(self, from_edge, to_edge, *args, **kwargs):
        calls.append((from_edge.getID(), to_edge.getID()))
        return real(self, from_edge, to_edge, *args, **kwargs)

    monkeypatch.setattr(sumolib.net.Net, "getShortestPath", counting)
    trips = "".join(f'<trip id="a{k}" depart="{k}" from="EB_1" to="EB_4"/>' for k in range(5))
    trips += "".join(f'<trip id="b{k}" depart="{k}" from="NB_J1_0" to="EB_3"/>' for k in range(3))
    cfg = _write_scenario(tmp_path, f"<routes>{trips}</routes>")
    derive_tmc_from_routes(cfg, ["J1"], corridor_id="demo")
    assert sorted(calls) == [("EB_1", "EB_4"), ("NB_J1_0", "EB_3")]


@requires_sumolib
def test_links_of_one_edge_pair_labelled_differently_are_reported(tmp_path, caplog):
    through_links = sorted(
        lm.link_index
        for lm in derive_link_movements(DEMO_NET, "J1")
        if lm.movement == "EB-through"
    )
    assert len(through_links) == 2  # one per lane
    cfg = _write_scenario(tmp_path, _east_through_j1('begin="0" end="3600" vehsPerHour="300"'))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        derive_tmc_from_routes(
            cfg, ["J1"], corridor_id="demo", overrides={"J1": {through_links[0]: "NB-left"}}
        )
    messages = [record.getMessage() for record in caplog.records if record.name == LOGGER]
    assert len(messages) == 1
    assert "J1" in messages[0] and "EB_1" in messages[0] and "EB_2" in messages[0]
    assert "NB-left" in messages[0] and "EB-through" in messages[0]


# ---------------------------------------------------------------------------
# A scenario route file that names each flow after its movement
# ---------------------------------------------------------------------------

#: One flow per movement at J1, named ``flow_<axis>_<direction>_<turn>``
#: (``flow_ew_east_left`` is the eastbound left turn). The edges of each
#: movement are read off the demo geometry: the arterial runs EB_1 -> J1 -> EB_2
#: eastbound and WB_3 -> J1 -> WB_4 westbound, the cross street NB_J1_1 -> J1
#: -> NB_J1_2 northbound and SB_J1_1 -> J1 -> SB_J1_2 southbound.
_J1_MOVEMENT_EDGES = {
    ("ew", "east", "left"): ("EB_1", "NB_J1_2"),
    ("ew", "east", "straight"): ("EB_1", "EB_2"),
    ("ew", "east", "right"): ("EB_1", "SB_J1_2"),
    ("ew", "west", "left"): ("WB_3", "SB_J1_2"),
    ("ew", "west", "straight"): ("WB_3", "WB_4"),
    ("ew", "west", "right"): ("WB_3", "NB_J1_2"),
    ("ns", "north", "left"): ("NB_J1_1", "WB_4"),
    ("ns", "north", "straight"): ("NB_J1_1", "NB_J1_2"),
    ("ns", "north", "right"): ("NB_J1_1", "EB_2"),
    ("ns", "south", "left"): ("SB_J1_1", "EB_2"),
    ("ns", "south", "straight"): ("SB_J1_1", "SB_J1_2"),
    ("ns", "south", "right"): ("SB_J1_1", "WB_4"),
}
#: veh/h of each movement's flow (all distinct, so a swapped label shows).
_J1_MOVEMENT_RATES = {
    ("east", "left"): 40, ("east", "straight"): 320, ("east", "right"): 60,
    ("west", "left"): 55, ("west", "straight"): 290, ("west", "right"): 75,
    ("north", "left"): 30, ("north", "straight"): 95, ("north", "right"): 45,
    ("south", "left"): 35, ("south", "straight"): 85, ("south", "right"): 50,
}
_TMC_TURNS = {"left": "Left", "straight": "Straight", "right": "Right"}


def _movement_named_routes() -> str:
    rows = ["<routes>"]
    for (axis, direction, turn), (from_edge, to_edge) in _J1_MOVEMENT_EDGES.items():
        name = f"{axis}_{direction}_{turn}"
        rows.append(f'  <route id="r_{name}" edges="{from_edge} {to_edge}"/>')
        rows.append(
            f'  <flow id="flow_{name}" route="r_{name}" begin="0" end="3600" '
            f'vehsPerHour="{_J1_MOVEMENT_RATES[(direction, turn)]}"/>'
        )
    rows.append("</routes>")
    return "\n".join(rows)


@requires_sumolib
@requires_netconvert
def test_movement_named_route_file_reproduces_its_flows(tmp_path, caplog):
    """A scenario route file names each flow after its movement at J1.

    ``flow_ew_east_left`` is the eastbound left turn, ``flow_ns_south_straight`` the southbound
    through movement, and so on, so the route file states the counts it should give back. The
    net is the demo network re-built with signalized pedestrian crossings: the signal's
    controlled links include the crossing links.
    """
    net = build_crossing_net(tmp_path / "net")
    routes = tmp_path / "scenario.rou.xml"
    routes.write_text(_movement_named_routes(), encoding="utf-8")
    cfg = write_sumocfg(tmp_path, net, name="scenario.sumocfg", route_files=(routes,))
    # Premise: J1 controls more links than it has vehicle connections.
    assert controlled_link_count(net, "J1") > len(derive_link_movements(net, "J1"))

    expected: dict[str, dict[str, int]] = {}
    for flow in ET.parse(routes).getroot().iter("flow"):
        _, _, direction, turn = flow.get("id").split("_")
        expected.setdefault(direction.capitalize(), {})[_TMC_TURNS[turn]] = int(
            flow.get("vehsPerHour")
        )
    assert sorted(expected) == ["East", "North", "South", "West"]

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        payload = derive_tmc_from_routes(cfg, ["J1", "J2"], corridor_id="demo_arterial")
    first, second = payload["intersections"]
    assert first["counts"] == expected
    # Every route ends on the edge that leaves J1, so the demand never reaches the J2
    # signal: all zero, and it says so.
    assert all(v == 0 for turns in second["counts"].values() for v in turns.values())
    messages = [record.getMessage() for record in caplog.records if record.name == LOGGER]
    assert len(messages) == 1 and "J2" in messages[0] and "J1" not in messages[0]
    # ...and the junction sits where the demo TMC puts the same intersection.
    lat, lon = DEMO_COORDS["i1"]
    assert first["lat"] == pytest.approx(lat, abs=1e-4)
    assert first["lon"] == pytest.approx(lon, abs=1e-4)
