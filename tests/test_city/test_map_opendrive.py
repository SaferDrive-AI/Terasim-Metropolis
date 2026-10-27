"""OpenDRIVE (.xodr) provider: geo-reference handling, conversion, signal fallback.

Inputs are the hand-built fixtures of :mod:`tests.test_city.hd_map_fixtures`
(no customer or vendor map). Test names avoid the substrings ``sumo`` and
``simulation`` so the repo-root conftest does not auto-mark them; the tests
that shell out to ``netconvert`` check for the binary themselves.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from terasim_city.maps import GeoReferenceError, MapPackage, OpenDriveMapProvider
from terasim_city.maps import opendrive as opendrive_module
from terasim_city.maps.opendrive import read_geo_reference, write_georeferenced_copy
from terasim_city.scenario.sumocfg_io import parse_sumocfg

from tests.test_city.hd_map_fixtures import UTM10, tiny_opendrive

requires_netconvert = pytest.mark.skipif(
    shutil.which("netconvert") is None, reason="netconvert not on PATH"
)

LOCAL_TMERC = (
    "+proj=tmerc +lat_0=37.30 +lon_0=-121.90 +k=1 +x_0=0 +y_0=0 "
    "+datum=WGS84 +units=m +no_defs"
)


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Header handling (no netconvert)
# ---------------------------------------------------------------------------

def test_reads_the_geo_reference_from_the_header(tmp_path):
    assert read_geo_reference(_write(tmp_path, "a.xodr", tiny_opendrive())) == UTM10
    missing = _write(tmp_path, "b.xodr", tiny_opendrive(geo_reference=None))
    assert read_geo_reference(missing) is None


def test_georeferenced_copy_replaces_or_inserts_the_header_entry(tmp_path):
    with_ref = _write(tmp_path, "a.xodr", tiny_opendrive())
    copy = write_georeferenced_copy(with_ref, tmp_path / "a2.xodr", LOCAL_TMERC)
    assert read_geo_reference(copy) == LOCAL_TMERC
    assert copy.read_text(encoding="utf-8").count("<geoReference>") == 1

    # A self-closing header (seen in vendor files) is expanded.
    self_closing = _write(
        tmp_path, "b.xodr",
        tiny_opendrive(geo_reference=None).replace(
            '<header revMajor="1" revMinor="6" name="tiny" version="1.0"></header>',
            '<header revMajor="1" revMinor="6" name="tiny" version="1.0"/>',
        ),
    )
    assert "<header revMajor=\"1\" revMinor=\"6\" name=\"tiny\" version=\"1.0\"/>" in (
        self_closing.read_text(encoding="utf-8")
    )
    copy = write_georeferenced_copy(self_closing, tmp_path / "b2.xodr", UTM10)
    assert read_geo_reference(copy) == UTM10
    # The rest of the document is untouched.
    assert ET.parse(str(copy)).getroot().findall("road")


@pytest.mark.parametrize("bad", ["", "utm zone 10", "+proj=utm ]]> <x/>", "+proj=nonsense"])
def test_invalid_projection_override_is_rejected_up_front(tmp_path, bad):
    xodr = _write(tmp_path, "a.xodr", tiny_opendrive(geo_reference=None))
    with pytest.raises(ValueError, match="proj"):
        OpenDriveMapProvider(xodr, proj=bad)


def test_missing_geo_reference_fails_loud_before_converting(tmp_path):
    xodr = _write(tmp_path, "nogeo.xodr", tiny_opendrive(geo_reference=None))
    out = tmp_path / "out"
    with pytest.raises(GeoReferenceError, match="no <geoReference>") as exc:
        OpenDriveMapProvider(xodr).build(out)
    assert "proj" in str(exc.value)  # tells the caller how to fix it
    assert not (out / "net.net.xml").exists()


def test_non_proj4_geo_reference_fails_loud(tmp_path):
    xodr = _write(tmp_path, "wkt.xodr", tiny_opendrive(geo_reference='PROJCS["x"]'))
    with pytest.raises(GeoReferenceError, match="not a proj.4"):
        OpenDriveMapProvider(xodr).build(tmp_path / "out")


# ---------------------------------------------------------------------------
# Conversion (netconvert)
# ---------------------------------------------------------------------------

@requires_netconvert
def test_converts_a_geo_referenced_junction_with_its_signal(tmp_path):
    xodr = _write(tmp_path, "tiny.xodr", tiny_opendrive())
    out = tmp_path / "map"
    pkg = OpenDriveMapProvider(xodr).build(out)

    assert pkg.source == "opendrive"
    assert pkg.geo_referenced is True
    # netconvert writes origBoundary="0,0,0,0" for OpenDRIVE; the provider
    # recomputes it, so the map store can find the map by coordinates.
    lon_min, lat_min, lon_max, lat_max = pkg.metadata["orig_boundary"]
    assert -121.91 < lon_min < lon_max < -121.90
    assert 37.31 < lat_min < lat_max < 37.32
    assert pkg.metadata["has_tls"] is True

    summary = pkg.metadata["conversion"]
    assert summary["format"] == "opendrive"
    assert (summary["edges"], summary["intersections"], summary["traffic_lights"]) == (6, 1, 1)
    assert summary["tls_source"] == "opendrive_signals"
    assert summary["largest_component_share"] == 1.0
    assert summary["geo_referenced"] is True and "+proj=utm" in summary["proj"]
    assert summary["warnings"] == []

    info = parse_sumocfg(pkg.base_sumocfg)
    assert info.net_file == pkg.net_file.resolve()
    assert MapPackage.load(out).metadata["conversion"] == summary


@requires_netconvert
def test_projection_override_places_a_local_frame_file(tmp_path):
    xodr = _write(tmp_path, "local.xodr", tiny_opendrive(geo_reference=None, origin=(0.0, 0.0)))
    pkg = OpenDriveMapProvider(xodr, proj=LOCAL_TMERC).build(tmp_path / "map")
    assert pkg.geo_referenced is True
    lon_min, lat_min, lon_max, lat_max = pkg.metadata["orig_boundary"]
    # The junction centre (0, 0) is the projection origin (37.30, -121.90).
    assert lon_min < -121.90 < lon_max and lat_min < 37.30 <= lat_max + 1e-6
    assert pkg.metadata["proj_override"] == LOCAL_TMERC
    assert pkg.metadata["conversion"]["proj"] == LOCAL_TMERC
    # The submitted file is never modified (the override goes into a copy).
    assert read_geo_reference(xodr) is None


@requires_netconvert
def test_utm_declared_over_local_coordinates_is_rejected(tmp_path):
    """The vendor pitfall: bare UTM geoReference, but x/y are local metres."""
    xodr = _write(tmp_path, "fake_utm.xodr", tiny_opendrive(origin=(0.0, 0.0)))
    with pytest.raises(GeoReferenceError, match="not UTM eastings") as exc:
        OpenDriveMapProvider(xodr).build(tmp_path / "map")
    assert "proj" in str(exc.value)


@requires_netconvert
def test_without_importable_signals_the_junctions_are_guessed(tmp_path, monkeypatch):
    # netconvert's default guess threshold never signalizes a single-lane
    # 3-arm junction; lower it so the fallback path itself is exercised.
    monkeypatch.setattr(
        opendrive_module, "TLS_GUESS_FLAGS", ("--tls.guess", "--tls.guess.threshold", "0")
    )
    xodr = _write(tmp_path, "nosignals.xodr", tiny_opendrive(signals=False))
    pkg = OpenDriveMapProvider(xodr).build(tmp_path / "map")
    summary = pkg.metadata["conversion"]
    assert summary["tls_source"] == "guessed"
    assert summary["traffic_lights"] == 1
    assert any("--tls.guess" in w for w in summary["warnings"])
    assert "--tls.guess" in pkg.metadata["netconvert_flags"]


@requires_netconvert
def test_without_any_signal_the_map_is_stored_with_a_warning(tmp_path):
    xodr = _write(tmp_path, "nosignals.xodr", tiny_opendrive(signals=False))
    pkg = OpenDriveMapProvider(xodr).build(tmp_path / "map")
    summary = pkg.metadata["conversion"]
    assert summary["tls_source"] == "none"
    assert summary["traffic_lights"] == 0
    assert pkg.metadata["has_tls"] is False
    assert any("no traffic light" in w for w in summary["warnings"])
