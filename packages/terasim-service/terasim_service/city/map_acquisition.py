"""Acquire and cache a map covering one parsed TMC payload.

This module owns only the online/offline acquisition decision. Existing stored
maps keep the selection semantics from :func:`terasim_city.flow.generate.select_map_for`;
when none covers the complete TMC footprint, the production Overpass downloader
(or an injected test downloader) writes one OSM extract which is converted and
persisted through :class:`MapStore`.

``TERASIM_CITY_OSM_DOWNLOAD=0`` switches the download off (deployments whose maps
ship as files): the production acquirer then only selects stored maps, never goes
online, and an area no stored map covers raises :class:`OsmDownloadDisabled`, whose
message names the setting and the area. The API answers that with 422.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from terasim_city.flow.descriptors import TMCData
from terasim_city.flow.generate import select_map_for
from terasim_city.maps import MapPackage, MapStore

EARTH_RADIUS_M = 6_371_000.0
DEFAULT_MARGIN_M = 250.0
DEFAULT_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
DEFAULT_OVERPASS_TIMEOUT_S = 60.0
OVERPASS_ENDPOINT_ENV = "TERASIM_CITY_OVERPASS_URL"
OVERPASS_TIMEOUT_ENV = "TERASIM_CITY_OVERPASS_TIMEOUT_S"
MAP_MARGIN_ENV = "TERASIM_CITY_MAP_MARGIN_M"
#: "0" / "false" / "no" / "off": never download OpenStreetMap data (default "1").
OSM_DOWNLOAD_ENV = "TERASIM_CITY_OSM_DOWNLOAD"
_FALSY = frozenset({"0", "false", "no", "off"})


def osm_download_enabled() -> bool:
    """``TERASIM_CITY_OSM_DOWNLOAD``: downloads allowed unless set to 0/false/no/off."""
    return os.environ.get(OSM_DOWNLOAD_ENV, "1").strip().lower() not in _FALSY


class OsmDownloadDisabled(RuntimeError):
    """No stored map covers the area, and downloading OpenStreetMap data is switched off."""


@dataclass(frozen=True)
class OsmBoundingBox:
    """WGS84 bounds in Overpass order: south, west, north, east."""

    south: float
    west: float
    north: float
    east: float


class OsmDownloader(Protocol):
    """Dependency boundary for fetching one OSM XML extract."""

    def download(self, bbox: OsmBoundingBox, destination: Path) -> None:
        """Write the OSM XML covering ``bbox`` to ``destination``."""
        ...


@dataclass(frozen=True)
class OverpassOsmDownloader:
    """Download one road-network extract from an Overpass API endpoint."""

    endpoint: str = DEFAULT_OVERPASS_ENDPOINT
    timeout_s: float = DEFAULT_OVERPASS_TIMEOUT_S

    def download(self, bbox: OsmBoundingBox, destination: Path) -> None:
        import requests

        bounds = f"{bbox.south},{bbox.west},{bbox.north},{bbox.east}"
        # The server-side execution timeout must track the configured HTTP
        # timeout (with headroom for transfer) — a hardcoded value would make
        # TERASIM_CITY_OVERPASS_TIMEOUT_S useless against the real bottleneck.
        server_timeout = max(10, int(self.timeout_s * 0.75))
        query = (
            f"[out:xml][timeout:{server_timeout}];"
            "("
            f'way["highway"]({bounds});'
            f'node["highway"="traffic_signals"]({bounds});'
            ");"
            "(._;>;);"
            "out body;"
        )
        response = requests.post(
            self.endpoint,
            data={"data": query},
            timeout=self.timeout_s,
            headers={"User-Agent": "TeraSim-City/0.1 map-cache"},
        )
        response.raise_for_status()
        # Overpass reports mid-stream aborts as HTTP 200 with a truncated
        # body; persisting that would poison the map cache for the whole
        # region. The closing root tag is the cheapest completeness check.
        if b"</osm>" not in response.content[-200:]:
            raise RuntimeError(
                f"Overpass returned an incomplete OSM extract for {bounds} "
                "(no closing </osm>); likely a server-side timeout — retry, "
                "shrink the area, or raise TERASIM_CITY_OVERPASS_TIMEOUT_S"
            )
        Path(destination).write_bytes(response.content)


@dataclass(frozen=True)
class DisabledOsmDownloader:
    """The downloader of a deployment without OpenStreetMap downloads: never goes online.

    Asked for an extract (no stored map covers the area), it raises
    :class:`OsmDownloadDisabled` naming the area and the setting.
    """

    def download(self, bbox: OsmBoundingBox, destination: Path) -> None:
        raise OsmDownloadDisabled(no_stored_map_message(bbox))


def no_stored_map_message(bbox: "OsmBoundingBox | None" = None) -> str:
    """Why a request cannot get a map while downloads are switched off."""
    area = (
        f" (lat {bbox.south:.5f} to {bbox.north:.5f}, lon {bbox.west:.5f} to {bbox.east:.5f})"
        if bbox is not None else ""
    )
    return (
        f"no shipped or registered map covers this area{area}, and downloading "
        f"OpenStreetMap data is disabled on this deployment ({OSM_DOWNLOAD_ENV}=0): "
        "register a map that covers it (POST /v1/city/maps) or name its base_sumocfg"
    )


@dataclass(frozen=True)
class MapAcquirer:
    """Callable dependency suitable for injection into optimization submission."""

    store: MapStore
    downloader: OsmDownloader
    margin_m: float = DEFAULT_MARGIN_M

    @classmethod
    def from_environment(cls) -> "MapAcquirer":
        """Build the production acquirer around the mounted persistent store.

        With ``TERASIM_CITY_OSM_DOWNLOAD=0`` it only selects stored maps
        (:class:`DisabledOsmDownloader`); otherwise it downloads from Overpass.
        """

        downloader: OsmDownloader
        if osm_download_enabled():
            downloader = OverpassOsmDownloader(
                endpoint=os.environ.get(OVERPASS_ENDPOINT_ENV, DEFAULT_OVERPASS_ENDPOINT),
                timeout_s=float(
                    os.environ.get(OVERPASS_TIMEOUT_ENV, DEFAULT_OVERPASS_TIMEOUT_S)
                ),
            )
        else:
            downloader = DisabledOsmDownloader()
        return cls(
            store=MapStore(),
            downloader=downloader,
            margin_m=float(os.environ.get(MAP_MARGIN_ENV, DEFAULT_MARGIN_M)),
        )

    def __call__(
        self,
        tmc_json: Path,
        extra_points: "tuple[tuple[float, float], ...]" = (),
    ) -> Path:
        return ensure_map_for_tmc(
            tmc_json,
            store=self.store,
            downloader=self.downloader,
            margin_m=self.margin_m,
            extra_points=extra_points,
        )


def bounding_box_for_tmc(
    tmc: TMCData,
    *,
    margin_m: float = DEFAULT_MARGIN_M,
    extra_points: "tuple[tuple[float, float], ...]" = (),
) -> OsmBoundingBox:
    """The TMC extent — plus any optimization-target points — expanded by ``margin_m``.

    Target coordinates participate so a downloaded map always contains the
    location the user asked to optimize, not just the camera footprint.
    """

    latitudes = [intersection.lat for intersection in tmc.intersections]
    longitudes = [intersection.lon for intersection in tmc.intersections]
    for lat, lon in extra_points:
        latitudes.append(float(lat))
        longitudes.append(float(lon))
    center_latitude = (min(latitudes) + max(latitudes)) / 2.0
    latitude_margin = math.degrees(float(margin_m) / EARTH_RADIUS_M)
    longitude_margin = math.degrees(
        float(margin_m)
        / (EARTH_RADIUS_M * math.cos(math.radians(center_latitude)))
    )
    return OsmBoundingBox(
        south=min(latitudes) - latitude_margin,
        west=min(longitudes) - longitude_margin,
        north=max(latitudes) + latitude_margin,
        east=max(longitudes) + longitude_margin,
    )


def acquire_map_for_tmc(
    tmc: TMCData,
    store: MapStore,
    downloader: OsmDownloader,
    *,
    margin_m: float = DEFAULT_MARGIN_M,
    extra_points: "tuple[tuple[float, float], ...]" = (),
) -> tuple[str, MapPackage]:
    """Return a covering cached map, downloading and caching OSM only if absent.

    ``extra_points`` (optimization-target ``(lat, lon)`` pairs) extend both the
    coverage requirement and the download bounding box.
    """

    try:
        return select_map_for(store, tmc, extra_points=extra_points)
    except ValueError:
        pass

    bbox = bounding_box_for_tmc(tmc, margin_m=margin_m, extra_points=extra_points)
    with tempfile.TemporaryDirectory(prefix="terasim-city-osm-") as temp_dir:
        osm_path = Path(temp_dir) / "download.osm"
        downloader.download(bbox, osm_path)
        store.ingest(osm_path)

    # Re-run the normal selector so the same coverage and source-priority rules
    # choose the result, including if a richer asset appeared concurrently.
    return select_map_for(store, tmc, extra_points=extra_points)


def ensure_map_for_tmc(
    tmc_json: Path,
    *,
    store: MapStore,
    downloader: OsmDownloader,
    margin_m: float = DEFAULT_MARGIN_M,
    extra_points: "tuple[tuple[float, float], ...]" = (),
) -> Path:
    """Return the cached ``base_sumocfg`` path for a TMC JSON file."""

    tmc = TMCData.from_json(tmc_json)
    _map_id, package = acquire_map_for_tmc(
        tmc,
        store,
        downloader,
        margin_m=margin_m,
        extra_points=extra_points,
    )
    return package.base_sumocfg


__all__ = [
    "DEFAULT_MARGIN_M",
    "OSM_DOWNLOAD_ENV",
    "DisabledOsmDownloader",
    "MapAcquirer",
    "OsmBoundingBox",
    "OsmDownloadDisabled",
    "OsmDownloader",
    "OverpassOsmDownloader",
    "acquire_map_for_tmc",
    "bounding_box_for_tmc",
    "ensure_map_for_tmc",
    "no_stored_map_message",
    "osm_download_enabled",
]
