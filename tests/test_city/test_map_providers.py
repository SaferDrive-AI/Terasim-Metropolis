"""Tests for the map providers (prebuilt / OSM / OpenDRIVE) and validation.

Test names deliberately avoid the substrings ``sumo`` and ``simulation`` so
the repo-root conftest does not auto-mark them ``requires_sumo``; the tests
that shell out to ``netconvert`` check for the binary themselves.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from terasim_city.maps import (
    MANIFEST_NAME,
    MapPackage,
    OsmMapProvider,
    PrebuiltSumoProvider,
    map_package_warnings,
    validate_map_package,
)
from terasim_city.scenario.sumocfg_io import parse_sumocfg

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_SUMOCFG = DEMO_DIR / "demo.sumocfg"

requires_demo_map = pytest.mark.skipif(
    not DEMO_SUMOCFG.exists(), reason=f"demo map not present at {DEMO_SUMOCFG}"
)
requires_netconvert = pytest.mark.skipif(
    shutil.which("netconvert") is None, reason="netconvert not on PATH"
)


# ---------------------------------------------------------------------------
# Fixture builders (hand-written minimal map inputs)
# ---------------------------------------------------------------------------

TINY_OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="terasim-test">
  <node id="1" lat="37.3350" lon="-121.9030" version="1"/>
  <node id="2" lat="37.3350" lon="-121.8970" version="1"/>
  <node id="3" lat="37.3377" lon="-121.9000" version="1"/>
  <node id="4" lat="37.3323" lon="-121.9000" version="1"/>
  <node id="5" lat="37.3350" lon="-121.9000" version="1">
    <tag k="highway" v="traffic_signals"/>
  </node>
  <way id="10" version="1">
    <nd ref="1"/>
    <nd ref="5"/>
    <nd ref="2"/>
    <tag k="highway" v="secondary"/>
    <tag k="name" v="Main Street"/>
  </way>
  <way id="11" version="1">
    <nd ref="3"/>
    <nd ref="5"/>
    <nd ref="4"/>
    <tag k="highway" v="residential"/>
    <tag k="name" v="Cross Street"/>
  </way>
</osm>
"""

# Geo-referenced net WITHOUT any tlLogic — for the validation test.
NET_NO_TLS = """<?xml version="1.0" encoding="UTF-8"?>
<net version="1.20" junctionCornerDetail="5" limitTurnSpeed="5.50">
  <location netOffset="0.00,0.00" convBoundary="0.00,0.00,100.00,100.00"
            origBoundary="-121.91,37.33,-121.89,37.34"
            projParameter="+proj=utm +zone=10 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"/>
</net>
"""


def _write_tls_less_map(root: Path) -> Path:
    """Write a parseable net (no TLS) + sumocfg; return the sumocfg path."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "plain.net.xml").write_text(NET_NO_TLS, encoding="utf-8")
    (root / "plain.rou.xml").write_text("<routes/>\n", encoding="utf-8")
    sumocfg = root / "plain.sumocfg"
    sumocfg.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <input>
    <net-file value="plain.net.xml"/>
    <route-files value="plain.rou.xml"/>
  </input>
</configuration>
""",
        encoding="utf-8",
    )
    return sumocfg


# ---------------------------------------------------------------------------
# Prebuilt provider
# ---------------------------------------------------------------------------


@requires_demo_map
def test_prebuilt_demo_manifest_round_trip(tmp_path):
    out_dir = tmp_path / "out"
    pkg = PrebuiltSumoProvider(DEMO_SUMOCFG).build(out_dir)
    assert (out_dir / MANIFEST_NAME).exists()

    loaded = MapPackage.load(out_dir)
    assert loaded.root == pkg.root.resolve()
    assert loaded.net_file == pkg.net_file.resolve()
    assert loaded.base_sumocfg == DEMO_SUMOCFG.resolve()
    assert loaded.tls_files == tuple(p.resolve() for p in pkg.tls_files)
    assert loaded.geo_referenced is True
    assert loaded.source == "prebuilt"


def _write_prebuilt_map(root: Path) -> Path:
    """A self-contained prebuilt map directory, shaped like a scenario export.

    Copies of the demo net, its signal programs and its routes, plus a detector
    additional file (no programs) and a GUI view file referenced from
    ``<gui_only>``. Returns the sumocfg path.
    """
    root.mkdir(parents=True, exist_ok=True)
    for name in ("demo.net.xml", "demo.tls.xml", "demo.rou.xml"):
        shutil.copyfile(DEMO_DIR / name, root / name)
    (root / "detectors.add.xml").write_text(
        '<additional><inductionLoop id="det_EB_2" lane="EB_2_0" pos="100" '
        'period="60" file="NUL"/></additional>\n',
        encoding="utf-8",
    )
    (root / "corridor.view.xml").write_text(
        '<viewsettings><scheme name="real world"/><delay value="20"/></viewsettings>\n',
        encoding="utf-8",
    )
    sumocfg = root / "corridor.sumocfg"
    sumocfg.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <input>
    <net-file value="demo.net.xml"/>
    <route-files value="demo.rou.xml"/>
    <additional-files value="demo.tls.xml,detectors.add.xml"/>
  </input>
  <time>
    <step-length value="0.1"/>
  </time>
  <gui_only>
    <gui-settings-file value="corridor.view.xml"/>
  </gui_only>
</configuration>
""",
        encoding="utf-8",
    )
    return sumocfg


@requires_demo_map
def test_prebuilt_map_package_from_a_scenario_directory(tmp_path):
    map_dir = tmp_path / "prebuilt_map"
    sumocfg = _write_prebuilt_map(map_dir)
    pkg = PrebuiltSumoProvider(sumocfg).build(tmp_path / "out")
    assert pkg.geo_referenced is True
    assert pkg.root == map_dir.resolve()
    assert [p.name for p in pkg.tls_files] == ["demo.tls.xml"]
    # The detector file is an additional file but NOT a TLS file; the GUI
    # view file is not an additional file at all.
    assert sorted(pkg.metadata["additional_files"]) == ["demo.tls.xml", "detectors.add.xml"]
    assert all(p.name not in {"detectors.add.xml", "corridor.view.xml"} for p in pkg.tls_files)


def test_prebuilt_rejects_missing_cfg(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        PrebuiltSumoProvider(tmp_path / "nope.sumocfg")


# ---------------------------------------------------------------------------
# OSM provider
# ---------------------------------------------------------------------------

@requires_netconvert
def test_osm_provider_builds_geo_referenced_net(tmp_path):
    osm_file = tmp_path / "tiny.osm"
    osm_file.write_text(TINY_OSM, encoding="utf-8")
    out_dir = tmp_path / "map"

    pkg = OsmMapProvider(osm_file=osm_file).build(out_dir)
    assert pkg.source == "osm"
    assert pkg.geo_referenced is True
    assert pkg.net_file.exists()

    root = ET.parse(str(pkg.net_file)).getroot()
    location = root.find("location")
    assert location is not None
    proj = location.attrib.get("projParameter", "")
    assert proj and proj != "!"
    # The traffic_signals node must survive as a TLS with a program.
    assert any(
        n.attrib.get("id") and n.attrib.get("programID")
        for n in root.findall(".//tlLogic")
    )
    # The written base sumocfg is parseable and points at the converted net.
    info = parse_sumocfg(pkg.base_sumocfg)
    assert info.net_file == pkg.net_file.resolve()

    loaded = MapPackage.load(out_dir)
    assert loaded.source == "osm"
    assert loaded.geo_referenced is True


# ---------------------------------------------------------------------------
# validate_map_package
# ---------------------------------------------------------------------------

def test_missing_tls_is_a_warning_not_fatal(tmp_path):
    sumocfg = _write_tls_less_map(tmp_path / "map")
    pkg = MapPackage(
        root=tmp_path / "map",
        net_file=tmp_path / "map" / "plain.net.xml",
        base_sumocfg=sumocfg,
        tls_files=(),
        geo_referenced=True,
        source="prebuilt",
    )
    assert validate_map_package(pkg) == []
    warnings = map_package_warnings(pkg)
    assert any("traffic light" in w for w in warnings)


def test_prebuilt_accepts_tls_less_map_with_has_tls_false(tmp_path):
    """A map without signals is stored (plans can add TLS later from a sheet)."""
    sumocfg = _write_tls_less_map(tmp_path / "map")
    pkg = PrebuiltSumoProvider(sumocfg).build(tmp_path / "out")
    assert pkg.metadata["has_tls"] is False
    # Common metadata is recorded for the store's coverage lookup.
    assert pkg.metadata["orig_boundary"] == [-121.91, 37.33, -121.89, 37.34]
    assert len(pkg.metadata["source_sha256"]) == 64
    assert pkg.metadata["source_filename"] == "plain.sumocfg"


def test_validate_flags_unparseable_net(tmp_path):
    root = tmp_path / "map"
    root.mkdir()
    (root / "broken.net.xml").write_text("<net", encoding="utf-8")
    (root / "base.sumocfg").write_text("<configuration/>", encoding="utf-8")
    pkg = MapPackage(
        root=root,
        net_file=root / "broken.net.xml",
        base_sumocfg=root / "base.sumocfg",
        tls_files=(),
        geo_referenced=False,
        source="prebuilt",
    )
    problems = validate_map_package(pkg)
    assert problems and "not parseable" in problems[0]
