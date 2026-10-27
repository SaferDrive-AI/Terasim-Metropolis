"""Map provider for a pre-existing SUMO scenario (sumocfg + net + tls files).

The prebuilt provider copies NOTHING: the package root stays the sumocfg's
own directory and only the ``map_package.json`` manifest is written into the
requested output directory (the manifest stores ``root`` relative to itself,
see :mod:`terasim_city.maps.base`).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from terasim_city.maps.base import MANIFEST_NAME, MapPackage
from terasim_city.maps.validate import (
    ensure_valid_map_package,
    file_defines_tls_program,
    map_package_warnings,
    net_is_geo_referenced,
    package_metadata,
)
from terasim_city.scenario.sumocfg_io import parse_sumocfg

logger = logging.getLogger(__name__)


class PrebuiltSumoProvider:
    """Wrap an existing base sumocfg into a validated :class:`MapPackage`."""

    name = "prebuilt"

    def __init__(self, base_sumocfg: Path | str) -> None:
        base_sumocfg = Path(base_sumocfg).resolve()
        if not base_sumocfg.exists():
            raise ValueError(f"base sumocfg does not exist: {base_sumocfg}")
        self.base_sumocfg = base_sumocfg

    def build(self, out_dir: Path) -> MapPackage:
        """Discover net/tls files from the sumocfg; write the manifest to ``out_dir``.

        Raises:
            ValueError: When the sumocfg's net file is missing, or the
                resulting package fails :func:`validate_map_package` (e.g. the
                net is not parseable XML). A map without any traffic-light
                program is NOT fatal — it is built with
                ``metadata["has_tls"] = False`` and a logged warning.
        """
        info = parse_sumocfg(self.base_sumocfg)
        if not info.net_file.exists():
            raise ValueError(
                f"{self.base_sumocfg}: declared net-file {info.net_file} does not "
                "exist; fix the sumocfg's <input><net-file> before building"
            )
        # TLS files = the additional files that actually carry TLS programs
        # (a view/detector additional file is not a TLS file).
        tls_files = tuple(
            p for p in info.additional_files if file_defines_tls_program(p)
        )
        root = self.base_sumocfg.parent
        pkg = MapPackage(
            root=root,
            net_file=info.net_file,
            base_sumocfg=self.base_sumocfg,
            tls_files=tls_files,
            geo_referenced=net_is_geo_referenced(info.net_file),
            source="prebuilt",
            metadata={
                **package_metadata(self.base_sumocfg, info.net_file, tls_files),
                "additional_files": [
                    os.path.relpath(p, root) for p in info.additional_files
                ],
            },
        )
        ensure_valid_map_package(pkg, context=str(self.base_sumocfg))
        for warning in map_package_warnings(pkg):
            logger.warning("%s: %s", self.base_sumocfg, warning)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        pkg.save(out_dir / MANIFEST_NAME)
        return pkg
