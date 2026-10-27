"""Export a :class:`~terasim_city.plans.model.CorridorPlan` as NEMA timing.

The export reads each program as a loop and groups every green :class:`PhaseSpec`
with the non-green specs that follow it, up to the next green, into one NEMA
stage row: yellow time goes to ``yellow_s``, all-red and ``"other"`` phases
(no vehicle movement green) to ``all_red_s``. Non-green phases that open the
program belong to the last green's clearance, and the offset is shifted to the
start of the first exported stage, so every intersection's splits add up to its
cycle (checked). The plan is "handoff ready" only when every green phase across
every intersection carries explicit ``nema_phases`` numbers — otherwise the
exported rows have ``nema_phase: null`` and a traffic engineer must map them
manually.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from terasim_city.plans.model import CorridorPlan, PhaseSpec, SignalPlan

NEMA_TIMING_SCHEMA = "terasim.nema_timing.v1"

JSON_FILENAME = "nema_plan.json"


#: Human-readable seed descriptions for static plans (which carry no
#: time-of-day pattern of their own).
_STATIC_PLAN_DESCRIPTIONS = {
    "webster_init": "Webster automatic (generated)",
    "baseline_sheet": "Baseline timing sheet",
    "bo_iteration": "Optimized plan",
    "sumo_tls": "The scenario's own signal programs",
}


def export_nema(
    plan: CorridorPlan,
    *,
    corridor_id: str,
    out_dir: Path,
    cycle_s: float | None = None,
) -> dict:
    """Write ``nema_plan.json`` into ``out_dir``.

    Args:
        plan: The corridor plan to export.
        corridor_id: Corridor identifier recorded in the export.
        out_dir: Directory the file is written into (created if absent).
        cycle_s: Coordination cycle override. When ``None`` the plan's common
            cycle is used (raises ``ValueError`` if per-TLS cycles differ).

    Returns:
        The dict written to ``nema_plan.json``.
    """
    out_dir = Path(out_dir)
    cycle = float(cycle_s) if cycle_s is not None else plan.common_cycle_s()

    # Generated/hand-entered static plans carry no time-of-day schedule, so the
    # export declares them valid all week: one window covering every day. This
    # mirrors the stored-plan window shape so consumers read a single format.
    description = _STATIC_PLAN_DESCRIPTIONS.get(
        plan.provenance.source, plan.plan_id
    )
    all_week = [
        {
            "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "start": "00:00",
            "end": "24:00",
        }
    ]

    intersections = []
    handoff_ready = True
    for signal_plan in plan.plans:
        rows, all_greens_numbered, leading_s = _stage_rows(signal_plan)
        handoff_ready = handoff_ready and all_greens_numbered
        # SUMO starts the program at offset_s; the exported stages start at its
        # first green, leading_s later.
        offset_s = signal_plan.offset_s
        if leading_s > 0.0:
            offset_s = (offset_s + leading_s) % signal_plan.cycle_s
        intersections.append(
            {
                "tls_id": signal_plan.tls_id,
                "offset_s": offset_s,
                "description": description,
                "valid_windows": all_week,
                "phases": rows,
            }
        )

    payload = {
        "schema": NEMA_TIMING_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corridor_id": corridor_id,
        "handoff_ready": handoff_ready,
        "coordination": {
            "cycle_length_s": cycle,
            "reference_intersection": plan.plans[0].tls_id,
        },
        "intersections": intersections,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / JSON_FILENAME).write_text(json.dumps(payload, indent=2))
    return payload


def _pattern_windows(intersection: dict, pattern_id: str) -> list[dict]:
    """Time-of-day windows in which ``pattern_id`` is scheduled to run.

    Reverse lookup over the intersection's schedule: within each day-group
    (rules sharing the same ``days`` list) rules are sorted by start time; a
    matching rule's window ends at the NEXT rule's start, wrapping to the
    day-group's first rule for the last one (the overnight period).
    """
    schedule = intersection.get("schedule") or []
    by_days: dict[tuple, list[dict]] = {}
    for rule in schedule:
        by_days.setdefault(tuple(rule.get("days", [])), []).append(rule)
    windows: list[dict] = []
    for days, rules in by_days.items():
        ordered = sorted(rules, key=lambda r: r["start"])
        for index, rule in enumerate(ordered):
            if rule.get("pattern_id") != pattern_id:
                continue
            nxt = ordered[(index + 1) % len(ordered)]
            windows.append(
                {"days": list(days), "start": rule["start"], "end": nxt["start"]}
            )
    return windows


def export_nema_plan(
    plan,
    *,
    corridor_id: str,
    out_dir: Path,
) -> dict:
    """Write ``nema_plan.json`` for a NEMA corridor plan (optimizer winner).

    The NEMA representation already carries phase numbers, movements, splits
    and per-phase clearances, so rows are built directly from each entry's
    selected pattern — always ``handoff_ready``. Each intersection also states
    WHICH time-of-day pattern this is (``description``, e.g. "AM PEAK") and
    WHEN it is scheduled to run (``valid_windows``), so the receiving engineer
    knows the deployment window without opening the source plan.
    """
    out_dir = Path(out_dir)
    cycles = [e.cycle_s for e in plan.entries]
    intersections = []
    for entry in plan.entries:
        meta = {p["phase"]: p for p in entry.intersection.get("phases", [])}
        rows = []
        for pno_str, split in sorted(
            entry.pattern.get("splits", {}).items(), key=lambda kv: int(kv[0])
        ):
            pno = int(pno_str)
            phase = meta.get(pno, {})
            yellow = float(phase.get("yellow_s", 0.0))
            all_red = float(phase.get("all_red_s", 0.0))
            rows.append(
                {
                    "nema_phase": pno,
                    "movements": list(phase.get("movements", [])),
                    "split_s": float(split),
                    "green_s": round(float(split) - yellow - all_red, 1),
                    "yellow_s": yellow,
                    "all_red_s": all_red,
                    "notes": "",
                }
            )
        intersections.append(
            {
                "tls_id": entry.tls_id,
                "pattern_id": entry.program_id,
                "description": str(entry.pattern.get("description", "")),
                "valid_windows": _pattern_windows(entry.intersection, entry.program_id),
                "offset_s": entry.offset_s,
                "cycle_s": entry.cycle_s,
                "phases": rows,
            }
        )

    payload = {
        "schema": NEMA_TIMING_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corridor_id": corridor_id,
        "handoff_ready": True,
        "coordination": {
            "cycle_length_s": max(cycles),
            "reference_intersection": plan.entries[0].tls_id,
        },
        "intersections": intersections,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / JSON_FILENAME).write_text(json.dumps(payload, indent=2))
    return payload


#: Tolerance of the splits-fill-the-cycle check (float sums of phase durations).
_SPLIT_SUM_TOLERANCE_S = 1e-6


def _stage_rows(signal_plan: SignalPlan) -> tuple[list[dict], bool, float]:
    """Group the program, read as a loop, into NEMA stage rows.

    The rows start at the first green phase. Every phase after a green, up to
    the next green, is that green's clearance: yellow phases add to ``yellow_s``,
    all-red and ``"other"`` phases (no vehicle movement is green) to ``all_red_s``.
    The non-green phases that open the program run after the last green when the
    program loops, so they are the last green's clearance.

    Returns ``(rows, all_greens_numbered, leading_s)``: ``all_greens_numbered`` is
    True iff every green phase in this plan has non-empty ``nema_phases``;
    ``leading_s`` is the duration of the phases before the first green (the
    offset shift that makes ``offset_s`` refer to the first exported stage).

    Raises:
        ValueError: the program has no green phase, or its stage splits do not
            add up to its cycle (both name the TLS).
    """
    phases = signal_plan.phases
    first_green = next(
        (index for index, spec in enumerate(phases) if spec.kind == "green"), None
    )
    if first_green is None:
        raise ValueError(
            f"TLS {signal_plan.tls_id!r}: the program has no green phase, so it has "
            "no NEMA stage to export"
        )
    leading = phases[:first_green]
    leading_s = sum(spec.duration_s for spec in leading)
    ordered = phases[first_green:] + leading
    last_green = max(index for index, spec in enumerate(ordered) if spec.kind == "green")

    rows: list[dict] = []
    splits: list[float] = []
    all_greens_numbered = True
    i = 0
    while i < len(ordered):
        spec = ordered[i]
        stage_index = i
        green_s = spec.duration_s
        yellow_s = 0.0
        all_red_s = 0.0
        no_vehicle_green_s = 0.0
        i += 1
        while i < len(ordered) and ordered[i].kind != "green":
            follower = ordered[i]
            if follower.kind == "yellow":
                yellow_s += follower.duration_s
            else:
                all_red_s += follower.duration_s
                if follower.kind == "other":
                    no_vehicle_green_s += follower.duration_s
            i += 1
        if not spec.nema_phases:
            all_greens_numbered = False
        notes = []
        if stage_index == last_green and leading:
            notes.append(
                f"all_red_s includes the program's {leading_s:g} s before its first "
                "green; offset_s refers to the start of the first stage"
            )
        if no_vehicle_green_s > 0.0:
            notes.append(
                f"all_red_s includes {no_vehicle_green_s:g} s without any vehicle green"
            )
        splits.append(green_s + yellow_s + all_red_s)
        rows.extend(_rows_for_green(spec, green_s, yellow_s, all_red_s, notes))
    _check_splits_fill_cycle(signal_plan, splits)
    return rows, all_greens_numbered, leading_s


def _check_splits_fill_cycle(signal_plan: SignalPlan, splits: list[float]) -> None:
    """Raise unless the stage splits of ``signal_plan`` add up to its cycle."""
    total = sum(splits)
    if abs(total - signal_plan.cycle_s) > _SPLIT_SUM_TOLERANCE_S:
        raise ValueError(
            f"TLS {signal_plan.tls_id!r}: the NEMA stage splits add up to {total:g} s "
            f"but the program's cycle is {signal_plan.cycle_s:g} s"
        )


def _rows_for_green(
    spec: PhaseSpec,
    green_s: float,
    yellow_s: float,
    all_red_s: float,
    notes: list[str] | tuple[str, ...] = (),
) -> list[dict]:
    """One row per NEMA phase number; a single null row when numbers are unknown.

    ``notes`` (about the stage's clearance) are joined after the concurrent-stage
    note, with "; ".
    """
    split_s = green_s + yellow_s + all_red_s
    base = {
        "movements": list(spec.movements),
        "split_s": split_s,
        "green_s": green_s,
        "yellow_s": yellow_s,
        "all_red_s": all_red_s,
        "notes": "; ".join(notes),
    }
    if not spec.nema_phases:
        return [{"nema_phase": None, **base}]
    if len(spec.nema_phases) == 1:
        return [{"nema_phase": spec.nema_phases[0], **base}]
    shared = "+".join(str(n) for n in spec.nema_phases)
    note = "; ".join(
        [f"concurrent stage: phases {shared} share one green interval", *notes]
    )
    return [
        {"nema_phase": number, **base, "notes": note}
        for number in spec.nema_phases
    ]
