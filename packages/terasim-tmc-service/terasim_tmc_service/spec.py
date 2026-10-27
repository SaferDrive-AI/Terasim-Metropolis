"""Corridor spec: NVIDIA's ``Metadata.json`` contract (Headings / Movements / Assignments).

The spec is the only place that knows how OSM edges map to approach headings and how
(entry, exit) heading pairs map to turning movements, so it is loaded once and shared by
the registry (which intersections may be activated) and the generator (how to count).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from terasim_tmc_service.nv_generate_tmc import (
    get_assignment_sensors,
    get_intersections,
    parse_observation_interval,
)


@dataclass(frozen=True)
class IntersectionSpec:
    """One intersection: identity, location and the camera/edge mapping tables."""

    intersection_id: str
    lat: float
    lon: float
    headings: dict[str, str]
    movements: dict[str, str]
    assignments: dict[str, str]

    @property
    def sensors(self) -> list[str]:
        """Distinct sensor ids in Assignments order (NVIDIA semantics)."""
        return get_assignment_sensors(self.assignments)


@dataclass(frozen=True)
class CorridorSpec:
    """The whole spec file: one corridor with one or more intersections."""

    corridor_id: str
    observation_interval: str
    intersections: dict[str, IntersectionSpec]

    @property
    def observation_seconds(self) -> float:
        return parse_observation_interval(self.observation_interval).total_seconds()

    def ids(self) -> list[str]:
        return list(self.intersections)

    def get(self, intersection_id: str) -> IntersectionSpec:
        try:
            return self.intersections[intersection_id]
        except KeyError:
            raise KeyError(
                f"unknown intersection_id {intersection_id!r}; known ids: {self.ids()}"
            ) from None

    @classmethod
    def from_json(cls, path: Path | str) -> "CorridorSpec":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CorridorSpec":
        if not isinstance(payload, Mapping):
            raise ValueError(f"spec must be a JSON object, got {type(payload).__name__}")
        corridor_id = payload.get("corridor_id")
        if not isinstance(corridor_id, str) or not corridor_id.strip():
            raise ValueError("spec is missing a non-empty 'corridor_id'")
        observation_interval = payload.get("observation_interval")
        try:
            parse_observation_interval(observation_interval)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"spec has an invalid 'observation_interval': {exc}") from exc

        try:
            raw_intersections = get_intersections(dict(payload))
        except TypeError as exc:
            raise ValueError(str(exc)) from exc

        intersections: dict[str, IntersectionSpec] = {}
        for index, item in enumerate(raw_intersections, start=1):
            prefix = f"spec intersections[{index}]"
            intersection_id = item.get("intersection_id")
            if not isinstance(intersection_id, str) or not intersection_id.strip():
                raise ValueError(f"{prefix}: missing a non-empty 'intersection_id'")
            if intersection_id in intersections:
                raise ValueError(f"{prefix}: duplicate intersection_id {intersection_id!r}")
            try:
                lat = float(item["lat"])
                lon = float(item["lon"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{prefix}: 'lat'/'lon' must be numeric ({exc})") from exc
            tables: dict[str, dict[str, str]] = {}
            for key in ("Headings", "Movements", "Assignments"):
                table = item.get(key)
                if not isinstance(table, dict) or not table:
                    raise ValueError(f"{prefix}: '{key}' must be a non-empty object")
                tables[key] = {str(k): str(v) for k, v in table.items()}
            try:
                get_assignment_sensors(tables["Assignments"])
            except TypeError as exc:
                raise ValueError(f"{prefix}: Assignments: {exc}") from exc
            intersections[intersection_id] = IntersectionSpec(
                intersection_id=intersection_id,
                lat=lat,
                lon=lon,
                headings=tables["Headings"],
                movements=tables["Movements"],
                assignments=tables["Assignments"],
            )
        return cls(
            corridor_id=corridor_id,
            observation_interval=str(observation_interval),
            intersections=intersections,
        )


__all__ = ["CorridorSpec", "IntersectionSpec"]
