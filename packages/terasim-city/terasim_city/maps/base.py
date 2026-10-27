"""Map-package contract: a self-describing bundle of SUMO network inputs.

A :class:`MapPackage` is what every map provider (prebuilt sumocfg, OSM /
OpenDRIVE / Lanelet2 conversion) produces, and what the corridor pipeline
consumes. The JSON manifest stores every path relative to ``root`` (and
``root`` relative to the manifest file itself), so a package directory can be
relocated as a unit without breaking, while still supporting the prebuilt case
where the manifest lives outside the original map directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

MANIFEST_NAME = "map_package.json"
MAP_PACKAGE_SCHEMA = "terasim.map_package.v1"

#: Legal values for :attr:`MapPackage.source`: a prebuilt SUMO scenario, or the
#: SUMO conversion of an OpenStreetMap / OpenDRIVE / Lanelet2 source map.
MAP_SOURCES: tuple[str, ...] = ("prebuilt", "osm", "opendrive", "lanelet2")


@dataclass(frozen=True)
class MapPackage:
    """SUMO map inputs plus the metadata the corridor pipeline needs.

    ``tls_files`` are the additional files carrying candidate TLS programs.
    ``geo_referenced`` records whether the net has a real projection (lat/lon
    matching only works when True).
    """

    root: Path
    net_file: Path
    base_sumocfg: Path
    tls_files: tuple[Path, ...]
    geo_referenced: bool
    source: str
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "net_file", Path(self.net_file))
        object.__setattr__(self, "base_sumocfg", Path(self.base_sumocfg))
        object.__setattr__(self, "tls_files", tuple(Path(p) for p in self.tls_files))
        object.__setattr__(self, "geo_referenced", bool(self.geo_referenced))
        if self.source not in MAP_SOURCES:
            raise ValueError(
                f"MapPackage.source must be one of {MAP_SOURCES}, got {self.source!r}"
            )

    def save(self, path: Path | None = None) -> Path:
        """Write the JSON manifest; default location is ``root/map_package.json``.

        Paths are stored relative to ``root``; ``root`` itself is stored
        relative to the manifest's directory so the manifest may live outside
        the map directory (prebuilt provider) and both remain relocatable
        together.
        """
        manifest_path = Path(path) if path is not None else self.root / MANIFEST_NAME
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        root = self.root.resolve()
        payload = {
            "schema": MAP_PACKAGE_SCHEMA,
            "root": os.path.relpath(root, manifest_path.resolve().parent),
            "net_file": _rel_to_root(self.net_file, root),
            "base_sumocfg": _rel_to_root(self.base_sumocfg, root),
            "tls_files": [_rel_to_root(p, root) for p in self.tls_files],
            "geo_referenced": self.geo_referenced,
            "source": self.source,
            "metadata": self.metadata,
        }
        # Atomic write: the manifest doubles as the "package is complete"
        # marker (the map store treats its presence as commit), so it must
        # never be observable half-written.
        tmp_path = manifest_path.with_name(manifest_path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, manifest_path)
        return manifest_path.resolve()

    @classmethod
    def load(cls, directory: Path | str) -> "MapPackage":
        """Load the manifest from ``directory/map_package.json``, failing loud.

        Raises:
            FileNotFoundError: If the manifest file is absent.
            ValueError: On schema mismatch, missing manifest keys, or when a
                referenced net/sumocfg file does not exist on disk.
        """
        directory = Path(directory)
        manifest_path = directory / MANIFEST_NAME
        if not manifest_path.exists():
            raise FileNotFoundError(f"map package manifest not found: {manifest_path}")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(
                f"{manifest_path}: expected a JSON object, got {type(payload).__name__}"
            )
        schema = payload.get("schema")
        if schema != MAP_PACKAGE_SCHEMA:
            raise ValueError(
                f"{manifest_path}: schema is {schema!r}, expected {MAP_PACKAGE_SCHEMA!r}"
            )
        missing = [
            key
            for key in ("root", "net_file", "base_sumocfg", "tls_files", "geo_referenced", "source")
            if key not in payload
        ]
        if missing:
            raise ValueError(f"{manifest_path}: missing required key(s): {missing}")

        root = (manifest_path.parent / payload["root"]).resolve()
        net_file = (root / payload["net_file"]).resolve()
        base_sumocfg = (root / payload["base_sumocfg"]).resolve()
        tls_files = tuple((root / p).resolve() for p in payload["tls_files"])
        for label, referenced in (("net_file", net_file), ("base_sumocfg", base_sumocfg)):
            if not referenced.exists():
                raise ValueError(
                    f"{manifest_path}: {label} {referenced} does not exist; "
                    "was the map package moved without its files?"
                )
        return cls(
            root=root,
            net_file=net_file,
            base_sumocfg=base_sumocfg,
            tls_files=tls_files,
            geo_referenced=payload["geo_referenced"],
            source=str(payload["source"]),
            metadata=dict(payload.get("metadata", {})),
        )


@runtime_checkable
class MapProvider(Protocol):
    """Anything that can produce a validated :class:`MapPackage`."""

    name: str

    def build(self, out_dir: Path) -> MapPackage: ...


def _rel_to_root(path: Path, root: Path) -> str:
    """Store a manifest path relative to ``root`` (works across directories)."""
    return os.path.relpath(Path(path).resolve(), root)
