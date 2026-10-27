"""Intersection Registry: which spec intersections are active for periodic TMC generation.

Registration is spec-driven: the agent supplies an id, a display name and coordinates,
the registry checks the id exists in the corridor spec and that the coordinates sit
within ``REGISTRATION_RADIUS_M`` of the spec's own location (wrong-intersection guard).
Records persist to a JSON file so restarts keep the active set.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from terasim_tmc_service.spec import CorridorSpec

REGISTRATION_RADIUS_M = 50.0
_EARTH_RADIUS_M = 6_371_000.0


class UnknownIntersectionError(KeyError):
    """The intersection id is not part of the corridor spec."""

    def __str__(self) -> str:  # KeyError.__str__ would repr-quote the message
        return str(self.args[0]) if self.args else ""


class IntersectionTooFarError(ValueError):
    """The supplied coordinates do not match the spec location."""


class CorridorMismatchError(ValueError):
    """The supplied corridor id differs from the spec corridor."""


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class RegisteredIntersection:
    intersection_id: str
    name: str
    lat: float
    lon: float
    corridor_id: str
    sensors: list[str]
    registered_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IntersectionRegistry:
    """Thread-safe in-memory registry with optional JSON persistence."""

    def __init__(
        self,
        spec: CorridorSpec,
        path: Path | str | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._spec = spec
        self._path = Path(path) if path is not None else None
        self._clock = clock or _utc_now
        self._lock = threading.Lock()
        self._records: dict[str, RegisteredIntersection] = {}
        self._load()

    # -- queries -------------------------------------------------------------

    def get(self, intersection_id: str) -> RegisteredIntersection | None:
        with self._lock:
            return self._records.get(intersection_id)

    def list(self) -> list[RegisteredIntersection]:
        with self._lock:
            return list(self._records.values())

    def registered_ids(self) -> list[str]:
        with self._lock:
            return list(self._records)

    # -- mutation ------------------------------------------------------------

    def register(
        self,
        intersection_id: str,
        name: str,
        lat: float,
        lon: float,
        corridor_id: str | None = None,
    ) -> RegisteredIntersection:
        try:
            intersection = self._spec.get(intersection_id)
        except KeyError as exc:
            raise UnknownIntersectionError(exc.args[0] if exc.args else str(exc)) from None
        if corridor_id is not None and corridor_id != self._spec.corridor_id:
            raise CorridorMismatchError(
                f"corridor_id {corridor_id!r} does not match the spec corridor "
                f"{self._spec.corridor_id!r}"
            )
        distance = distance_m(float(lat), float(lon), intersection.lat, intersection.lon)
        if distance > REGISTRATION_RADIUS_M:
            raise IntersectionTooFarError(
                f"({lat}, {lon}) is {distance:.0f} m from the spec location of "
                f"{intersection_id!r} ({intersection.lat}, {intersection.lon}); "
                f"limit is {REGISTRATION_RADIUS_M:.0f} m"
            )
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        record = RegisteredIntersection(
            intersection_id=intersection_id,
            name=name,
            lat=float(lat),
            lon=float(lon),
            corridor_id=self._spec.corridor_id,
            sensors=intersection.sensors,
            registered_at=self._clock().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )
        with self._lock:
            self._records[intersection_id] = record
            self._save()
        return record

    # -- persistence ---------------------------------------------------------

    def _save(self) -> None:
        if self._path is None:
            return
        payload = {"intersections": [record.to_dict() for record in self._records.values()]}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        for item in payload.get("intersections", []):
            intersection_id = item.get("intersection_id")
            if intersection_id not in self._spec.intersections:
                continue  # spec changed since the file was written; drop silently
            self._records[intersection_id] = RegisteredIntersection(
                intersection_id=intersection_id,
                name=str(item.get("name", intersection_id)),
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                corridor_id=self._spec.corridor_id,
                sensors=self._spec.get(intersection_id).sensors,
                registered_at=str(item.get("registered_at", "")),
            )


__all__ = [
    "REGISTRATION_RADIUS_M",
    "CorridorMismatchError",
    "IntersectionRegistry",
    "IntersectionTooFarError",
    "RegisteredIntersection",
    "UnknownIntersectionError",
    "distance_m",
]
