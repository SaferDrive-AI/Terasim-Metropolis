"""Frozen signal-plan value objects shared by optimization, export, and service.

These dataclasses are the contract between plan producers (SUMO tls files,
baseline timing sheets, Webster initialiser, Bayesian offset search) and
plan consumers (materialisation, evaluation, NEMA export). All validation is
performed eagerly in ``__post_init__`` — a constructed object is always
internally consistent, and ``to_json_dict``/``from_json_dict`` round-trip
losslessly through plain JSON.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Every movement label the corridor pipeline understands, in canonical order.
MOVEMENTS: tuple[str, ...] = tuple(
    f"{c}-{t}" for c in ("EB", "WB", "NB", "SB") for t in ("left", "through", "right")
)

#: Legal values for :attr:`PhaseSpec.kind`.
PHASE_KINDS: tuple[str, ...] = ("green", "yellow", "all_red", "other")

#: Legal values for :attr:`PlanProvenance.source`.
PLAN_SOURCES: tuple[str, ...] = (
    "sumo_tls",
    "baseline_sheet",
    "webster_init",
    "stored_nema",
    "bo_iteration",
    "explicit",
)

#: Characters NOT allowed in a SUMO programID; replaced with ``_`` on construction.
_PROGRAM_ID_ILLEGAL = re.compile(r"[^A-Za-z0-9._-]")

#: Two per-TLS cycle lengths within this tolerance count as "common".
CYCLE_TOLERANCE_S = 0.5

CORRIDOR_PLAN_SCHEMA = "terasim.corridor_plan.v1"


@dataclass(frozen=True)
class PhaseSpec:
    """One phase of a static SUMO signal program.

    ``state`` is the SUMO state string over the TLS link indices. ``movements``
    entries must come from :data:`MOVEMENTS`; an empty tuple means "unknown"
    (e.g. a plan loaded from a raw tls.xml without movement metadata).
    """

    state: str
    duration_s: float
    kind: str
    name: str = ""
    nema_phases: tuple[int, ...] = ()
    movements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "nema_phases", tuple(int(p) for p in self.nema_phases))
        object.__setattr__(self, "movements", tuple(str(m) for m in self.movements))
        out_of_range = [p for p in self.nema_phases if not 1 <= p <= 8]
        if out_of_range:
            raise ValueError(
                f"PhaseSpec.nema_phases must be NEMA dual-ring phase numbers "
                f"in 1..8, got {out_of_range}"
            )
        if not self.state:
            raise ValueError("PhaseSpec.state must be a non-empty SUMO state string")
        duration = float(self.duration_s)
        if not duration > 0.0:  # also rejects NaN
            raise ValueError(f"PhaseSpec.duration_s must be > 0, got {self.duration_s!r}")
        object.__setattr__(self, "duration_s", duration)
        if self.kind not in PHASE_KINDS:
            raise ValueError(
                f"PhaseSpec.kind must be one of {PHASE_KINDS}, got {self.kind!r}"
            )
        unknown = [m for m in self.movements if m not in MOVEMENTS]
        if unknown:
            raise ValueError(
                f"PhaseSpec.movements contains unknown movement(s) {unknown}; "
                f"allowed: {list(MOVEMENTS)}"
            )

    def to_json_dict(self) -> dict:
        return {
            "state": self.state,
            "duration_s": self.duration_s,
            "kind": self.kind,
            "name": self.name,
            "nema_phases": list(self.nema_phases),
            "movements": list(self.movements),
        }

    @classmethod
    def from_json_dict(cls, payload: dict) -> "PhaseSpec":
        _require_keys(payload, ("state", "duration_s", "kind"), what="PhaseSpec")
        return cls(
            state=str(payload["state"]),
            duration_s=float(payload["duration_s"]),
            kind=str(payload["kind"]),
            name=str(payload.get("name", "")),
            nema_phases=tuple(payload.get("nema_phases", ())),
            movements=tuple(payload.get("movements", ())),
        )


@dataclass(frozen=True)
class SignalPlan:
    """A complete static program for one traffic light.

    ``program_id`` becomes the SUMO programID and is therefore sanitised to
    ``[A-Za-z0-9._-]`` on construction (illegal characters become ``_``); TLS
    ids from OSM-derived nets routinely contain ``#`` so this cannot be a
    validation error. ``offset_s`` must lie in ``[0, cycle_s)``.
    """

    tls_id: str
    program_id: str
    offset_s: float
    phases: tuple[PhaseSpec, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "phases", tuple(self.phases))
        if not self.tls_id:
            raise ValueError("SignalPlan.tls_id must be non-empty")
        if not self.program_id:
            raise ValueError(f"SignalPlan.program_id must be non-empty (tls {self.tls_id!r})")
        object.__setattr__(
            self, "program_id", _PROGRAM_ID_ILLEGAL.sub("_", str(self.program_id))
        )
        if not self.phases:
            raise ValueError(f"SignalPlan for tls {self.tls_id!r} has no phases")
        offset = float(self.offset_s)
        object.__setattr__(self, "offset_s", offset)
        cycle = self.cycle_s
        if not 0.0 <= offset < cycle:
            raise ValueError(
                f"SignalPlan.offset_s for tls {self.tls_id!r} must be in "
                f"[0, {cycle:g}) (cycle length), got {offset!r}"
            )

    @property
    def cycle_s(self) -> float:
        """Cycle length: the sum of all phase durations."""
        return sum(p.duration_s for p in self.phases)

    def to_json_dict(self) -> dict:
        return {
            "tls_id": self.tls_id,
            "program_id": self.program_id,
            "offset_s": self.offset_s,
            "phases": [p.to_json_dict() for p in self.phases],
        }

    @classmethod
    def from_json_dict(cls, payload: dict) -> "SignalPlan":
        _require_keys(payload, ("tls_id", "program_id", "offset_s", "phases"), what="SignalPlan")
        return cls(
            tls_id=str(payload["tls_id"]),
            program_id=str(payload["program_id"]),
            offset_s=float(payload["offset_s"]),
            phases=tuple(PhaseSpec.from_json_dict(p) for p in payload["phases"]),
        )


@dataclass(frozen=True)
class PlanProvenance:
    """Where a plan came from, for the trials ledger and result reporting."""

    source: str
    parent_plan_id: str | None = None
    iteration: int | None = None
    #: ``(name, value)`` pairs. Numbers are coerced to float; strings (labels
    #: such as the Webster seed's ``stage_mode``) are kept verbatim.
    params: tuple[tuple[str, float | str], ...] = ()

    def __post_init__(self) -> None:
        if self.source not in PLAN_SOURCES:
            raise ValueError(
                f"PlanProvenance.source must be one of {PLAN_SOURCES}, got {self.source!r}"
            )
        object.__setattr__(
            self,
            "params",
            tuple(
                (str(name), value if isinstance(value, str) else float(value))
                for name, value in self.params
            ),
        )

    def to_json_dict(self) -> dict:
        return {
            "source": self.source,
            "parent_plan_id": self.parent_plan_id,
            "iteration": self.iteration,
            "params": [[name, value] for name, value in self.params],
        }

    @classmethod
    def from_json_dict(cls, payload: dict) -> "PlanProvenance":
        _require_keys(payload, ("source",), what="PlanProvenance")
        iteration = payload.get("iteration")
        parent = payload.get("parent_plan_id")
        return cls(
            source=str(payload["source"]),
            parent_plan_id=None if parent is None else str(parent),
            iteration=None if iteration is None else int(iteration),
            params=tuple((name, value) for name, value in payload.get("params", ())),
        )


@dataclass(frozen=True)
class CorridorPlan:
    """A coordinated set of per-TLS signal plans, ordered along the corridor."""

    plan_id: str
    plans: tuple[SignalPlan, ...]
    provenance: PlanProvenance

    def __post_init__(self) -> None:
        object.__setattr__(self, "plans", tuple(self.plans))
        if not self.plan_id:
            raise ValueError("CorridorPlan.plan_id must be non-empty")
        object.__setattr__(
            self, "plan_id", _PROGRAM_ID_ILLEGAL.sub("_", str(self.plan_id))
        )
        if not self.plans:
            raise ValueError(f"CorridorPlan {self.plan_id!r} has no per-TLS plans")
        seen: set[str] = set()
        for plan in self.plans:
            if plan.tls_id in seen:
                raise ValueError(
                    f"CorridorPlan {self.plan_id!r} contains duplicate tls_id "
                    f"{plan.tls_id!r}"
                )
            seen.add(plan.tls_id)

    def by_tls(self) -> dict[str, SignalPlan]:
        """Return ``{tls_id: SignalPlan}`` in corridor order (dicts preserve it)."""
        return {plan.tls_id: plan for plan in self.plans}

    def common_cycle_s(self) -> float:
        """The shared cycle length across all TLS.

        Raises:
            ValueError: If per-TLS cycles differ by more than
                ``CYCLE_TOLERANCE_S``; the message lists every per-TLS cycle so
                the caller can see which intersection is off.
        """
        cycles = [plan.cycle_s for plan in self.plans]
        if max(cycles) - min(cycles) > CYCLE_TOLERANCE_S:
            detail = ", ".join(
                f"{plan.tls_id}={plan.cycle_s:g}s" for plan in self.plans
            )
            raise ValueError(
                f"CorridorPlan {self.plan_id!r} has no common cycle: {detail}"
            )
        return cycles[0]

    def to_json_dict(self) -> dict:
        return {
            "schema": CORRIDOR_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "plans": [plan.to_json_dict() for plan in self.plans],
            "provenance": self.provenance.to_json_dict(),
        }

    @classmethod
    def from_json_dict(cls, payload: dict) -> "CorridorPlan":
        schema = payload.get("schema")
        if schema is not None and schema != CORRIDOR_PLAN_SCHEMA:
            raise ValueError(
                f"CorridorPlan payload has schema {schema!r}, "
                f"expected {CORRIDOR_PLAN_SCHEMA!r}"
            )
        _require_keys(payload, ("plan_id", "plans", "provenance"), what="CorridorPlan")
        return cls(
            plan_id=str(payload["plan_id"]),
            plans=tuple(SignalPlan.from_json_dict(p) for p in payload["plans"]),
            provenance=PlanProvenance.from_json_dict(payload["provenance"]),
        )


def _require_keys(payload: dict, keys: tuple[str, ...], *, what: str) -> None:
    """Fail loud when a JSON payload is missing contract keys."""
    if not isinstance(payload, dict):
        raise ValueError(f"{what} payload must be a JSON object, got {type(payload).__name__}")
    missing = [k for k in keys if k not in payload]
    if missing:
        raise ValueError(f"{what} payload is missing required key(s): {missing}")
