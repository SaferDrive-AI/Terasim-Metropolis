"""OpenDRIVE (``.xodr``) map provider: ``netconvert --opendrive-files`` → MapPackage.

Steps (one netconvert run; up to three when a repair is needed):

1. **Geo-reference.** The converted net is lat/lon-matchable only through the
   file's ``<header><geoReference>`` (a proj.4 string). Many vendor files carry
   none — netconvert then writes ``projParameter="!"`` and lat/lon matching
   silently cannot work — so such a file is rejected BEFORE conversion with
   :class:`~terasim_city.maps.geo.GeoReferenceError`, unless the caller passes
   a projection override ``proj`` describing the file's x/y coordinates.
   The override is written as the ``<geoReference>`` of a temporary copy of
   the file. It is deliberately NOT passed as ``netconvert --proj``: that
   option makes netconvert read the OpenDRIVE x/y as lon/lat, which fails
   ("Invalid longitude ...") on any real file.
2. ``netconvert --opendrive-files <file> --junctions.join --tls.default-type
   static --output.street-names``. Signals netconvert recognizes as traffic
   lights (OpenDRIVE type 1000001 and friends) become signalized junctions
   with netconvert-generated static programs; the corridor optimizer replaces
   the timings anyway. netconvert has no other OpenDRIVE signal switch that
   helps here (``--opendrive.signal-groups`` needs ``<controller>`` elements,
   ``--opendrive.ignore-misplaced-signals`` only drops signals).
3. **Repairs.** A fragmented result (largest connected component below 80 %
   of the edges — vendor files often leave road ends that meet at one point
   unlinked) is reconverted with ``--junctions.join-same`` and kept when that
   clearly reconnects it. If no signal was imported, netconvert reruns with
   ``--tls.guess``; the summary records ``tls_source="guessed"`` and a warning.
4. **Boundary + plausibility.** netconvert leaves ``origBoundary`` at zeros for
   OpenDRIVE; it is recomputed from the projection (the map store's coverage
   lookup depends on it). :func:`~terasim_city.maps.geo.geo_reference_problem`
   then rejects nets whose projection does not match their coordinates — e.g.
   a file declaring plain UTM while its x/y are local metres, which would
   place the map at the zone's false origin, thousands of km away. Finally the
   net must load in SUMO: netconvert can exit 0 yet write NaN junction shapes
   for degenerate source geometry.
5. A base sumocfg (empty routes) and a validated :class:`MapPackage` with
   ``source="opendrive"`` and a ``metadata["conversion"]`` summary.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from terasim_city.maps.base import MapPackage
from terasim_city.maps.geo import (
    PROJ_OVERRIDE_HINT,
    GeoReferenceError,
    ensure_orig_boundary,
    geo_reference_problem,
    validate_proj_string,
)
from terasim_city.maps.osm import MapConversionError, _run_netconvert, write_base_sumocfg
from terasim_city.maps.summary import (
    FRAGMENTED_SHARE,
    conversion_summary,
    ensure_net_loads,
    net_summary,
    netconvert_warning_summary,
)
from terasim_city.maps.validate import (
    ensure_valid_map_package,
    map_package_warnings,
    package_metadata,
)

logger = logging.getLogger(__name__)

#: netconvert flags for OpenDRIVE import. ``--junctions.join`` merges the many
#: small nodes vendor files produce at one intersection; ``--tls.default-type
#: static`` because the corridor pipeline evaluates fixed-time plans.
NETCONVERT_OPENDRIVE_FLAGS: tuple[str, ...] = (
    "--junctions.join",
    "--tls.default-type", "static",
    "--output.street-names",
)

#: Added on a rerun when no OpenDRIVE signal became a traffic light.
TLS_GUESS_FLAGS: tuple[str, ...] = ("--tls.guess",)

#: Added on a rerun when the first net is fragmented (see ``build``).
JOIN_SAME_FLAGS: tuple[str, ...] = ("--junctions.join-same",)
#: Minimum gain in largest-component share for the join-same rerun to be kept.
JOIN_SAME_MIN_GAIN = 0.05

#: The OpenDRIVE header must start within this many bytes of the file.
_HEADER_SCAN_LIMIT = 16 << 20
_HEADER_OPEN = re.compile(rb"<header\b[^>]*>")
_GEOREFERENCE = re.compile(rb"<geoReference\b[^>]*/>|<geoReference\b.*?</geoReference>", re.S)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_geo_reference(xodr_file: Path | str) -> str | None:
    """The text of the file's ``<header><geoReference>`` (CDATA unwrapped), or ``None``.

    Stops at the end of the header, so large files are not parsed.

    Raises:
        ValueError: The file is not parseable XML.
    """
    try:
        for event, elem in ET.iterparse(str(xodr_file), events=("start", "end")):
            tag = _local_name(elem.tag)
            if event == "end" and tag == "geoReference":
                text = (elem.text or "").strip()
                return text or None
            if (event == "end" and tag == "header") or (event == "start" and tag == "road"):
                return None
    except ET.ParseError as exc:
        raise ValueError(f"{xodr_file}: not parseable XML ({exc})") from exc
    return None


def _patched_head(head: bytes, proj: bytes) -> bytes | None:
    """``head`` with its header's geoReference replaced by ``proj``; None if incomplete."""
    match = _HEADER_OPEN.search(head)
    if match is None:
        return None
    georef = b"<geoReference><![CDATA[" + proj + b"]]></geoReference>"
    opening = match.group(0)
    if opening.endswith(b"/>"):
        return (
            head[: match.start()] + opening[:-2].rstrip() + b">" + georef + b"</header>"
            + head[match.end():]
        )
    close = head.find(b"</header>", match.end())
    if close < 0:
        return None
    inner = _GEOREFERENCE.sub(b"", head[match.end():close])
    return head[: match.end()] + georef + inner + head[close:]


def write_georeferenced_copy(src: Path | str, dst: Path | str, proj: str) -> Path:
    """Copy an OpenDRIVE file, setting its header's ``<geoReference>`` to ``proj``.

    Any existing geoReference is replaced; a self-closing ``<header/>`` is
    expanded. Only the head of the file is rewritten; the rest is streamed.

    Raises:
        ValueError: No complete ``<header>`` within the first 16 MiB.
    """
    src, dst = Path(src), Path(dst)
    proj_bytes = validate_proj_string(proj).encode("utf-8")
    with open(src, "rb") as fh:
        head = b""
        patched = None
        while len(head) < _HEADER_SCAN_LIMIT:
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            head += chunk
            patched = _patched_head(head, proj_bytes)
            if patched is not None:
                break
        if patched is None:
            raise ValueError(f"{src}: no complete OpenDRIVE <header> element found")
        with open(dst, "wb") as out:
            out.write(patched)
            shutil.copyfileobj(fh, out, 1 << 20)
    return dst


def _count_tls_programs(net_file: Path) -> int:
    """Number of ``<tlLogic`` elements, counted on the raw bytes (streamed)."""
    needle = b"<tlLogic "
    count = 0
    tail = b""
    with open(net_file, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            data = tail + chunk
            count += data.count(needle)
            # The carried-over tail is shorter than the needle, so a match is
            # never counted twice, and one split across chunks is still found.
            tail = data[-(len(needle) - 1):]
    return count


class OpenDriveMapProvider:
    """Convert a local ``.xodr`` file into a geo-referenced :class:`MapPackage`.

    Args:
        xodr_file: The OpenDRIVE file.
        proj: Optional proj.4 definition of the file's x/y coordinates. Required
            when the header has no usable ``<geoReference>``; when given it
            replaces the file's own geoReference.
    """

    name = "opendrive"

    def __init__(self, xodr_file: Path | str, *, proj: str | None = None) -> None:
        xodr_file = Path(xodr_file).resolve()
        if not xodr_file.exists():
            raise ValueError(f"OpenDRIVE file does not exist: {xodr_file}")
        self.xodr_file = xodr_file
        self.proj = validate_proj_string(proj) if proj is not None else None

    def build(self, out_dir: Path) -> MapPackage:
        """Convert into ``out_dir`` and assemble a validated, geo-referenced package.

        Raises:
            GeoReferenceError: No usable geo-reference (no/unsupported
                ``<geoReference>`` and no ``proj``, or a projection that does
                not match the coordinates). A ``ValueError``.
            MapConversionError: netconvert failed (a ``RuntimeError``).
            ValueError: The converted package fails validation.
        """
        started = time.monotonic()
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        net_file = out_dir / "net.net.xml"
        file_proj = read_geo_reference(self.xodr_file)
        warnings: list[str] = []

        if self.proj is None:
            if file_proj is None:
                raise GeoReferenceError(
                    f"{self.xodr_file}: the OpenDRIVE header has no <geoReference>, so the "
                    "converted net could not be matched to lat/lon (netconvert would write "
                    f"projParameter='!'). Fix: {PROJ_OVERRIDE_HINT}"
                )
            if "+proj=" not in file_proj:
                raise GeoReferenceError(
                    f"{self.xodr_file}: the <geoReference> {file_proj[:120]!r} is not a proj.4 "
                    "definition, the only form netconvert understands. Fix: "
                    f"{PROJ_OVERRIDE_HINT}"
                )
        elif file_proj:
            warnings.append(
                f"the file's own geoReference {file_proj!r} was replaced by the projection "
                f"override {self.proj!r}"
            )

        with tempfile.TemporaryDirectory(prefix=".xodr-", dir=out_dir) as work_dir:
            work = Path(work_dir)
            source = self.xodr_file
            if self.proj is not None:
                source = write_georeferenced_copy(
                    self.xodr_file, work / self.xodr_file.name, self.proj
                )

            def convert(run_flags: tuple[str, ...], target: Path):
                cmd = ["netconvert", "--opendrive-files", str(source), *run_flags]
                return _run_netconvert([*cmd, "-o", str(target)], input_file=self.xodr_file)

            flags = NETCONVERT_OPENDRIVE_FLAGS
            result = convert(flags, net_file)

            # Connectivity repair: vendor files often leave road ends that meet
            # at one point unlinked; joining identical positions reconnects them.
            # Only tried on a fragmented net, kept only if it clearly helps
            # (netconvert itself can crash on badly broken topology).
            share = net_summary(net_file)["largest_component_share"]
            if share < FRAGMENTED_SHARE:
                candidate = work / "join-same.net.xml"
                try:
                    retry = convert((*flags, *JOIN_SAME_FLAGS), candidate)
                except MapConversionError as exc:
                    warnings.append(
                        "the network is fragmented and the repair run with "
                        f"--junctions.join-same failed ({str(exc).splitlines()[0]}); the "
                        "unrepaired network was kept"
                    )
                else:
                    repaired = net_summary(candidate)["largest_component_share"]
                    if repaired >= share + JOIN_SAME_MIN_GAIN:
                        os.replace(candidate, net_file)
                        flags, result = (*flags, *JOIN_SAME_FLAGS), retry
                        warnings.append(
                            "unlinked road ends at identical positions were joined "
                            f"(--junctions.join-same): the largest connected component grew "
                            f"from {share:.0%} to {repaired:.0%} of the edges"
                        )

            tls_source = "opendrive_signals"
            if _count_tls_programs(net_file) == 0:
                flags = (*flags, *TLS_GUESS_FLAGS)
                result = convert(flags, net_file)
                if _count_tls_programs(net_file):
                    tls_source = "guessed"
                    warnings.append(
                        "no OpenDRIVE signal could be imported as a traffic light; netconvert "
                        "guessed the signalized junctions (--tls.guess) — review them; signal "
                        "programs are placeholders until a timing sheet or optimization "
                        "replaces them"
                    )
                else:
                    tls_source = "none"

        ensure_orig_boundary(net_file)
        problem = geo_reference_problem(net_file)
        if problem is not None:
            raise GeoReferenceError(f"{self.xodr_file}: {problem}. Fix: {PROJ_OVERRIDE_HINT}")
        ensure_net_loads(net_file, source=self.xodr_file)

        base_sumocfg = write_base_sumocfg(out_dir, net_file)
        pkg = MapPackage(
            root=out_dir.resolve(),
            net_file=net_file.resolve(),
            base_sumocfg=base_sumocfg,
            tls_files=(),
            geo_referenced=True,
            source="opendrive",
            metadata={
                **package_metadata(self.xodr_file, net_file),
                "opendrive_file": str(self.xodr_file),
                "geo_reference": file_proj,
                "proj_override": self.proj,
                "netconvert_flags": list(flags),
            },
        )
        ensure_valid_map_package(pkg, context=str(self.xodr_file))
        warnings.extend(map_package_warnings(pkg))
        for warning in warnings:
            logger.warning("%s: %s", self.xodr_file, warning)
        pkg.metadata["conversion"] = conversion_summary(
            net_file,
            source_format="opendrive",
            warnings=warnings,
            tls_source=tls_source,
            proj_override=self.proj,
            conversion_seconds=round(time.monotonic() - started, 2),
            netconvert_warnings=netconvert_warning_summary(result.stderr),
        )
        pkg.save()
        return pkg


__all__ = [
    "JOIN_SAME_FLAGS",
    "NETCONVERT_OPENDRIVE_FLAGS",
    "TLS_GUESS_FLAGS",
    "OpenDriveMapProvider",
    "read_geo_reference",
    "write_georeferenced_copy",
]
