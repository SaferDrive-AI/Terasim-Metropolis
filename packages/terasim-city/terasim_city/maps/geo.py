"""Geo-referencing helpers shared by the HD-map providers (OpenDRIVE, Lanelet2).

A converted net is only useful to the corridor pipeline when its ``<location>``
carries a real projection: intersections are matched by lat/lon, and the map
store finds the map covering a corridor through the net's WGS84 bounding box
(``origBoundary``). HD maps break this in three ways that netconvert does not
report, so the providers check for them explicitly:

* No projection at all — an OpenDRIVE file without ``<geoReference>`` yields
  ``projParameter="!"`` (cartesian-only net).
* A projection that does not match the coordinates — e.g. a vendor file that
  declares plain UTM while its x/y are LOCAL metres around an undisclosed
  origin: the net is "geo-referenced" on paper but lands near (0°N, zone
  edge), thousands of kilometres off. :func:`geo_reference_problem` catches
  the UTM variant (eastings outside any UTM zone).
* A zeroed ``origBoundary`` — netconvert does not compute it for OpenDRIVE
  input; :func:`ensure_orig_boundary` recomputes it from the projection.

The fix in every case is a projection override for the map's own planar
coordinates (``proj``), validated by :func:`validate_proj_string`.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from xml.etree import ElementTree as ET

#: Generous bounds for a real UTM easting (zone interior is ~166 km..834 km).
UTM_EASTING_RANGE = (100_000.0, 900_000.0)

#: How to describe the override in error messages (shared by both providers).
PROJ_OVERRIDE_HINT = (
    "pass a projection override `proj` (MCP upload_map / POST /v1/city/maps) "
    "that describes the map's own planar coordinates, e.g. "
    "'+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs' when they are true UTM "
    "metres, or '+proj=tmerc +lat_0=<LAT> +lon_0=<LON> +k=1 +x_0=0 +y_0=0 "
    "+datum=WGS84 +units=m +no_defs' when they are local metres around a known "
    "origin <LAT>,<LON>"
)


class GeoReferenceError(ValueError):
    """The map has no usable geographic reference; lat/lon matching would silently fail."""


def validate_proj_string(proj: str) -> str:
    """Normalize a user-supplied proj.4 override and fail loud on obvious mistakes.

    The string ends up inside an XML attribute / CDATA section and in a
    netconvert command line, so XML-hostile characters are rejected outright
    (no real proj.4 definition contains them). When pyproj is importable the
    definition is also parsed, so a typo fails here instead of deep inside
    netconvert.

    Raises:
        ValueError: Empty, not a ``+proj=`` definition, or unparseable.
    """
    if not isinstance(proj, str) or not proj.strip():
        raise ValueError("projection override must be a non-empty proj.4 string")
    proj = " ".join(proj.split())
    if "+proj=" not in proj:
        raise ValueError(
            f"projection override must be a proj.4 definition containing '+proj=' "
            f"(e.g. '+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs'); got {proj!r}"
        )
    if any(ch in proj for ch in "<>&]\"'"):
        raise ValueError(f"projection override contains characters not allowed in proj.4: {proj!r}")
    try:
        from pyproj import CRS
    except ImportError:  # pragma: no cover - pyproj ships with terasim
        return proj
    try:
        CRS.from_proj4(proj)
    except Exception as exc:  # pyproj raises CRSError (a subclass of Exception)
        raise ValueError(
            f"projection override {proj!r} is not a valid proj.4 definition: {exc}"
        ) from exc
    return proj


def utm_proj_string(lat: float, lon: float) -> str:
    """proj.4 definition of the UTM zone containing ``(lat, lon)`` (netconvert's form)."""
    zone = int((lon + 180.0) // 6.0) % 60 + 1
    south = " +south" if lat < 0 else ""
    return f"+proj=utm +zone={zone}{south} +ellps=WGS84 +datum=WGS84 +units=m +no_defs"


def read_location(net_file: Path | str) -> dict | None:
    """Attributes of the net's ``<location>`` element, read without parsing the whole net.

    Returns ``None`` when the net has no ``<location>`` before its first edge.
    """
    try:
        for _event, elem in ET.iterparse(str(net_file), events=("start",)):
            if elem.tag == "location":
                return dict(elem.attrib)
            if elem.tag in ("edge", "junction"):
                return None
    except ET.ParseError:
        return None
    return None


def _floats(raw: str | None, count: int) -> tuple[float, ...] | None:
    if not raw:
        return None
    try:
        values = tuple(float(v) for v in raw.split(","))
    except ValueError:
        return None
    return values if len(values) == count else None


def _transformer_to_wgs84(proj: str):
    from pyproj import CRS, Transformer

    return Transformer.from_crs(CRS.from_proj4(proj), CRS.from_epsg(4326), always_xy=True)


def _boundary_samples(conv: tuple[float, ...]) -> list[tuple[float, float]]:
    """Corners, edge midpoints and centre of an axis-aligned conv boundary."""
    xmin, ymin, xmax, ymax = conv
    xs = (xmin, (xmin + xmax) / 2.0, xmax)
    ys = (ymin, (ymin + ymax) / 2.0, ymax)
    return [(x, y) for x in xs for y in ys]


def projected_boundary(location: dict) -> tuple[float, float, float, float] | None:
    """The net's extent in the SOURCE projection's coordinates (conv minus netOffset)."""
    conv = _floats(location.get("convBoundary"), 4)
    offset = _floats(location.get("netOffset"), 2)
    if conv is None or offset is None:
        return None
    ox, oy = offset
    return (conv[0] - ox, conv[1] - oy, conv[2] - ox, conv[3] - oy)


def wgs84_boundary(location: dict) -> tuple[float, float, float, float] | None:
    """(lon_min, lat_min, lon_max, lat_max) of the net, computed from its projection.

    Returns ``None`` for cartesian-only nets or when the projection cannot be
    evaluated (no pyproj, unparseable definition, non-finite result).
    """
    proj = location.get("projParameter")
    conv = _floats(location.get("convBoundary"), 4)
    offset = _floats(location.get("netOffset"), 2)
    if not proj or proj == "!" or conv is None or offset is None:
        return None
    try:
        transformer = _transformer_to_wgs84(proj)
    except Exception:
        return None
    ox, oy = offset
    lons: list[float] = []
    lats: list[float] = []
    for x, y in _boundary_samples(conv):
        lon, lat = transformer.transform(x - ox, y - oy)
        if not (math.isfinite(lon) and math.isfinite(lat)):
            return None
        lons.append(lon)
        lats.append(lat)
    return (min(lons), min(lats), max(lons), max(lats))


def _degenerate(boundary: tuple[float, ...] | None) -> bool:
    if boundary is None:
        return True
    lon_min, lat_min, lon_max, lat_max = boundary
    return lon_min >= lon_max or lat_min >= lat_max


def ensure_orig_boundary(net_file: Path | str) -> tuple[float, float, float, float] | None:
    """Make sure a geo-referenced net carries a real ``origBoundary``; return it.

    netconvert leaves ``origBoundary="0,0,0,0"`` for OpenDRIVE input, which
    would make the map invisible to the store's coverage lookup. When the
    boundary is missing or degenerate it is recomputed from ``convBoundary``,
    ``netOffset`` and ``projParameter`` and written back IN PLACE (only that
    attribute of the ``<location>`` element changes). Cartesian nets are left
    untouched and yield ``None``.
    """
    net_file = Path(net_file)
    location = read_location(net_file)
    if location is None:
        return None
    current = _floats(location.get("origBoundary"), 4)
    proj = location.get("projParameter")
    if not proj or proj == "!":
        return None
    if not _degenerate(current):
        return current  # type: ignore[return-value]
    boundary = wgs84_boundary(location)
    if boundary is None:
        return None
    _rewrite_location_attribute(
        net_file, "origBoundary", ",".join(f"{v:.6f}" for v in boundary)
    )
    return boundary


def _rewrite_location_attribute(net_file: Path, name: str, value: str) -> None:
    """Replace (or add) one attribute of the first ``<location>`` element in place.

    Only the head of the file is rewritten in memory; the rest is streamed, so
    multi-hundred-MB nets are fine. The write is atomic (temp file + rename).
    """
    with open(net_file, "rb") as fh:
        head = fh.read(1 << 20)
        start = head.find(b"<location")
        if start < 0:
            raise ValueError(f"{net_file}: no <location> element in the first MiB")
        end = head.find(b">", start)
        if end < 0:
            raise ValueError(f"{net_file}: unterminated <location> element")
        tag = head[start:end]
        attr = re.compile(rb"\s" + re.escape(name.encode()) + rb'="[^"]*"')
        replacement = f' {name}="{value}"'.encode()
        if attr.search(tag):
            new_tag = attr.sub(lambda _m: replacement, tag, count=1)
        else:
            closing = b"/" if tag.endswith(b"/") else b""
            new_tag = tag[: len(tag) - len(closing)].rstrip() + replacement + closing
        tmp = net_file.with_name(net_file.name + ".tmp")
        with open(tmp, "wb") as out:
            out.write(head[:start] + new_tag + head[end:])
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
    os.replace(tmp, net_file)


def geo_reference_problem(net_file: Path | str) -> str | None:
    """Why the net's geo-reference is unusable, or ``None`` when it looks sound.

    Checks, in order: a projection exists; it maps the net onto valid WGS84
    coordinates; and, for UTM projections, the source x coordinates are real
    UTM eastings (local metres around (0, 0) under a bare UTM definition are
    the classic vendor mistake — the map would land at the zone's false
    origin, far from where it belongs).
    """
    location = read_location(net_file)
    if location is None:
        return "the net has no <location> element"
    proj = location.get("projParameter")
    if not proj or proj == "!":
        return "the net has no projection (projParameter '!'): the source carries no geo-reference"
    boundary = wgs84_boundary(location)
    if boundary is None:
        return f"the projection {proj!r} could not be evaluated for the net's extent"
    lon_min, lat_min, lon_max, lat_max = boundary
    if not (-85.0 <= lat_min <= lat_max <= 85.0 and -180.0 <= lon_min <= lon_max <= 180.0):
        return (
            f"the projection {proj!r} puts the map at lon {lon_min:.4f}..{lon_max:.4f}, "
            f"lat {lat_min:.4f}..{lat_max:.4f}, which is not a valid location"
        )
    if "+proj=utm" in proj:
        extent = projected_boundary(location)
        if extent is not None:
            x_min, _y_min, x_max, _y_max = extent
            lo, hi = UTM_EASTING_RANGE
            if x_min < lo or x_max > hi:
                return (
                    f"the map declares a UTM projection ({proj!r}) but its x coordinates span "
                    f"{x_min:.0f}..{x_max:.0f} m, which are not UTM eastings (always within "
                    f"~{lo:.0f}..{hi:.0f} m): the coordinates are local metres around an "
                    f"undisclosed origin, so the map would be placed at lon "
                    f"{lon_min:.3f}..{lon_max:.3f}, lat {lat_min:.3f}..{lat_max:.3f}"
                )
    return None


# ---------------------------------------------------------------------------
# Autoware local frames (Lanelet2 maps without lat/lon)
# ---------------------------------------------------------------------------

_MGRS_ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUV"
_MGRS_COLUMN_SETS = ("STUVWXYZ", "ABCDEFGH", "JKLMNPQR")  # indexed by zone % 3
_MGRS_BANDS = "CDEFGHJKLMNPQRSTUVWX"
#: zone, latitude band, 100 km column letter, 100 km row letter (I and O unused)
_MGRS_SQUARE = re.compile(
    r"(\d{1,2})([C-HJ-NP-Xc-hj-np-x])([A-HJ-NP-Za-hj-np-z])([A-HJ-NP-Va-hj-np-v])"
)


def mgrs_grid_origin(grid: str) -> tuple[int, bool, float, float]:
    """UTM zone, southern flag, easting and northing of an MGRS 100 km square's SW corner.

    Autoware's MGRS projector stores ``local_x``/``local_y`` as offsets inside
    one 100 km square (``mgrs_grid: 54SUE``); adding this origin turns them
    into UTM coordinates of ``zone`` (WGS84).

    Raises:
        ValueError: The grid reference is not ``<zone><band><col><row>``.
    """
    match = _MGRS_SQUARE.fullmatch((grid or "").strip())
    if not match:
        raise ValueError(f"not an MGRS 100 km grid square (e.g. '54SUE'): {grid!r}")
    zone = int(match.group(1))
    band, column, row = (g.upper() for g in match.groups()[1:])
    if not 1 <= zone <= 60:
        raise ValueError(f"MGRS zone out of range: {grid!r}")
    columns = _MGRS_COLUMN_SETS[zone % 3]
    if column not in columns:
        raise ValueError(f"MGRS column letter {column!r} is not valid for zone {zone}: {grid!r}")
    easting = (columns.index(column) + 1) * 100_000.0
    row_index = _MGRS_ROW_LETTERS.index(row)
    if zone % 2 == 0:
        row_index = (row_index - 5) % 20
    northing = row_index * 100_000.0
    south = band < "N"
    band_south_lat = -80.0 + 8.0 * _MGRS_BANDS.index(band)
    from pyproj import Transformer

    central_lon = -183.0 + 6.0 * zone
    utm = Transformer.from_crs(
        "EPSG:4326", utm_proj_string(-1.0 if south else 1.0, central_lon), always_xy=True
    )
    band_min_northing = utm.transform(central_lon, band_south_lat)[1]
    while northing + 100_000.0 <= band_min_northing:
        northing += 2_000_000.0
    return zone, south, easting, northing


def parse_projector_info(path: Path | str) -> dict:
    """Read Autoware's ``map_projector_info.yaml`` (flat keys + ``map_origin``) without PyYAML."""
    info: dict = {}
    section: str | None = None
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.strip().partition(":")
        value = value.strip().strip("'\"")
        if raw[:1] in (" ", "\t") and section is not None:
            info.setdefault(section, {})[key] = value
        elif value == "":
            section = key
            info.setdefault(section, {})
        else:
            section = None
            info[key] = value
    return info


def local_frame_proj(info: dict) -> str:
    """proj.4 definition of an Autoware local frame described by ``map_projector_info``.

    Supported ``projector_type`` values: ``MGRS`` (needs ``mgrs_grid``),
    ``LocalCartesianUTM`` (local = UTM minus the UTM of ``map_origin``) and
    ``LocalCartesian`` (east/north metres around ``map_origin``; modelled as a
    unit-scale transverse Mercator, exact to millimetres over city extents).

    Raises:
        ValueError: Unsupported projector type or missing fields.
    """
    kind = str(info.get("projector_type", "")).strip()
    if kind == "MGRS":
        zone, south, easting, northing = mgrs_grid_origin(str(info.get("mgrs_grid", "")))
        central_lon = -183.0 + 6.0 * zone
        false_northing = (10_000_000.0 if south else 0.0) - northing
        return (
            f"+proj=tmerc +lat_0=0 +lon_0={central_lon:g} +k=0.9996 "
            f"+x_0={500_000.0 - easting:.3f} +y_0={false_northing:.3f} "
            "+ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
    origin = info.get("map_origin") or {}
    try:
        lat0 = float(origin["latitude"])
        lon0 = float(origin["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"projector_type {kind!r} needs map_origin.latitude/longitude in map_projector_info"
        ) from exc
    if kind == "LocalCartesianUTM":
        from pyproj import Transformer

        utm = utm_proj_string(lat0, lon0)
        e0, n0 = Transformer.from_crs("EPSG:4326", utm, always_xy=True).transform(lon0, lat0)
        zone = int((lon0 + 180.0) // 6.0) % 60 + 1
        central_lon = -183.0 + 6.0 * zone
        false_northing = (10_000_000.0 if lat0 < 0 else 0.0) - n0
        return (
            f"+proj=tmerc +lat_0=0 +lon_0={central_lon:g} +k=0.9996 "
            f"+x_0={500_000.0 - e0:.3f} +y_0={false_northing:.3f} "
            "+ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
    if kind == "LocalCartesian":
        return (
            f"+proj=tmerc +lat_0={lat0:.10f} +lon_0={lon0:.10f} +k=1 +x_0=0 +y_0=0 "
            "+ellps=WGS84 +datum=WGS84 +units=m +no_defs"
        )
    raise ValueError(
        f"unsupported map_projector_info projector_type {kind!r} "
        "(supported: MGRS, LocalCartesianUTM, LocalCartesian)"
    )


__all__ = [
    "GeoReferenceError",
    "PROJ_OVERRIDE_HINT",
    "UTM_EASTING_RANGE",
    "ensure_orig_boundary",
    "geo_reference_problem",
    "local_frame_proj",
    "mgrs_grid_origin",
    "parse_projector_info",
    "projected_boundary",
    "read_location",
    "utm_proj_string",
    "validate_proj_string",
    "wgs84_boundary",
]
