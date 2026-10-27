"""NEMA corridor plan: the optimizer-facing representation of stored plans.

A :class:`NemaCorridorPlan` is what the assembler's per-intersection selections
become once the optimizer needs to iterate on them: for every TLS it pairs the
intersection definition (phases / rings / clearances from ``nema_plan.v1``)
with ONE selected coordination pattern. The Bayesian offset search derives new
plans via :meth:`with_offsets` — only ``pattern["offset_s"]`` changes; splits,
rings and phase structure stay frozen to the stored plan.

Coordination rule (v1): a corridor can be offset-optimized when every selected
pattern's cycle is EQUAL to, or a clean INTEGER MULTIPLE of, the smallest cycle
(a double-cycled intersection fits twice into the corridor cycle). Each TLS
searches its offset in ``[0, its own cycle)``.

``materialize_nema_plan`` mirrors :func:`terasim_city.plans.materialize
.materialize_plan`: it writes ``plan.tls.xml`` (one ``type="NEMA"`` tlLogic per
TLS, ``programID = pattern_id``), ``plan.json`` and a trial ``simulation
.sumocfg`` with the plan file appended last.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from terasim_city.plans.materialize import _read_end_and_seed
from terasim_city.plans.model import PlanProvenance
from terasim_city.plans.nema_materialize import (
    materialize_nema_tls,
    write_nema_additional,
)
from terasim_city.scenario.sumocfg_io import parse_sumocfg, write_sumocfg

NEMA_PLAN_KIND = "nema"
PLAN_TLS_FILENAME = "plan.tls.xml"
PLAN_JSON_FILENAME = "plan.json"
TRIAL_SUMOCFG_FILENAME = "simulation.sumocfg"

#: Cycles within this many seconds of a clean integer multiple coordinate.
CYCLE_MULTIPLE_TOLERANCE_S = 1.0


@dataclass(frozen=True)
class NemaSignalEntry:
    """One TLS: its intersection definition + the selected pattern."""

    tls_id: str
    intersection: dict
    pattern: dict

    def __post_init__(self) -> None:
        if not self.tls_id:
            raise ValueError("NemaSignalEntry.tls_id must be non-empty")
        if not self.pattern.get("pattern_id"):
            raise ValueError(f"{self.tls_id}: pattern needs a 'pattern_id'")
        cycle = float(self.pattern.get("cycle_s") or 0.0)
        if not cycle > 0.0:
            raise ValueError(f"{self.tls_id}: pattern cycle_s must be > 0")

    @property
    def cycle_s(self) -> float:
        return float(self.pattern["cycle_s"])

    @property
    def offset_s(self) -> float:
        return float(self.pattern.get("offset_s", 0.0))

    @property
    def program_id(self) -> str:
        return str(self.pattern["pattern_id"])


@dataclass(frozen=True)
class NemaCorridorPlan:
    """Ordered per-TLS NEMA entries making up one corridor plan."""

    plan_id: str
    entries: tuple[NemaSignalEntry, ...]
    provenance: PlanProvenance = field(
        default_factory=lambda: PlanProvenance(source="explicit")
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        if not self.plan_id:
            raise ValueError("NemaCorridorPlan.plan_id must be non-empty")
        if not self.entries:
            raise ValueError("NemaCorridorPlan needs at least one entry")
        seen = set()
        for entry in self.entries:
            if entry.tls_id in seen:
                raise ValueError(f"duplicate tls_id {entry.tls_id!r}")
            seen.add(entry.tls_id)

    # ------------------------------------------------------------------
    # Introspection used by the optimizer / ledger
    # ------------------------------------------------------------------

    def offsets(self) -> dict[str, float]:
        return {e.tls_id: e.offset_s for e in self.entries}

    def programs(self) -> dict[str, str]:
        return {e.tls_id: e.program_id for e in self.entries}

    def base_cycle_s(self) -> float:
        """Smallest cycle, after checking every cycle is a clean multiple of it.

        Raises:
            ValueError: Some cycle is neither equal to nor an integer multiple
                of the smallest (within ``CYCLE_MULTIPLE_TOLERANCE_S``) — the
                corridor cannot hold a stable coordination.
        """
        cycles = [e.cycle_s for e in self.entries]
        base = min(cycles)
        bad = []
        for e in self.entries:
            k = round(e.cycle_s / base)
            if k < 1 or abs(e.cycle_s - k * base) > CYCLE_MULTIPLE_TOLERANCE_S:
                bad.append(f"{e.tls_id}={e.cycle_s:g}s")
        if bad:
            raise ValueError(
                f"plan {self.plan_id!r}: cycles are not equal/integer multiples of "
                f"{base:g}s — cannot coordinate: {', '.join(bad)}"
            )
        return base

    def with_offsets(self, offsets: dict[str, float]) -> "NemaCorridorPlan":
        """A copy with ``pattern.offset_s`` replaced for the listed TLS."""
        unknown = set(offsets) - {e.tls_id for e in self.entries}
        if unknown:
            raise ValueError(f"with_offsets: unknown tls_ids {sorted(unknown)}")
        entries = []
        for e in self.entries:
            if e.tls_id in offsets:
                pattern = dict(e.pattern)
                pattern["offset_s"] = float(offsets[e.tls_id])
                entries.append(NemaSignalEntry(e.tls_id, e.intersection, pattern))
            else:
                entries.append(e)
        return NemaCorridorPlan(self.plan_id, tuple(entries), self.provenance)

    def with_splits(self, splits: dict[str, dict[str, float]]) -> "NemaCorridorPlan":
        """A copy with ``pattern.splits`` replaced for the listed TLS.

        ``splits`` maps ``tls_id -> {phase_no_str: split_s}``. Only the splits
        change — cycle, offset, rings and phase structure stay frozen. The
        caller (the split optimizer) is responsible for barrier/cycle
        consistency; this is a plain data operation.
        """
        unknown = set(splits) - {e.tls_id for e in self.entries}
        if unknown:
            raise ValueError(f"with_splits: unknown tls_ids {sorted(unknown)}")
        entries = []
        for e in self.entries:
            if e.tls_id in splits:
                pattern = dict(e.pattern)
                pattern["splits"] = {
                    str(k): float(v) for k, v in splits[e.tls_id].items()
                }
                entries.append(NemaSignalEntry(e.tls_id, e.intersection, pattern))
            else:
                entries.append(e)
        return NemaCorridorPlan(self.plan_id, tuple(entries), self.provenance)

    # ------------------------------------------------------------------
    # JSON round-trip (spawn-pool payloads)
    # ------------------------------------------------------------------

    def to_json_dict(self) -> dict:
        return {
            "kind": NEMA_PLAN_KIND,
            "plan_id": self.plan_id,
            "entries": [
                {"tls_id": e.tls_id, "intersection": e.intersection, "pattern": e.pattern}
                for e in self.entries
            ],
            "provenance": self.provenance.to_json_dict(),
        }

    @classmethod
    def from_json_dict(cls, payload: dict) -> "NemaCorridorPlan":
        if payload.get("kind") != NEMA_PLAN_KIND:
            raise ValueError(f"'kind' must be {NEMA_PLAN_KIND!r}, got {payload.get('kind')!r}")
        entries = tuple(
            NemaSignalEntry(e["tls_id"], e["intersection"], e["pattern"])
            for e in payload["entries"]
        )
        return cls(
            plan_id=payload["plan_id"],
            entries=entries,
            provenance=PlanProvenance.from_json_dict(payload.get("provenance", {"source": "explicit"})),
        )


def materialize_nema_plan(
    plan: NemaCorridorPlan,
    scenario_sumocfg: Path,
    trial_dir: Path,
) -> Path:
    """Write the plan's NEMA tls.xml + plan.json + a trial sumocfg.

    Mirrors ``materialize_plan``: the scenario is never mutated; the plan file
    is appended LAST to the additional files so its programs win.
    """
    scenario_sumocfg = Path(scenario_sumocfg)
    trial_dir = Path(trial_dir)
    trial_dir.mkdir(parents=True, exist_ok=True)

    info = parse_sumocfg(scenario_sumocfg)
    if not info.route_files:
        raise ValueError(
            f"scenario sumocfg {scenario_sumocfg} declares no <route-files>; "
            "cannot materialise a trial without demand"
        )
    sim_end, seed = _read_end_and_seed(scenario_sumocfg)

    tls_elements = [
        materialize_nema_tls(
            e.intersection, e.pattern, info.net_file, e.tls_id
        )
        for e in plan.entries
    ]
    plan_tls_path = write_nema_additional(tls_elements, trial_dir / PLAN_TLS_FILENAME)
    (trial_dir / PLAN_JSON_FILENAME).write_text(
        json.dumps(plan.to_json_dict(), indent=2), encoding="utf-8"
    )

    route_value = ",".join(str(p) for p in info.route_files)
    return write_sumocfg(
        trial_dir / TRIAL_SUMOCFG_FILENAME,
        net_file=info.net_file,
        route_file=Path(route_value) if len(info.route_files) == 1 else route_value,  # type: ignore[arg-type]
        additional_files=tuple(info.additional_files) + (plan_tls_path,),
        gui_settings_file=info.gui_settings_file,
        processing_params=dict(info.processing_params),
        routing_params=dict(info.routing_params),
        report_params=dict(info.report_params),
        sim_end=sim_end,
        seed=seed,
        step_length=info.step_length,
    )


__all__ = [
    "CYCLE_MULTIPLE_TOLERANCE_S",
    "NEMA_PLAN_KIND",
    "NemaCorridorPlan",
    "NemaSignalEntry",
    "materialize_nema_plan",
]
