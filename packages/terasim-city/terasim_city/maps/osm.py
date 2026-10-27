"""Map provider that converts an OpenStreetMap extract with ``netconvert``.

The input is always a local ``.osm`` file: users submit map files through the
upload surface; the platform never downloads map data from the internet
itself.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from terasim_city.maps.base import MapPackage
from terasim_city.maps.summary import conversion_summary, netconvert_warning_summary
from terasim_city.maps.validate import (
    ensure_valid_map_package,
    map_package_warnings,
    net_is_geo_referenced,
    package_metadata,
)
from terasim_city.scenario.sumocfg_io import write_sumocfg

logger = logging.getLogger(__name__)

#: Doc-recommended netconvert flags for OSM import (mirrors SUMO's osmBuild
#: defaults; ``--tls.default-type static`` because the corridor pipeline
#: evaluates fixed-time plans).
NETCONVERT_OSM_FLAGS: tuple[str, ...] = (
    "--geometry.remove",
    "--junctions.join",
    "--tls.guess-signals",
    "--tls.discard-simple",
    "--ramps.guess",
    "--tls.default-type", "static",
    "--output.street-names",
)


class OsmMapProvider:
    """Convert a local ``.osm`` file into a geo-referenced :class:`MapPackage`."""

    name = "osm"

    def __init__(self, osm_file: Path | str) -> None:
        osm_file = Path(osm_file).resolve()
        if not osm_file.exists():
            raise ValueError(f"OSM file does not exist: {osm_file}")
        self.osm_file = osm_file

    def build(self, out_dir: Path) -> MapPackage:
        """Run netconvert into ``out_dir`` and assemble a validated package.

        Raises:
            MapConversionError: When netconvert exits non-zero (a RuntimeError).
            ValueError: When the converted net is not geo-referenced (should
                never happen for real OSM data) or fails package validation.
        """
        started = time.monotonic()
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        net_file = out_dir / "net.net.xml"
        cmd = [
            "netconvert",
            "--osm-files", str(self.osm_file),
            *NETCONVERT_OSM_FLAGS,
            "-o", str(net_file),
        ]
        result = _run_netconvert(cmd, input_file=self.osm_file)
        if not net_is_geo_referenced(net_file):
            raise ValueError(
                f"netconvert output {net_file} is not geo-referenced; the OSM "
                f"input {self.osm_file} is missing usable lat/lon coordinates"
            )

        base_sumocfg = write_base_sumocfg(out_dir, net_file)

        pkg = MapPackage(
            root=out_dir.resolve(),
            net_file=net_file.resolve(),
            base_sumocfg=base_sumocfg,
            tls_files=(),
            geo_referenced=True,
            source="osm",
            metadata={
                **package_metadata(self.osm_file, net_file),
                "osm_file": str(self.osm_file),
                "netconvert_flags": list(NETCONVERT_OSM_FLAGS),
            },
        )
        ensure_valid_map_package(pkg, context=str(self.osm_file))
        warnings = map_package_warnings(pkg)
        for warning in warnings:
            logger.warning("%s: %s", self.osm_file, warning)
        pkg.metadata["conversion"] = conversion_summary(
            net_file,
            source_format="osm",
            warnings=warnings,
            tls_source="osm",
            conversion_seconds=round(time.monotonic() - started, 2),
            netconvert_warnings=netconvert_warning_summary(result.stderr),
        )
        pkg.save()
        return pkg


def write_base_sumocfg(out_dir: Path, net_file: Path) -> Path:
    """Write ``empty.rou.xml`` + ``base.sumocfg`` next to ``net_file``; return the cfg.

    A base sumocfg needs a route-files entry to be runnable; routes are
    generated later by the TMC pipeline, so start from an empty file. Paths
    are written RELATIVE (bare filenames — everything sits in ``out_dir``) so
    the package directory can be moved/renamed as a unit; the map store relies
    on this when it commits a staging directory.
    """
    routes_file = Path(out_dir) / "empty.rou.xml"
    routes_file.write_text("<routes/>\n", encoding="utf-8")
    return write_sumocfg(
        Path(out_dir) / "base.sumocfg",
        net_file=Path(Path(net_file).name),
        route_file=Path(routes_file.name),
    )


def _netconvert_env() -> dict[str, str]:
    """Environment for netconvert with a usable (or absent) SUMO_HOME.

    netconvert resolves its typemaps via ``$SUMO_HOME/data/typemap``; a wrong
    SUMO_HOME (common with pyenv/pip SUMO installs) turns into a hard
    "Could not open types-file" error even though netconvert ships built-in
    typemaps. Drop the variable when the typemap directory is not actually
    there so the built-ins are used.
    """
    env = os.environ.copy()
    sumo_home = env.get("SUMO_HOME")
    if sumo_home and not (Path(sumo_home) / "data" / "typemap").is_dir():
        env.pop("SUMO_HOME")
    return env


class MapConversionError(RuntimeError):
    """The submitted map could not be turned into a usable SUMO net (details in the message)."""


#: Longest netconvert stderr excerpt carried in a :class:`MapConversionError`.
_STDERR_EXCERPT = 4000


def _run_netconvert(cmd: list[str], *, input_file: Path) -> subprocess.CompletedProcess:
    """Run netconvert; return the finished process (its stderr carries the warnings).

    Raises:
        MapConversionError: netconvert exited non-zero (or is not installed);
            the message ends with netconvert's error lines.
    """
    logger.info("running: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=_netconvert_env())
    except FileNotFoundError as exc:
        raise MapConversionError(
            f"netconvert is not installed or not on PATH (needed to convert {input_file})"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        errors = [line for line in stderr.splitlines() if line.startswith("Error")]
        excerpt = "\n".join(errors) if errors else stderr
        if len(excerpt) > _STDERR_EXCERPT:
            excerpt = excerpt[:_STDERR_EXCERPT] + "\n..."
        raise MapConversionError(
            f"netconvert failed (exit {result.returncode}) for {input_file}:\n{excerpt}"
        )
    return result


#: Upper bound for SUMO to load a converted net in :func:`sumo_load_problem`.
SUMO_LOAD_TIMEOUT_S = 900


def sumo_load_problem(net_file: Path) -> str | None:
    """Load ``net_file`` in SUMO for one step; return SUMO's errors, or None if it loads.

    netconvert can exit 0 and still write a net SUMO rejects (e.g. NaN junction
    shapes from degenerate source geometry), so HD-map providers check this
    before storing a package. Returns None — no verdict — when the ``sumo``
    binary is not installed (logged).
    """
    cmd = [
        "sumo", "--net-file", str(net_file), "--begin", "0", "--end", "1",
        "--no-step-log", "true", "--no-warnings", "true", "--duration-log.disable", "true",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=_netconvert_env(),
            timeout=SUMO_LOAD_TIMEOUT_S,
        )
    except FileNotFoundError:
        logger.warning("sumo is not on PATH; skipping the load check of %s", net_file)
        return None
    except subprocess.TimeoutExpired:
        return f"SUMO did not finish loading the net within {SUMO_LOAD_TIMEOUT_S} s"
    if result.returncode == 0:
        return None
    stderr = (result.stderr or "").strip()
    errors = [line for line in stderr.splitlines() if line.startswith("Error")]
    return "; ".join(errors[:3]) if errors else (stderr[-500:] or f"exit {result.returncode}")

