"""Initial green-split solvers: turn counts -> a coordinated ``CorridorPlan``.

The pipeline needs a defensible starting plan when no baseline timing sheet is
available. This module implements the classic Webster procedure over a simple
stage-based decomposition (stages run strictly sequentially, no ring overlap):

1. :func:`stages_from_counts` collapses TMC-style turning-movement counts into
   stages: ``mode="two_stage"`` (default) gives all E-W movements and all N-S
   movements one stage each, carrying the critical-approach volume;
   ``mode="per_approach"`` gives every approach its own protected stage
   (long-cycle style), carrying that approach's volume and a lane-aware
   saturation flow; an approach with controlled lanes but no demand still gets
   its stage, held at the minimum green.
2. :func:`webster_cycle` computes the Webster optimum cycle
   ``C0 = (1.5 L + 5) / (1 - Y)`` per intersection, ``Y`` being the sum of the
   stage flow ratios (volume / saturation flow).
3. :class:`WebsterSolver` distributes the effective green ``C - L``
   proportionally to stage flow ratios, flooring every stage at
   ``min_green_s``.
4. :func:`build_webster_plan` assembles the per-TLS plans under one corridor
   cycle — the MAX of the per-intersection Webster cycles (a shorter cycle
   would oversaturate the busiest intersection; the others just get slack).

Green-time splitting is the closed-form :class:`WebsterSolver`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from terasim_city.corridor.resolve import ResolvedCorridor
from terasim_city.flow.descriptors import CANONICAL_DIRECTIONS, CANONICAL_TURNS, TMCData
from terasim_city.plans.model import (
    MOVEMENTS,
    CorridorPlan,
    PhaseSpec,
    PlanProvenance,
    SignalPlan,
)
from terasim_city.plans.signal_groups import (
    LinkMovement,
    approach_lane_counts,
    build_state,
    controlled_link_count,
    derive_link_movements,
    load_overrides,
    yield_permissive_lefts,
)

#: Saturation flow rate of one lane (veh/h): the default stage saturation flow
#: in Webster flow ratios, and the per-lane unit of per-approach stages.
SATURATION_FLOW_VPH = 1800.0

#: Legal stage decompositions for the Webster seed.
STAGE_MODES: tuple[str, ...] = ("two_stage", "per_approach")
#: per_approach stage order: approach from north, east, south, west (clockwise).
PER_APPROACH_ORDER: tuple[str, ...] = ("SB", "WB", "NB", "EB")

#: plan_id (and per-TLS program_id) of every Webster-initialised plan.
WEBSTER_PLAN_ID = "webster_init"

#: Change-interval defaults shared by the solvers and plan assembly.
DEFAULT_MIN_GREEN_S = 5.0
DEFAULT_YELLOW_S = 3.0
DEFAULT_ALL_RED_S = 1.0

_EPS = 1e-9


@dataclass(frozen=True)
class TurnGroup:
    """Movements that receive green together in one (ring-less) stage.

    ``saturation_vph`` is the stage's saturation flow (veh/h); the Webster
    flow ratio is ``volume_vph / saturation_vph``. The default is one lane's
    worth (:data:`SATURATION_FLOW_VPH`), which is what every two-stage group
    uses.
    """

    movements: tuple[str, ...]
    volume_vph: float
    saturation_vph: float = SATURATION_FLOW_VPH

    def __post_init__(self) -> None:
        object.__setattr__(self, "movements", tuple(str(m) for m in self.movements))
        if not self.movements:
            raise ValueError("TurnGroup.movements must be non-empty")
        unknown = sorted(m for m in self.movements if m not in MOVEMENTS)
        if unknown:
            raise ValueError(
                f"TurnGroup.movements contains unknown movement(s) {unknown}; "
                f"allowed: {list(MOVEMENTS)}"
            )
        if len(set(self.movements)) != len(self.movements):
            raise ValueError(
                f"TurnGroup.movements contains duplicates: {list(self.movements)}"
            )
        volume = float(self.volume_vph)
        if not math.isfinite(volume) or volume < 0.0:
            raise ValueError(
                f"TurnGroup.volume_vph must be finite and >= 0, got {self.volume_vph!r}"
            )
        object.__setattr__(self, "volume_vph", volume)
        saturation = float(self.saturation_vph)
        if not math.isfinite(saturation) or saturation <= 0.0:
            raise ValueError(
                f"TurnGroup.saturation_vph must be finite and > 0, got {self.saturation_vph!r}"
            )
        object.__setattr__(self, "saturation_vph", saturation)

    @property
    def flow_ratio(self) -> float:
        """Webster flow ratio ``y = volume / saturation flow``."""
        return self.volume_vph / self.saturation_vph


@dataclass(frozen=True)
class GroupTiming:
    """Solved green duration for one :class:`TurnGroup`."""

    movements: tuple[str, ...]
    green_s: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "movements", tuple(str(m) for m in self.movements))
        if not self.movements:
            raise ValueError("GroupTiming.movements must be non-empty")
        green = float(self.green_s)
        if not math.isfinite(green) or green <= 0.0:
            raise ValueError(
                f"GroupTiming.green_s must be finite and > 0, got {self.green_s!r}"
            )
        object.__setattr__(self, "green_s", green)


class WebsterSolver:
    """Proportional (Webster) split: green share follows stage flow ratio.

    Stages run strictly sequentially, so lost time is
    ``n_stages * (yellow_s + all_red_s)`` and the greens must sum EXACTLY to
    ``cycle_s`` minus that lost time. Stages whose proportional share falls
    below ``min_green_s`` are pinned to the minimum and the remainder is
    redistributed among the rest.
    """

    name = "webster"

    def solve(
        self,
        groups: Sequence[TurnGroup],
        cycle_s: float,
        *,
        min_green_s: float = DEFAULT_MIN_GREEN_S,
        yellow_s: float = DEFAULT_YELLOW_S,
        all_red_s: float = DEFAULT_ALL_RED_S,
    ) -> tuple[GroupTiming, ...]:
        groups = tuple(groups)
        _validate_solve_args(
            groups, cycle_s, min_green_s=min_green_s, yellow_s=yellow_s, all_red_s=all_red_s
        )
        n = len(groups)
        lost_s = n * (float(yellow_s) + float(all_red_s))
        effective_s = float(cycle_s) - lost_s
        required_s = n * float(min_green_s)
        if effective_s + _EPS < required_s:
            raise ValueError(
                f"cycle {cycle_s:g}s is infeasible for {n} stage(s): effective green "
                f"{effective_s:g}s (cycle minus lost time {lost_s:g}s) is below the "
                f"{required_s:g}s needed for min_green_s={min_green_s:g}"
            )

        greens = [0.0] * n
        pending = list(range(n))
        remaining = effective_s
        while pending:
            total_ratio = sum(groups[i].flow_ratio for i in pending)
            if total_ratio <= 0.0:
                # No demand information left: split the remainder evenly.
                share = remaining / len(pending)
                for i in pending:
                    greens[i] = share
                break
            shares = {
                i: remaining * groups[i].flow_ratio / total_ratio for i in pending
            }
            starved = [i for i in pending if shares[i] < min_green_s - _EPS]
            if not starved:
                for i in pending:
                    greens[i] = shares[i]
                break
            # Pin starved stages at min green, redistribute the rest. The
            # feasibility check above guarantees remaining >= len(pending) *
            # min_green_s at every iteration, so this always terminates with
            # every stage at or above the minimum.
            for i in starved:
                greens[i] = float(min_green_s)
            remaining -= float(min_green_s) * len(starved)
            pending = [i for i in pending if i not in starved]

        return tuple(
            GroupTiming(movements=groups[i].movements, green_s=greens[i])
            for i in range(n)
        )


def webster_cycle(
    groups: Sequence[TurnGroup],
    *,
    yellow_s: float = DEFAULT_YELLOW_S,
    all_red_s: float = DEFAULT_ALL_RED_S,
    min_cycle_s: float = 45.0,
    max_cycle_s: float = 150.0,
) -> float:
    """Webster optimum cycle ``C0 = (1.5 L + 5) / (1 - Y)``, clamped and rounded.

    ``L`` is the total lost time (one ``yellow_s + all_red_s`` per stage) and
    ``Y`` the sum of stage flow ratios ``volume / saturation_vph`` (see
    :attr:`TurnGroup.flow_ratio`), capped at 0.9 so oversaturated inputs yield
    the maximum cycle instead of a division blow-up. The result is clamped to
    ``[min_cycle_s, max_cycle_s]`` and rounded to a whole second.
    """
    groups = tuple(groups)
    if not groups:
        raise ValueError("webster_cycle requires at least one TurnGroup")
    if float(yellow_s) < 0.0 or float(all_red_s) < 0.0:
        raise ValueError(
            f"yellow_s/all_red_s must be >= 0, got {yellow_s!r}/{all_red_s!r}"
        )
    if not 0.0 < float(min_cycle_s) <= float(max_cycle_s):
        raise ValueError(
            f"need 0 < min_cycle_s <= max_cycle_s, got {min_cycle_s!r}/{max_cycle_s!r}"
        )
    lost_s = len(groups) * (float(yellow_s) + float(all_red_s))
    flow_ratio = min(sum(g.flow_ratio for g in groups), 0.9)
    c0 = (1.5 * lost_s + 5.0) / (1.0 - flow_ratio)
    return float(round(min(max(c0, float(min_cycle_s)), float(max_cycle_s))))


def stages_from_counts(
    counts: Mapping[str, Mapping[str, float]],
    *,
    scale: float = 1.0,
    mode: str = "two_stage",
    lanes: Mapping[str, int] | None = None,
) -> tuple[TurnGroup, ...]:
    """Stage decomposition of TMC-style counts ``{DIR: {turn: veh}}``.

    ``mode="two_stage"`` (default): stage 1 serves every E-W movement with the
    critical-approach volume ``max(sum(EB), sum(WB))``; stage 2 serves every
    N-S movement with ``max(sum(NB), sum(SB))``. Both keep the default
    saturation flow and ``lanes`` is ignored.

    ``mode="per_approach"``: one stage per approach, in
    :data:`PER_APPROACH_ORDER`, serving all three movements of that approach
    with its total volume. The stage's saturation flow is the approach's lane
    count times :data:`SATURATION_FLOW_VPH`; ``lanes`` maps direction to
    incoming lane count, and a direction it does not list (or lists below 1)
    counts as one lane. An approach gets a stage when it carries demand or when
    ``lanes`` lists it with at least one lane (the signal controls vehicle links
    there): a real approach is never left permanently red, and the solver holds
    a zero-volume stage at the minimum green. No approach with demand raises
    ValueError.

    Directions absent from ``counts`` contribute 0. Unknown direction/turn
    keys, negative counts or an unknown ``mode`` raise ValueError.

    ``scale`` converts window counts to hourly volumes (``3600 /
    observation_seconds``); callers that already hold veh/h keep the default.
    A ``scale`` that is not finite and > 0 raises ValueError.
    """
    if mode not in STAGE_MODES:
        raise ValueError(f"mode must be one of {STAGE_MODES}, got {mode!r}")
    scale = float(scale)
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError(f"scale must be finite and > 0, got {scale!r}")
    per_direction: dict[str, float] = {}
    for direction, turns in counts.items():
        if direction not in CANONICAL_DIRECTIONS:
            raise ValueError(
                f"counts contains unknown direction {direction!r}; "
                f"allowed: {list(CANONICAL_DIRECTIONS)}"
            )
        if not isinstance(turns, Mapping):
            raise ValueError(
                f"counts[{direction!r}] must be a mapping turn -> count, "
                f"got {type(turns).__name__}"
            )
        total = 0.0
        for turn, raw_value in turns.items():
            if turn not in CANONICAL_TURNS:
                raise ValueError(
                    f"counts[{direction!r}] contains unknown turn {turn!r}; "
                    f"allowed: {list(CANONICAL_TURNS)}"
                )
            value = float(raw_value)
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(
                    f"counts[{direction!r}][{turn!r}] must be finite and >= 0, "
                    f"got {raw_value!r}"
                )
            total += value
        per_direction[direction] = total * scale

    if mode == "per_approach":
        if not any(per_direction.get(d, 0.0) > 0.0 for d in PER_APPROACH_ORDER):
            raise ValueError("per_approach stages: no approach carries demand")
        groups: list[TurnGroup] = []
        for direction in PER_APPROACH_ORDER:
            volume = per_direction.get(direction, 0.0)
            has_lanes = int((lanes or {}).get(direction, 0)) >= 1
            if volume <= 0.0 and not has_lanes:
                continue
            n_lanes = max(1, int((lanes or {}).get(direction, 1)))
            groups.append(
                TurnGroup(
                    movements=tuple(
                        m for m in MOVEMENTS if m.startswith(direction + "-")
                    ),
                    volume_vph=volume,
                    saturation_vph=n_lanes * SATURATION_FLOW_VPH,
                )
            )
        return tuple(groups)

    ew_volume = max(per_direction.get("EB", 0.0), per_direction.get("WB", 0.0))
    ns_volume = max(per_direction.get("NB", 0.0), per_direction.get("SB", 0.0))
    ew_movements = tuple(m for m in MOVEMENTS if m.startswith(("EB-", "WB-")))
    ns_movements = tuple(m for m in MOVEMENTS if m.startswith(("NB-", "SB-")))
    return (
        TurnGroup(movements=ew_movements, volume_vph=ew_volume),
        TurnGroup(movements=ns_movements, volume_vph=ns_volume),
    )


def build_webster_plan(
    tmc_json: Path,
    corridor: ResolvedCorridor,
    net_file: Path,
    *,
    overrides_path: Path | None = None,
    stage_mode: str = "two_stage",
    cycle_min_s: float = 45.0,
    cycle_max_s: float = 150.0,
) -> CorridorPlan:
    """Build a Webster-initialised ``CorridorPlan`` for a resolved corridor.

    The corridor-wide common cycle is the MAX of the per-intersection
    :func:`webster_cycle` values (locked rule: a shorter cycle would starve the
    busiest intersection), each clamped to ``[cycle_min_s, cycle_max_s]``.
    Splits come from :class:`WebsterSolver` at that common cycle; every offset
    is 0.0 — offsets are the Bayesian optimizer's job, not the initialiser's.

    ``stage_mode`` (one of :data:`STAGE_MODES`) picks the stage decomposition:
    ``"two_stage"`` (E-W / N-S, lefts permissive against the opposing through)
    or ``"per_approach"`` (one protected stage per approach; the approach lane
    counts are read from the net so the flow ratios are lane-aware, and every
    approach with controlled vehicle links gets its stage even without demand,
    at the minimum green). The mode is recorded in the provenance as
    ``stage_mode``.

    TMC counts cover one observation window, while Webster's flow ratios are
    hourly: the counts are scaled by ``3600 / observation_seconds`` (a
    10-minute window fed raw would read 6x too light and collapse the cycle to
    its floor). The factor is recorded in the provenance as ``count_scale``.

    Raises:
        ValueError: Unknown ``stage_mode``; a corridor intersection has no TMC
            counterpart (counts are required to size the splits); or split
            solving is infeasible.
    """
    if stage_mode not in STAGE_MODES:
        raise ValueError(
            f"stage_mode must be one of {STAGE_MODES}, got {stage_mode!r}"
        )
    solver = WebsterSolver()
    plan_id = f"{solver.name}_init"  # "webster_init"
    tmc = TMCData.from_json(Path(tmc_json))
    scale = 3600.0 / tmc.observation_seconds()
    tmc_by_id = {inter.intersection_id: inter for inter in tmc.intersections}
    overrides = load_overrides(overrides_path) if overrides_path is not None else {}

    staged: list[tuple[str, tuple[TurnGroup, ...]]] = []
    missing: list[str] = []
    for item in corridor.intersections:
        if item.intersection_id is None or item.intersection_id not in tmc_by_id:
            missing.append(f"tls {item.tls_id!r} (node {item.node_id!r})")
            continue
        lanes = (
            approach_lane_counts(
                Path(net_file), item.tls_id, overrides=overrides.get(item.tls_id)
            )
            if stage_mode == "per_approach"
            else None
        )
        staged.append(
            (
                item.tls_id,
                stages_from_counts(
                    tmc_by_id[item.intersection_id].counts,
                    scale=scale,
                    mode=stage_mode,
                    lanes=lanes,
                ),
            )
        )
    if missing:
        available = ", ".join(sorted(tmc_by_id)) or "(none)"
        raise ValueError(
            f"{tmc_json}: no TMC counts for corridor intersection(s) "
            f"{'; '.join(missing)}; TMC intersections available: {available}"
        )

    common_cycle_s = max(
        webster_cycle(groups, min_cycle_s=cycle_min_s, max_cycle_s=cycle_max_s)
        for _tls_id, groups in staged
    )

    plans: list[SignalPlan] = []
    for tls_id, groups in staged:
        timings = solver.solve(
            groups,
            common_cycle_s,
            min_green_s=DEFAULT_MIN_GREEN_S,
            yellow_s=DEFAULT_YELLOW_S,
            all_red_s=DEFAULT_ALL_RED_S,
        )
        link_movements = derive_link_movements(
            Path(net_file), tls_id, overrides=overrides.get(tls_id)
        )
        # State length is anchored to the NET's own tlLogic (what SUMO
        # validates against), not to the vehicle links: cluster junctions also
        # control pedestrian crossings, which pad red in every phase.
        state_length = controlled_link_count(Path(net_file), tls_id)
        specs: list[PhaseSpec] = []
        for stage_no, timing in enumerate(timings, start=1):
            specs.extend(
                _stage_specs(
                    timing,
                    stage_no=stage_no,
                    link_movements=link_movements,
                    state_length=state_length,
                )
            )
        plans.append(
            SignalPlan(
                tls_id=tls_id,
                program_id=plan_id,
                offset_s=0.0,
                phases=tuple(specs),
            )
        )

    return CorridorPlan(
        plan_id=plan_id,
        plans=tuple(plans),
        provenance=PlanProvenance(
            source=plan_id,
            params=(
                ("cycle_s", common_cycle_s),
                ("count_scale", scale),
                ("stage_mode", stage_mode),
            ),
        ),
    )


def _stage_specs(
    timing: GroupTiming,
    *,
    stage_no: int,
    link_movements: tuple[LinkMovement, ...],
    state_length: int,
) -> list[PhaseSpec]:
    """Expand one solved stage into green + yellow + all_red PhaseSpecs."""
    movement_set = set(timing.movements)
    yellow_chars = ["r"] * state_length
    for lm in link_movements:
        if lm.movement in movement_set:
            yellow_chars[lm.link_index] = "y"
    return [
        PhaseSpec(
            # Lefts sharing a stage with their opposing through are permissive
            # and must yield ('g'), matching the dual-ring expansion's rule.
            state=yield_permissive_lefts(
                build_state(link_movements, movement_set, n_links=state_length),
                link_movements,
            ),
            duration_s=timing.green_s,
            kind="green",
            name=f"stage{stage_no}_green",
            movements=timing.movements,
        ),
        PhaseSpec(
            state="".join(yellow_chars),
            duration_s=DEFAULT_YELLOW_S,
            kind="yellow",
            name=f"stage{stage_no}_yellow",
            movements=timing.movements,
        ),
        PhaseSpec(
            state="r" * state_length,
            duration_s=DEFAULT_ALL_RED_S,
            kind="all_red",
            name=f"stage{stage_no}_all_red",
        ),
    ]


def _validate_solve_args(
    groups: tuple[TurnGroup, ...],
    cycle_s: float,
    *,
    min_green_s: float,
    yellow_s: float,
    all_red_s: float,
) -> None:
    """Shared argument validation for the Webster solver."""
    if not groups:
        raise ValueError("solve requires at least one TurnGroup")
    if not float(cycle_s) > 0.0:
        raise ValueError(f"cycle_s must be > 0, got {cycle_s!r}")
    if not float(min_green_s) > 0.0:
        raise ValueError(f"min_green_s must be > 0, got {min_green_s!r}")
    if float(yellow_s) < 0.0 or float(all_red_s) < 0.0:
        raise ValueError(
            f"yellow_s/all_red_s must be >= 0, got {yellow_s!r}/{all_red_s!r}"
        )
