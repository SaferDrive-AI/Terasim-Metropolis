"""Unit tests for :mod:`terasim_city.scenario.sumocfg_io`.

Covers:

* ``parse_sumocfg`` resolves relative paths against the sumocfg's own dir.
* ``parse_sumocfg`` fails loudly when required fields are missing.
* ``write_sumocfg`` produces a re-parseable output.
* Round-trip preserves processing/routing/report sections and step length.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from terasim_city.scenario.sumocfg_io import (
    SumocfgInfo,
    parse_sumocfg,
    write_sumocfg,
)


# ---------------------------------------------------------------------------
# Fixtures: tiny on-disk sumocfg + companion XML files
# ---------------------------------------------------------------------------

_MINIMAL_SUMOCFG = """<?xml version="1.0" encoding="UTF-8"?>
<sumoConfiguration>
    <input>
        <net-file value="osm.net.xml"/>
        <route-files value="osm.rou.xml"/>
        <additional-files value="osm.tls.xml"/>
    </input>
    <time>
        <begin value="0"/>
        <end value="3600"/>
        <step-length value="0.25"/>
    </time>
    <processing>
        <lateral-resolution value="0.8"/>
        <ignore-junction-blocker value="20"/>
    </processing>
    <routing>
        <device.rerouting.adaptation-steps value="180"/>
    </routing>
    <report>
        <no-warnings value="false"/>
    </report>
    <gui_only>
        <gui-settings-file value="osm.view.xml"/>
    </gui_only>
</sumoConfiguration>
"""


@pytest.fixture
def scenario_dir(tmp_path: Path) -> Path:
    """Create a self-contained scenario dir with a parse-able sumocfg.

    Companion files are empty placeholders — ``parse_sumocfg`` only checks
    for path resolution, not file content.
    """
    cfg = tmp_path / "osm.sumocfg"
    cfg.write_text(_MINIMAL_SUMOCFG, encoding="utf-8")
    (tmp_path / "osm.net.xml").write_text("<net/>", encoding="utf-8")
    (tmp_path / "osm.rou.xml").write_text("<routes/>", encoding="utf-8")
    (tmp_path / "osm.tls.xml").write_text("<additional/>", encoding="utf-8")
    (tmp_path / "osm.view.xml").write_text("<viewsettings/>", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# parse_sumocfg
# ---------------------------------------------------------------------------

def test_parse_resolves_relative_paths_against_cfg_dir(scenario_dir: Path):
    info = parse_sumocfg(scenario_dir / "osm.sumocfg")

    assert isinstance(info, SumocfgInfo)
    assert info.sumocfg_path == (scenario_dir / "osm.sumocfg").resolve()
    assert info.net_file == (scenario_dir / "osm.net.xml").resolve()
    assert info.route_files == ((scenario_dir / "osm.rou.xml").resolve(),)
    assert info.additional_files == ((scenario_dir / "osm.tls.xml").resolve(),)
    assert info.gui_settings_file == (scenario_dir / "osm.view.xml").resolve()


def test_parse_preserves_processing_routing_report_sections(scenario_dir: Path):
    info = parse_sumocfg(scenario_dir / "osm.sumocfg")
    assert info.processing_params == {
        "lateral-resolution": "0.8",
        "ignore-junction-blocker": "20",
    }
    assert info.routing_params == {"device.rerouting.adaptation-steps": "180"}
    assert info.report_params == {"no-warnings": "false"}


def test_parse_step_length(scenario_dir: Path):
    info = parse_sumocfg(scenario_dir / "osm.sumocfg")
    assert info.step_length == pytest.approx(0.25)


def test_parse_step_length_defaults_when_absent(tmp_path: Path):
    from terasim_city.scenario.sumocfg_io import DEFAULT_STEP_LENGTH_S

    cfg = tmp_path / "x.sumocfg"
    cfg.write_text(
        """<?xml version="1.0"?>
<sumoConfiguration>
    <input><net-file value="net.xml"/></input>
</sumoConfiguration>
""",
        encoding="utf-8",
    )
    info = parse_sumocfg(cfg)
    assert info.step_length == pytest.approx(DEFAULT_STEP_LENGTH_S) == pytest.approx(1.0)


def test_parse_raises_when_net_file_missing(tmp_path: Path):
    cfg = tmp_path / "broken.sumocfg"
    cfg.write_text(
        """<?xml version="1.0"?>
<sumoConfiguration>
    <input><route-files value="r.rou.xml"/></input>
</sumoConfiguration>
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="net-file"):
        parse_sumocfg(cfg)


def test_parse_handles_comma_separated_additional_files(tmp_path: Path):
    cfg = tmp_path / "multi.sumocfg"
    cfg.write_text(
        """<?xml version="1.0"?>
<sumoConfiguration>
    <input>
        <net-file value="net.xml"/>
        <additional-files value="a.xml, b.xml ,c.xml"/>
    </input>
</sumoConfiguration>
""",
        encoding="utf-8",
    )
    info = parse_sumocfg(cfg)
    assert [p.name for p in info.additional_files] == ["a.xml", "b.xml", "c.xml"]


def test_parse_absolute_paths_kept_as_absolute(tmp_path: Path):
    abs_net = tmp_path / "abs_net.xml"
    abs_net.write_text("<net/>", encoding="utf-8")
    cfg = tmp_path / "abs.sumocfg"
    cfg.write_text(
        f"""<?xml version="1.0"?>
<sumoConfiguration>
    <input><net-file value="{abs_net}"/></input>
</sumoConfiguration>
""",
        encoding="utf-8",
    )
    info = parse_sumocfg(cfg)
    assert info.net_file == abs_net


# ---------------------------------------------------------------------------
# write_sumocfg
# ---------------------------------------------------------------------------

def test_write_produces_valid_xml_with_absolute_paths(tmp_path: Path):
    net = tmp_path / "net.xml"
    routes = tmp_path / "routes.rou.xml"
    out = tmp_path / "out" / "simulation.sumocfg"

    returned = write_sumocfg(out, net_file=net, route_file=routes, sim_end=42.0, seed=7)

    assert returned == out.resolve()
    assert out.exists()
    root = ET.parse(out).getroot()
    assert root.tag == "sumoConfiguration"

    inp = root.find("input")
    assert inp.find("net-file").attrib["value"] == str(net)
    assert inp.find("route-files").attrib["value"] == str(routes)

    time_node = root.find("time")
    assert float(time_node.find("end").attrib["value"]) == pytest.approx(42.0)

    seed_node = root.find("random_number/seed")
    assert seed_node.attrib["value"] == "7"


def test_write_emits_additional_files_as_csv(tmp_path: Path):
    out = tmp_path / "x.sumocfg"
    additional = (tmp_path / "a.xml", tmp_path / "b.xml")
    write_sumocfg(out, net_file=tmp_path / "n.xml", route_file=tmp_path / "r.xml",
                  additional_files=additional)
    value = ET.parse(out).getroot().find("input/additional-files").attrib["value"]
    assert value == f"{additional[0]},{additional[1]}"


def test_write_skips_gui_section_when_no_settings(tmp_path: Path):
    out = tmp_path / "x.sumocfg"
    write_sumocfg(out, net_file=tmp_path / "n.xml", route_file=tmp_path / "r.xml")
    assert ET.parse(out).getroot().find("gui_only") is None


# ---------------------------------------------------------------------------
# Round-trip: parse → write → parse
# ---------------------------------------------------------------------------

def test_round_trip_preserves_tuned_sections(scenario_dir: Path, tmp_path: Path):
    info = parse_sumocfg(scenario_dir / "osm.sumocfg")
    out = tmp_path / "rt" / "simulation.sumocfg"
    write_sumocfg(
        out,
        net_file=info.net_file,
        route_file=scenario_dir / "osm.rou.xml",
        additional_files=info.additional_files,
        gui_settings_file=info.gui_settings_file,
        processing_params=info.processing_params,
        routing_params=info.routing_params,
        report_params=info.report_params,
        step_length=info.step_length,
        sim_end=600.0,
        seed=1234,
    )
    re_parsed = parse_sumocfg(out)
    assert re_parsed.net_file == info.net_file
    assert re_parsed.processing_params == info.processing_params
    assert re_parsed.routing_params == info.routing_params
    assert re_parsed.report_params == info.report_params
    assert re_parsed.step_length == pytest.approx(info.step_length)
    assert re_parsed.gui_settings_file == info.gui_settings_file
