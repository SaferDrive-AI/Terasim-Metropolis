"""Deterministic map acquisition from TMC coordinates.

The tests inject both the downloader and the one-time OSM converter, so they
exercise the real MapStore persistence/cache path without network or
``netconvert``.
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from terasim_city.flow.descriptors import TMCData
from terasim_city.maps import MANIFEST_NAME, MapPackage, MapStore
from terasim_city.maps import store as map_store_module
from terasim_service.city.map_acquisition import (
    MapAcquirer,
    OsmBoundingBox,
    OverpassOsmDownloader,
    acquire_map_for_tmc,
)


def _tmc(*points: tuple[str, float, float]) -> TMCData:
    return TMCData.from_payload(
        {
            "corridor_id": "c1",
            "timestamp_start": "2026-07-22T12:00:00Z",
            "observation_interval": "10m",
            "intersections": [
                {
                    "intersection_id": intersection_id,
                    "lat": lat,
                    "lon": lon,
                    "counts": {},
                }
                for intersection_id, lat, lon in points
            ],
        }
    )


def _write_prebuilt(root: Path, *, boundary: str) -> Path:
    root.mkdir(parents=True)
    (root / "plain.net.xml").write_text(
        "<net><location "
        f'origBoundary="{boundary}" '
        'projParameter="+proj=utm +zone=10 +units=m"/>'
        "</net>",
        encoding="utf-8",
    )
    (root / "empty.rou.xml").write_text("<routes/>\n", encoding="utf-8")
    config = root / "plain.sumocfg"
    config.write_text(
        "<configuration><input>"
        '<net-file value="plain.net.xml"/>'
        '<route-files value="empty.rou.xml"/>'
        "</input></configuration>\n",
        encoding="utf-8",
    )
    return config


class RecordingDownloader:
    def __init__(self) -> None:
        self.calls: list[OsmBoundingBox] = []

    def download(self, bbox: OsmBoundingBox, destination: Path) -> None:
        self.calls.append(bbox)
        destination.write_text(
            "<osm version='0.6'>"
            f"<bounds minlat='{bbox.south}' minlon='{bbox.west}' "
            f"maxlat='{bbox.north}' maxlon='{bbox.east}'/>"
            "</osm>",
            encoding="utf-8",
        )


def _install_local_osm_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace only the external converter; MapStore itself remains real."""

    class LocalOsmBuilder:
        def __init__(self, osm_file: Path | str) -> None:
            self.osm_file = Path(osm_file)

        def build(self, out_dir: Path) -> MapPackage:
            out_dir = Path(out_dir)
            bounds = ET.parse(self.osm_file).getroot().find("bounds")
            assert bounds is not None
            west = float(bounds.attrib["minlon"])
            south = float(bounds.attrib["minlat"])
            east = float(bounds.attrib["maxlon"])
            north = float(bounds.attrib["maxlat"])
            boundary = [west, south, east, north]

            net = out_dir / "net.net.xml"
            net.write_text(
                "<net><location "
                f'origBoundary="{west},{south},{east},{north}" '
                'projParameter="+proj=utm +zone=10 +units=m"/>'
                "</net>",
                encoding="utf-8",
            )
            routes = out_dir / "empty.rou.xml"
            routes.write_text("<routes/>\n", encoding="utf-8")
            config = out_dir / "base.sumocfg"
            config.write_text(
                "<configuration><input>"
                '<net-file value="net.net.xml"/>'
                '<route-files value="empty.rou.xml"/>'
                "</input></configuration>\n",
                encoding="utf-8",
            )
            package = MapPackage(
                root=out_dir,
                net_file=net,
                base_sumocfg=config,
                tls_files=(),
                geo_referenced=True,
                source="osm",
                metadata={"orig_boundary": boundary},
            )
            package.save()
            return package

    monkeypatch.setattr(map_store_module, "OsmMapProvider", LocalOsmBuilder)


def test_existing_prebuilt_map_wins_without_download(tmp_path: Path) -> None:
    store = MapStore(tmp_path / "store")
    prebuilt = store.ingest(
        _write_prebuilt(
            tmp_path / "prebuilt",
            boundary="-121.92,37.32,-121.88,37.36",
        )
    )
    # A lexicographically earlier OSM package covers the same point; source
    # quality, not id order, must pick the prebuilt asset.
    MapPackage(
        root=prebuilt.package.root,
        net_file=prebuilt.package.net_file,
        base_sumocfg=prebuilt.package.base_sumocfg,
        tls_files=(),
        geo_referenced=True,
        source="osm",
        metadata={"orig_boundary": [-121.92, 37.32, -121.88, 37.36]},
    ).save(store.maps_root / "aaa-osm-cache" / MANIFEST_NAME)
    downloader = RecordingDownloader()

    map_id, package = acquire_map_for_tmc(
        _tmc(("i1", 37.34, -121.90)),
        store,
        downloader,
    )

    assert map_id == prebuilt.map_id
    assert package.source == "prebuilt"
    assert downloader.calls == []


def test_missing_map_downloads_margin_bbox_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_local_osm_builder(monkeypatch)
    store = MapStore(tmp_path / "store")
    downloader = RecordingDownloader()
    tmc = _tmc(
        ("southwest", 37.330, -121.910),
        ("northeast", 37.350, -121.890),
    )

    map_id, package = acquire_map_for_tmc(
        tmc,
        store,
        downloader,
        margin_m=100.0,
    )

    assert len(downloader.calls) == 1
    bbox = downloader.calls[0]
    assert bbox.south < 37.330
    assert bbox.west < -121.910
    assert bbox.north > 37.350
    assert bbox.east > -121.890
    assert package.source == "osm"
    assert store.list_ids() == [map_id]
    assert (store.maps_root / map_id / MANIFEST_NAME).is_file()
    assert (store.maps_root / map_id / "source.osm").is_file()


def test_downloaded_map_is_reused_without_second_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_local_osm_builder(monkeypatch)
    store = MapStore(tmp_path / "store")
    downloader = RecordingDownloader()
    tmc = _tmc(("i1", 37.340, -121.900))

    first_id, _ = acquire_map_for_tmc(tmc, store, downloader)
    second_id, _ = acquire_map_for_tmc(tmc, store, downloader)

    assert second_id == first_id
    assert len(downloader.calls) == 1


def test_map_acquirer_callable_returns_cached_base_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_local_osm_builder(monkeypatch)
    store = MapStore(tmp_path / "store")
    downloader = RecordingDownloader()
    tmc_path = tmp_path / "tmc.json"
    tmc_path.write_text(
        json.dumps(_tmc(("i1", 37.340, -121.900)).raw),
        encoding="utf-8",
    )
    map_acquirer = MapAcquirer(store, downloader)

    base_config = map_acquirer(tmc_path)

    assert base_config.is_file()
    assert base_config == store.get(store.list_ids()[0]).base_sumocfg


def test_overpass_downloader_fetches_highways_and_signal_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    class Response:
        content = b"<osm version='0.6'></osm>"

        def raise_for_status(self) -> None:
            return None

    def post(url, *, data, timeout, headers):
        captured.update(url=url, data=data, timeout=timeout, headers=headers)
        return Response()

    monkeypatch.setattr("requests.post", post)
    destination = tmp_path / "download.osm"
    downloader = OverpassOsmDownloader(
        endpoint="https://overpass.example/api/interpreter", timeout_s=45
    )

    downloader.download(
        OsmBoundingBox(37.32, -121.94, 37.35, -121.90), destination
    )

    assert captured["url"] == "https://overpass.example/api/interpreter"
    assert captured["timeout"] == 45
    query = captured["data"]["data"]
    assert 'way["highway"](37.32,-121.94,37.35,-121.9)' in query
    assert 'node["highway"="traffic_signals"]' in query
    # The server-side execution timeout must track the configured HTTP timeout
    # (75% headroom) — otherwise raising TERASIM_CITY_OVERPASS_TIMEOUT_S would
    # not move the real bottleneck.
    assert "[timeout:33]" in query  # int(45 * 0.75)
    assert destination.read_bytes() == Response.content


def test_truncated_overpass_response_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-stream Overpass abort (HTTP 200, no closing </osm>) must not land.

    Persisting a truncated extract would poison the map cache for the whole
    region: coverage lookup would keep matching a map with missing roads.
    """

    class TruncatedResponse:
        content = b"<osm version='0.6'><way id='1'>"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("requests.post", lambda *a, **k: TruncatedResponse())
    destination = tmp_path / "download.osm"
    downloader = OverpassOsmDownloader(
        endpoint="https://overpass.example/api/interpreter", timeout_s=45
    )

    with pytest.raises(RuntimeError, match="incomplete OSM extract"):
        downloader.download(
            OsmBoundingBox(37.32, -121.94, 37.35, -121.90), destination
        )
    assert not destination.exists()


def test_map_acquirer_from_environment_uses_persistent_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "persistent-store"
    monkeypatch.setenv("TERASIM_CITY_DATA_ROOT", str(data_root))
    monkeypatch.setenv(
        "TERASIM_CITY_OVERPASS_URL", "https://overpass.example/api/interpreter"
    )

    acquirer = MapAcquirer.from_environment()

    assert acquirer.store.data_root == data_root
    assert isinstance(acquirer.downloader, OverpassOsmDownloader)
    assert acquirer.downloader.endpoint == "https://overpass.example/api/interpreter"
