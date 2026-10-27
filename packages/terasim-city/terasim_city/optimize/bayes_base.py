"""Shared optuna-TPE ask/tell mechanics for the Bayesian plan optimizers.

Both concrete optimizers (offsets over a corridor, green splits over a single
intersection) differ ONLY in how a plan is encoded to/from optuna parameters;
every bit of search mechanics — trial bookkeeping, budget/patience stopping,
best tracking, warm starting — lives here exactly once.
"""

from __future__ import annotations

import math
import warnings

import optuna
from optuna.distributions import FloatDistribution
from optuna.samplers import TPESampler

from terasim_city.optimize.base import Budget, PlanEvaluation
from terasim_city.plans.model import CorridorPlan

__all__ = ["INF_PENALTY", "TpeSearchBase"]

# optuna cannot ingest math.inf as an objective value; invalid runs are told
# to the study as this large finite penalty while the PlanEvaluation keeps inf.
INF_PENALTY = 1e9


class TpeSearchBase:
    """Ask/tell TPE search over corridor plans; subclasses define the encoding.

    Subclass contract:

    * ``_plan_from_trial(trial)`` — decode one sampled optuna trial into a
      :class:`CorridorPlan` (the plan's ``plan_id`` must be unique per trial).
    * ``_warm_start_params(plan)`` — encode an already-evaluated plan into
      optuna parameter space (snapping into the distributions' domain; raise
      ``ValueError`` when the plan is incompatible with the seed).

    ``is_finished`` triggers on ``budget.max_trials`` trials told, or on
    ``budget.patience`` consecutive tells without a best-score improvement
    (``patience <= 0`` disables early stopping).
    """

    def __init__(
        self,
        seed_plan: CorridorPlan,
        *,
        budget: Budget,
        rng_seed: int,
        distributions: dict[str, FloatDistribution],
    ):
        self._seed_plan = seed_plan
        self._budget = budget
        self._distributions = distributions
        with warnings.catch_warnings():
            # constant_liar is flagged experimental by optuna; the API is a
            # frozen part of this module's contract, so silence the warning.
            warnings.simplefilter("ignore")
            sampler = TPESampler(seed=rng_seed, constant_liar=True)
        self._study = optuna.create_study(direction="minimize", sampler=sampler)
        self._pending: dict[str, optuna.trial.Trial] = {}
        self._n_told = 0
        self._best: PlanEvaluation | None = None
        self._tells_since_improvement = 0

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    def _plan_from_trial(self, trial: optuna.trial.Trial) -> CorridorPlan:
        raise NotImplementedError

    def _warm_start_params(self, plan: CorridorPlan) -> dict[str, float]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared mechanics
    # ------------------------------------------------------------------

    def ask(self, n: int) -> list[CorridorPlan]:
        """Sample up to ``n`` new plans (fewer near the trial budget)."""
        if self.is_finished():
            return []
        remaining = self._budget.max_trials - self._n_told - len(self._pending)
        plans: list[CorridorPlan] = []
        for _ in range(max(0, min(int(n), remaining))):
            trial = self._study.ask(dict(self._distributions))
            plan = self._plan_from_trial(trial)
            self._pending[plan.plan_id] = trial
            plans.append(plan)
        return plans

    def tell(self, evaluations: list[PlanEvaluation]) -> None:
        for evaluation in evaluations:
            trial = self._pending.pop(evaluation.plan.plan_id, None)
            if trial is None:
                raise ValueError(
                    f"{type(self).__name__}.tell: plan "
                    f"{evaluation.plan.plan_id!r} is not a pending ask() result "
                    f"(pending: {sorted(self._pending)})"
                )
            self._study.tell(trial, self._penalized(evaluation.score))
            self._n_told += 1
            if self._is_improvement(evaluation):
                self._best = evaluation
                self._tells_since_improvement = 0
            else:
                self._tells_since_improvement += 1

    def is_finished(self) -> bool:
        if self._n_told >= self._budget.max_trials:
            return True
        patience = self._budget.patience
        return patience > 0 and self._tells_since_improvement >= patience

    def best(self) -> PlanEvaluation | None:
        """Best valid finite evaluation seen so far (ask/tell observation port)."""
        return self._best

    def warm_start(self, evaluations: list[PlanEvaluation]) -> None:
        """Feed already-evaluated points (e.g. the seed evaluation).

        Warm-start trials inform the sampler and may set the running best, but they
        consume neither ``max_trials`` nor patience. Parameter values are
        snapped into the search domain by the subclass encoding (optuna
        rejects off-distribution values); the snap only affects what the
        sampler is told, never the evaluation itself.
        """
        for evaluation in evaluations:
            params = self._warm_start_params(evaluation.plan)
            self._study.add_trial(
                optuna.trial.create_trial(
                    params=params,
                    distributions=dict(self._distributions),
                    value=self._penalized(evaluation.score),
                )
            )
            if self._is_improvement(evaluation):
                self._best = evaluation

    def _penalized(self, score: float) -> float:
        return float(score) if math.isfinite(score) else INF_PENALTY

    def _is_improvement(self, evaluation: PlanEvaluation) -> bool:
        return (
            evaluation.valid
            and math.isfinite(evaluation.score)
            and (self._best is None or evaluation.score < self._best.score)
        )
