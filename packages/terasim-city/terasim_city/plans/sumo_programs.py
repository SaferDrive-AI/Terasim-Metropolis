"""Build a CorridorPlan seed from the <tlLogic> programs of a SUMO scenario."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from lxml import etree as ET

from terasim_city.plans.model import CorridorPlan, PhaseSpec, PlanProvenance, SignalPlan
from terasim_city.plans.signal_groups import derive_link_movements, load_overrides
from terasim_city.scenario.sumocfg_io import parse_sumocfg

logger = logging.getLogger(__name__)

SCENARIO_PLAN_ID = "scenario_programs"


def _phase_kind(state: str) -> str:
    """``y`` anywhere makes a phase yellow, else any ``G``/``g`` makes it green.

    This reads the state string only; :func:`load_scenario_programs` then turns a
    green whose green links serve no vehicle movement (only pedestrian crossings)
    into ``"other"``.
    """
    if "y" in state or "Y" in state:
        return "yellow"
    if "G" in state or "g" in state:
        return "green"
    if set(state) <= {"r", "s", "o", "O", "u"}:
        return "all_red"
    return "other"


def _collect_programs(
    files: list[Path],
) -> dict[str, tuple[str, float, list[tuple[str, float]], str]]:
    """tls_id -> (programID, offset_s, [(state, duration)], type) — later definitions win."""
    programs: dict[str, tuple[str, float, list[tuple[str, float]], str]] = {}
    for file in files:
        for _event, elem in ET.iterparse(str(file), events=("end",), tag="tlLogic"):
            phases = [(ph.get("state"), float(ph.get("duration"))) for ph in elem.iter("phase")]
            if phases:
                programs[elem.get("id")] = (
                    elem.get("programID", "0"),
                    float(elem.get("offset", 0.0) or 0.0),
                    phases,
                    elem.get("type") or "static",
                )
            elem.clear()
    return programs


def load_scenario_programs(
    base_sumocfg: Path | str,
    tls_ids: Iterable[str],
    *,
    overrides_path: Path | None = None,
) -> CorridorPlan:
    """The scenario's current signal timings as a static corridor plan.

    Programs are read from the net file and then each additional file, in order;
    for a TLS the last ``<tlLogic>`` definition wins. Durations keep their
    fractional seconds. A green phase lists the movements of the vehicle links it
    serves, classified as in the rest of the pipeline (``overrides_path`` is the
    signal-groups file that corrects that classification). A phase whose green
    links serve no vehicle movement (only pedestrian crossings) is kind
    ``"other"``: fixed time, not a searchable stage.

    Only the fixed-time reading of a program is kept. A TLS whose ``<tlLogic type>`` is
    not ``static`` (actuated, NEMA, ...) is read as the sequence of its phase durations,
    dropping minDur/maxDur and detector logic, so the plan only approximates what that
    controller does: each such TLS is logged as a warning, and the provenance carries
    ``("program_types", "<tls>:<type>,...")`` for every TLS, in the order given.

    Raises:
        ValueError: a TLS has no ``<tlLogic>`` program in any of those files.
    """
    info = parse_sumocfg(Path(base_sumocfg))
    programs = _collect_programs([info.net_file, *info.additional_files])
    overrides = load_overrides(overrides_path) if overrides_path is not None else {}
    plans: list[SignalPlan] = []
    program_types: list[str] = []
    for tls_id in tls_ids:
        if tls_id not in programs:
            raise ValueError(
                f"{base_sumocfg}: no <tlLogic> program for TLS {tls_id!r} in the net or "
                f"additional files (found: {sorted(programs)})"
            )
        program_id, offset, phases, tls_type = programs[tls_id]
        if tls_type != "static":
            logger.warning(
                "TLS %r runs a %r program: it is read as a fixed-time sequence of its phase "
                "durations (minDur/maxDur, detectors and phase switching are dropped), so the "
                "baseline only approximates the controller's behaviour",
                tls_id,
                tls_type,
            )
        program_types.append(f"{tls_id}:{tls_type}")
        link_movements = derive_link_movements(
            info.net_file, tls_id, overrides=overrides.get(tls_id)
        )
        specs = []
        for index, (state, duration) in enumerate(phases):
            kind = _phase_kind(state)
            # Several links of one phase often share a movement (two through lanes):
            # list each movement once, in link order.
            movements = tuple(dict.fromkeys(
                lm.movement for lm in link_movements
                if lm.link_index < len(state) and state[lm.link_index] in "Gg"
            )) if kind == "green" else ()
            if kind == "green" and not movements:
                # Only links that carry no vehicle movement are green (pedestrian
                # crossings): an exclusive pedestrian phase. Kept at its duration like
                # an all-red interval, never searched as a vehicle stage.
                kind = "other"
            specs.append(PhaseSpec(
                state=state, duration_s=duration, kind=kind, name=f"p{index}", movements=movements
            ))
        cycle = sum(d for _s, d in phases)
        plans.append(SignalPlan(
            tls_id=tls_id,
            program_id=f"{tls_id}_{program_id}",
            offset_s=offset % cycle if cycle > 0 else 0.0,
            phases=tuple(specs),
        ))
    return CorridorPlan(
        plan_id=SCENARIO_PLAN_ID,
        plans=tuple(plans),
        provenance=PlanProvenance(
            source="sumo_tls",
            params=(
                ("base_sumocfg", str(base_sumocfg)),
                ("program_types", ",".join(program_types)),
            ),
        ),
    )
