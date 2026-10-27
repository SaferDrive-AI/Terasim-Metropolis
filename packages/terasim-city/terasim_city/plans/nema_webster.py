"""Demand-timed pattern on an official NEMA skeleton.

When the government schedule says an hour runs "free" (actuated, no fixed
coordination plan), the plan document still carries data that is valid around
the clock: the dual-ring structure, which phases serve which movements
(including protected lefts), the barrier pairing and the clearance times.
Only the NUMBERS for that hour — cycle and per-phase splits — are absent.

This module fills in exactly those numbers from measured demand (a Webster
allocation constrained by the dual-ring barrier rule), so a free-period seed
keeps the official phase structure instead of being replaced by a synthetic
two-stage plan. The result is a normal pattern dict that passes
``check_pattern_timing`` and materializes through the standard static
expansion.

Barrier rule: within each barrier segment both rings run concurrently, so the
sum of ring-1 splits in a segment must equal the sum of ring-2 splits in the
same segment; the cycle is the sum of the segment durations.
"""

from __future__ import annotations

import logging

from terasim_city.plans.init_splits import SATURATION_FLOW_VPH
from terasim_city.plans.nema_materialize import (
    DEFAULT_ALL_RED_S,
    DEFAULT_MIN_GREEN_S,
    DEFAULT_YELLOW_S,
    check_pattern_timing,
)

__all__ = [
    "FREE_PATTERN_ID",
    "phase_demand_veh_h",
    "recommended_cycle_s",
    "synthesize_free_pattern",
]

logger = logging.getLogger(__name__)

FREE_PATTERN_ID = "webster_free"

#: Webster cycle bounds (s). Below the floor coordination is pointless; above
#: the cap the formula is extrapolating an oversaturated corridor.
MIN_CYCLE_S = 45.0
MAX_CYCLE_S = 180.0


def phase_demand_veh_h(
    intersection: dict, counts: dict, *, scale: float = 1.0
) -> dict[int, float]:
    """Per-phase demand: sum of the TMC counts of the phase's movements.

    ``counts`` is the canonical TMC form (``{"EB": {"through": 1000, ...}}``);
    movements are ``"EB-through"`` strings. A movement absent from the counts
    contributes zero (the phase still exists — it just saw no demand).

    ``scale`` converts window counts to veh/h (``3600 / observation_seconds``);
    the default leaves counts that are already hourly unchanged.
    """
    demand: dict[int, float] = {}
    for phase in intersection.get("phases", []):
        total = 0.0
        for movement in phase.get("movements", []):
            direction, _, turn = str(movement).partition("-")
            total += float((counts.get(direction) or {}).get(turn, 0))
        demand[int(phase["phase"])] = total * float(scale)
    return demand


def _clearance(meta: dict) -> float:
    return float(meta.get("yellow_s", DEFAULT_YELLOW_S)) + float(
        meta.get("all_red_s", DEFAULT_ALL_RED_S)
    )


def _floor(meta: dict) -> float:
    return float(meta.get("min_green_s", DEFAULT_MIN_GREEN_S)) + _clearance(meta)


def _segments(rings: dict) -> list[tuple[list[int], list[int]]]:
    """Split (ring1, ring2) into barrier segments.

    A ring is cut AFTER each of its phases listed in ``barrier2_phases`` (end
    of the first concurrency group) and ``barrier_phases`` (end of the second,
    i.e. the cycle wrap). Rings without barrier members form one segment.
    Returns aligned ``(ring1_part, ring2_part)`` pairs; a ring missing from a
    segment contributes an empty list.
    """
    cut_after = set(rings.get("barrier2_phases") or []) | set(
        rings.get("barrier_phases") or []
    )

    def split(ring: list[int]) -> list[list[int]]:
        parts: list[list[int]] = []
        current: list[int] = []
        for pno in ring:
            current.append(int(pno))
            if int(pno) in cut_after:
                parts.append(current)
                current = []
        if current:
            parts.append(current)
        return parts or [[]]

    ring1_parts = split([int(p) for p in rings.get("ring1", []) if p])
    ring2_parts = split([int(p) for p in rings.get("ring2", []) if p])
    n = max(len(ring1_parts), len(ring2_parts))
    ring1_parts += [[]] * (n - len(ring1_parts))
    ring2_parts += [[]] * (n - len(ring2_parts))
    return list(zip(ring1_parts, ring2_parts))


def recommended_cycle_s(
    intersection: dict, counts: dict, *, scale: float = 1.0
) -> float:
    """Webster's cycle for this skeleton under the measured demand.

    ``C = (1.5 L + 5) / (1 - Y)`` where, per barrier segment, the CRITICAL
    ring is the one with the higher flow-ratio sum; ``Y`` sums the critical
    flow ratios and ``L`` the critical rings' clearance times. Clamped to
    ``[MIN_CYCLE_S, MAX_CYCLE_S]``; an (over)saturated ``Y`` pins the cap.

    ``scale`` converts window counts to veh/h (see :func:`phase_demand_veh_h`):
    the flow ratios are hourly, so a sub-hour window must be scaled up.
    """
    meta = {int(p["phase"]): p for p in intersection.get("phases", [])}
    demand = phase_demand_veh_h(intersection, counts, scale=scale)
    total_y = 0.0
    lost = 0.0
    for ring1_part, ring2_part in _segments(intersection.get("rings", {})):
        best_y, best_lost = 0.0, 0.0
        for part in (ring1_part, ring2_part):
            if not part:
                continue
            y = sum(demand.get(p, 0.0) for p in part) / SATURATION_FLOW_VPH
            part_lost = sum(_clearance(meta.get(p, {})) for p in part)
            if y > best_y or (y == best_y and part_lost > best_lost):
                best_y, best_lost = y, part_lost
        total_y += best_y
        lost += best_lost
    if total_y >= 0.9:  # (over)saturated: the formula diverges; use the cap
        return MAX_CYCLE_S
    cycle = (1.5 * lost + 5.0) / (1.0 - total_y)
    return min(max(cycle, MIN_CYCLE_S), MAX_CYCLE_S)


def _allocate(amount: float, weights: list[float], floors: list[float]) -> list[float]:
    """Split ``amount`` over items proportionally to ``weights``, respecting
    per-item ``floors`` and summing EXACTLY to ``amount`` (0.1 s grid)."""
    n = len(weights)
    spare = amount - sum(floors)
    if spare < -1e-9:
        raise ValueError(
            f"cannot fit minimum phase times ({sum(floors):.1f}s) into {amount:.1f}s"
        )
    total_w = sum(weights)
    shares = [w / total_w if total_w > 0 else 1.0 / n for w in weights]
    raw = [floors[i] + spare * shares[i] for i in range(n)]
    # 0.1 s grid, largest-remainder so the segment sum stays exact — but never
    # round an item below its floor: an item sitting exactly at its minimum
    # used to absorb a -0.1 drift correction and then fail the downstream
    # min-green check, silently discarding the whole seed plan.
    floors_scaled = [round(f * 10) for f in floors]
    scaled = [max(round(v * 10), fs) for v, fs in zip(raw, floors_scaled)]
    target = round(amount * 10)
    drift = target - sum(scaled)
    order = sorted(range(n), key=lambda i: raw[i] * 10 - scaled[i], reverse=drift > 0)
    idx = 0
    guard = 0
    while drift != 0:
        i = order[idx % n]
        idx += 1
        if drift > 0:
            scaled[i] += 1
            drift -= 1
        elif scaled[i] > floors_scaled[i]:
            scaled[i] -= 1
            drift += 1
        guard += 1
        if guard > 20 * n:
            # Every item is pinned at its floor and the grid sum still exceeds
            # the target: the minimums genuinely do not fit on the 0.1s grid.
            raise ValueError(
                f"cannot fit minimum phase times ({sum(floors):.1f}s) into "
                f"{amount:.1f}s on the 0.1s grid"
            )
    return [v / 10.0 for v in scaled]


def synthesize_free_pattern(
    intersection: dict,
    counts: dict,
    *,
    cycle_s: float,
    pattern_id: str = FREE_PATTERN_ID,
    scale: float = 1.0,
) -> dict:
    """Build a pattern for a free period: official skeleton, demand-timed.

    Segment durations share the cycle proportionally to each segment's
    critical demand; within a segment each ring distributes its duration over
    its phases proportionally to demand, floored at min-green + clearance.
    Raises ValueError when the cycle cannot fit the official minimums (the
    caller then falls back to the static ladder).

    ``scale`` converts window counts to veh/h (see :func:`phase_demand_veh_h`).
    Shares are proportional, so a uniform scale leaves the splits unchanged;
    it is forwarded so the demand here is in the same unit as the cycle's.
    """
    meta = {int(p["phase"]): p for p in intersection.get("phases", [])}
    demand = phase_demand_veh_h(intersection, counts, scale=scale)
    segments = _segments(intersection.get("rings", {}))
    if not any(part for pair in segments for part in pair):
        raise ValueError("intersection defines no ring phases to time")

    # Segment floors and demand weights (critical ring of each segment).
    seg_floor: list[float] = []
    seg_weight: list[float] = []
    for ring1_part, ring2_part in segments:
        seg_floor.append(
            max(
                sum(_floor(meta.get(p, {})) for p in part)
                for part in (ring1_part, ring2_part)
                if part is not None
            )
        )
        seg_weight.append(
            max(
                sum(demand.get(p, 0.0) for p in part)
                for part in (ring1_part, ring2_part)
            )
        )
    durations = _allocate(float(cycle_s), seg_weight, seg_floor)

    splits: dict[str, float] = {}
    for (ring1_part, ring2_part), duration in zip(segments, durations):
        for part in (ring1_part, ring2_part):
            if not part:
                continue
            floors = [_floor(meta.get(p, {})) for p in part]
            weights = [demand.get(p, 0.0) for p in part]
            for pno, split in zip(part, _allocate(duration, weights, floors)):
                splits[str(pno)] = split

    pattern = {
        "pattern_id": pattern_id,
        "description": "Webster automatic (free period, official phase structure)",
        "cycle_s": float(cycle_s),
        "offset_s": 0.0,
        "splits": splits,
    }
    check_pattern_timing(intersection, pattern)
    return pattern
