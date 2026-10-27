"""Validation and introspection for :class:`~terasim_city.maps.base.MapPackage`.

Checks are split into two severities:

* **Fatal** (:func:`validate_map_package`): the package is unusable — net
  missing/unparseable, geo flag inconsistent. Providers fail loud on these via
  :func:`ensure_valid_map_package` so a broken map never reaches the corridor
  pipeline silently.
* **Warnings** (:func:`map_package_warnings`): the package is usable but
  incomplete — e.g. no traffic-light program anywhere. Such a map is stored
  with ``metadata["has_tls"] = False``; signal programs can be supplied later
  from a timing sheet (plans module) before optimization.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from terasim_city.maps.base import MapPackage

logger = logging.getLogger(__name__)


def read_proj_parameter(net_file: Path | str) -> str | None:
    """Return the ``projParameter`` attribute of the net's ``<location>``.

    Returns ``None`` when the net has no ``<location>`` element or no
    ``projParameter`` attribute. Raises nothing on parse errors — callers that
    need parse validation use :func:`validate_map_package`; this helper
    propagates :class:`xml.etree.ElementTree.ParseError` unchanged so the
    caller sees the real failure.
    """
    root = ET.parse(str(net_file)).getroot()
    location = root.find("location")
    if location is None:
        return None
    return location.attrib.get("projParameter")


def read_orig_boundary(net_file: Path | str) -> tuple[float, float, float, float] | None:
    """Return the net's WGS84 bounding box (lon_min, lat_min, lon_max, lat_max).

    Parsed from the ``origBoundary`` attribute of ``<location>``. Returns
    ``None`` when the element/attribute is absent or not four floats — a
    cartesian-only net has no meaningful geographic boundary.
    """
    root = ET.parse(str(net_file)).getroot()
    location = root.find("location")
    if location is None:
        return None
    raw = location.attrib.get("origBoundary")
    if not raw:
        return None
    try:
        parts = tuple(float(v) for v in raw.split(","))
    except ValueError:
        return None
    if len(parts) != 4:
        return None
    return parts  # type: ignore[return-value]


def file_sha256(path: Path | str) -> str:
    """Full sha256 hex digest of a file, streamed in 1 MiB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def net_is_geo_referenced(net_file: Path | str) -> bool:
    """True iff the net carries a real projection (lat/lon matching works).

    SUMO writes ``projParameter="!"`` for purely cartesian nets (e.g. a net
    converted from a source lacking a ``<geoReference>`` header); that counts
    as NOT geo-referenced, as does a missing/empty attribute.
    """
    proj = read_proj_parameter(net_file)
    return bool(proj) and proj != "!"


def file_defines_tls_program(xml_file: Path | str) -> bool:
    """True iff the XML file contains a ``<tlLogic>`` with id and programID.

    Missing or unparseable files return False (with a warning), mirroring the
    additional-file scan in :mod:`terasim_city.signals`.
    """
    xml_file = Path(xml_file)
    if not xml_file.exists():
        logger.warning("skipping TLS scan (file not found): %s", xml_file)
        return False
    try:
        root = ET.parse(str(xml_file)).getroot()
    except ET.ParseError as exc:
        logger.warning("skipping TLS scan (XML parse error): %s (%s)", xml_file, exc)
        return False
    for node in root.findall(".//tlLogic"):
        if node.attrib.get("id") and node.attrib.get("programID"):
            return True
    return False


def validate_map_package(pkg: MapPackage) -> list[str]:
    """Check a map package for FATAL problems; return human-readable descriptions.

    Checks (an empty list means the package is usable):

    * The net file exists and parses as XML.
    * ``geo_referenced`` is consistent with the net's actual ``projParameter``.

    A missing traffic-light program is deliberately NOT fatal — see
    :func:`map_package_warnings`.
    """
    net_file = Path(pkg.net_file)
    if not net_file.exists():
        return [f"net file does not exist: {net_file}"]
    try:
        ET.parse(str(net_file))
    except ET.ParseError as exc:
        return [f"net file is not parseable XML: {net_file} ({exc})"]

    problems: list[str] = []
    actual_geo = net_is_geo_referenced(net_file)
    if pkg.geo_referenced != actual_geo:
        proj = read_proj_parameter(net_file)
        problems.append(
            f"geo_referenced flag is {pkg.geo_referenced} but the net's "
            f"projParameter is {proj!r} (geo-referenced={actual_geo}): {net_file}"
        )

    return problems


def map_package_warnings(pkg: MapPackage) -> list[str]:
    """Non-fatal problems: the package is stored, but callers should know.

    Currently one check: no traffic light with a program anywhere in the net
    or ``tls_files``. Such a map cannot be optimized as-is, but signal
    programs can be materialized later from a timing sheet, so ingestion
    proceeds with ``metadata["has_tls"] = False``.
    """
    warnings: list[str] = []
    tls_sources = (Path(pkg.net_file), *pkg.tls_files)
    if not any(file_defines_tls_program(p) for p in tls_sources):
        scanned = ", ".join(str(p) for p in tls_sources)
        warnings.append(
            "no traffic light with a program (<tlLogic id=.. programID=..>) "
            f"found in the net or tls_files; scanned: {scanned}"
        )
    return warnings


def package_metadata(
    source_file: Path | str,
    net_file: Path | str,
    tls_files: tuple[Path, ...] = (),
) -> dict:
    """Common metadata every provider records in the manifest.

    * ``source_filename`` / ``source_sha256`` — identity of the submitted
      file; the map store derives its map_id from the same digest.
    * ``created_at`` — UTC ISO-8601 conversion timestamp.
    * ``orig_boundary`` — the net's WGS84 bbox (lon_min, lat_min, lon_max,
      lat_max) or None; used to find which stored map covers a corridor.
    * ``has_tls`` — whether any traffic-light program exists yet.
    """
    source_file = Path(source_file)
    net_file = Path(net_file)
    boundary = read_orig_boundary(net_file)
    has_tls = any(
        file_defines_tls_program(p) for p in (net_file, *tls_files)
    )
    return {
        "source_filename": source_file.name,
        "source_sha256": file_sha256(source_file),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "orig_boundary": list(boundary) if boundary is not None else None,
        "has_tls": has_tls,
    }


def ensure_valid_map_package(pkg: MapPackage, *, context: str) -> MapPackage:
    """Raise ``ValueError`` listing every problem found; return ``pkg`` if clean."""
    problems = validate_map_package(pkg)
    if problems:
        bullet_list = "\n".join(f"  - {p}" for p in problems)
        raise ValueError(f"{context}: map package failed validation:\n{bullet_list}")
    return pkg
