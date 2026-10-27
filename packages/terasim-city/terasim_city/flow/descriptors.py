"""Data structures shared across the flow pipeline.

These are intentionally simple frozen dataclasses so they can be passed across
module boundaries without coupling to any specific implementation. They are the
"narrow waist" of the package: every flow source (TMC, OD matrix, etc.) ends up
producing a list of :class:`FlowDescriptor`, which the writer then serializes to
SUMO ``rou.xml``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

#: Direction names accepted from the external turn-movement-count contract.
TMC_DIRECTIONS: tuple[str, ...] = ("South", "West", "North", "East")

#: Turn names accepted from the external turn-movement-count contract.
TMC_TURNS: tuple[str, ...] = ("Right", "Straight", "Left")

#: Stable travel-direction codes used by routing and signal-control internals.
CANONICAL_DIRECTIONS: tuple[str, ...] = ("EB", "WB", "NB", "SB")

#: Stable turn labels used by routing and signal-control internals.
CANONICAL_TURNS: tuple[str, ...] = ("left", "through", "right")

TMC_DIRECTION_TO_CANONICAL: dict[str, str] = {
    "South": "SB",
    "West": "WB",
    "North": "NB",
    "East": "EB",
}

TMC_TURN_TO_CANONICAL: dict[str, str] = {
    "Right": "right",
    "Straight": "through",
    "Left": "left",
}


# ---------------------------------------------------------------------------
# Input descriptors — what we ingest from the data source
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TMCIntersection:
    """A single intersection's normalized turn-movement counts.

    Attributes:
        intersection_id: Stable string identifier (also used in logs / metadata).
        lat: Latitude in WGS84 degrees.
        lon: Longitude in WGS84 degrees.
        counts: Nested dict ``{direction: {turn: count}}`` where ``direction`` is
            one of ``CANONICAL_DIRECTIONS`` and ``turn`` is one of
            ``CANONICAL_TURNS``. Values are integer vehicle counts over the
            observation window.
    """

    intersection_id: str
    lat: float
    lon: float
    counts: dict[str, dict[str, int]]


@dataclass(frozen=True)
class TMCData:
    """A complete turn-movement-count observation payload.

    The external schema uses full direction names (``East``) and turn names
    (``Straight``). Construction normalizes them once to the routing model's
    stable codes (``EB`` and ``through``).
    """

    corridor_id: str
    observation_interval: str
    timestamp_start: str
    intersections: tuple[TMCIntersection, ...]
    # Preserve the raw payload so downstream code can inspect optional fields
    # (e.g. ``schema_version``, ``bucket_seconds``, ``window``) without forcing
    # us to mirror every TMC field as a dataclass attribute.
    raw: dict = field(default_factory=dict, repr=False)

    # -- Construction ------------------------------------------------------

    @classmethod
    def from_json(cls, path: Path | str) -> "TMCData":
        """Load a TMC payload from a JSON file."""
        path = Path(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_payload(payload, source_path=path)

    @classmethod
    def from_payload(
        cls, payload: dict, *, source_path: Path | str = "<payload>"
    ) -> "TMCData":
        """Parse an in-memory TMC payload.

        Args:
            payload: Decoded JSON object conforming to the TMC schema.
            source_path: Label included in validation errors.

        Returns:
            A :class:`TMCData` instance with all intersections parsed.

        Raises:
            ValueError: If the payload is missing required top-level fields
                (``corridor_id``, ``observation_interval``, ``intersections``)
                or any intersection record is malformed.
        """
        if not isinstance(payload, dict):
            raise ValueError(
                f"TMC payload at {source_path!s}: expected an object, "
                f"got {type(payload).__name__}."
            )

        # ---- Required top-level fields -----------------------------------
        # Fail fast rather than silently defaulting — empty or malformed
        # payloads otherwise produce empty scenarios that are hard to debug.
        for required in ("corridor_id", "observation_interval", "intersections"):
            if required not in payload:
                raise ValueError(
                    f"TMC payload at {source_path!s} is missing required field "
                    f"{required!r}."
                )

        raw_intersections = payload["intersections"]
        if not isinstance(raw_intersections, list):
            raise ValueError(
                f"TMC payload at {source_path!s}: 'intersections' must be a list, "
                f"got {type(raw_intersections).__name__}."
            )
        if not raw_intersections:
            raise ValueError(
                f"TMC payload at {source_path!s}: 'intersections' is empty. "
                "Refusing to build an empty scenario."
            )

        # ---- Per-intersection parsing -----------------------------------
        # Each intersection is validated explicitly so malformed payloads
        # surface as ValueError early, instead of producing silently empty
        # scenarios or leaking AttributeError / KeyError to callers.
        intersections = tuple(
            cls._parse_intersection(item, index=i, source_path=source_path)
            for i, item in enumerate(raw_intersections)
        )

        return cls(
            corridor_id=str(payload["corridor_id"]),
            observation_interval=str(payload["observation_interval"]),
            timestamp_start=str(payload.get("timestamp_start", "")),
            intersections=intersections,
            raw=payload,
        )

    # -- Validation helpers ------------------------------------------------

    @classmethod
    def _parse_intersection(
        cls, item, *, index: int, source_path: Path | str
    ) -> "TMCIntersection":
        """Parse a single intersection record with strict schema validation.

        Raises:
            ValueError: If any required field is missing or has the wrong
                type. ``counts`` must be a ``dict``, each direction value
                must be a ``dict``, and each turn count must be int-coercible.
        """
        prefix = f"TMC payload at {source_path!s} intersection[{index}]"

        if not isinstance(item, dict):
            raise ValueError(
                f"{prefix}: expected an object, got {type(item).__name__}."
            )

        # Required scalar fields.
        for required in ("intersection_id", "lat", "lon"):
            if required not in item:
                raise ValueError(f"{prefix}: missing required field {required!r}.")
        try:
            iid = str(item["intersection_id"])
            lat = float(item["lat"])
            lon = float(item["lon"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{prefix}: lat/lon must be numeric and intersection_id "
                f"must be string-coercible ({exc})."
            ) from exc

        # ``counts`` must exist and be a dict (an empty dict is fine — it just
        # means this camera observed nothing during the window).
        if "counts" not in item:
            raise ValueError(f"{prefix}: missing required field 'counts'.")
        raw_counts = item["counts"]
        if not isinstance(raw_counts, dict):
            raise ValueError(
                f"{prefix}: 'counts' must be a dict mapping direction → "
                f"turn-counts, got {type(raw_counts).__name__}."
            )

        # Each direction's turn-count payload must also be a dict.
        counts: dict[str, dict[str, int]] = {}
        for direction, turns in raw_counts.items():
            direction = str(direction)
            try:
                canonical_direction = TMC_DIRECTION_TO_CANONICAL[direction]
            except KeyError:
                raise ValueError(
                    f"{prefix}: unknown direction {direction!r}; expected one of "
                    f"{list(TMC_DIRECTIONS)}."
                ) from None
            if not isinstance(turns, dict):
                raise ValueError(
                    f"{prefix}: counts[{direction!r}] must be a dict "
                    f"mapping turn → count, got {type(turns).__name__}."
                )
            normalised: dict[str, int] = {}
            for turn, turn_count in turns.items():
                turn = str(turn)
                try:
                    canonical_turn = TMC_TURN_TO_CANONICAL[turn]
                except KeyError:
                    raise ValueError(
                        f"{prefix}: unknown turn {turn!r} for direction "
                        f"{direction!r}; expected one of {list(TMC_TURNS)}."
                    ) from None
                try:
                    normalised[canonical_turn] = int(turn_count)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{prefix}: counts[{direction!r}][{turn!r}] must "
                        f"be int-coercible, got {turn_count!r} ({exc})."
                    ) from exc
            counts[canonical_direction] = normalised

        return TMCIntersection(
            intersection_id=iid,
            lat=lat,
            lon=lon,
            counts=counts,
        )

    # -- Helpers -----------------------------------------------------------

    def observation_seconds(self) -> float:
        """Translate ``observation_interval`` (e.g. ``"1h"``) to seconds.

        Supported suffixes are ``h`` (hours), ``m``/``min`` (minutes), and ``s``
        (seconds). The numeric prefix may include a decimal point. Unknown
        formats raise ``ValueError`` rather than silently default — picking the
        wrong observation window scales the flow rates incorrectly.
        """
        text = self.observation_interval.strip().lower()
        match = re.match(
            r"^([0-9]+(?:\.[0-9]+)?)\s*"
            r"(h|min|m|s|sec|hour|hours|minute|minutes|seconds)$",
            text,
        )
        if not match:
            raise ValueError(
                f"Cannot parse observation_interval={self.observation_interval!r}. "
                "Expected e.g. '1h', '10m', '30min', '60s'."
            )
        value = float(match.group(1))
        unit = match.group(2)
        if unit.startswith("h"):
            return value * 3600.0
        if unit == "m" or unit.startswith("min") or unit.startswith("minute"):
            return value * 60.0
        return value  # seconds


# ---------------------------------------------------------------------------
# Output descriptors — what flows downstream
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FlowDescriptor:
    """A single SUMO ``<flow>`` definition in dataclass form.

    Every flow source (TMC, OD matrix, etc.) ultimately produces a list of
    these. The writer module turns the list into XML.

    Attributes:
        flow_id: The ``id`` attribute on the resulting ``<flow>``.
        route_id: The ``id`` attribute on the corresponding ``<route>``.
        route_edges: Ordered tuple of SUMO edge IDs that the route traverses.
        vehs_per_hour: Vehicle departure rate (already scaled to hourly units).
        vehicle_type: SUMO ``<vType>`` reference used by the flow.
        begin: Earliest simulation time (seconds) at which a vehicle may depart.
        end: Latest simulation time (seconds). ``None`` lets SUMO use the
            simulation end time configured elsewhere.
        depart_lane: Lane selection strategy for the spawn point.
        depart_speed: Speed selection strategy for the spawn point.
        source_metadata: Free-form provenance data (e.g. which TMC movement
            produced this flow). Written into ``metadata.json`` for audit, never
            into the SUMO XML itself.
    """

    flow_id: str
    route_id: str
    route_edges: tuple[str, ...]
    vehs_per_hour: float
    vehicle_type: str = "car_normal"
    begin: float = 0.0
    end: float | None = None
    depart_lane: str = "best"
    depart_speed: str = "max"
    source_metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Output descriptors — what we return to the caller
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GeneratedScenario:
    """Result of running the end-to-end pipeline.

    Returned by :func:`terasim_city.api.run_tmc_pipeline`. All paths point
    to files that already exist on disk by the time this object is constructed.
    """

    output_dir: Path
    routes_file: Path
    sumocfg_file: Path
    metadata_file: Path
    net_file: Path
    n_flows: int
    total_vehs_per_hour: float
