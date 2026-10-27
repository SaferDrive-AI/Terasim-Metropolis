"""Unit tests for the SUMO ``rou.xml`` writer.

These tests run :func:`write_routes_xml` with ``net=None`` so the topology
helpers (which need a real ``sumolib.net.Net``) are bypassed. That lets us
exercise the XML-shaping logic in isolation.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from terasim_city.flow.descriptors import FlowDescriptor
from terasim_city.flow.writers.routes_xml import write_routes_xml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fd(
    flow_id: str = "f0",
    route_id: str = "r0",
    edges: tuple[str, ...] = ("e1", "e2", "e3"),
    vehs_per_hour: float = 100.0,
    **overrides,
) -> FlowDescriptor:
    """Construct a FlowDescriptor with sensible test defaults."""
    return FlowDescriptor(
        flow_id=flow_id,
        route_id=route_id,
        route_edges=edges,
        vehs_per_hour=vehs_per_hour,
        **overrides,
    )


def _parse(out: Path) -> ET.Element:
    """Parse the generated routes file via stdlib ElementTree."""
    return ET.parse(out).getroot()


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------


def test_single_descriptor_writes_one_route_and_one_flow(tmp_path: Path):
    out = tmp_path / "routes.rou.xml"
    fd = _fd(vehs_per_hour=120.0, edges=("a", "b", "c"))

    n_flows, vehph, starts = write_routes_xml([fd], out)

    assert n_flows == 1
    assert vehph == pytest.approx(120.0)
    assert starts == {"a": 1}

    root = _parse(out)
    routes = root.findall("route")
    flows = root.findall("flow")
    assert len(routes) == 1
    assert len(flows) == 1

    assert routes[0].attrib["id"] == "r0"
    assert routes[0].attrib["edges"] == "a b c"

    fl = flows[0].attrib
    assert fl["id"] == "f0"
    assert fl["type"] == "car_normal"
    assert fl["route"] == "r0"
    assert float(fl["vehsPerHour"]) == pytest.approx(120.0)
    assert fl["departLane"] == "best"
    assert fl["departSpeed"] == "max"


def test_multiple_descriptors_aggregate_start_edge_counts(tmp_path: Path):
    out = tmp_path / "routes.rou.xml"
    fds = [
        _fd(flow_id="f0", route_id="r0", edges=("a", "b"), vehs_per_hour=10.0),
        _fd(flow_id="f1", route_id="r1", edges=("a", "c"), vehs_per_hour=20.0),
        _fd(flow_id="f2", route_id="r2", edges=("d", "e"), vehs_per_hour=30.0),
    ]
    n_flows, vehph, starts = write_routes_xml(fds, out)

    assert n_flows == 3
    assert vehph == pytest.approx(60.0)
    assert starts == {"a": 2, "d": 1}


# ---------------------------------------------------------------------------
# Filtering rules
# ---------------------------------------------------------------------------


def test_flow_scale_multiplies_rate_and_can_drop_below_threshold(tmp_path: Path):
    out = tmp_path / "routes.rou.xml"
    fd = _fd(vehs_per_hour=100.0)
    # 100 * 0.0001 = 0.01 → just at the drop boundary
    n_flows, vehph, _ = write_routes_xml([fd], out, flow_scale=0.0001)
    assert n_flows == 0
    assert vehph == 0.0


def test_flow_scale_multiplies_emitted_rate(tmp_path: Path):
    out = tmp_path / "routes.rou.xml"
    fd = _fd(vehs_per_hour=100.0)
    n_flows, vehph, _ = write_routes_xml([fd], out, flow_scale=2.5)
    assert n_flows == 1
    assert vehph == pytest.approx(250.0)
    root = _parse(out)
    assert float(root.find("flow").attrib["vehsPerHour"]) == pytest.approx(250.0)


def test_single_edge_descriptor_is_skipped(tmp_path: Path):
    """SUMO refuses single-edge routes; the writer must drop them."""
    out = tmp_path / "routes.rou.xml"
    fd = _fd(edges=("only_one",), vehs_per_hour=50.0)
    n_flows, _, _ = write_routes_xml([fd], out)
    assert n_flows == 0


# ---------------------------------------------------------------------------
# end-time handling
# ---------------------------------------------------------------------------

def test_end_attribute_from_sim_end_overrides_descriptor(tmp_path: Path):
    out = tmp_path / "routes.rou.xml"
    fd = _fd(end=99.0, vehs_per_hour=10.0)
    write_routes_xml([fd], out, sim_end=200.0)
    flow = _parse(out).find("flow")
    assert float(flow.attrib["end"]) == pytest.approx(200.0)


def test_end_attribute_from_descriptor_when_sim_end_none(tmp_path: Path):
    out = tmp_path / "routes.rou.xml"
    fd = _fd(end=42.0, vehs_per_hour=10.0)
    write_routes_xml([fd], out, sim_end=None)
    flow = _parse(out).find("flow")
    assert float(flow.attrib["end"]) == pytest.approx(42.0)


def test_no_end_attribute_when_both_none(tmp_path: Path):
    out = tmp_path / "routes.rou.xml"
    fd = _fd(end=None, vehs_per_hour=10.0)
    write_routes_xml([fd], out, sim_end=None)
    flow = _parse(out).find("flow")
    assert "end" not in flow.attrib


# ---------------------------------------------------------------------------
# Parent directory creation
# ---------------------------------------------------------------------------

def test_output_parent_directory_is_created(tmp_path: Path):
    out = tmp_path / "nested" / "deeper" / "routes.rou.xml"
    fd = _fd(vehs_per_hour=10.0)
    write_routes_xml([fd], out)
    assert out.exists()


# ---------------------------------------------------------------------------
# XML well-formedness
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _summarise_routes (private helper in api.py)
# ---------------------------------------------------------------------------
# This belongs here (not in test_pipeline.py) because the helper only parses
# the generated rou.xml — it doesn't need sumolib or a real net. Keeping it
# out of test_pipeline.py avoids the (unnecessary) sumolib gate.


def test_summarise_routes_counts_flows_and_sums_rates(tmp_path: Path):
    from terasim_city.api import _summarise_routes

    out = tmp_path / "routes.rou.xml"
    write_routes_xml(
        [
            _fd(flow_id="f0", route_id="r0", edges=("a", "b"), vehs_per_hour=10.0),
            _fd(flow_id="f1", route_id="r1", edges=("c", "d"), vehs_per_hour=25.0),
        ],
        out,
    )
    n, vph = _summarise_routes(out)
    assert n == 2
    assert vph == pytest.approx(35.0)


