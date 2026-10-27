"""The bundled demo scenario (routes + sumocfg + placeholder TMC) must be complete and consistent."""
from __future__ import annotations

import json
from xml.etree import ElementTree as ET

from tests.test_city.conftest import REPO_ROOT, requires_sumolib

DEMO = REPO_ROOT / "examples" / "corridor_demo"
SIGNALS = ["J1", "J2", "J3"]


def test_demo_scenario_assets_exist_and_reference_each_other():
    cfg = ET.parse(DEMO / "demo_scenario.sumocfg").getroot()
    assert cfg.find(".//net-file").get("value") == "demo.net.xml"
    assert cfg.find(".//route-files").get("value") == "demo_routes.rou.xml"
    assert cfg.find(".//additional-files").get("value") == "demo.tls.xml"
    routes = ET.parse(DEMO / "demo_routes.rou.xml").getroot()
    assert len(routes.findall("flow")) >= 4
    placeholder = json.loads((DEMO / "placeholder_tmc.json").read_text(encoding="utf-8"))
    assert placeholder["corridor_id"] == "demo_scenario" and placeholder["intersections"]


def test_demo_routes_run_on_edges_of_the_demo_net():
    """Every flow names a route that is defined, and every route edge exists in the net."""
    net = ET.parse(DEMO / "demo.net.xml").getroot()
    net_edges = {edge.get("id") for edge in net.iter("edge") if edge.get("function") != "internal"}
    routes = ET.parse(DEMO / "demo_routes.rou.xml").getroot()
    defined = {route.get("id"): route.get("edges").split() for route in routes.findall("route")}
    for flow in routes.findall("flow"):
        assert flow.get("route") in defined, f"flow {flow.get('id')} names no defined route"
    for route_id, edges in defined.items():
        missing = [edge for edge in edges if edge not in net_edges]
        assert not missing, f"route {route_id} uses edges that are not in demo.net.xml: {missing}"


@requires_sumolib
def test_demo_scenario_demand_reaches_every_signal_on_both_axes_within_the_run():
    """What ``counts_source=scenario_routes`` derives for the self-test's horizon [0, 600)."""
    from terasim_city.flow.counts_from_routes import derive_tmc_from_routes

    payload = derive_tmc_from_routes(
        DEMO / "demo_scenario.sumocfg",
        SIGNALS,
        corridor_id="demo_scenario",
        horizon_s=(0.0, 600.0),
    )
    assert [item["intersection_id"] for item in payload["intersections"]] == SIGNALS
    for item in payload["intersections"]:
        counts = item["counts"]
        total = {
            axis: sum(sum(counts[direction].values()) for direction in directions)
            for axis, directions in (("east-west", ("East", "West")), ("north-south", ("North", "South")))
        }
        assert all(vehicles > 0 for vehicles in total.values()), (item["intersection_id"], total)


@requires_sumolib
def test_demo_scenario_programs_share_one_cycle():
    """The customer-style baseline must have a common cycle, or a winning baseline exports no NEMA plan."""
    from terasim_city.plans.sumo_programs import load_scenario_programs

    plan = load_scenario_programs(DEMO / "demo_scenario.sumocfg", SIGNALS)
    assert plan.provenance.source == "sumo_tls"
    assert plan.common_cycle_s() > 0
