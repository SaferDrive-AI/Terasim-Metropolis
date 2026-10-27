"""Typed inputs and outputs of a Traffic Operation plug-in.

A request bundles what every operations module needs:

* :class:`MapInput` -- the road network: a SUMO ``.sumocfg``, or a map package
  the map adapter (``terasim_city.maps.MapStore``) built from an OpenStreetMap,
  OpenDRIVE, Lanelet2 or prebuilt SUMO map;
* :class:`TrafficSnapshot` -- the turning-movement counts (TMC JSON);
* :class:`CurrentPlan` -- the timing running today (a timing sheet, a NEMA plan
  document, a plan store, or the map's own signal programs), optional;
* :class:`KpiWeights` -- the weights of the KPI cost the caller wants minimized;
* :class:`CorridorSelection` -- which signals form the corridor;

plus the run settings (simulated horizon, seed, worker processes, vehicle
behavior, plugin-specific ``params``) and the output directory.

A plugin answers with an :class:`OperationResult`: the recommended plan as NEMA
timing (``terasim.nema_timing.v1``), the KPI comparison in the same shape as the
HTTP service's ``kpis`` block, and the artifacts it wrote.

All types are frozen dataclasses that validate on construction (raising
:class:`~terasim_plugin_sdk.errors.PluginInputError`) and round-trip through
plain JSON with ``to_dict`` / ``from_dict``.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from terasim_plugin_sdk.constants import (
    API_VERSION,
    DEFAULT_KPI_WEIGHTS,
    DEFAULT_N_WORKERS,
    DEFAULT_SEED,
    DEFAULT_SIM_END,
    DEFAULT_VEHICLE_BEHAVIOR,
    INPUTS_DIRNAME,
    NEMA_PLAN_SCHEMA,
    REQUEST_SCHEMA,
    RESULT_FILENAME,
    RESULT_SCHEMA,
    TIMING_SHEET_SCHEMA,
)
from terasim_plugin_sdk.errors import PluginInputError

__all__ = [
    "PLAN_KINDS",
    "CorridorSelection",
    "CurrentPlan",
    "KpiWeights",
    "MapInput",
    "OperationRequest",
    "OperationResult",
    "TrafficSnapshot",
]

#: The kinds of current plan a request can carry (see :class:`CurrentPlan`).
PLAN_KINDS: tuple[str, ...] = ("timing_sheet", "nema_plan", "plan_store", "scenario_programs")

#: Name of the TMC file written for an in-memory snapshot.
_TMC_PAYLOAD_FILENAME = "tmc.json"


def _optional_path(value) -> "Path | None":
    return None if value is None else Path(value)


def _absolute(path: "Path | None") -> "Path | None":
    return None if path is None else Path(os.path.abspath(path))


def _json_copy(value, *, what: str):
    """A deep, JSON-compatible copy of ``value`` (finite numbers only)."""
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise PluginInputError(
            f"{what} must be JSON-compatible with finite numbers: {exc}"
        ) from None


def _require_str(value, *, what: str, allow_none: bool = False) -> "str | None":
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PluginInputError(f"{what} must be a non-empty string, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MapInput:
    """The road network to simulate.

    Three forms (use the constructors):

    * :meth:`from_sumocfg` -- a SUMO ``.sumocfg`` file (net, additional files);
    * :meth:`from_store` with a ``map_id`` -- a map package stored by the map
      adapter, e.g. an HD map (OpenDRIVE ``.xodr``, Lanelet2 ``.osm``) that
      :func:`terasim_plugin_sdk.toolkit.ingest_map` or the service's map upload
      converted to SUMO;
    * :meth:`from_store` without a ``map_id`` -- the stored map that covers every
      TMC intersection, chosen by the core's rule (HD maps first, then prebuilt
      scenarios, then OpenStreetMap).

    ``data_root`` is the asset volume holding the map store; ``None`` means
    ``$TERASIM_CITY_DATA_ROOT``.
    """

    sumocfg: "Path | None" = None
    map_id: "str | None" = None
    data_root: "Path | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sumocfg", _optional_path(self.sumocfg))
        object.__setattr__(self, "data_root", _optional_path(self.data_root))
        _require_str(self.map_id, what="MapInput.map_id", allow_none=True)
        if self.sumocfg is not None and (self.map_id is not None or self.data_root is not None):
            raise PluginInputError(
                "MapInput takes either a sumocfg file or a stored map (map_id / data_root), "
                "not both"
            )

    @classmethod
    def from_sumocfg(cls, path) -> "MapInput":
        """A SUMO scenario file (``.sumocfg``)."""
        return cls(sumocfg=Path(path))

    @classmethod
    def from_store(cls, map_id: "str | None" = None, *, data_root=None) -> "MapInput":
        """A stored map package by id, or (``map_id=None``) the one covering the corridor."""
        return cls(map_id=map_id, data_root=_optional_path(data_root))

    @property
    def kind(self) -> str:
        """``"sumocfg"``, ``"store"`` (by map id) or ``"auto"`` (covering stored map)."""
        if self.sumocfg is not None:
            return "sumocfg"
        return "store" if self.map_id is not None else "auto"

    def describe(self) -> str:
        if self.kind == "sumocfg":
            return f"sumocfg {self.sumocfg}"
        root = self.data_root or "$TERASIM_CITY_DATA_ROOT"
        if self.kind == "store":
            return f"stored map {self.map_id!r} in {root}"
        return f"the stored map covering the corridor in {root}"

    def to_dict(self) -> dict:
        return {
            "sumocfg": None if self.sumocfg is None else str(self.sumocfg),
            "map_id": self.map_id,
            "data_root": None if self.data_root is None else str(self.data_root),
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "MapInput":
        payload = _mapping(payload, what="map")
        return cls(
            sumocfg=payload.get("sumocfg"),
            map_id=payload.get("map_id"),
            data_root=payload.get("data_root"),
        )


@dataclass(frozen=True)
class TrafficSnapshot:
    """Turning-movement counts for the corridor: a TMC JSON file or an in-memory payload.

    The payload follows the core's TMC contract (``terasim_city.flow.descriptors.TMCData``)::

        {"corridor_id": "demo_arterial",
         "observation_interval": "1h",            # "10m", "15m", "1h", "60s", ...
         "timestamp_start": "2026-07-01T08:00:00",
         "intersections": [
           {"intersection_id": "i1", "lat": 37.335, "lon": -121.90339,
            "counts": {"East": {"Left": 60, "Straight": 560, "Right": 90},
                       "West": {...}, "North": {...}, "South": {...}}}]}
    """

    path: "Path | None" = None
    payload: "Mapping[str, Any] | None" = field(default=None, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _optional_path(self.path))
        if (self.path is None) == (self.payload is None):
            raise PluginInputError("TrafficSnapshot takes exactly one of path / payload")
        if self.payload is not None:
            if not isinstance(self.payload, Mapping):
                raise PluginInputError(
                    "TrafficSnapshot.payload must be a JSON object, "
                    f"got {type(self.payload).__name__}"
                )
            object.__setattr__(self, "payload", _json_copy(dict(self.payload), what="TMC payload"))

    @classmethod
    def from_file(cls, path) -> "TrafficSnapshot":
        return cls(path=Path(path))

    @classmethod
    def from_payload(cls, payload: Mapping) -> "TrafficSnapshot":
        return cls(payload=payload)

    def load(self):
        """Parse and validate the counts as the core's ``TMCData`` (PluginInputError if invalid)."""
        from terasim_city.flow.descriptors import TMCData

        try:
            if self.path is not None:
                if not self.path.is_file():
                    raise PluginInputError(f"TMC file not found: {self.path}")
                return TMCData.from_json(self.path)
            return TMCData.from_payload(dict(self.payload), source_path="<request payload>")
        except PluginInputError:
            raise
        except (OSError, ValueError) as exc:
            raise PluginInputError(f"invalid TMC data: {exc}") from exc

    def write_to(self, directory) -> Path:
        """The TMC file to run on: the given file, or the payload written into ``directory``."""
        if self.path is not None:
            if not self.path.is_file():
                raise PluginInputError(f"TMC file not found: {self.path}")
            return self.path
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / _TMC_PAYLOAD_FILENAME
        target.write_text(json.dumps(self.payload, indent=2) + "\n", encoding="utf-8")
        return target

    def to_dict(self) -> dict:
        return {
            "path": None if self.path is None else str(self.path),
            "payload": None if self.payload is None else dict(self.payload),
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "TrafficSnapshot":
        payload = _mapping(payload, what="traffic")
        return cls(path=payload.get("path"), payload=payload.get("payload"))


@dataclass(frozen=True)
class CurrentPlan:
    """The signal timing running today, which the operation is compared against.

    ``kind`` is one of :data:`PLAN_KINDS`:

    * ``"timing_sheet"`` -- ``path`` is a ``terasim.timing_sheet.v1`` JSON file
      (stages, splits, offsets against one corridor cycle);
    * ``"nema_plan"`` -- ``path`` is a ``terasim.nema_plan.v1`` document (rings,
      phases, time-of-day patterns and schedule per intersection); the SDK stores
      it in a plan store under the output directory;
    * ``"plan_store"`` -- ``path`` is an asset volume whose plan store already
      holds uploaded ``terasim.nema_plan.v1`` documents (``$TERASIM_CITY_DATA_ROOT``);
    * ``"scenario_programs"`` -- the map's own ``<tlLogic>`` programs; no path.

    For the two NEMA kinds, ``plan_time`` (ISO 8601) selects each intersection's
    time-of-day pattern; ``None`` means the TMC ``timestamp_start``. A NEMA plan
    that does not cover every corridor signal at that time is not used: the
    operation falls back to its reference plan and says so in the result's notes.
    """

    kind: str
    path: "Path | None" = None
    plan_time: "str | None" = None

    def __post_init__(self) -> None:
        if self.kind not in PLAN_KINDS:
            raise PluginInputError(
                f"CurrentPlan.kind must be one of {PLAN_KINDS}, got {self.kind!r}"
            )
        object.__setattr__(self, "path", _optional_path(self.path))
        if self.kind == "scenario_programs":
            if self.path is not None:
                raise PluginInputError("CurrentPlan 'scenario_programs' takes no path")
        elif self.path is None:
            raise PluginInputError(f"CurrentPlan {self.kind!r} needs a path")
        if self.plan_time is not None:
            if self.kind not in ("nema_plan", "plan_store"):
                raise PluginInputError(
                    "CurrentPlan.plan_time applies to the NEMA kinds "
                    "('nema_plan', 'plan_store') only"
                )
            _require_str(self.plan_time, what="CurrentPlan.plan_time")
            try:
                datetime.fromisoformat(self.plan_time.replace("Z", "+00:00"))
            except ValueError:
                raise PluginInputError(
                    f"CurrentPlan.plan_time must be an ISO 8601 timestamp, got {self.plan_time!r}"
                ) from None

    @classmethod
    def timing_sheet(cls, path) -> "CurrentPlan":
        return cls("timing_sheet", Path(path))

    @classmethod
    def nema_plan(cls, path, *, plan_time: "str | None" = None) -> "CurrentPlan":
        return cls("nema_plan", Path(path), plan_time)

    @classmethod
    def plan_store(cls, data_root, *, plan_time: "str | None" = None) -> "CurrentPlan":
        return cls("plan_store", Path(data_root), plan_time)

    @classmethod
    def scenario_programs(cls) -> "CurrentPlan":
        return cls("scenario_programs")

    @classmethod
    def detect(cls, value, *, plan_time: "str | None" = None) -> "CurrentPlan":
        """Build a current plan from a CLI-style value.

        ``"scenario"`` (or ``"scenario_programs"``) -> the map's own programs; a
        directory -> a plan store; a JSON file -> a timing sheet or a NEMA plan
        document, told apart by its ``schema`` field.
        """
        text = str(value)
        if text in ("scenario", "scenario_programs"):
            if plan_time is not None:
                raise PluginInputError("plan_time applies to NEMA plans only")
            return cls.scenario_programs()
        path = Path(text)
        if path.is_dir():
            return cls.plan_store(path, plan_time=plan_time)
        if not path.is_file():
            raise PluginInputError(f"current plan not found: {path}")
        try:
            schema = json.loads(path.read_text(encoding="utf-8")).get("schema")
        except (OSError, ValueError, AttributeError) as exc:
            raise PluginInputError(f"current plan {path} is not a JSON object: {exc}") from None
        if schema == TIMING_SHEET_SCHEMA:
            if plan_time is not None:
                raise PluginInputError("plan_time applies to NEMA plans only")
            return cls.timing_sheet(path)
        if schema == NEMA_PLAN_SCHEMA:
            return cls.nema_plan(path, plan_time=plan_time)
        raise PluginInputError(
            f"current plan {path} has schema {schema!r}; expected {TIMING_SHEET_SCHEMA!r} "
            f"or {NEMA_PLAN_SCHEMA!r}"
        )

    def check(self) -> None:
        """Raise PluginInputError unless the referenced file or directory exists."""
        if self.kind in ("timing_sheet", "nema_plan") and not self.path.is_file():
            raise PluginInputError(f"current plan ({self.kind}) file not found: {self.path}")
        if self.kind == "plan_store" and not self.path.is_dir():
            raise PluginInputError(f"current plan store directory not found: {self.path}")

    def describe(self) -> str:
        if self.kind == "scenario_programs":
            return "the map's own signal programs"
        when = f" at {self.plan_time}" if self.plan_time else ""
        return f"{self.kind} {self.path}{when}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "path": None if self.path is None else str(self.path),
            "plan_time": self.plan_time,
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "CurrentPlan":
        payload = _mapping(payload, what="current_plan")
        return cls(
            kind=payload.get("kind"), path=payload.get("path"), plan_time=payload.get("plan_time")
        )


@dataclass(frozen=True)
class KpiWeights:
    """Weights of the KPI cost, as the core's ``KpiConfig`` and the service's ``cost.weights``.

    Keys: ``"control_delay"`` (weights ``control_delay_s_per_veh``) and ``"stops"``
    (weights ``stops_per_veh``); at least one, each a finite number > 0. The cost
    of a plan is ``sum(w * metric / baseline_metric)``: lower is better and the
    baseline scores about the sum of the weights. Accepts a mapping or
    ``(key, weight)`` pairs.
    """

    weights: "tuple[tuple[str, float], ...] | Mapping[str, float]" = DEFAULT_KPI_WEIGHTS

    def __post_init__(self) -> None:
        from terasim_city.evaluation.moe import KpiConfig

        raw = self.weights
        if isinstance(raw, Mapping):
            pairs = list(raw.items())
        else:
            try:
                pairs = [tuple(entry) for entry in raw]
            except TypeError:
                raise PluginInputError(
                    f"KPI weights must be a mapping or (key, weight) pairs, got {raw!r}"
                ) from None
            if any(len(entry) != 2 for entry in pairs):
                raise PluginInputError(f"KPI weights must be (key, weight) pairs, got {raw!r}")
        for name, weight in pairs:
            # The service's rules for cost.weights: real JSON numbers, finite.
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise PluginInputError(f"KPI weight {name!r} must be a number, got {weight!r}")
            if not math.isfinite(float(weight)):
                raise PluginInputError(f"KPI weight {name!r} must be finite, got {weight!r}")
        try:
            config = KpiConfig(weights=tuple((str(name), weight) for name, weight in pairs))
        except ValueError as exc:
            raise PluginInputError(str(exc)) from None
        object.__setattr__(self, "weights", config.weights)

    @classmethod
    def from_dict(cls, weights: Mapping[str, float]) -> "KpiWeights":
        return cls(weights=_mapping(weights, what="weights"))

    @classmethod
    def parse(cls, text: str) -> "KpiWeights":
        """Parse ``'{"control_delay": 0.7, "stops": 0.3}'`` or ``"control_delay=0.7,stops=0.3"``."""
        text = str(text).strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except ValueError as exc:
                raise PluginInputError(f"KPI weights are not valid JSON: {exc}") from None
            return cls.from_dict(payload)
        pairs = []
        for item in filter(None, (part.strip() for part in text.split(","))):
            name, sep, value = item.partition("=")
            if not sep:
                raise PluginInputError(f"KPI weight {item!r} must look like name=value")
            try:
                number = float(value)
            except ValueError:
                raise PluginInputError(
                    f"KPI weight {name.strip()!r} is not a number: {value!r}"
                ) from None
            pairs.append((name.strip(), number))
        return cls(weights=tuple(pairs))

    def to_dict(self) -> dict[str, float]:
        return {name: float(weight) for name, weight in self.weights}

    def to_kpi_config(self):
        """The core ``terasim_city.evaluation.moe.KpiConfig`` with these weights."""
        from terasim_city.evaluation.moe import KpiConfig

        return KpiConfig(weights=tuple(self.weights))


@dataclass(frozen=True)
class CorridorSelection:
    """Which signals the operation covers.

    Either explicit SUMO traffic-light ids (``tls_ids``, kept in the given
    order) or two endpoints (``from_ref``/``to_ref``: TMC intersection ids or
    ``"lat,lon"`` strings; the corridor is the signals along the shortest drivable
    path between them). Neither: a map with a single traffic light selects it;
    a map with several needs a selection. ``from_ref == to_ref`` selects one
    intersection.
    """

    from_ref: "str | None" = None
    to_ref: "str | None" = None
    tls_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.tls_ids, str):
            raise PluginInputError(
                "CorridorSelection.tls_ids must be a sequence of ids, not a string"
            )
        tls_ids = tuple(self.tls_ids)
        for tls_id in tls_ids:
            _require_str(tls_id, what="CorridorSelection.tls_ids entry")
        if len(set(tls_ids)) != len(tls_ids):
            raise PluginInputError(f"CorridorSelection.tls_ids has duplicates: {list(tls_ids)}")
        object.__setattr__(self, "tls_ids", tls_ids)
        _require_str(self.from_ref, what="CorridorSelection.from_ref", allow_none=True)
        _require_str(self.to_ref, what="CorridorSelection.to_ref", allow_none=True)
        if (self.from_ref is None) != (self.to_ref is None):
            raise PluginInputError("CorridorSelection needs from_ref and to_ref together")
        if tls_ids and self.from_ref is not None:
            raise PluginInputError("CorridorSelection takes tls_ids or from_ref/to_ref, not both")

    @classmethod
    def between(cls, from_ref: str, to_ref: str) -> "CorridorSelection":
        return cls(from_ref=from_ref, to_ref=to_ref)

    @classmethod
    def signals(cls, *tls_ids: str) -> "CorridorSelection":
        return cls(tls_ids=tuple(tls_ids))

    def to_dict(self) -> dict:
        return {"from_ref": self.from_ref, "to_ref": self.to_ref, "tls_ids": list(self.tls_ids)}

    @classmethod
    def from_dict(cls, payload: Mapping) -> "CorridorSelection":
        payload = _mapping(payload, what="corridor")
        return cls(
            from_ref=payload.get("from_ref"),
            to_ref=payload.get("to_ref"),
            tls_ids=tuple(payload.get("tls_ids") or ()),
        )


@dataclass(frozen=True)
class OperationRequest:
    """Everything a Traffic Operation plug-in receives for one run.

    ``params`` holds plugin-specific options (a JSON object); each plugin
    documents the keys it accepts and rejects unknown ones with
    :class:`~terasim_plugin_sdk.errors.PluginInputError`. Every file a plugin
    writes goes under ``output_dir``.
    """

    map: MapInput
    traffic: TrafficSnapshot
    output_dir: Path
    corridor: CorridorSelection = field(default_factory=CorridorSelection)
    current_plan: "CurrentPlan | None" = None
    weights: KpiWeights = field(default_factory=KpiWeights)
    sim_end: float = DEFAULT_SIM_END
    seed: int = DEFAULT_SEED
    n_workers: int = DEFAULT_N_WORKERS
    vehicle_behavior: str = DEFAULT_VEHICLE_BEHAVIOR
    params: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        for name, kind in (
            ("map", MapInput),
            ("traffic", TrafficSnapshot),
            ("corridor", CorridorSelection),
            ("weights", KpiWeights),
        ):
            if not isinstance(getattr(self, name), kind):
                raise PluginInputError(
                    f"OperationRequest.{name} must be a {kind.__name__}, "
                    f"got {type(getattr(self, name)).__name__}"
                )
        if self.current_plan is not None and not isinstance(self.current_plan, CurrentPlan):
            raise PluginInputError("OperationRequest.current_plan must be a CurrentPlan or None")
        if self.output_dir is None or str(self.output_dir) == "":
            raise PluginInputError("OperationRequest.output_dir is required")
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        if isinstance(self.sim_end, bool) or not isinstance(self.sim_end, (int, float)):
            raise PluginInputError(f"sim_end must be a number of seconds, got {self.sim_end!r}")
        if not (math.isfinite(float(self.sim_end)) and float(self.sim_end) > 0.0):
            raise PluginInputError(f"sim_end must be finite and > 0, got {self.sim_end!r}")
        object.__setattr__(self, "sim_end", float(self.sim_end))
        for name, minimum in (("seed", 0), ("n_workers", 1)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise PluginInputError(f"{name} must be an integer >= {minimum}, got {value!r}")
        _require_str(self.vehicle_behavior, what="vehicle_behavior")
        params = _mapping(self.params, what="params")
        bad_keys = [key for key in params if not isinstance(key, str)]
        if bad_keys:
            raise PluginInputError(f"params keys must be strings, got {bad_keys!r}")
        object.__setattr__(self, "params", _json_copy(dict(params), what="params"))

    @property
    def inputs_dir(self) -> Path:
        """Where the SDK materializes inputs (``<output_dir>/inputs``)."""
        return self.output_dir / INPUTS_DIRNAME

    def check_inputs(self) -> None:
        """Raise PluginInputError unless every referenced file or directory exists."""
        if self.map.sumocfg is not None and not self.map.sumocfg.is_file():
            raise PluginInputError(f"map file not found: {self.map.sumocfg}")
        if self.map.data_root is not None and not self.map.data_root.is_dir():
            raise PluginInputError(f"map store directory not found: {self.map.data_root}")
        if self.traffic.path is not None and not self.traffic.path.is_file():
            raise PluginInputError(f"TMC file not found: {self.traffic.path}")
        if self.current_plan is not None:
            self.current_plan.check()
        if self.output_dir.exists() and not self.output_dir.is_dir():
            raise PluginInputError(f"output_dir is not a directory: {self.output_dir}")

    def resolved(self) -> "OperationRequest":
        """A copy with every path made absolute (what :func:`run_operation` hands plugins)."""
        current = self.current_plan
        if current is not None and current.path is not None:
            current = dataclasses.replace(current, path=_absolute(current.path))
        return dataclasses.replace(
            self,
            map=MapInput(
                sumocfg=_absolute(self.map.sumocfg),
                map_id=self.map.map_id,
                data_root=_absolute(self.map.data_root),
            ),
            traffic=(
                TrafficSnapshot(path=_absolute(self.traffic.path))
                if self.traffic.path is not None
                else self.traffic
            ),
            current_plan=current,
            output_dir=_absolute(self.output_dir),
        )

    def to_dict(self) -> dict:
        return {
            "schema": REQUEST_SCHEMA,
            "map": self.map.to_dict(),
            "traffic": self.traffic.to_dict(),
            "corridor": self.corridor.to_dict(),
            "current_plan": None if self.current_plan is None else self.current_plan.to_dict(),
            "weights": self.weights.to_dict(),
            "sim_end": self.sim_end,
            "seed": self.seed,
            "n_workers": self.n_workers,
            "vehicle_behavior": self.vehicle_behavior,
            "params": dict(self.params),
            "output_dir": str(self.output_dir),
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "OperationRequest":
        payload = _mapping(payload, what="operation request")
        schema = payload.get("schema", REQUEST_SCHEMA)
        if schema != REQUEST_SCHEMA:
            raise PluginInputError(f"request schema is {schema!r}, expected {REQUEST_SCHEMA!r}")
        missing = [key for key in ("map", "traffic", "output_dir") if payload.get(key) is None]
        if missing:
            raise PluginInputError(f"operation request is missing {missing}")
        current = payload.get("current_plan")
        return cls(
            map=MapInput.from_dict(payload["map"]),
            traffic=TrafficSnapshot.from_dict(payload["traffic"]),
            output_dir=Path(payload["output_dir"]),
            corridor=CorridorSelection.from_dict(payload.get("corridor") or {}),
            current_plan=None if current is None else CurrentPlan.from_dict(current),
            weights=(
                KpiWeights.from_dict(payload["weights"])
                if payload.get("weights") is not None
                else KpiWeights()
            ),
            sim_end=payload.get("sim_end", DEFAULT_SIM_END),
            seed=payload.get("seed", DEFAULT_SEED),
            n_workers=payload.get("n_workers", DEFAULT_N_WORKERS),
            vehicle_behavior=payload.get("vehicle_behavior", DEFAULT_VEHICLE_BEHAVIOR),
            params=payload.get("params") or {},
        )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationResult:
    """What a plugin returns for one run.

    * ``nema_plan`` -- the recommended plan as a ``terasim.nema_timing.v1``
      document (also written to ``<output_dir>/nema_plan.json``);
    * ``kpis`` -- ``{"cost_weights": {...}, "baseline": {"cost", "metrics",
      "timing"}, "optimized": {"cost", "metrics", "timing"}}``, the shape of the
      HTTP service's ``kpis`` block: ``baseline`` is the current (or reference)
      plan, ``optimized`` the recommended one, both simulated on the same scenario;
      ``timing`` is left out when a plan has none;
    * ``tls_ids`` -- the corridor's signals, in corridor order;
    * ``artifacts`` -- name -> path relative to ``output_dir`` of every file or
      directory worth keeping;
    * ``notes`` -- sentences a caller should show (fallbacks, skipped steps);
    * ``unserved_movements`` -- per signal, vehicle movements the recommended
      plan never serves (empty when it serves all);
    * ``details`` -- plugin-specific JSON data.
    """

    plugin: str
    plugin_version: str
    corridor_id: str
    tls_ids: tuple[str, ...]
    nema_plan: Mapping[str, Any] = field(hash=False)
    kpis: Mapping[str, Any] = field(hash=False)
    output_dir: Path
    artifacts: Mapping[str, str] = field(default_factory=dict, hash=False)
    notes: tuple[str, ...] = ()
    unserved_movements: Mapping[str, Any] = field(default_factory=dict, hash=False)
    details: Mapping[str, Any] = field(default_factory=dict, hash=False)
    api_version: str = API_VERSION
    schema: str = RESULT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        object.__setattr__(self, "tls_ids", tuple(self.tls_ids))
        object.__setattr__(self, "notes", tuple(self.notes))
        for name in ("nema_plan", "kpis", "artifacts", "unserved_movements", "details"):
            value = getattr(self, name)
            object.__setattr__(self, name, dict(value) if isinstance(value, Mapping) else value)

    @property
    def nema_plan_path(self) -> Path:
        return self.output_dir / self.artifacts.get("nema_plan", "nema_plan.json")

    def artifact_path(self, name: str) -> Path:
        """Absolute path of one artifact (KeyError when the plugin listed none by that name)."""
        return self.output_dir / self.artifacts[name]

    def response(self) -> dict:
        """The body the service's ``POST /v1/city/optimize`` returns: ``{nema_plan, kpis}``.

        ``unserved_movements`` is included only when there are some, as the service does.
        """
        body = {"nema_plan": dict(self.nema_plan), "kpis": dict(self.kpis)}
        if self.unserved_movements:
            body["unserved_movements"] = dict(self.unserved_movements)
        return body

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "api_version": self.api_version,
            "plugin": self.plugin,
            "plugin_version": self.plugin_version,
            "corridor_id": self.corridor_id,
            "tls_ids": list(self.tls_ids),
            "nema_plan": self.nema_plan,
            "kpis": self.kpis,
            "unserved_movements": self.unserved_movements,
            "artifacts": self.artifacts,
            "notes": list(self.notes),
            "details": self.details,
            "output_dir": str(self.output_dir),
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "OperationResult":
        payload = _mapping(payload, what="operation result")
        missing = [
            key
            for key in ("plugin", "plugin_version", "corridor_id", "nema_plan", "kpis")
            if key not in payload
        ]
        if missing:
            raise PluginInputError(f"operation result is missing {missing}")
        return cls(
            plugin=payload["plugin"],
            plugin_version=payload["plugin_version"],
            corridor_id=payload["corridor_id"],
            tls_ids=tuple(payload.get("tls_ids") or ()),
            nema_plan=payload["nema_plan"],
            kpis=payload["kpis"],
            output_dir=Path(payload.get("output_dir") or "."),
            artifacts=payload.get("artifacts") or {},
            notes=tuple(payload.get("notes") or ()),
            unserved_movements=payload.get("unserved_movements") or {},
            details=payload.get("details") or {},
            api_version=payload.get("api_version", API_VERSION),
            schema=payload.get("schema", RESULT_SCHEMA),
        )

    def write(self, path=None) -> Path:
        """Write the result as JSON (default ``<output_dir>/operation_result.json``)."""
        target = Path(path) if path is not None else self.output_dir / RESULT_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        return target

    @classmethod
    def read(cls, path) -> "OperationResult":
        """Read a result file, or ``operation_result.json`` inside a directory."""
        path = Path(path)
        if path.is_dir():
            path = path / RESULT_FILENAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PluginInputError(f"cannot read operation result {path}: {exc}") from None
        return cls.from_dict(payload)


def _mapping(value, *, what: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise PluginInputError(f"{what} must be a JSON object, got {type(value).__name__}")
    return value
