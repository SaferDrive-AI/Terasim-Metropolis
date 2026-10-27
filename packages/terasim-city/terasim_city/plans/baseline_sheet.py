"""Ingest an engineering timing sheet (JSON) into a :class:`CorridorPlan`.

A timing sheet describes each intersection's stages the way a signal-timing
engineer would — NEMA phase numbers, served movements, and green/yellow/all-red
splits against a corridor-wide cycle — without any knowledge of SUMO link
indices. This module expands each stage into concrete SUMO ``PhaseSpec`` state
strings via :mod:`terasim_city.plans.signal_groups`.

Schema (``terasim.timing_sheet.v1``)::

    {"schema": "terasim.timing_sheet.v1", "corridor_id": "...", "cycle_s": 90,
     "intersections": [
       {"intersection_id": "i1", "tls_id": "J1", "offset_s": 0,
        "phases": [
          {"nema_phases": [2, 6], "movements": ["EB-through", ...],
           "green_s": 40, "yellow_s": 3, "all_red_s": 1},
          ...]}]}

``tls_id`` may be null in the sheet, in which case the caller must provide a
``tls_by_intersection`` mapping (typically built from a resolved corridor).
Every intersection's phase durations must sum to the corridor ``cycle_s``
(within 0.5 s) — a sheet that does not close its cycle is rejected, never
"fixed" silently.

A sheet may describe more signals than one run needs (a corridor's sheet, a
request for one of its intersections): ``tls_ids`` then picks the signals to load,
and a requested signal the sheet has no entry for raises
:class:`TimingSheetCoverageError`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

from terasim_city.plans.model import (
    CYCLE_TOLERANCE_S,
    MOVEMENTS,
    CorridorPlan,
    PhaseSpec,
    PlanProvenance,
    SignalPlan,
)
from terasim_city.plans.signal_groups import (
    LinkMovement,
    build_state,
    controlled_link_count,
    derive_link_movements,
    load_overrides,
    yield_permissive_lefts,
)

TIMING_SHEET_SCHEMA = "terasim.timing_sheet.v1"

#: plan_id (and per-TLS program_id) of every sheet-derived plan.
BASELINE_PLAN_ID = "baseline"


class TimingSheetCoverageError(ValueError):
    """The timing sheet does not describe every signal the run covers."""


def load_timing_sheet(
    path: Path,
    *,
    net_file: Path,
    tls_by_intersection: Mapping[str, str] | None = None,
    overrides_path: Path | None = None,
    tls_ids: Sequence[str] | None = None,
) -> CorridorPlan:
    """Load a v1 timing sheet and expand it into a ``CorridorPlan``.

    Each sheet phase becomes up to three ``PhaseSpec`` entries (green, yellow,
    all-red; zero-duration entries are skipped). Green/yellow states are built
    from the movements served, using link movements derived from ``net_file``
    (optionally corrected by a signal-groups overrides file, see
    :func:`terasim_city.plans.signal_groups.load_overrides`).

    ``tls_ids`` (optional) loads only the entries of those signals, in sheet order:
    entries of other signals, and entries whose signal neither the sheet nor
    ``tls_by_intersection`` names, are left out unread.

    Raises:
        FileNotFoundError: ``path`` (or ``overrides_path``) does not exist.
        TimingSheetCoverageError: a signal of ``tls_ids`` has no entry in the sheet.
        ValueError: schema mismatch, unknown movement, per-intersection phase
            durations not summing to ``cycle_s`` (±0.5 s), or (without
            ``tls_ids``) an intersection without a ``tls_id`` and no
            ``tls_by_intersection`` entry.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"timing sheet not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object, got {type(payload).__name__}")

    schema = payload.get("schema")
    if schema != TIMING_SHEET_SCHEMA:
        raise ValueError(
            f"{path}: 'schema' must be {TIMING_SHEET_SCHEMA!r}, got {schema!r}"
        )

    cycle_s = _require_number(payload, "cycle_s", where=str(path))
    if cycle_s <= 0.0:
        raise ValueError(f"{path}: 'cycle_s' must be > 0, got {cycle_s!r}")

    raw_intersections = payload.get("intersections")
    if not isinstance(raw_intersections, list) or not raw_intersections:
        raise ValueError(f"{path}: 'intersections' must be a non-empty list")

    overrides = load_overrides(overrides_path) if overrides_path is not None else {}
    wanted = None if tls_ids is None else [str(t) for t in tls_ids]
    described: list[str] = []  # the signals (else intersections) the sheet names

    plans: list[SignalPlan] = []
    for position, entry in enumerate(raw_intersections):
        where = f"{path}: intersections[{position}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object, got {type(entry).__name__}")
        intersection_id = entry.get("intersection_id")
        if not intersection_id:
            raise ValueError(f"{where} is missing 'intersection_id'")
        intersection_id = str(intersection_id)
        where = f"{path}: intersection {intersection_id!r}"

        if wanted is not None:
            tls_id = _sheet_tls_id(entry, intersection_id, tls_by_intersection)
            described.append(tls_id or f"intersection {intersection_id!r}")
            if tls_id not in wanted:
                continue  # another signal's entry: not part of this plan
        else:
            tls_id = _resolve_tls_id(
                entry, intersection_id, tls_by_intersection, where=where
            )
        offset_s = float(entry.get("offset_s", 0.0))

        raw_phases = entry.get("phases")
        if not isinstance(raw_phases, list) or not raw_phases:
            raise ValueError(f"{where}: 'phases' must be a non-empty list")

        link_movements = derive_link_movements(
            Path(net_file), tls_id, overrides=overrides.get(tls_id)
        )
        # State length is anchored to the NET's own tlLogic (what SUMO
        # validates against): cluster junctions control pedestrian-crossing
        # links beyond the vehicle connections, which pad red in every phase.
        state_length = controlled_link_count(Path(net_file), tls_id)

        specs: list[PhaseSpec] = []
        for stage_no, raw_phase in enumerate(raw_phases, start=1):
            specs.extend(
                _expand_stage(
                    raw_phase,
                    stage_no=stage_no,
                    link_movements=link_movements,
                    state_length=state_length,
                    where=f"{where} phases[{stage_no - 1}]",
                )
            )

        total_s = sum(spec.duration_s for spec in specs)
        if abs(total_s - cycle_s) > CYCLE_TOLERANCE_S:
            breakdown = ", ".join(
                f"{spec.kind}={spec.duration_s:g}s" for spec in specs
            )
            raise ValueError(
                f"{where}: phase durations sum to {total_s:g}s but 'cycle_s' is "
                f"{cycle_s:g}s (tolerance {CYCLE_TOLERANCE_S}s); breakdown: {breakdown}"
            )

        plans.append(
            SignalPlan(
                tls_id=tls_id,
                program_id=BASELINE_PLAN_ID,
                offset_s=offset_s,
                phases=tuple(specs),
            )
        )

    if wanted is not None:
        loaded = {signal_plan.tls_id for signal_plan in plans}
        missing = [tls_id for tls_id in wanted if tls_id not in loaded]
        if missing:
            raise TimingSheetCoverageError(
                f"timing sheet {path} has no entry for signal(s) {missing} of the "
                f"requested {wanted}; it describes {described}"
            )

    return CorridorPlan(
        plan_id=BASELINE_PLAN_ID,
        plans=tuple(plans),
        provenance=PlanProvenance(source="baseline_sheet"),
    )


def _expand_stage(
    raw_phase: dict,
    *,
    stage_no: int,
    link_movements: tuple[LinkMovement, ...],
    state_length: int,
    where: str,
) -> list[PhaseSpec]:
    """Expand one sheet stage into green/yellow/all_red PhaseSpecs."""
    if not isinstance(raw_phase, dict):
        raise ValueError(f"{where} must be an object, got {type(raw_phase).__name__}")

    raw_movements = raw_phase.get("movements")
    if not isinstance(raw_movements, list) or not raw_movements:
        raise ValueError(f"{where}: 'movements' must be a non-empty list")
    movements = tuple(str(m) for m in raw_movements)
    unknown = sorted(m for m in movements if m not in MOVEMENTS)
    if unknown:
        raise ValueError(
            f"{where}: unknown movement(s) {unknown}; allowed: {list(MOVEMENTS)}"
        )
    nema_phases = tuple(int(p) for p in raw_phase.get("nema_phases", ()))

    green_s = _require_number(raw_phase, "green_s", where=where)
    yellow_s = _optional_number(raw_phase, "yellow_s", where=where)
    all_red_s = _optional_number(raw_phase, "all_red_s", where=where)
    for name, value in (("green_s", green_s), ("yellow_s", yellow_s), ("all_red_s", all_red_s)):
        if value < 0.0:
            raise ValueError(f"{where}: {name!r} must be >= 0, got {value!r}")

    movement_set = set(movements)
    specs: list[PhaseSpec] = []
    if green_s > 0.0:
        specs.append(
            PhaseSpec(
                # Lefts sharing a stage with their opposing through are
                # permissive and must yield ('g') — same rule as the
                # dual-ring expansion.
                state=yield_permissive_lefts(
                    build_state(link_movements, movement_set, n_links=state_length),
                    link_movements,
                ),
                duration_s=green_s,
                kind="green",
                name=f"stage{stage_no}_green",
                nema_phases=nema_phases,
                movements=movements,
            )
        )
    if yellow_s > 0.0:
        yellow_chars = ["r"] * state_length
        for lm in link_movements:
            if lm.movement in movement_set:
                yellow_chars[lm.link_index] = "y"
        specs.append(
            PhaseSpec(
                state="".join(yellow_chars),
                duration_s=yellow_s,
                kind="yellow",
                name=f"stage{stage_no}_yellow",
                nema_phases=nema_phases,
                movements=movements,
            )
        )
    if all_red_s > 0.0:
        specs.append(
            PhaseSpec(
                state="r" * state_length,
                duration_s=all_red_s,
                kind="all_red",
                name=f"stage{stage_no}_all_red",
            )
        )
    return specs


def _sheet_tls_id(
    entry: dict, intersection_id: str, tls_by_intersection: Mapping[str, str] | None
) -> str | None:
    """The signal of a sheet entry: its own ``tls_id``, else the mapping's, else None."""
    if entry.get("tls_id"):
        return str(entry["tls_id"])
    if tls_by_intersection and intersection_id in tls_by_intersection:
        return str(tls_by_intersection[intersection_id])
    return None


def _resolve_tls_id(
    entry: dict,
    intersection_id: str,
    tls_by_intersection: Mapping[str, str] | None,
    *,
    where: str,
) -> str:
    """Take tls_id from the sheet, falling back to the caller's mapping."""
    tls_id = entry.get("tls_id")
    if tls_id:
        return str(tls_id)
    if tls_by_intersection and intersection_id in tls_by_intersection:
        return str(tls_by_intersection[intersection_id])
    raise ValueError(
        f"{where}: no 'tls_id' in the sheet and no tls_by_intersection entry for "
        f"{intersection_id!r}; pass tls_by_intersection={{intersection_id: tls_id}} "
        f"(e.g. from a resolved corridor)"
    )


def _require_number(payload: dict, key: str, *, where: str) -> float:
    """Fetch a required numeric field, failing loud when missing/non-numeric."""
    if key not in payload:
        raise ValueError(f"{where}: missing required key {key!r}")
    return _as_number(payload[key], key, where=where)


def _optional_number(payload: dict, key: str, *, where: str) -> float:
    """Fetch an optional numeric field (absent -> 0.0)."""
    if key not in payload:
        return 0.0
    return _as_number(payload[key], key, where=where)


def _as_number(value, key: str, *, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: {key!r} must be a number, got {value!r}")
    return float(value)
