"""Filesystem-backed map store: submitted map files → stored map packages.

This is the core of the map module's "upload" path, deliberately free of any
HTTP concern (a future endpoint is a thin wrapper around
:meth:`MapStore.ingest`). Responsibilities:

* **Format detection** — identify a submitted file as OpenStreetMap / Lanelet2
  (both ``.osm``: a Lanelet2 map is an OSM file holding ``type=lanelet``
  relations) / OpenDRIVE ``.xodr`` / prebuilt sumocfg by suffix and content
  (both must agree).
* **Hash dedup** — ``map_id`` is a 12-hex content digest; re-submitting the
  identical map is idempotent and never re-converts. For OSM the source file
  alone is hashed; for OpenDRIVE / Lanelet2 the projection override (and, for
  Lanelet2, an Autoware ``map_projector_info.yaml`` next to the file) is folded
  in, since it changes the conversion; for prebuilt sumocfg the digest also
  folds in the referenced net/additional files (the config is just a pointer
  file).
* **Layout** — one directory per map under ``<data_root>/maps/<map_id>/``
  holding the converted net, base sumocfg, archived source file
  (``source.osm`` / ``source.xodr``) and the ``map_package.json`` manifest
  (whose ``metadata["conversion"]`` is the summary returned to the uploader).
  The filesystem is the source of truth: listing = scanning manifests, so the
  store survives restarts with no database. ``data_root`` is meant to be a
  mounted volume (``$TERASIM_CITY_DATA_ROOT``, e.g. ``/data/store``) so users
  manage the map collection themselves.
* **Coverage lookup** — :meth:`MapStore.find_covering` returns the stored
  maps whose WGS84 bounding box contains a point, preferring higher-fidelity
  sources (HD maps — OpenDRIVE, Lanelet2 — then prebuilt, then OSM). There is
  deliberately NO coverage-based dedup/supersede logic: map management is the
  user's job.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from terasim_city.maps.base import MANIFEST_NAME, MapPackage
from terasim_city.maps.geo import validate_proj_string
from terasim_city.maps.lanelet2 import PROJECTOR_INFO_NAME, Lanelet2MapProvider, is_lanelet2_osm
from terasim_city.maps.opendrive import OpenDriveMapProvider
from terasim_city.maps.osm import OsmMapProvider
from terasim_city.maps.prebuilt import PrebuiltSumoProvider
from terasim_city.maps.summary import map_summary
from terasim_city.maps.validate import file_sha256
from terasim_city.scenario.sumocfg_io import parse_sumocfg

logger = logging.getLogger(__name__)

#: Environment variable naming the persistent asset root (mounted volume).
DATA_ROOT_ENV = "TERASIM_CITY_DATA_ROOT"

#: Subdirectory of the data root holding one directory per stored map.
MAPS_SUBDIR = "maps"

#: Length of the map_id prefix taken from the source file's sha256.
MAP_ID_LEN = 12

#: Source-type preference for :meth:`MapStore.find_covering` — HD maps first
#: (lane-accurate geometry), then prebuilt scenarios, then OSM.
SOURCE_PREFERENCE: tuple[str, ...] = ("opendrive", "lanelet2", "prebuilt", "osm")

#: Formats whose conversion takes a projection override (``proj``).
PROJ_OVERRIDE_FORMATS: tuple[str, ...] = ("opendrive", "lanelet2")

_SUFFIX_FORMATS = {".osm": "osm", ".sumocfg": "sumocfg", ".xodr": "opendrive"}
# SUMO tooling writes both roots: hand-written configs use <configuration>,
# while SUMO's own --save-configuration / netedit / this repo's write_sumocfg
# emit <sumoConfiguration> (sumoConfiguration.xsd). Accept both.
_ROOT_TAG_FORMATS = {
    "osm": "osm",
    "configuration": "sumocfg",
    "sumoConfiguration": "sumocfg",
    "OpenDRIVE": "opendrive",
}
#: Archived copy of the submitted source, per format (prebuilt stays in place).
_SOURCE_COPY = {"osm": "source.osm", "lanelet2": "source.osm", "opendrive": "source.xodr"}
_SUPPORTED = (
    ".osm (OpenStreetMap or Lanelet2), .xodr (OpenDRIVE), .sumocfg (prebuilt SUMO scenario)"
)


class MapFormatError(ValueError):
    """The submitted file is not a recognizable/supported map format."""


def detect_map_format(path: Path | str) -> str:
    """Classify a submitted file as ``osm`` / ``lanelet2`` / ``opendrive`` / ``sumocfg``.

    Uses the filename suffix and the XML root element; when both are present
    they must agree (a ``.osm`` file containing ``<configuration>`` is a user
    error worth failing loud on). An unknown suffix falls back to the content.
    An ``<osm>`` document holding at least one ``type=lanelet`` relation is a
    Lanelet2 map; any other ``<osm>`` document stays plain OpenStreetMap.

    Raises:
        MapFormatError: Not XML, unknown root element, or suffix/content
            disagreement.
    """
    path = Path(path)
    suffix_fmt = _SUFFIX_FORMATS.get(path.suffix.lower())
    try:
        for _event, elem in ET.iterparse(str(path), events=("start",)):
            root_tag = elem.tag.rsplit("}", 1)[-1]  # namespaced OpenDRIVE 1.8 roots
            break
        else:  # pragma: no cover - empty file yields ParseError first
            raise MapFormatError(f"{path}: empty XML document")
    except ET.ParseError as exc:
        raise MapFormatError(
            f"{path}: not parseable XML ({exc}); supported map formats: {_SUPPORTED}"
        ) from exc
    content_fmt = _ROOT_TAG_FORMATS.get(root_tag)
    if content_fmt is None:
        raise MapFormatError(
            f"{path}: unrecognized XML root element <{root_tag}>; supported "
            "map formats: <osm> (.osm: OpenStreetMap or Lanelet2), <OpenDRIVE> "
            "(.xodr), <configuration>/<sumoConfiguration> (.sumocfg)"
        )
    if suffix_fmt is not None and suffix_fmt != content_fmt:
        raise MapFormatError(
            f"{path}: suffix says {suffix_fmt!r} but content root "
            f"<{root_tag}> says {content_fmt!r}; refusing ambiguous input"
        )
    if content_fmt == "osm" and is_lanelet2_osm(path):
        return "lanelet2"
    return content_fmt


def _boundary_contains(boundary, *, lat: float, lon: float) -> bool:
    """True iff the WGS84 point ``(lat, lon)`` falls inside ``boundary``.

    ``boundary`` is a stored ``orig_boundary`` — SUMO's ``origBoundary``, which
    is **lon-first**: ``(lon_min, lat_min, lon_max, lat_max)``. This is the one
    place that convention lives; everything else in the store speaks (lat, lon).
    A missing or malformed boundary (cartesian-only nets) never contains a
    point.
    """
    if not boundary or len(boundary) != 4:
        return False
    lon_min, lat_min, lon_max, lat_max = boundary
    return lon_min <= lon <= lon_max and lat_min <= lat <= lat_max


@dataclass(frozen=True)
class IngestResult:
    """Outcome of one :meth:`MapStore.ingest` call.

    ``duplicate`` is True when the identical source file (same sha256) was
    already in the store; ``package`` then refers to the existing map and no
    conversion was run.
    """

    map_id: str
    package: MapPackage
    duplicate: bool

    @property
    def summary(self) -> dict:
        """What the converted net contains (edges, junctions, signals, geo, warnings)."""
        return map_summary(self.package)


class MapStore:
    """One directory per stored map under ``<data_root>/maps/<map_id>/``."""

    def __init__(self, data_root: Path | str | None = None) -> None:
        if data_root is None:
            data_root = os.environ.get(DATA_ROOT_ENV)
        if not data_root:
            raise ValueError(
                "MapStore needs a data root: pass data_root=... or set the "
                f"{DATA_ROOT_ENV} environment variable (the mounted asset "
                "volume, e.g. /data/store)"
            )
        self.data_root = Path(data_root)
        self.maps_root = self.data_root / MAPS_SUBDIR

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def ingest(self, source_file: Path | str, *, proj: str | None = None) -> IngestResult:
        """Store a submitted map file; idempotent on identical content.

        The package is built in a hidden staging directory and committed with
        an atomic rename, so a crash mid-ingest leaves no half-built package
        and a concurrent identical submission resolves to one winner.

        Args:
            source_file: ``.osm`` (OpenStreetMap or Lanelet2), ``.xodr``
                (OpenDRIVE) or ``.sumocfg`` (prebuilt scenario).
            proj: Optional proj.4 definition of an HD map's own planar
                coordinates (OpenDRIVE x/y, Lanelet2 ``local_x``/``local_y``),
                for maps without a usable geo-reference. Only valid for
                OpenDRIVE and Lanelet2.

        Raises:
            ValueError: Source file missing, or ``proj`` invalid / given for a
                format that carries its own geo-reference.
            MapFormatError: Unrecognized/unsupported map format.
            GeoReferenceError: An HD map without a usable geo-reference (a
                ValueError; the message says how to pass ``proj``).
            MapConversionError / ValueError: Conversion or validation failure
                from the underlying provider; the staging directory is removed
                so a failed ingest leaves no trace.
        """
        source_file = Path(source_file).resolve()
        if not source_file.exists():
            raise ValueError(f"map source file does not exist: {source_file}")
        fmt = detect_map_format(source_file)
        if proj is not None:
            if fmt not in PROJ_OVERRIDE_FORMATS:
                raise ValueError(
                    f"a projection override applies only to OpenDRIVE (.xodr) and Lanelet2 "
                    f"(.osm) maps; {source_file.name} is {fmt!r}, which carries its own "
                    "geo-reference"
                )
            proj = validate_proj_string(proj)
        map_id = self._map_id(source_file, fmt, proj=proj)
        pkg_dir = self.maps_root / map_id
        if (pkg_dir / MANIFEST_NAME).exists():
            return IngestResult(map_id, MapPackage.load(pkg_dir), duplicate=True)

        self.maps_root.mkdir(parents=True, exist_ok=True)
        # Staging sits directly under maps_root at the same depth as the
        # final directory, so the manifest's relative paths stay valid across
        # the commit rename (in-store packages use relative paths only).
        staging = Path(tempfile.mkdtemp(prefix=f".ingest-{map_id}-", dir=self.maps_root))
        try:
            # The package is read back from its final location after the commit
            # rename, so the providers' return values are not kept here.
            if fmt == "osm":
                OsmMapProvider(source_file).build(staging)
            elif fmt == "opendrive":
                OpenDriveMapProvider(source_file, proj=proj).build(staging)
            elif fmt == "lanelet2":
                provider = Lanelet2MapProvider(source_file, proj=proj)
                provider.build(staging)
                if provider.projector_info is not None:
                    shutil.copy2(provider.projector_info, staging / f"source.{PROJECTOR_INFO_NAME}")
            else:  # sumocfg — the scenario stays in place, only the manifest lands here
                PrebuiltSumoProvider(source_file).build(staging)
            if fmt in _SOURCE_COPY:
                # Archive the submitted source next to the conversion so the
                # map can be re-converted/audited later.
                shutil.copy2(source_file, staging / _SOURCE_COPY[fmt])
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        try:
            os.rename(staging, pkg_dir)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            if (pkg_dir / MANIFEST_NAME).exists():
                # A concurrent ingest of the same content won the rename.
                return IngestResult(map_id, MapPackage.load(pkg_dir), duplicate=True)
            raise
        # Reload from the final location: paths inside ``pkg`` still point at
        # the staging directory that no longer exists.
        return IngestResult(map_id, MapPackage.load(pkg_dir), duplicate=False)

    def _map_id(self, source_file: Path, fmt: str, *, proj: str | None = None) -> str:
        """Content identity of a submission.

        OSM: the file fully determines the conversion, so hash it alone.
        OpenDRIVE / Lanelet2: the projection override changes the conversion,
        so it is folded in; Lanelet2 additionally folds in the format name (a
        Lanelet2 file stored by an older, OSM-only store must not resolve to
        that stale package) and Autoware's projector file next to the map.
        Prebuilt sumocfg: the config usually references net/additional files
        by RELATIVE name, so byte-identical configs in different scenario
        directories can describe entirely different maps — fold the digests
        of every referenced file that exists into the identity so dedup keys
        on the map content, not on the pointer file.
        """
        if fmt in PROJ_OVERRIDE_FORMATS:
            parts = [file_sha256(source_file)]
            if fmt == "lanelet2":
                parts.insert(0, "lanelet2")
                projector = source_file.with_name(PROJECTOR_INFO_NAME)
                if projector.exists():
                    parts.append(file_sha256(projector))
            if proj is not None:
                parts.append(f"proj={proj}")
            if len(parts) == 1:
                return parts[0][:MAP_ID_LEN]
            return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:MAP_ID_LEN]
        if fmt != "sumocfg":
            return file_sha256(source_file)[:MAP_ID_LEN]
        digest = hashlib.sha256(file_sha256(source_file).encode())
        try:
            info = parse_sumocfg(source_file)
        except Exception:
            # Unparseable references — the provider will fail loud with a
            # better message; the source-only digest is fine for cleanup.
            return digest.hexdigest()[:MAP_ID_LEN]
        for ref in (info.net_file, *info.additional_files):
            ref = Path(ref)
            if ref.exists():
                digest.update(file_sha256(ref).encode())
        return digest.hexdigest()[:MAP_ID_LEN]

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get(self, map_id: str) -> MapPackage:
        """Load one stored map by id.

        Raises:
            KeyError: No stored map has this id.
        """
        pkg_dir = self.maps_root / map_id
        if not (pkg_dir / MANIFEST_NAME).exists():
            known = ", ".join(sorted(self.list_ids())) or "(store is empty)"
            raise KeyError(f"no stored map with id {map_id!r}; known ids: {known}")
        return MapPackage.load(pkg_dir)

    def list_ids(self) -> list[str]:
        """Ids of every stored map (directories holding a manifest), sorted.

        Dot-prefixed directories are in-flight staging areas, not maps.
        """
        if not self.maps_root.is_dir():
            return []
        return sorted(
            d.name
            for d in self.maps_root.iterdir()
            if d.is_dir()
            and not d.name.startswith(".")
            and (d / MANIFEST_NAME).exists()
        )

    def list_packages(self) -> list[tuple[str, MapPackage]]:
        """(map_id, package) for every LOADABLE stored map, sorted by id.

        A package whose external files were moved/deleted by the user (map
        management is the user's job — prebuilt scenarios live outside the
        store) is skipped with a warning instead of poisoning the whole
        listing; :meth:`get` on that specific id still fails loud.
        """
        packages: list[tuple[str, MapPackage]] = []
        for map_id in self.list_ids():
            try:
                packages.append((map_id, self.get(map_id)))
            except (ValueError, FileNotFoundError, KeyError) as exc:
                logger.warning("skipping unloadable map package %s: %s", map_id, exc)
        return packages

    def find_covering(self, lat: float, lon: float) -> list[tuple[str, MapPackage]]:
        """Stored maps whose WGS84 bbox contains the point, best source first.

        Ordering: source preference (:data:`SOURCE_PREFERENCE`: OpenDRIVE >
        Lanelet2 > prebuilt > OSM — richer data wins when several cover the
        point), then map_id for determinism. Maps without an
        ``orig_boundary`` in their metadata (cartesian-only nets) never match.
        """
        hits: list[tuple[str, MapPackage]] = []
        for map_id, pkg in self.list_packages():
            if _boundary_contains(pkg.metadata.get("orig_boundary"), lat=lat, lon=lon):
                hits.append((map_id, pkg))

        def rank(item: tuple[str, MapPackage]) -> tuple[int, str]:
            map_id, pkg = item
            try:
                pref = SOURCE_PREFERENCE.index(pkg.source)
            except ValueError:  # unknown source sorts last
                pref = len(SOURCE_PREFERENCE)
            return (pref, map_id)

        return sorted(hits, key=rank)


__all__ = [
    "DATA_ROOT_ENV",
    "MAP_ID_LEN",
    "MAPS_SUBDIR",
    "PROJ_OVERRIDE_FORMATS",
    "SOURCE_PREFERENCE",
    "IngestResult",
    "MapFormatError",
    "MapStore",
    "detect_map_format",
]
