"""Bayesian (optuna TPE) search over corridor signal offsets.

The search space is the offset of every TLS EXCEPT the first (the reference
intersection keeps its seed offset), each on a ``step_s`` grid over
``[0, cycle - step_s]``. Splits/phase structure are frozen to the seed plan —
only coordination offsets move. Search mechanics live in
:class:`~terasim_city.optimize.bayes_base.TpeSearchBase`.
"""

from __future__ import annotations

import math
from dataclasses import replace

import optuna
from optuna.distributions import FloatDistribution

from terasim_city.optimize.base import Budget
from terasim_city.optimize.bayes_base import INF_PENALTY, TpeSearchBase
from terasim_city.plans.model import CorridorPlan, PlanProvenance

__all__ = ["BayesianOffsetOptimizer", "INF_PENALTY"]


class BayesianOffsetOptimizer(TpeSearchBase):
    """Ask/tell offset optimizer over a fixed-cycle corridor seed plan.

    Requires a seed with >= 2 TLS and a common cycle (``common_cycle_s`` —
    per-TLS cycles differing by more than the model tolerance raise there).
    """

    def __init__(
        self,
        seed_plan: CorridorPlan,
        *,
        budget: Budget,
        rng_seed: int = 42,
        step_s: float = 1.0,
    ):
        if len(seed_plan.plans) < 2:
            raise ValueError(
                "BayesianOffsetOptimizer needs a corridor of >= 2 TLS to "
                f"optimize offsets; seed plan {seed_plan.plan_id!r} has "
                f"{len(seed_plan.plans)}. A single intersection has no relative "
                "offset — its green splits are searched instead "
                "(BayesianSplitOptimizer)."
            )
        step = float(step_s)
        if not step > 0:
            raise ValueError(f"step_s must be > 0, got {step_s!r}")
        cycle = seed_plan.common_cycle_s()
        # optuna requires (high - low) to be an exact multiple of step; snap
        # the ceiling DOWN onto the grid (e.g. cycle 100, step 3 -> high 96).
        grid_high = math.floor((cycle - step) / step) * step
        if grid_high <= 0:
            raise ValueError(
                f"step_s={step:g} leaves no search range for cycle {cycle:g}s"
            )
        self._cycle = cycle
        self._step = step
        self._grid_high = grid_high
        self._variable_tls: tuple[str, ...] = tuple(
            plan.tls_id for plan in seed_plan.plans[1:]
        )
        super().__init__(
            seed_plan,
            budget=budget,
            rng_seed=rng_seed,
            distributions={
                f"offset_{tls_id}": FloatDistribution(0.0, grid_high, step=step)
                for tls_id in self._variable_tls
            },
        )

    # ------------------------------------------------------------------
    # Encoding hooks
    # ------------------------------------------------------------------

    def _plan_from_trial(self, trial: optuna.trial.Trial) -> CorridorPlan:
        offsets = {
            tls_id: float(trial.params[f"offset_{tls_id}"])
            for tls_id in self._variable_tls
        }
        signal_plans = [self._seed_plan.plans[0]]
        signal_plans.extend(
            replace(plan, offset_s=offsets[plan.tls_id])
            for plan in self._seed_plan.plans[1:]
        )
        return CorridorPlan(
            plan_id=f"bo_{trial.number:04d}",
            plans=tuple(signal_plans),
            provenance=PlanProvenance(
                source="bo_iteration",
                parent_plan_id=self._seed_plan.plan_id,
                iteration=trial.number,
                params=tuple(
                    (f"offset_{tls_id}", offsets[tls_id])
                    for tls_id in self._variable_tls
                ),
            ),
        )

    def _warm_start_params(self, plan: CorridorPlan) -> dict[str, float]:
        by_tls = plan.by_tls()
        missing = [t for t in self._variable_tls if t not in by_tls]
        if missing:
            raise ValueError(
                f"warm_start: plan {plan.plan_id!r} lacks TLS "
                f"{missing} present in the seed plan"
            )
        return {
            f"offset_{tls_id}": self._snap_to_grid(by_tls[tls_id].offset_s)
            for tls_id in self._variable_tls
        }

    def _snap_to_grid(self, offset: float) -> float:
        snapped = round(float(offset) / self._step) * self._step
        return min(max(snapped, 0.0), self._grid_high)
