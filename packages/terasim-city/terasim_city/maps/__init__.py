"""Map providers, the MapPackage contract, and the filesystem map store.

Provider relationship: :class:`OsmMapProvider`, :class:`OpenDriveMapProvider`
and :class:`Lanelet2MapProvider` are ONE-TIME offline converters — each output
directory IS a reusable cached map package (net, base sumocfg, manifest with a
conversion summary). :class:`PrebuiltSumoProvider` is the loader for any such
package, whether partner-delivered or previously converted. Optimization runs
always consume the cached package; they never re-convert source maps.

:class:`MapStore` is the submission path: give it a map file (``.osm`` —
OpenStreetMap or Lanelet2 —, ``.xodr`` or ``.sumocfg``; optionally a ``proj``
override for HD maps without a usable geo-reference), it detects the format,
dedups by content hash, runs the right provider and stores the package under
``$TERASIM_CITY_DATA_ROOT/maps/<map_id>/`` (a mounted volume — users manage
the map collection themselves).
"""

from terasim_city.maps.base import (
    MANIFEST_NAME,
    MAP_PACKAGE_SCHEMA,
    MAP_SOURCES,
    MapPackage,
    MapProvider,
)
from terasim_city.maps.geo import GeoReferenceError, validate_proj_string
from terasim_city.maps.lanelet2 import Lanelet2MapProvider, is_lanelet2_osm
from terasim_city.maps.opendrive import OpenDriveMapProvider
from terasim_city.maps.osm import MapConversionError, OsmMapProvider
from terasim_city.maps.prebuilt import PrebuiltSumoProvider
from terasim_city.maps.store import (
    DATA_ROOT_ENV,
    SOURCE_PREFERENCE,
    IngestResult,
    MapFormatError,
    MapStore,
    detect_map_format,
)
from terasim_city.maps.summary import map_summary, net_summary
from terasim_city.maps.validate import (
    ensure_valid_map_package,
    file_sha256,
    map_package_warnings,
    net_is_geo_referenced,
    package_metadata,
    read_orig_boundary,
    read_proj_parameter,
    validate_map_package,
)

__all__ = [
    "DATA_ROOT_ENV",
    "MANIFEST_NAME",
    "MAP_PACKAGE_SCHEMA",
    "MAP_SOURCES",
    "SOURCE_PREFERENCE",
    "GeoReferenceError",
    "IngestResult",
    "Lanelet2MapProvider",
    "MapConversionError",
    "MapFormatError",
    "MapPackage",
    "MapProvider",
    "MapStore",
    "OpenDriveMapProvider",
    "OsmMapProvider",
    "PrebuiltSumoProvider",
    "detect_map_format",
    "is_lanelet2_osm",
    "map_summary",
    "net_summary",
    "validate_proj_string",
    "ensure_valid_map_package",
    "file_sha256",
    "map_package_warnings",
    "net_is_geo_referenced",
    "package_metadata",
    "read_orig_boundary",
    "read_proj_parameter",
    "validate_map_package",
]
