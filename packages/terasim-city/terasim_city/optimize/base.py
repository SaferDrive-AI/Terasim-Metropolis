"""Optimizer-facing contracts: evaluation records, ask/tell protocol, budget."""

from __future__ import annotations

from dataclasses import dataclass

from terasim_city.plans.model import CorridorPlan


@dataclass(frozen=True)
class PlanEvaluation:
    """Outcome of simulating one :class:`CorridorPlan`.

    ``score`` is lower-is-better and must be ``math.inf`` when ``valid`` is
    False (the optimizer never treats an invalid run as an improvement).
    ``moe`` is the output of ``extract_moe``; the full per-run kpi payload
    lives on disk in the trial dir (``kpi.json``), not in memory.
    """

    plan: CorridorPlan
    moe: dict
    score: float
    valid: bool
    trial_dir: str




@dataclass(frozen=True)
class Budget:
    """Trial budget for an optimization run.

    ``patience`` stops the search after that many consecutive ``tell`` calls
    without an improvement of the best score; ``<= 0`` disables early stopping.
    """

    max_trials: int = 20
    patience: int = 8

    def __post_init__(self) -> None:
        if int(self.max_trials) <= 0:
            raise ValueError(f"Budget.max_trials must be > 0, got {self.max_trials!r}")
        object.__setattr__(self, "max_trials", int(self.max_trials))
        object.__setattr__(self, "patience", int(self.patience))
