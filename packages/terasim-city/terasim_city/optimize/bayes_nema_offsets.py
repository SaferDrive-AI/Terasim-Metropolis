"""Bayesian (optuna TPE) search over NEMA corridor offsets.

The NEMA counterpart of :class:`~terasim_city.optimize.bayes_offsets
.BayesianOffsetOptimizer`: the search space is the offset of every TLS except
the first (the reference intersection keeps its stored offset), each on a
``step_s`` grid over ``[0, its OWN cycle - step]``. Splits, rings and phase
structure stay frozen to the seed plan.

Because each TLS searches within its own cycle, a double-cycled intersection
(e.g. 70 s inside a 140 s corridor) participates naturally: its offset domain
is ``[0, 70)`` and it aligns twice per corridor cycle. Construction fails loud
when the cycles are neither equal nor clean integer multiples
(:meth:`NemaCorridorPlan.base_cycle_s`).
"""

from __future__ import annotations

import math

import optuna
from optuna.distributions import FloatDistribution

from terasim_city.optimize.base import Budget
from terasim_city.optimize.bayes_base import INF_PENALTY, TpeSearchBase
from terasim_city.plans.model import PlanProvenance
from terasim_city.plans.nema_plan import NemaCorridorPlan

__all__ = ["BayesianNemaOffsetOptimizer", "INF_PENALTY"]


class BayesianNemaOffsetOptimizer(TpeSearchBase):
    """Ask/tell offset optimizer over a NEMA corridor seed plan."""

    def __init__(
        self,
        seed_plan: NemaCorridorPlan,
        *,
        budget: Budget,
        rng_seed: int = 42,
        step_s: float = 1.0,
    ):
        if len(seed_plan.entries) < 2:
            raise ValueError(
                "BayesianNemaOffsetOptimizer needs a corridor of >= 2 TLS to "
                f"optimize offsets; plan {seed_plan.plan_id!r} has "
                f"{len(seed_plan.entries)} — a single intersection has no "
                "relative offset"
            )
        step = float(step_s)
        if not step > 0:
            raise ValueError(f"step_s must be > 0, got {step_s!r}")
        # Fails loud when cycles are not equal / clean integer multiples.
        seed_plan.base_cycle_s()

        self._step = step
        self._grid_high: dict[str, float] = {}
        distributions: dict[str, FloatDistribution] = {}
        for entry in seed_plan.entries[1:]:
            grid_high = math.floor((entry.cycle_s - step) / step) * step
            if grid_high <= 0:
                raise ValueError(
                    f"step_s={step:g} leaves no search range for "
                    f"{entry.tls_id} (cycle {entry.cycle_s:g}s)"
                )
            self._grid_high[entry.tls_id] = grid_high
            distributions[f"offset_{entry.tls_id}"] = FloatDistribution(
                0.0, grid_high, step=step
            )
        self._variable_tls: tuple[str, ...] = tuple(
            e.tls_id for e in seed_plan.entries[1:]
        )
        super().__init__(
            seed_plan,
            budget=budget,
            rng_seed=rng_seed,
            distributions=distributions,
        )

    # ------------------------------------------------------------------
    # Encoding hooks
    # ------------------------------------------------------------------

    def _plan_from_trial(self, trial: optuna.trial.Trial) -> NemaCorridorPlan:
        offsets = {
            tls_id: float(trial.params[f"offset_{tls_id}"])
            for tls_id in self._variable_tls
        }
        derived = self._seed_plan.with_offsets(offsets)
        return NemaCorridorPlan(
            plan_id=f"bo_{trial.number:04d}",
            entries=derived.entries,
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

    def _warm_start_params(self, plan: NemaCorridorPlan) -> dict[str, float]:
        offsets = plan.offsets()
        missing = [t for t in self._variable_tls if t not in offsets]
        if missing:
            raise ValueError(
                f"warm_start: plan {plan.plan_id!r} lacks TLS {missing} "
                "present in the seed plan"
            )
        return {
            f"offset_{tls_id}": self._snap(tls_id, offsets[tls_id])
            for tls_id in self._variable_tls
        }

    def _snap(self, tls_id: str, offset: float) -> float:
        snapped = round(float(offset) / self._step) * self._step
        return min(max(snapped, 0.0), self._grid_high[tls_id])
