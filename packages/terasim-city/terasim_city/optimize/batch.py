"""Parallel plan evaluation over a spawn-based multiprocessing pool.

``BatchEvaluator`` turns :class:`CorridorPlan` objects into evaluation tasks
(one monotonically-numbered trial directory each) and runs them through
:func:`terasim_city.optimize.evaluator.evaluate_plan_task` — normally in a
``spawn`` pool with ``maxtasksperchild=1`` so every SUMO run gets a fresh
libsumo process, or sequentially in-process when constructed with
``inline=True`` (imports terasim/libsumo into the calling process — a
harness/test trade-off; the ``TERASIM_CITY_INLINE_EVAL`` env var supplies the
default when the flag is not passed).
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from typing import Sequence

from terasim_city.optimize.evaluator import QUALITY_KEYS, evaluate_plan_task
from terasim_city.plans.model import CorridorPlan

__all__ = ["BatchEvaluator", "INLINE_EVAL_ENV_VAR"]

INLINE_EVAL_ENV_VAR = "TERASIM_CITY_INLINE_EVAL"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _inline_mode_enabled() -> bool:
    return os.environ.get(INLINE_EVAL_ENV_VAR, "").strip().lower() in _TRUTHY


class BatchEvaluator:
    """Evaluate corridor plans in parallel, one trial dir per plan.

    Trial directories are ``trials_root/trial_0000``, ``trial_0001``, ... —
    monotonic across successive :meth:`evaluate` calls, so a full optimization
    run never reuses a directory. Results are returned in ``plans`` order.
    """

    def __init__(
        self,
        *,
        scenario_sumocfg: Path,
        net_file: Path,
        trials_root: Path,
        sim_end: float,
        stalls: list[dict],
        quality_thresholds: dict,
        n_workers: int = 4,
        vehicle_behavior: str = "sumo_passive",
        inline: bool | None = None,
        map_dir: "Path | str | None" = None,
    ):
        n_workers = int(n_workers)
        if n_workers < 1:
            raise ValueError(f"n_workers must be >= 1, got {n_workers}")
        missing = [key for key in QUALITY_KEYS if key not in quality_thresholds]
        if missing:
            # No silent lenient/strict fallback inside the child: the policy
            # must be stated explicitly by the caller.
            raise ValueError(
                f"quality_thresholds missing required key(s): {missing}; "
                f"expected all of {sorted(QUALITY_KEYS)}"
            )
        self._scenario_sumocfg = Path(scenario_sumocfg)
        self._net_file = Path(net_file)
        self._trials_root = Path(trials_root)
        self._trials_root.mkdir(parents=True, exist_ok=True)
        self._sim_end = float(sim_end)
        self._stalls = [dict(stall) for stall in stalls]
        self._quality_thresholds = dict(quality_thresholds)
        self._n_workers = n_workers
        # Registry name only: the factory itself is resolved inside the child
        # process (see evaluate_plan_task), never in the parent.
        self._vehicle_behavior = str(vehicle_behavior)
        # MapStore package directory for the "cnde" behavior (needs it to
        # build/reuse cnde_map.pkl / cnde_id_mapping.pkl — see
        # terasim_city.maps.cnde_artifacts.ensure_cnde_artifacts). None for
        # every other behavior; carried through the JSON-safe task dict
        # unconditionally (see _build_task) so the evaluator's cnde branch
        # can find it without threading extra plumbing through here.
        self._map_dir = None if map_dir is None else Path(map_dir)
        self._inline = _inline_mode_enabled() if inline is None else bool(inline)
        self._seq = 0
        self._pool = None

    def evaluate(self, plans: Sequence[CorridorPlan]) -> list[dict]:
        """Run every plan; the result list is ordered like ``plans``."""
        tasks = [self._build_task(plan) for plan in plans]
        if not tasks:
            return []
        if self._inline:
            # Sequential, in-process. No pool is ever created, but
            # evaluate_plan_task will import terasim (and libsumo) into THIS
            # process — accepted trade-off for hookability.
            return [evaluate_plan_task(task) for task in tasks]
        return self._ensure_pool().map(evaluate_plan_task, tasks)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool.join()
            self._pool = None

    def __enter__(self) -> "BatchEvaluator":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _ensure_pool(self):
        if self._pool is None:
            context = multiprocessing.get_context("spawn")
            self._pool = context.Pool(
                processes=self._n_workers, maxtasksperchild=1
            )
        return self._pool

    def _build_task(self, plan: CorridorPlan) -> dict:
        trial_dir = self._trials_root / f"trial_{self._seq:04d}"
        self._seq += 1
        return {
            "plan": plan.to_json_dict(),
            "scenario_sumocfg": str(self._scenario_sumocfg),
            "net_file": str(self._net_file),
            "trial_dir": str(trial_dir),
            "sim_end": self._sim_end,
            "stalls": [dict(stall) for stall in self._stalls],
            "quality_thresholds": dict(self._quality_thresholds),
            "vehicle_behavior": self._vehicle_behavior,
            # None for every behavior except "cnde" (see the map_dir
            # docstring note in __init__); JSON-safe (str or None) so it
            # survives the spawn boundary like every other task field.
            "map_dir": None if self._map_dir is None else str(self._map_dir),
        }
