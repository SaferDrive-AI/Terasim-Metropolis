"""Assemble a corridor's NEMA baseline from stored per-intersection plans.

Run-time glue between corridor resolution (which intersections form the
corridor) and the plan store (what each runs): for each resolved
intersection, look up its stored plan, pick the pattern active at the query
time, and materialize a SUMO ``type="NEMA"`` program. Intersections with no
stored plan are omitted, so SUMO runs the net's own program (the optimizer's
seed ladder provides the Webster fallback). The optimizer is NOT involved
here — this produces the *baseline* only.

``select_pattern`` / ``plan_selections`` are net-free (unit-testable).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from terasim_city.binding import AmbiguousMatchError
from terasim_city.plans.nema_materialize import materialize_nema_tls, write_nema_additional
from terasim_city.plans.store import (
    SAME_INTERSECTION_RADIUS_M,
    PlanStore,
    StoredPlan,
)

_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # datetime.weekday() 0..6


def _to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def select_pattern(intersection_plan: dict, when: datetime) -> dict | None:
    """Pattern active at ``when`` for one intersection, or None (free/none).

    Rules for the query weekday are ordered by start time; the latest rule
    at/before ``when`` wins. Before the first rule of the day, the schedule
    wraps to the day's last rule (the overnight period). A ``free`` rule (or no
    rule for the day) means the intersection runs actuated → None.
    """
    patterns = {p["pattern_id"]: p for p in intersection_plan.get("patterns", [])}
    schedule = intersection_plan.get("schedule") or []
    if not schedule:
        pats = intersection_plan.get("patterns", [])
        return pats[0] if pats else None

    def rules_for(dt: datetime) -> list[tuple[int, str]]:
        day = _DOW[dt.weekday()]
        return sorted(
            (_to_minutes(r["start"]), r["pattern_id"])
            for r in schedule if day in r.get("days", [])
        )

    minutes = when.hour * 60 + when.minute
    todays = rules_for(when)
    chosen = None
    for start, pid in todays:
        if start <= minutes:
            chosen = pid
    if chosen is None:
        # Before the first rule today: the active pattern started the previous
        # day (which may be a DIFFERENT day-type — e.g. Sat 03:00 belongs to
        # Friday's overnight rule, not Saturday's last rule). Walk back to the
        # most recent prior day that has any rule and take its last.
        for back in range(1, 8):
            prev = rules_for(when - timedelta(days=back))
            if prev:
                chosen = prev[-1][1]
                break
    if chosen is None or chosen == "free":
        return None
    return patterns.get(chosen)


@dataclass(frozen=True)
class Selection:
    """What one corridor intersection resolves to at the query time."""

    intersection_id: str
    tls_id: str
    source: str                 # "nema" | "no_plan" | "free"
    pattern_id: str | None
    cycle_s: float | None
    matched_by: str | None      # "key" | "geolocation" | None




def _attr(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def plan_selections(
    corridor_id: str,
    resolved_intersections,
    store: PlanStore,
    when: datetime,
) -> tuple[list[tuple[Selection, StoredPlan | None, dict | None]], list[str]]:
    """Per-intersection: look up plan + pick pattern (net-free).

    Returns ``(rows, warnings)`` where each row is
    ``(Selection, StoredPlan|None, pattern|None)``. Also flags cross-intersection
    cycle incompatibility (coordination needs a shared cycle).
    """
    rows: list[tuple[Selection, StoredPlan | None, dict | None]] = []
    warnings: list[str] = []
    for it in resolved_intersections:
        iid = _attr(it, "intersection_id")
        tls_id = _attr(it, "tls_id")
        # Plans are matched against the map junction's OWN position (the
        # anchor), not against the TMC camera's coordinates: a plan 25 m from
        # the junction must bind even when the camera sits 40 m off on the
        # other side, and a junction no camera observes must still find its
        # plan. TMC coordinates remain only as a fallback for legacy callers
        # that never resolved an anchor.
        lat = _attr(it, "anchor_lat")
        lon = _attr(it, "anchor_lon")
        if lat is None or lon is None:
            lat, lon = _attr(it, "lat"), _attr(it, "lon")
        try:
            sp = store.find_for(corridor_id, iid, lat, lon)
        except AmbiguousMatchError as exc:
            # Ambiguous plans must not silently time the wrong intersection,
            # but they must not kill the run either — the documented fallback
            # (no plan -> static seed ladder) still applies. The warning is
            # the report the contract demands.
            warnings.append(f"{iid or tls_id}: {exc}")
            rows.append((Selection(iid, tls_id, "no_plan", None, None, None), None, None))
            continue
        if (
            sp is not None
            and sp.match_distance_m is not None
            and sp.match_distance_m > SAME_INTERSECTION_RADIUS_M
        ):
            # Found within the shared binding radius but beyond the
            # same-intersection radius: most likely the NEIGHBOURING
            # intersection's plan (plan lookup has no structural ranking to
            # protect it, unlike TMC matching). Surface it, don't apply it.
            warnings.append(
                f"{iid or tls_id}: nearest stored plan "
                f"{sp.corridor_id}/{sp.intersection_id} is "
                f"{sp.match_distance_m} m from the junction — beyond the "
                f"{SAME_INTERSECTION_RADIUS_M:g} m same-intersection radius, "
                "not applied; re-upload with precise coordinates if it does "
                "describe this junction"
            )
            sp = None
        if sp is None:
            rows.append((Selection(iid, tls_id, "no_plan", None, None, None), None, None))
            warnings.append(f"{iid}: no stored plan — SUMO runs the net's built-in program")
            continue
        inter_plan = sp.plan["intersections"][0]
        pattern = select_pattern(inter_plan, when)
        if pattern is None:
            rows.append((Selection(iid, tls_id, "free", None, None, sp.matched_by), sp, None))
            continue
        rows.append((
            Selection(iid, tls_id, "nema", pattern["pattern_id"], float(pattern["cycle_s"]), sp.matched_by),
            sp,
            pattern,
        ))

    cycles = [sel.cycle_s for sel, _, _ in rows if sel.cycle_s]
    if cycles and not _cycles_compatible(cycles):
        warnings.append(
            f"selected patterns have incompatible cycles {sorted(set(cycles))} — "
            "the corridor cannot coordinate without a common cycle (or clean multiples)"
        )
    return rows, warnings


def _cycles_compatible(cycles: list[float], tol_s: float = 1.0) -> bool:
    """Cycles coordinate iff each is a clean integer multiple of the smallest.

    Uses an ABSOLUTE tolerance (seconds): 100/200 or 70/140 pass; 138/140 or
    300/305 (a few seconds off, not multiples) do not.
    """
    base = min(cycles)
    if base <= 0:
        return False
    for c in cycles:
        k = round(c / base)
        if k < 1 or abs(c - k * base) > tol_s:
            return False
    return True




__all__ = [
    "select_pattern",
    "plan_selections",
    "Selection",
]
