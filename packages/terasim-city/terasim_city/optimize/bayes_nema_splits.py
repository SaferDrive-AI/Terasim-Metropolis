"""Bayesian (optuna TPE) search over NEMA splits for ONE intersection.

The green-ratio counterpart of :class:`~terasim_city.optimize
.bayes_nema_offsets.BayesianNemaOffsetOptimizer`: the cycle, the offset and
the ring/phase structure stay frozen to the seed pattern — only how the cycle
is CUT between phases changes.

Constraint model (the NEMA dual-ring rules, enforced by construction):

* The barrier splits the cycle into side A (phases 1/2/5/6) and side B
  (phases 3/4/7/8): ``gA + gB = cycle``.
* Within each side, every ring THAT HAS PHASES there must tile exactly that
  side's duration (a ring with no phases on a side simply rests).
* Every phase keeps a floor of ``min_green + yellow + all_red`` seconds.

Parameterization: one ``barrier_frac`` chooses how the SLACK (cycle minus all
floors) is divided between the two sides; per-phase factors then divide each
ring-side cell's slack proportionally on top of the floors. Every sampled
candidate therefore satisfies all constraints exactly — no rejection step.
"""

from __future__ import annotations

import optuna
from optuna.distributions import FloatDistribution

from terasim_city.optimize.base import Budget
from terasim_city.optimize.bayes_base import INF_PENALTY, TpeSearchBase
from terasim_city.plans.model import PlanProvenance
from terasim_city.plans.nema_plan import NemaCorridorPlan

__all__ = ["BayesianNemaSplitOptimizer", "INF_PENALTY"]

#: NEMA numbering convention: barrier side A phases / side B phases.
_SIDE_A = frozenset({1, 2, 5, 6})

_MIN_FACTOR, _MAX_FACTOR = 0.05, 1.0
_MIN_BARRIER_FRAC, _MAX_BARRIER_FRAC = 0.05, 0.95

DEFAULT_MIN_GREEN_S = 5.0
DEFAULT_YELLOW_S = 3.0
DEFAULT_ALL_RED_S = 2.0


def _phase_floor(meta: dict) -> float:
    """Smallest legal split: min green plus the fixed clearance tail."""
    return (
        float(meta.get("min_green_s", DEFAULT_MIN_GREEN_S))
        + float(meta.get("yellow_s", DEFAULT_YELLOW_S))
        + float(meta.get("all_red_s", DEFAULT_ALL_RED_S))
    )


class BayesianNemaSplitOptimizer(TpeSearchBase):
    """Ask/tell split optimizer over a single-intersection NEMA seed plan."""

    def __init__(
        self,
        seed_plan: NemaCorridorPlan,
        *,
        budget: Budget,
        rng_seed: int = 42,
    ):
        if len(seed_plan.entries) != 1:
            raise ValueError(
                "BayesianNemaSplitOptimizer optimizes ONE intersection's green "
                f"ratios; plan {seed_plan.plan_id!r} has "
                f"{len(seed_plan.entries)} TLS — corridors coordinate via "
                "offsets (BayesianNemaOffsetOptimizer)"
            )
        entry = seed_plan.entries[0]
        self._tls_id = entry.tls_id
        self._cycle = entry.cycle_s
        rings = entry.intersection["rings"]
        phase_meta = {p["phase"]: p for p in entry.intersection["phases"]}

        # Ring-side cells: (ring_name, side) -> ordered phase list.
        self._cells: dict[tuple[str, str], list[int]] = {}
        present: list[int] = []
        for ring_name in ("ring1", "ring2"):
            members = [p for p in rings.get(ring_name, []) if p]
            for side_name, side_set in (("A", _SIDE_A), ("B", None)):
                phases = [
                    p for p in members
                    if (p in _SIDE_A) == (side_name == "A")
                ]
                if phases:
                    self._cells[(ring_name, side_name)] = phases
                    present.extend(phases)
        missing = [p for p in present if p not in phase_meta]
        if missing:
            raise ValueError(
                f"{self._tls_id}: ring phases {sorted(set(missing))} have no "
                "definition in the intersection's phases"
            )
        self._floors = {p: _phase_floor(phase_meta[p]) for p in set(present)}

        # A side's floor is the WIDEST ring on that side (both rings must fit).
        def side_floor(side: str) -> float:
            sums = [
                sum(self._floors[p] for p in phases)
                for (ring, s), phases in self._cells.items()
                if s == side
            ]
            return max(sums) if sums else 0.0

        self._floor_a, self._floor_b = side_floor("A"), side_floor("B")
        self._slack = self._cycle - self._floor_a - self._floor_b
        if self._slack <= 0:
            raise ValueError(
                f"{self._tls_id}: cycle {self._cycle:g}s cannot cover the "
                f"minimum greens + clearances ({self._floor_a + self._floor_b:g}s) "
                "— nothing to optimize"
            )
        if len(set(present)) < 2:
            raise ValueError(
                f"{self._tls_id}: fewer than 2 phases — no green ratio to trade"
            )

        distributions: dict[str, FloatDistribution] = {}
        # Both sides populated -> the barrier position is a free variable.
        self._has_two_sides = self._floor_a > 0 and self._floor_b > 0
        if self._has_two_sides:
            distributions["barrier_frac"] = FloatDistribution(
                _MIN_BARRIER_FRAC, _MAX_BARRIER_FRAC
            )
        for p in sorted(set(present)):
            distributions[f"split_{p}"] = FloatDistribution(_MIN_FACTOR, _MAX_FACTOR)
        super().__init__(
            seed_plan,
            budget=budget,
            rng_seed=rng_seed,
            distributions=distributions,
        )

    # ------------------------------------------------------------------
    # Params -> a constraint-satisfying splits table
    # ------------------------------------------------------------------

    def _splits_from_params(self, params: dict) -> dict[str, float]:
        if self._has_two_sides:
            frac = float(params["barrier_frac"])
            g_a = self._floor_a + frac * self._slack
        else:
            # Single-sided intersection: that side takes the whole cycle.
            g_a = self._cycle if self._floor_a > 0 else 0.0
        g_b = self._cycle - g_a

        splits: dict[str, float] = {}
        for (ring, side), phases in self._cells.items():
            side_total = g_a if side == "A" else g_b
            cell_floor = sum(self._floors[p] for p in phases)
            cell_slack = side_total - cell_floor  # >= 0 by construction
            factors = [float(params[f"split_{p}"]) for p in phases]
            total = sum(factors)
            for p, f in zip(phases, factors):
                splits[str(p)] = round(
                    self._floors[p] + cell_slack * (f / total), 1
                )
            # Absorb the rounding residue into the cell's last phase so each
            # ring still tiles its side EXACTLY.
            residue = round(side_total - sum(splits[str(p)] for p in phases), 1)
            splits[str(phases[-1])] = round(splits[str(phases[-1])] + residue, 1)
        return splits

    # ------------------------------------------------------------------
    # Encoding hooks
    # ------------------------------------------------------------------

    def _plan_from_trial(self, trial: optuna.trial.Trial) -> NemaCorridorPlan:
        splits = self._splits_from_params(trial.params)
        derived = self._seed_plan.with_splits({self._tls_id: splits})
        return NemaCorridorPlan(
            plan_id=f"bo_{trial.number:04d}",
            entries=derived.entries,
            provenance=PlanProvenance(
                source="bo_iteration",
                parent_plan_id=self._seed_plan.plan_id,
                iteration=trial.number,
                params=tuple(sorted((k, float(v)) for k, v in trial.params.items())),
            ),
        )

    def _warm_start_params(self, plan: NemaCorridorPlan) -> dict[str, float]:
        entry = plan.entries[0]
        splits = {int(k): float(v) for k, v in entry.pattern.get("splits", {}).items()}
        params: dict[str, float] = {}
        if self._has_two_sides:
            g_a = max(
                (
                    sum(splits.get(p, self._floors[p]) for p in phases)
                    for (ring, side), phases in self._cells.items()
                    if side == "A"
                ),
                default=self._floor_a,
            )
            frac = (g_a - self._floor_a) / self._slack
            params["barrier_frac"] = min(
                max(frac, _MIN_BARRIER_FRAC), _MAX_BARRIER_FRAC
            )
        for (ring, side), phases in self._cells.items():
            overs = [
                max(splits.get(p, self._floors[p]) - self._floors[p], 0.0)
                for p in phases
            ]
            top = max(overs) or 1.0
            for p, over in zip(phases, overs):
                params[f"split_{p}"] = min(
                    max(over / top, _MIN_FACTOR), _MAX_FACTOR
                )
        return params
