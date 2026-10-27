"""Joint Bayesian (optuna TPE) search over a corridor plan.

Search space, one optuna parameter each:

* ``cycle_s``: the common cycle, on a 1 s grid (``CYCLE_STEP_S``) inside
  ``cycle_bounds``. Only present when ``cycle_bounds`` is given; otherwise every
  plan keeps the seed's common cycle. ``low`` is raised to the floor (per TLS, its
  non-green time plus ``min_green_s`` per green stage; the largest over the
  corridor) and both ends are snapped onto the grid
  (:func:`~terasim_city.optimize.bayes_splits.cycle_grid_bounds`).
* ``f_<tls>_<k>``: the split factor of green stage ``k`` of every TLS, mapped to
  durations exactly as in :mod:`~terasim_city.optimize.bayes_splits`. The greens of
  a TLS fill ``cycle - its non-green time``, so yellow / all-red time is never
  touched and every TLS of a decoded plan runs exactly the sampled cycle.
* ``offset_<tls>``: the offset of every TLS but the first, which is the reference
  and is pinned at 0, on an ``offset_step_s`` grid over ``[0, cycle_high - step]``.
  A sampled value is wrapped into the sampled cycle (``offset % cycle``), so every
  offset stays reachable whichever cycle is sampled.

Every decoded plan shares one cycle, so ``CorridorPlan.common_cycle_s``,
``materialize_plan`` and the NEMA export keep working. A seed whose TLS run
different cycles is accepted when ``cycle_bounds`` is given: its warm start encodes
each TLS's own green fractions and the seed's maximum cycle, clamped into the
bounds. ``warm_start`` takes only plans the optimizer could have decoded, i.e. with the
seed's TLS, green-stage counts and non-green times (and, without ``cycle_bounds``, its
cycle), and raises ``ValueError`` for any other; that is how a caller finds out which
extra seeds the search can use. Search mechanics live in
:class:`~terasim_city.optimize.bayes_base.TpeSearchBase`.
"""

from __future__ import annotations

import math
from dataclasses import replace

import optuna
from optuna.distributions import FloatDistribution

from terasim_city.optimize.base import Budget
from terasim_city.optimize.bayes_base import TpeSearchBase
from terasim_city.optimize.bayes_splits import (
    _FACTOR_MAX,
    _FACTOR_MIN,
    CYCLE_STEP_S,
    cycle_grid_bounds,
    factors_from_greens,
    greens_from_factors,
)
from terasim_city.plans.model import (
    CYCLE_TOLERANCE_S,
    CorridorPlan,
    PlanProvenance,
    SignalPlan,
)

__all__ = ["BayesianCorridorOptimizer"]

#: A warm-start plan's non-green time may differ from the seed's by float noise only.
_NON_GREEN_TOLERANCE_S = 1e-6


class BayesianCorridorOptimizer(TpeSearchBase):
    """Ask/tell optimizer over common cycle + per-TLS splits + offsets.

    Requires a seed with >= 2 TLS, each with at least one green phase. Without
    ``cycle_bounds`` the seed must have a common cycle (``common_cycle_s``) and the
    cycle is not searched; with ``cycle_bounds=(low, high)`` it is, and the seed's
    per-TLS cycles may differ.
    """

    def __init__(
        self,
        seed_plan: CorridorPlan,
        *,
        budget: Budget,
        rng_seed: int = 42,
        min_green_s: float = 5.0,
        cycle_bounds: tuple[float, float] | None = None,
        offset_step_s: float = 1.0,
    ):
        if len(seed_plan.plans) < 2:
            raise ValueError(
                "BayesianCorridorOptimizer needs a corridor of >= 2 TLS; seed plan "
                f"{seed_plan.plan_id!r} has {len(seed_plan.plans)} (use "
                "BayesianSplitOptimizer for a single intersection)"
            )
        min_green = float(min_green_s)
        if not min_green > 0:
            raise ValueError(f"min_green_s must be > 0, got {min_green_s!r}")
        step = float(offset_step_s)
        if not step > 0:
            raise ValueError(f"offset_step_s must be > 0, got {offset_step_s!r}")
        self._min_green = min_green
        self._step = step
        self._tls_ids: tuple[str, ...] = tuple(p.tls_id for p in seed_plan.plans)
        self._green_indices: dict[str, tuple[int, ...]] = {}
        self._fixed_s: dict[str, float] = {}
        for plan in seed_plan.plans:
            indices = tuple(i for i, ph in enumerate(plan.phases) if ph.kind == "green")
            if not indices:
                raise ValueError(
                    f"seed plan {seed_plan.plan_id!r}: TLS {plan.tls_id!r} has no green phase"
                )
            self._green_indices[plan.tls_id] = indices
            self._fixed_s[plan.tls_id] = sum(
                ph.duration_s for ph in plan.phases if ph.kind != "green"
            )
        floor_cycle = max(
            self._fixed_s[t] + len(self._green_indices[t]) * min_green for t in self._tls_ids
        )
        self._search_cycle = cycle_bounds is not None
        cycle_domain: FloatDistribution | None = None
        if cycle_bounds is None:
            cycle = seed_plan.common_cycle_s()  # raises on mixed per-TLS cycles
            if cycle < floor_cycle:
                raise ValueError(
                    f"seed cycle {cycle:g}s is below the floor {floor_cycle:g}s "
                    "(fixed time + min greens)"
                )
            self._cycle_low = self._cycle_high = cycle
        else:
            low, high = cycle_grid_bounds(cycle_bounds, floor_cycle)
            cycle_domain = FloatDistribution(low, high, step=CYCLE_STEP_S)
            # Read the bounds back from the distribution: optuna normalises its domain
            # itself, and the snap targets stored here must never diverge from it.
            self._cycle_low, self._cycle_high = cycle_domain.low, cycle_domain.high
        offset_high = math.floor((self._cycle_high - step) / step) * step
        if offset_high <= 0:
            raise ValueError(
                f"offset_step_s={step:g} leaves no offset range for cycle {self._cycle_high:g}s"
            )
        # One domain shared by every offset parameter, read back like the cycle's: an
        # inexact step (e.g. 0.1 s) is normalised by optuna too.
        offset_domain = FloatDistribution(0.0, offset_high, step=step)
        self._offset_high = offset_domain.high
        distributions: dict[str, FloatDistribution] = {}
        if cycle_domain is not None:
            distributions["cycle_s"] = cycle_domain
        for tls_id in self._tls_ids:
            for k in range(len(self._green_indices[tls_id])):
                distributions[f"f_{tls_id}_{k}"] = FloatDistribution(_FACTOR_MIN, _FACTOR_MAX)
        for tls_id in self._tls_ids[1:]:
            distributions[f"offset_{tls_id}"] = offset_domain
        super().__init__(seed_plan, budget=budget, rng_seed=rng_seed, distributions=distributions)

    # ------------------------------------------------------------------
    # Encoding hooks
    # ------------------------------------------------------------------

    def _plan_from_trial(self, trial: optuna.trial.Trial) -> CorridorPlan:
        params = trial.params
        cycle = float(params["cycle_s"]) if self._search_cycle else self._cycle_low
        signal_plans: list[SignalPlan] = []
        provenance: list[tuple[str, float]] = [("cycle_s", cycle)]
        for position, plan in enumerate(self._seed_plan.plans):
            tls_id = plan.tls_id
            indices = self._green_indices[tls_id]
            factors = [float(params[f"f_{tls_id}_{k}"]) for k in range(len(indices))]
            greens = greens_from_factors(factors, cycle - self._fixed_s[tls_id], self._min_green)
            phases = list(plan.phases)
            for index, green in zip(indices, greens):
                phases[index] = replace(phases[index], duration_s=green)
            # Wrap, not clamp: a shorter sampled cycle than the offset domain's top still
            # reaches every offset value (SignalPlan needs 0 <= offset < cycle).
            offset = 0.0 if position == 0 else float(params[f"offset_{tls_id}"]) % cycle
            signal_plans.append(replace(plan, phases=tuple(phases), offset_s=offset))
            provenance.extend((f"green_{tls_id}_{k}", g) for k, g in enumerate(greens))
            provenance.append((f"offset_{tls_id}", offset))
        return CorridorPlan(
            plan_id=f"bo_corr_{trial.number:04d}",
            plans=tuple(signal_plans),
            provenance=PlanProvenance(
                source="bo_iteration",
                parent_plan_id=self._seed_plan.plan_id,
                iteration=trial.number,
                params=tuple(provenance),
            ),
        )

    def _warm_start_params(self, plan: CorridorPlan) -> dict[str, float]:
        """Encode an evaluated plan as a point of the search space.

        Each later TLS's offset is taken relative to the seed's first TLS,
        ``(offset - first_offset) % cycle``: decoding pins that TLS at 0, and a common
        shift of every offset leaves the coordination unchanged. Values are snapped into
        the optuna domains (optuna rejects anything outside them).

        Raises:
            ValueError: if the plan is not one this optimizer could have decoded: it lacks
                a TLS of the seed plan, or has a different number of green stages or amount
                of non-green time at one, or (cycle not searched) runs another cycle.
        """
        by_tls = plan.by_tls()
        params: dict[str, float] = {}
        cycles: list[float] = []
        first_offset = 0.0
        for position, tls_id in enumerate(self._tls_ids):
            signal_plan = by_tls.get(tls_id)
            if signal_plan is None:
                raise ValueError(
                    f"warm_start: plan {plan.plan_id!r} lacks TLS {tls_id!r} "
                    "present in the seed plan"
                )
            greens = [ph.duration_s for ph in signal_plan.phases if ph.kind == "green"]
            if len(greens) != len(self._green_indices[tls_id]):
                raise ValueError(
                    f"warm_start: plan {plan.plan_id!r} has {len(greens)} green stage(s) at "
                    f"{tls_id!r} but the seed plan has {len(self._green_indices[tls_id])}"
                )
            self._require_seed_timing(plan, signal_plan)
            for k, factor in enumerate(factors_from_greens(greens, self._min_green)):
                params[f"f_{tls_id}_{k}"] = factor
            cycles.append(signal_plan.cycle_s)
            if position == 0:
                first_offset = signal_plan.offset_s
            else:
                relative = (signal_plan.offset_s - first_offset) % signal_plan.cycle_s
                params[f"offset_{tls_id}"] = self._snap_offset(relative, signal_plan.cycle_s)
        if self._search_cycle:
            params["cycle_s"] = self._snap_cycle(max(cycles))
        return params

    def _require_seed_timing(self, plan: CorridorPlan, signal_plan: SignalPlan) -> None:
        """Raise ValueError unless this TLS of a warm-start plan could have been decoded.

        That takes the seed's non-green time and, when the cycle is not searched, the seed's
        cycle (within ``CYCLE_TOLERANCE_S``).
        """
        tls_id = signal_plan.tls_id
        non_green_s = sum(ph.duration_s for ph in signal_plan.phases if ph.kind != "green")
        if abs(non_green_s - self._fixed_s[tls_id]) > _NON_GREEN_TOLERANCE_S:
            raise ValueError(
                f"warm_start: plan {plan.plan_id!r} has {non_green_s:g}s of non-green time "
                f"at {tls_id!r} but the seed plan has {self._fixed_s[tls_id]:g}s"
            )
        cycle_gap = abs(signal_plan.cycle_s - self._cycle_low)
        if not self._search_cycle and cycle_gap > CYCLE_TOLERANCE_S:
            raise ValueError(
                f"warm_start: plan {plan.plan_id!r} runs a {signal_plan.cycle_s:g}s cycle at "
                f"{tls_id!r} but the search keeps the seed's {self._cycle_low:g}s cycle"
            )

    def _snap_offset(self, offset: float, cycle: float) -> float:
        """Nearest point of the offset grid inside the domain.

        ``offset`` is already wrapped into ``[0, cycle)``. One that rounds up to the whole
        cycle (a hair under it, or float noise below 0) is the cycle's start: 0, not the top
        of the domain.
        """
        snapped = round(float(offset) / self._step) * self._step
        if snapped >= cycle - 1e-9:
            snapped = 0.0
        return min(max(snapped, 0.0), self._offset_high)

    def _snap_cycle(self, cycle: float) -> float:
        """Nearest point of the cycle grid inside the bounds (cycle search only)."""
        low, high = self._cycle_low, self._cycle_high
        snapped = low + round((float(cycle) - low) / CYCLE_STEP_S) * CYCLE_STEP_S
        return min(max(snapped, low), high)
