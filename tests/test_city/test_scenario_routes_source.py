"""The scenario_routes flow source copies the base scenario's demand verbatim."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from terasim_city.api import FLOW_SOURCES, build_flow_source, run_tmc_pipeline
from tests.fixtures.tmc_templates import demo_tmc, write_tmc
from tests.test_city.conftest import REPO_ROOT, requires_sumo_runtime, requires_sumolib

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_NET = DEMO_DIR / "demo.net.xml"

# EB_1 -> EB_2 -> EB_3 are three consecutive eastbound edges of the demo net
# (W1 -> J1 -> J2 -> J3); WB_1 -> WB_2 -> WB_3 run the other way.
ROUTES = """<routes>
  <vType id="car" length="4.5" maxSpeed="20"/>
  <route id="r1" edges="EB_1 EB_2 EB_3"/>
  <flow id="f1" route="r1" type="car" begin="0" end="600" vehsPerHour="300"/>
  <vehicle id="v1" route="r1" type="car" depart="10"/>
</routes>
"""

# Per-mode route files that cover the same ten minutes in two intervals each.
# SUMO merges several route files by departure time itself; one concatenated
# file has to stay sorted, or SUMO ignores the elements that go back in time.
CARS = """<routes>
  <!-- cars, eastbound -->
  <vType id="car" length="4.5" maxSpeed="20"/>
  <route id="rc" edges="EB_1 EB_2 EB_3"/>
  <flow id="car_a" route="rc" type="car" begin="0" end="300" vehsPerHour="600"/>
  <flow id="car_b" route="rc" type="car" begin="300" end="600" vehsPerHour="600"/>
</routes>
"""
BIKES = """<routes>
  <vType id="bike" length="2" maxSpeed="15"/>
  <route id="rb" edges="WB_1 WB_2 WB_3"/>
  <flow id="bike_a" route="rb" type="bike" begin="0" end="300" vehsPerHour="600"/>
  <flow id="bike_b" route="rb" type="bike" begin="300" end="600" vehsPerHour="600"/>
</routes>
"""


def _base_scenario(tmp_path: Path, *route_texts: str) -> Path:
    """A base sumocfg on the demo net whose route files hold ``route_texts``."""
    route_paths = []
    for index, text in enumerate(route_texts or (ROUTES,)):
        path = tmp_path / f"demand{index}.rou.xml"
        path.write_text(text, encoding="utf-8")
        route_paths.append(path)
    cfg = tmp_path / "base.sumocfg"
    cfg.write_text(
        f"""<configuration><input>
<net-file value="{DEMO_NET}"/>
<route-files value="{','.join(str(path) for path in route_paths)}"/>
<additional-files value="{DEMO_DIR / 'demo.tls.xml'}"/>
</input><time><begin value="0"/><end value="600"/></time></configuration>""",
        encoding="utf-8",
    )
    return cfg


def _run_pipeline(tmp_path: Path, *route_texts: str, sim_end: float = 600):
    base = _base_scenario(tmp_path, *route_texts)
    tmc_path = write_tmc(tmp_path, demo_tmc())
    return run_tmc_pipeline(
        tmc_path, base, tmp_path / "out", sim_end=sim_end, flow_source="scenario_routes"
    )


def _generate(tmp_path: Path, *route_texts: str):
    """Run only the source; return ``(source, written routes file)``."""
    base = _base_scenario(tmp_path, *route_texts)
    tmc_path = write_tmc(tmp_path, demo_tmc())
    source = build_flow_source("scenario_routes", tmc_path, base_sumocfg=base)
    out = tmp_path / "merged" / "routes.rou.xml"
    source.generate(DEMO_NET, out, sim_end=600)
    return source, out


def _ids(routes_file: Path) -> list[str | None]:
    return [child.get("id") for child in ET.parse(routes_file).getroot()]


def test_registered():
    assert "scenario_routes" in FLOW_SOURCES


@requires_sumolib
def test_pipeline_copies_scenario_routes(tmp_path):
    base = _base_scenario(tmp_path)
    tmc_path = write_tmc(tmp_path, demo_tmc())
    scenario = run_tmc_pipeline(
        tmc_path, base, tmp_path / "out", sim_end=600, flow_source="scenario_routes"
    )
    root = ET.parse(scenario.routes_file).getroot()
    assert {child.get("id") for child in root} == {"car", "r1", "f1", "v1"}
    assert scenario.n_flows == 1
    cfg_text = Path(scenario.sumocfg_file).read_text(encoding="utf-8")
    assert "routes.rou.xml" in cfg_text and "demo.tls.xml" in cfg_text


@requires_sumolib
def test_pipeline_rejects_base_without_routes(tmp_path):
    cfg = tmp_path / "base.sumocfg"
    cfg.write_text(
        f'<configuration><input><net-file value="{DEMO_NET}"/></input></configuration>',
        encoding="utf-8",
    )
    tmc_path = write_tmc(tmp_path, demo_tmc())
    with pytest.raises(ValueError, match="route-files"):
        run_tmc_pipeline(tmc_path, cfg, tmp_path / "out", flow_source="scenario_routes")


@requires_sumolib
def test_single_route_file_is_copied_element_for_element(tmp_path):
    scenario = _run_pipeline(tmp_path)
    source_file = tmp_path / "demand0.rou.xml"

    def shape(path):
        return [(child.tag, dict(child.attrib)) for child in ET.parse(path).getroot()]

    assert shape(scenario.routes_file) == shape(source_file)  # same elements, attributes, order
    assert source_file.read_text(encoding="utf-8") == ROUTES  # the customer's file is left alone


@requires_sumolib
def test_metadata_names_the_scenario_routes_source(tmp_path):
    scenario = _run_pipeline(tmp_path)
    meta = json.loads(scenario.metadata_file.read_text(encoding="utf-8"))
    assert meta["source"]["type"] == "scenario_routes"
    assert meta["source"]["flow_source"] == "scenario_routes"
    assert meta["params"]["flow_source"] == "scenario_routes"
    assert meta["source"]["corridor_id"] == demo_tmc()["corridor_id"]
    assert meta["output"] == {"n_flows": 1, "total_vehs_per_hour": 300.0}


@requires_sumolib
def test_build_flow_source_forwards_base_sumocfg_to_every_source(tmp_path):
    base = _base_scenario(tmp_path)
    tmc_path = write_tmc(tmp_path, demo_tmc())
    routes_source = build_flow_source("scenario_routes", tmc_path, base_sumocfg=base)
    assert isinstance(routes_source, FLOW_SOURCES["scenario_routes"])
    assert routes_source.source_name == "scenario_routes"
    # corridor_graph takes the argument only so the registry can call every
    # constructor the same way.
    graph_source = build_flow_source("corridor_graph", tmc_path, base_sumocfg=base)
    assert isinstance(graph_source, FLOW_SOURCES["corridor_graph"])
    assert graph_source.source_name == "corridor_graph"


@requires_sumolib
def test_scenario_routes_needs_a_base_sumocfg(tmp_path):
    with pytest.raises(ValueError, match="base_sumocfg"):
        build_flow_source("scenario_routes", write_tmc(tmp_path, demo_tmc()))


@requires_sumolib
def test_route_files_already_in_departure_order_are_concatenated_unchanged(tmp_path):
    morning = """<routes>
  <vType id="car"/>
  <route id="rc" edges="EB_1 EB_2 EB_3"/>
  <flow id="am" route="rc" type="car" begin="0" end="300" vehsPerHour="600"/>
</routes>"""
    evening = """<routes>
  <route id="rb" edges="WB_1 WB_2 WB_3"/>
  <flow id="pm" route="rb" type="car" begin="300" end="600" vehsPerHour="600"/>
</routes>"""
    _, out = _generate(tmp_path, morning, evening)
    assert _ids(out) == ["car", "rc", "am", "rb", "pm"]


@requires_sumolib
def test_a_single_route_file_is_never_reordered(tmp_path):
    # Out of order inside one file: that is the customer's file and stays as
    # written, only several files get merged.
    backwards = """<routes>
  <route id="rc" edges="EB_1 EB_2 EB_3"/>
  <flow id="late" route="rc" begin="300" end="600" vehsPerHour="600"/>
  <flow id="early" route="rc" begin="0" end="300" vehsPerHour="600"/>
</routes>"""
    _, out = _generate(tmp_path, backwards)
    assert _ids(out) == ["rc", "late", "early"]


@requires_sumolib
def test_interleaved_route_files_are_merged_by_departure_time(tmp_path):
    source, out = _generate(tmp_path, CARS, BIKES)
    # Definitions first, in file order; then every flow of both files by begin
    # time, ties keeping file order. Nothing lost, nothing duplicated.
    assert _ids(out) == ["car", "rc", "bike", "rb", "car_a", "bike_a", "car_b", "bike_b"]
    assert source.n_flows == 4
    text = out.read_text(encoding="utf-8")
    assert "<!--" not in text  # comments are not demand
    lines = text.splitlines()
    assert lines[1] == "<routes>" and lines[-1] == "</routes>"
    assert all(line.startswith("  <") for line in lines[2:-1])  # one tidy element per line


@requires_sumolib
def test_clock_time_and_symbolic_departures_are_ordered_sensibly(tmp_path):
    late = (
        '<routes><route id="r" edges="EB_1 EB_2 EB_3"/>'
        '<vehicle id="a" route="r" depart="00:10:00"/>'  # H:M:S, 600 s
        '<vehicle id="d" route="r" depart="00:20:00"/>'  # 1200 s
        '<vehicle id="f" route="r" depart="0:00:25:00"/></routes>'  # D:H:M:S, 1500 s
    )
    early = (
        '<routes><vehicle id="b" route="r" depart="300"/>'
        '<vehicle id="c" route="r" depart="triggered"/>'  # no time: stays behind "b"
        '<vehicle id="e" route="r" depart="900.5"/></routes>'
    )
    _, out = _generate(tmp_path, late, early)
    assert _ids(out) == ["r", "b", "c", "a", "e", "d", "f"]


@requires_sumolib
def test_a_flow_without_begin_starts_at_zero(tmp_path):
    later = (
        '<routes><route id="r" edges="EB_1 EB_2 EB_3"/>'
        '<flow id="late" route="r" begin="300" end="600" vehsPerHour="60"/></routes>'
    )
    unset = '<routes><flow id="unset" route="r" end="600" vehsPerHour="60"/></routes>'
    _, out = _generate(tmp_path, later, unset)
    assert _ids(out) == ["r", "unset", "late"]  # SUMO would ignore "unset" behind "late"


@requires_sumolib
def test_source_counts_every_demand_element(tmp_path):
    source, _ = _generate(tmp_path)
    assert source.n_flows == 2  # the flow and the vehicle; vType and route are definitions


@requires_sumolib
def test_route_files_without_demand_are_copied_with_a_warning(tmp_path, caplog):
    import logging

    definitions_only = """<routes>
  <vType id="car" length="4.5" maxSpeed="20"/>
  <route id="r1" edges="EB_1 EB_2 EB_3"/>
</routes>
"""
    with caplog.at_level(logging.WARNING, logger="terasim_city.flow.sources.scenario_routes"):
        source, out = _generate(tmp_path, definitions_only)

    assert source.n_flows == 0
    assert _ids(out) == ["car", "r1"]
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "no demand" in warning and "demand0.rou.xml" in warning


@requires_sumolib
def test_route_files_with_demand_do_not_warn(tmp_path, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="terasim_city.flow.sources.scenario_routes"):
        _generate(tmp_path)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@requires_sumolib
@requires_sumo_runtime
def test_every_flow_of_every_route_file_departs_when_run(tmp_path):
    scenario = _run_pipeline(tmp_path, CARS, BIKES, sim_end=900)
    trips = tmp_path / "trips.xml"
    done = subprocess.run(
        [
            "sumo",
            "-c",
            str(scenario.sumocfg_file),
            "--tripinfo-output",
            str(trips),
            "--no-step-log",
            "true",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "sorted by departure time" not in done.stdout + done.stderr
    tripinfos = ET.parse(trips).getroot().iter("tripinfo")
    departed = {trip.get("id").rsplit(".", 1)[0] for trip in tripinfos}
    assert departed == {"car_a", "car_b", "bike_a", "bike_b"}
