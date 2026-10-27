"""Tests for the filesystem map store (maps/store.py).

Test names deliberately avoid the substrings ``sumo`` and ``simulation`` so
the repo-root conftest does not auto-mark them ``requires_sumo``; the tests
that shell out to ``netconvert`` check for the binary themselves.

Most tests use prebuilt (sumocfg) submissions because those need no external
binary; the OSM conversion path is covered once under ``requires_netconvert``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from terasim_city.maps import (
    DATA_ROOT_ENV,
    MANIFEST_NAME,
    GeoReferenceError,
    MapFormatError,
    MapStore,
    detect_map_format,
)
from terasim_city.maps import store as store_module
from terasim_city.maps.validate import file_sha256
from tests.test_city.hd_map_fixtures import UTM10, tiny_lanelet2, tiny_opendrive

LOCAL_TMERC = (
    "+proj=tmerc +lat_0=37.30 +lon_0=-121.90 +k=1 +x_0=0 +y_0=0 "
    "+datum=WGS84 +units=m +no_defs"
)

requires_netconvert = pytest.mark.skipif(
    shutil.which("netconvert") is None, reason="netconvert not on PATH"
)

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
  </way>
  <way id="11" version="1">
    <nd ref="3"/>
    <nd ref="5"/>
    <nd ref="4"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
"""

NET_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<net version="1.20" junctionCornerDetail="5" limitTurnSpeed="5.50">
  <location netOffset="0.00,0.00" convBoundary="0.00,0.00,100.00,100.00"
            origBoundary="{orig_boundary}"
            projParameter="+proj=utm +zone=10 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"/>
</net>
"""


def _write_scenario(
    root: Path,
    *,
    orig_boundary: str = "-121.91,37.33,-121.89,37.34",
    marker: str = "",
) -> Path:
    """A minimal TLS-less scenario; ``marker`` varies content (and so the hash)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "plain.net.xml").write_text(
        NET_TEMPLATE.format(orig_boundary=orig_boundary), encoding="utf-8"
    )
    (root / "plain.rou.xml").write_text("<routes/>\n", encoding="utf-8")
    sumocfg = root / "plain.sumocfg"
    sumocfg.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <!-- {marker} -->
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
# Format detection
# ---------------------------------------------------------------------------

def test_detect_format_by_suffix_and_content(tmp_path):
    osm = tmp_path / "a.osm"
    osm.write_text("<osm version='0.6'/>", encoding="utf-8")
    assert detect_map_format(osm) == "osm"

    cfg = _write_scenario(tmp_path / "scen")
    assert detect_map_format(cfg) == "sumocfg"

    # OpenDRIVE: the .xodr suffix and the <OpenDRIVE> root.
    xodr = tmp_path / "a.xodr"
    xodr.write_text("<OpenDRIVE/>", encoding="utf-8")
    assert detect_map_format(xodr) == "opendrive"


def test_detect_format_tells_hd_maps_apart(tmp_path):
    xodr = tmp_path / "corridor.xodr"
    xodr.write_text(tiny_opendrive(), encoding="utf-8")
    assert detect_map_format(xodr) == "opendrive"

    lanelet2 = tmp_path / "lanelet2_map.osm"
    lanelet2.write_text(tiny_lanelet2(), encoding="utf-8")
    assert detect_map_format(lanelet2) == "lanelet2"  # .osm suffix, lanelet content

    plain = tmp_path / "plain.osm"
    plain.write_text(TINY_OSM, encoding="utf-8")
    assert detect_map_format(plain) == "osm"

    sniffed = tmp_path / "download.xml"
    sniffed.write_text(tiny_lanelet2(), encoding="utf-8")
    assert detect_map_format(sniffed) == "lanelet2"

    namespaced = tmp_path / "v18.xodr"
    namespaced.write_text('<OpenDRIVE xmlns="http://code.asam.net/opendrive"/>', encoding="utf-8")
    assert detect_map_format(namespaced) == "opendrive"

    liar = tmp_path / "liar.xodr"
    liar.write_text(TINY_OSM, encoding="utf-8")
    with pytest.raises(MapFormatError, match="ambiguous"):
        detect_map_format(liar)


def test_plain_osm_is_not_mistaken_for_lanelet2(tmp_path):
    """Only a type=lanelet relation makes a Lanelet2 map — not the word itself."""
    osm = tmp_path / "noted.osm"
    osm.write_text(
        TINY_OSM.replace(
            '<node id="2" lat="37.3350" lon="-121.8970" version="1"/>',
            '<node id="2" lat="37.3350" lon="-121.8970" version="1">'
            '<tag k="note" v="lanelet"/></node>',
        ).replace(
            "</osm>",
            '  <relation id="30" version="1"><member type="way" ref="10" role=""/>'
            '<tag k="type" v="route"/><tag k="name" v="lanelet"/></relation>\n</osm>',
        ),
        encoding="utf-8",
    )
    assert "lanelet" in osm.read_text(encoding="utf-8")
    assert detect_map_format(osm) == "osm"


def test_detect_format_content_sniff_with_unknown_suffix(tmp_path):
    weird = tmp_path / "download.xml"
    weird.write_text("<osm version='0.6'/>", encoding="utf-8")
    assert detect_map_format(weird) == "osm"


def test_detect_format_rejects_suffix_content_mismatch(tmp_path):
    liar = tmp_path / "map.osm"
    liar.write_text("<configuration/>", encoding="utf-8")
    with pytest.raises(MapFormatError, match="ambiguous"):
        detect_map_format(liar)


def test_detect_format_rejects_non_xml_and_unknown_root(tmp_path):
    garbage = tmp_path / "map.osm"
    garbage.write_text("not xml at all", encoding="utf-8")
    with pytest.raises(MapFormatError, match="not parseable XML"):
        detect_map_format(garbage)

    unknown = tmp_path / "net.xml"
    unknown.write_text("<net/>", encoding="utf-8")
    with pytest.raises(MapFormatError, match="unrecognized XML root"):
        detect_map_format(unknown)


# ---------------------------------------------------------------------------
# Store construction / data root resolution
# ---------------------------------------------------------------------------

def test_store_requires_a_data_root(monkeypatch):
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    with pytest.raises(ValueError, match=DATA_ROOT_ENV):
        MapStore()


def test_store_reads_data_root_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path / "store"))
    store = MapStore()
    assert store.maps_root == tmp_path / "store" / "maps"


# ---------------------------------------------------------------------------
# Ingest: prebuilt path (no external binary needed)
# ---------------------------------------------------------------------------

def test_ingest_prebuilt_scenario(tmp_path):
    sumocfg = _write_scenario(tmp_path / "scen")
    store = MapStore(tmp_path / "store")

    result = store.ingest(sumocfg)
    assert result.duplicate is False
    assert len(result.map_id) == 12
    assert (store.maps_root / result.map_id / MANIFEST_NAME).exists()
    # Prebuilt scenarios stay in place; the store holds the manifest.
    assert result.package.root == sumocfg.parent
    assert result.package.metadata["has_tls"] is False


def test_ingest_is_idempotent_on_identical_content(tmp_path):
    sumocfg = _write_scenario(tmp_path / "scen")
    store = MapStore(tmp_path / "store")

    first = store.ingest(sumocfg)
    second = store.ingest(sumocfg)
    assert second.duplicate is True
    assert second.map_id == first.map_id
    assert store.list_ids() == [first.map_id]


def test_ingest_rejects_missing_file(tmp_path):
    store = MapStore(tmp_path / "store")
    with pytest.raises(ValueError, match="does not exist"):
        store.ingest(tmp_path / "nope.osm")


def test_failed_ingest_leaves_no_partial_directory(tmp_path, monkeypatch):
    sumocfg = _write_scenario(tmp_path / "scen")
    store = MapStore(tmp_path / "store")

    def boom(self, out_dir):
        (Path(out_dir) / "partial.txt").parent.mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "partial.txt").write_text("junk", encoding="utf-8")
        raise RuntimeError("conversion exploded")

    monkeypatch.setattr(store_module.PrebuiltSumoProvider, "build", boom)
    with pytest.raises(RuntimeError, match="exploded"):
        store.ingest(sumocfg)
    assert store.list_ids() == []
    # No leftovers at all — neither a package dir nor a staging dir.
    if store.maps_root.exists():
        assert list(store.maps_root.iterdir()) == []


def test_ingest_accepts_sumoconfiguration_root(tmp_path):
    """SUMO's own tools write <sumoConfiguration> as the root element."""
    scen = tmp_path / "scen"
    scen.mkdir()
    (scen / "plain.net.xml").write_text(
        NET_TEMPLATE.format(orig_boundary="-121.91,37.33,-121.89,37.34"),
        encoding="utf-8",
    )
    (scen / "plain.rou.xml").write_text("<routes/>\n", encoding="utf-8")
    cfg = scen / "saved.sumocfg"
    cfg.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<sumoConfiguration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <input>
    <net-file value="plain.net.xml"/>
    <route-files value="plain.rou.xml"/>
  </input>
</sumoConfiguration>
""",
        encoding="utf-8",
    )
    assert detect_map_format(cfg) == "sumocfg"
    result = MapStore(tmp_path / "store").ingest(cfg)
    assert result.duplicate is False
    assert result.package.source == "prebuilt"


def test_identical_sumocfg_bytes_different_nets_are_different_maps(tmp_path):
    """The sumocfg is a pointer file: dedup must key on the referenced map."""
    a = _write_scenario(tmp_path / "a", orig_boundary="-121.91,37.33,-121.89,37.34")
    b = _write_scenario(tmp_path / "b", orig_boundary="-121.80,37.40,-121.78,37.41")
    assert a.read_bytes() == b.read_bytes()  # byte-identical configs

    store = MapStore(tmp_path / "store")
    ra = store.ingest(a)
    rb = store.ingest(b)
    assert ra.map_id != rb.map_id
    assert rb.duplicate is False
    boundaries = {
        tuple(pkg.metadata["orig_boundary"]) for _, pkg in store.list_packages()
    }
    assert len(boundaries) == 2


def test_one_broken_package_does_not_poison_list_or_find(tmp_path):
    """User deletes one prebuilt scenario dir → other maps stay reachable."""
    a = _write_scenario(tmp_path / "a", orig_boundary="-121.91,37.33,-121.89,37.34")
    b = _write_scenario(tmp_path / "b", orig_boundary="-121.80,37.40,-121.78,37.41")
    store = MapStore(tmp_path / "store")
    a_id = store.ingest(a).map_id
    b_id = store.ingest(b).map_id

    shutil.rmtree(tmp_path / "a")  # user moves/deletes scenario A
    loadable = [mid for mid, _ in store.list_packages()]
    assert loadable == [b_id]
    hits = store.find_covering(lat=37.405, lon=-121.79)
    assert [mid for mid, _ in hits] == [b_id]
    # The broken map itself still fails loud on direct access.
    with pytest.raises(ValueError, match="does not exist"):
        store.get(a_id)


# ---------------------------------------------------------------------------
# Ingest: OSM conversion path
# ---------------------------------------------------------------------------

@requires_netconvert
def test_ingest_osm_converts_and_archives_source(tmp_path):
    osm = tmp_path / "tiny.osm"
    osm.write_text(TINY_OSM, encoding="utf-8")
    store = MapStore(tmp_path / "store")

    result = store.ingest(osm)
    pkg_dir = store.maps_root / result.map_id
    assert result.duplicate is False
    assert result.package.source == "osm"
    assert result.package.geo_referenced is True
    assert (pkg_dir / "source.osm").exists()
    assert (pkg_dir / MANIFEST_NAME).exists()
    assert result.package.metadata["has_tls"] is True
    boundary = result.package.metadata["orig_boundary"]
    assert boundary is not None and len(boundary) == 4

    again = store.ingest(osm)
    assert again.duplicate is True


# ---------------------------------------------------------------------------
# Ingest: HD maps (OpenDRIVE / Lanelet2) and the projection override
# ---------------------------------------------------------------------------

def test_projection_override_is_refused_for_maps_with_their_own_reference(tmp_path):
    store = MapStore(tmp_path / "store")
    sumocfg = _write_scenario(tmp_path / "scen")
    with pytest.raises(ValueError, match="projection override"):
        store.ingest(sumocfg, proj=UTM10)
    osm = tmp_path / "tiny.osm"
    osm.write_text(TINY_OSM, encoding="utf-8")
    with pytest.raises(ValueError, match="projection override"):
        store.ingest(osm, proj=UTM10)
    assert store.list_ids() == []


def test_invalid_projection_override_is_rejected_before_converting(tmp_path):
    xodr = tmp_path / "nogeo.xodr"
    xodr.write_text(tiny_opendrive(geo_reference=None), encoding="utf-8")
    store = MapStore(tmp_path / "store")
    with pytest.raises(ValueError, match="proj"):
        store.ingest(xodr, proj="+proj=not-a-projection")
    assert store.list_ids() == []


@requires_netconvert
def test_ingest_opendrive_converts_archives_and_summarizes(tmp_path):
    xodr = tmp_path / "tiny.xodr"
    xodr.write_text(tiny_opendrive(), encoding="utf-8")
    store = MapStore(tmp_path / "store")

    result = store.ingest(xodr)
    pkg_dir = store.maps_root / result.map_id
    assert result.duplicate is False
    assert result.package.source == "opendrive"
    assert (pkg_dir / "source.xodr").read_bytes() == xodr.read_bytes()
    assert result.map_id == file_sha256(xodr)[:12]
    summary = result.summary
    assert (summary["edges"], summary["traffic_lights"], summary["geo_referenced"]) == (6, 1, True)

    again = store.ingest(xodr)
    assert again.duplicate is True and again.map_id == result.map_id
    assert again.summary == summary  # read back from the manifest
    lon_min, lat_min, lon_max, lat_max = result.package.metadata["orig_boundary"]
    hits = store.find_covering(lat=(lat_min + lat_max) / 2, lon=(lon_min + lon_max) / 2)
    assert [map_id for map_id, _ in hits] == [result.map_id]


@requires_netconvert
def test_ingest_opendrive_without_geo_reference_needs_a_projection(tmp_path):
    xodr = tmp_path / "local.xodr"
    xodr.write_text(tiny_opendrive(geo_reference=None, origin=(0.0, 0.0)), encoding="utf-8")
    store = MapStore(tmp_path / "store")

    with pytest.raises(GeoReferenceError, match="geoReference"):
        store.ingest(xodr)
    assert store.list_ids() == []
    assert not store.maps_root.exists() or list(store.maps_root.iterdir()) == []

    first = store.ingest(xodr, proj=LOCAL_TMERC)
    assert first.package.metadata["proj_override"] == LOCAL_TMERC
    assert store.find_covering(lat=37.2998, lon=-121.9) != []
    # Another projection is another conversion, hence another map.
    other = store.ingest(xodr, proj=LOCAL_TMERC.replace("37.30", "37.40"))
    assert other.duplicate is False and other.map_id != first.map_id
    assert store.ingest(xodr, proj=LOCAL_TMERC).duplicate is True


@requires_netconvert
def test_ingest_lanelet2_converts_and_archives(tmp_path):
    osm = tmp_path / "lanelet2_map.osm"
    osm.write_text(tiny_lanelet2(), encoding="utf-8")
    store = MapStore(tmp_path / "store")

    result = store.ingest(osm)
    pkg_dir = store.maps_root / result.map_id
    assert result.package.source == "lanelet2"
    assert (pkg_dir / "source.osm").read_bytes() == osm.read_bytes()
    assert (pkg_dir / "lanelet2_mapping.json").exists()
    # The id is format-qualified: never the id an OSM-only store gave the file.
    assert result.map_id != file_sha256(osm)[:12]
    assert result.summary["traffic_lights"] == 1
    assert result.summary["lanelet2"]["driving_side"] == "right"
    assert store.ingest(osm).duplicate is True


# ---------------------------------------------------------------------------
# Read path: get / list / restart persistence / find_covering
# ---------------------------------------------------------------------------

def test_get_unknown_id_raises_with_known_ids(tmp_path):
    store = MapStore(tmp_path / "store")
    with pytest.raises(KeyError, match="store is empty"):
        store.get("deadbeef0000")

    sumocfg = _write_scenario(tmp_path / "scen")
    known = store.ingest(sumocfg).map_id
    with pytest.raises(KeyError, match=known):
        store.get("deadbeef0000")


def test_store_survives_restart(tmp_path):
    sumocfg = _write_scenario(tmp_path / "scen")
    map_id = MapStore(tmp_path / "store").ingest(sumocfg).map_id

    # A fresh instance over the same directory sees the stored map.
    reopened = MapStore(tmp_path / "store")
    assert reopened.list_ids() == [map_id]
    assert reopened.get(map_id).base_sumocfg == sumocfg.resolve()


def test_find_covering_matches_bbox_and_prefers_hd_sources(tmp_path):
    # Two disjoint areas; the query point lands in the first one only.
    west = _write_scenario(
        tmp_path / "west", orig_boundary="-121.91,37.33,-121.89,37.34", marker="west"
    )
    east = _write_scenario(
        tmp_path / "east", orig_boundary="-121.80,37.40,-121.78,37.41", marker="east"
    )
    store = MapStore(tmp_path / "store")
    west_id = store.ingest(west).map_id
    store.ingest(east)

    hits = store.find_covering(lat=37.335, lon=-121.90)
    assert [map_id for map_id, _ in hits] == [west_id]
    assert store.find_covering(lat=0.0, lon=0.0) == []

    # Source preference: with a (hand-written) osm-source package covering the
    # same point, the richer prebuilt source must sort before osm.
    from terasim_city.maps import MapPackage

    osm_dir = store.maps_root / "aaaosmmap0001"
    osm_scen = _write_scenario(tmp_path / "osm", marker="osm")
    MapPackage(
        root=osm_scen.parent,
        net_file=osm_scen.parent / "plain.net.xml",
        base_sumocfg=osm_scen,
        tls_files=(),
        geo_referenced=True,
        source="osm",
        metadata={"orig_boundary": [-121.91, 37.33, -121.89, 37.34]},
    ).save(osm_dir / MANIFEST_NAME)

    hits = store.find_covering(lat=37.335, lon=-121.90)
    assert [pkg.source for _, pkg in hits] == ["prebuilt", "osm"]

    # HD maps (OpenDRIVE, then Lanelet2) outrank both.
    for source, map_id in (("lanelet2", "aaalanelet201"), ("opendrive", "zzzopendrive1")):
        scen = _write_scenario(tmp_path / source, marker=source)
        MapPackage(
            root=scen.parent,
            net_file=scen.parent / "plain.net.xml",
            base_sumocfg=scen,
            tls_files=(),
            geo_referenced=True,
            source=source,
            metadata={"orig_boundary": [-121.91, 37.33, -121.89, 37.34]},
        ).save(store.maps_root / map_id / MANIFEST_NAME)
    hits = store.find_covering(lat=37.335, lon=-121.90)
    assert [pkg.source for _, pkg in hits] == ["opendrive", "lanelet2", "prebuilt", "osm"]
