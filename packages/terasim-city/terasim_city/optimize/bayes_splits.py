"""Bayesian (optuna TPE) search over single-intersection green splits.

Single intersections have no relative offset to coordinate, so the search
space is the split of the available green time across the plan's green stages:
one factor ``f_i`` in ``[0.05, 1.0]`` per green-kind :class:`PhaseSpec`, mapped
deterministically to durations::

    green_i = min_green_s + (total_green - n * min_green_s) * f_i / sum(f)

where ``total_green = cycle - sum(non-green durations)``. Each green is rounded
to 0.1 s and the rounding remainder goes to the largest stage, so the cycle
length of every derived plan is exactly the seed's. Phase order, states, kinds,
non-green durations, and the offset never change (except that, under cycle
search, an offset a shorter sampled cycle cannot hold is wrapped into it).

Optionally the cycle is searched too: with ``cycle_bounds=(low, high)`` a
``cycle_s`` dimension on a 1 s grid (``CYCLE_STEP_S``) is added and
``total_green`` becomes ``cycle_s - sum(non-green durations)`` per trial, so the
yellow / all-red time is still never touched and every derived plan's cycle is
exactly the sampled ``cycle_s``. ``low`` is raised to the floor
``sum(non-green durations) + n * min_green_s`` so every stage keeps its minimum
green, and both ends are snapped onto the grid (:func:`cycle_grid_bounds`). The
split mapping itself lives in the module-level
:func:`factors_from_greens` / :func:`greens_from_factors`, which the joint
corridor optimizer reuses. Search mechanics live in
:class:`~terasim_city.optimize.bayes_base.TpeSearchBase`.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Sequence

import optuna
from optuna.distributions import FloatDistribution

from terasim_city.optimize.base import Budget
from terasim_city.optimize.bayes_base import TpeSearchBase
from terasim_city.plans.model import CorridorPlan, PlanProvenance, SignalPlan

__all__ = [
    "CYCLE_STEP_S",
    "BayesianSplitOptimizer",
    "cycle_grid_bounds",
    "factors_from_greens",
    "greens_from_factors",
]

#: Lower bound of every split factor: keeps the ratio mapping well-defined
#: (sum(f) > 0) and stops any stage from collapsing onto min_green exactly.
_FACTOR_MIN = 0.05
_FACTOR_MAX = 1.0

#: Grid of the optional cycle dimension, in seconds.
CYCLE_STEP_S = 1.0


def cycle_grid_bounds(
    cycle_bounds: tuple[float, float], floor_cycle_s: float
) -> tuple[float, float]:
    """Snap requested cycle bounds onto the ``CYCLE_STEP_S`` grid above a floor.

    ``low`` is raised to ``floor_cycle_s``, then rounded UP and ``high`` rounded
    DOWN to multiples of the step (with a 1e-9 tolerance so float noise around a
    grid point never moves it by a whole step).

    The snap exists because optuna normalises a stepped ``FloatDistribution``
    itself: when ``high - low`` is not an exact multiple of the step (e.g. a floor
    of 48.400000000000006 s from summed yellow / all-red times) it warns and
    silently lowers ``high``. Bounds that were not already on the grid would
    leave the domain optuna samples from different from the bounds we hold, and a
    warm start snapped to our ``high`` would then be rejected as outside it.

    Raises:
        ValueError: if a bound is not finite, or no range is left above the floor.
    """
    low, high = float(cycle_bounds[0]), float(cycle_bounds[1])
    for value in (low, high):
        if not math.isfinite(value):
            raise ValueError(
                f"cycle_bounds must be finite, got {value!r} in {cycle_bounds!r}"
            )
    low = max(low, floor_cycle_s)
    low = math.ceil(low / CYCLE_STEP_S - 1e-9) * CYCLE_STEP_S
    high = low + math.floor((high - low) / CYCLE_STEP_S + 1e-9) * CYCLE_STEP_S
    if high <= low:
        raise ValueError(
            f"cycle_bounds {cycle_bounds!r} leave no cycle range above the "
            f"floor {floor_cycle_s:g}s (fixed time + min greens)"
        )
    return low, high


def factors_from_greens(greens: Sequence[float], min_green: float) -> list[float]:
    """Invert the split mapping: factors proportional to green headroom."""
    headrooms = [max(0.0, float(g) - min_green) for g in greens]
    denominator = max(headrooms) if headrooms else 0.0
    if denominator <= 0.0:
        return [_FACTOR_MIN] * len(headrooms)
    return [min(max(h / denominator, _FACTOR_MIN), _FACTOR_MAX) for h in headrooms]


def greens_from_factors(
    factors: Sequence[float], total_green: float, min_green: float
) -> list[float]:
    """Factor -> green durations summing exactly to ``total_green`` (0.1 s grid)."""
    n = len(factors)
    if n == 0:
        raise ValueError("expected at least one split factor")
    factor_sum = sum(factors)
    headroom = float(total_green) - n * min_green
    if headroom < -1e-9:
        raise ValueError(
            f"total_green={total_green:g}s cannot hold {n} stage(s) at min_green={min_green:g}s"
        )
    greens = [
        round((min_green + headroom * f / factor_sum) * 10.0) / 10.0 for f in factors
    ]
    largest = greens.index(max(greens))
    greens[largest] += float(total_green) - sum(greens)
    assert abs(sum(greens) - total_green) < 1e-6
    return greens


class BayesianSplitOptimizer(TpeSearchBase):
    """Ask/tell green-split optimizer over a single-intersection seed plan.

    Requires a seed with exactly 1 TLS whose plan has >= 2 green-kind phases
    and enough green time for ``min_green_s`` per stage. The cycle stays at the
    seed's unless ``cycle_bounds=(low, high)`` is given, which adds the cycle
    length as a search dimension (see the module docstring).
    """

    def __init__(
        self,
        seed_plan: CorridorPlan,
        *,
        budget: Budget,
        rng_seed: int = 42,
        min_green_s: float = 5.0,
        cycle_bounds: tuple[float, float] | None = None,
    ):
        if len(seed_plan.plans) != 1:
            raise ValueError(
                "BayesianSplitOptimizer needs a single-intersection seed plan; "
                f"seed plan {seed_plan.plan_id!r} has {len(seed_plan.plans)} "
                "TLS. Corridors search offsets instead — use "
                "BayesianOffsetOptimizer."
            )
        min_green = float(min_green_s)
        if not min_green > 0:
            raise ValueError(f"min_green_s must be > 0, got {min_green_s!r}")
        signal_plan = seed_plan.plans[0]
        green_indices = tuple(
            index
            for index, phase in enumerate(signal_plan.phases)
            if phase.kind == "green"
        )
        if len(green_indices) < 2:
            raise ValueError(
                f"BayesianSplitOptimizer needs >= 2 green stages to split "
                f"green time between; seed plan {seed_plan.plan_id!r} (tls "
                f"{signal_plan.tls_id!r}) has {len(green_indices)}."
            )
        cycle = signal_plan.cycle_s
        fixed_s = sum(
            phase.duration_s
            for phase in signal_plan.phases
            if phase.kind != "green"
        )
        total_green = cycle - fixed_s
        if total_green < len(green_indices) * min_green:
            raise ValueError(
                f"seed plan {seed_plan.plan_id!r} (tls {signal_plan.tls_id!r}) "
                f"has only {total_green:g}s of green time in a {cycle:g}s "
                f"cycle, less than min_green_s={min_green:g} x "
                f"{len(green_indices)} green stages"
            )
        self._min_green = min_green
        self._green_indices = green_indices
        self._fixed_s = fixed_s
        self._seed_cycle = cycle
        self._cycle_low: float | None = None
        self._cycle_high: float | None = None
        distributions = {
            f"f_{stage}": FloatDistribution(_FACTOR_MIN, _FACTOR_MAX)
            for stage in range(len(green_indices))
        }
        if cycle_bounds is not None:
            low, high = cycle_grid_bounds(
                cycle_bounds, fixed_s + len(green_indices) * min_green
            )
            cycle_domain = FloatDistribution(low, high, step=CYCLE_STEP_S)
            # Read the bounds back from the distribution: optuna normalises its
            # domain itself, and the warm-start snap target must never diverge
            # from it.
            self._cycle_low, self._cycle_high = cycle_domain.low, cycle_domain.high
            distributions["cycle_s"] = cycle_domain
        super().__init__(
            seed_plan,
            budget=budget,
            rng_seed=rng_seed,
            distributions=distributions,
        )

    # ------------------------------------------------------------------
    # Encoding hooks
    # ------------------------------------------------------------------

    def _plan_from_trial(self, trial: optuna.trial.Trial) -> CorridorPlan:
        factors = [
            float(trial.params[f"f_{stage}"])
            for stage in range(len(self._green_indices))
        ]
        cycle = (
            float(trial.params["cycle_s"])
            if self._cycle_low is not None
            else self._seed_cycle
        )
        greens = greens_from_factors(factors, cycle - self._fixed_s, self._min_green)
        signal_plan = self._seed_plan.plans[0]
        phases = list(signal_plan.phases)
        for index, green in zip(self._green_indices, greens):
            phases[index] = replace(phases[index], duration_s=green)
        offset = signal_plan.offset_s
        if self._cycle_low is not None:
            # SignalPlan needs 0 <= offset < cycle; a shorter sampled cycle can
            # undercut the seed's offset, so wrap it (a valid offset is kept).
            offset %= cycle
        derived: SignalPlan = replace(
            signal_plan, phases=tuple(phases), offset_s=offset
        )
        return CorridorPlan(
            plan_id=f"bo_split_{trial.number:04d}",
            plans=(derived,),
            provenance=PlanProvenance(
                source="bo_iteration",
                parent_plan_id=self._seed_plan.plan_id,
                iteration=trial.number,
                params=tuple(
                    (f"green_{stage}", green)
                    for stage, green in enumerate(greens)
                )
                + ((("cycle_s", cycle),) if self._cycle_low is not None else ()),
            ),
        )

    def _warm_start_params(self, plan: CorridorPlan) -> dict[str, float]:
        """Factors are recovered proportional to ``green_i - min_green_s``
        with a floor of ``_FACTOR_MIN``; under cycle search the plan's cycle is
        snapped onto the cycle grid inside the bounds (optuna rejects values
        outside the distribution's domain)."""
        seed_tls = self._seed_plan.plans[0].tls_id
        signal_plan = plan.by_tls().get(seed_tls)
        if signal_plan is None:
            raise ValueError(
                f"warm_start: plan {plan.plan_id!r} lacks TLS "
                f"{seed_tls!r} present in the seed plan"
            )
        greens = [
            phase.duration_s
            for phase in signal_plan.phases
            if phase.kind == "green"
        ]
        if len(greens) != len(self._green_indices):
            raise ValueError(
                f"warm_start: plan {plan.plan_id!r} has "
                f"{len(greens)} green stage(s) but the seed plan has "
                f"{len(self._green_indices)}"
            )
        params = {
            f"f_{stage}": factor
            for stage, factor in enumerate(self._factors_from_greens(greens))
        }
        if self._cycle_low is not None:
            params["cycle_s"] = self._snap_cycle(signal_plan.cycle_s)
        return params

    def _snap_cycle(self, cycle: float) -> float:
        """Nearest point of the cycle grid inside the bounds (cycle search only)."""
        low, high = self._cycle_low, self._cycle_high
        snapped = low + round((float(cycle) - low) / CYCLE_STEP_S) * CYCLE_STEP_S
        return min(max(snapped, low), high)

    # ------------------------------------------------------------------
    # Split mapping
    # ------------------------------------------------------------------

    def _factors_from_greens(self, greens: Sequence[float]) -> list[float]:
        """Seed-independent inverse of the split mapping (see
        :func:`factors_from_greens`)."""
        return factors_from_greens(greens, self._min_green)

