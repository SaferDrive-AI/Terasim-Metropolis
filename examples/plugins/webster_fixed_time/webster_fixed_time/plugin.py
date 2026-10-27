"""A Webster fixed-time plan as a Traffic Operation plug-in.

The module derives a fixed-time plan from the turning-movement counts with
Webster's method, simulates it next to the current plan on the same scenario,
and recommends whichever has the lower KPI cost. It is built only on the public
Plugin Interface (``terasim_plugin_sdk``) and the public core API
(``terasim_city``), and reaches TeraSim Metropolis through its entry point
(see ``pyproject.toml``); nothing in the core changes.
"""

from __future__ import annotations

from terasim_city.plans import build_webster_plan
from terasim_plugin_sdk import (
    API_VERSION,
    OperationRequest,
    OperationResult,
    PluginInputError,
    ProgressCallback,
)
from terasim_plugin_sdk.toolkit import (
    baseline_plan,
    best_plan,
    build_result,
    evaluate_plans,
    export_nema,
    prepare_scenario,
)

#: Options accepted in ``request.params`` (all optional).
PARAMS = {
    "stage_mode": "'two_stage' (default: an E-W and an N-S stage) or 'per_approach'",
    "cycle_min_s": "shortest cycle the plan may use, seconds (default 45)",
    "cycle_max_s": "longest cycle the plan may use, seconds (default 150)",
}


class WebsterFixedTimePlugin:
    """Recommend a Webster fixed-time plan when it beats the current timing."""

    name = "webster_fixed_time"
    version = "0.1.0"
    api_version = API_VERSION
    description = (
        "Webster fixed-time plan from the TMC counts, recommended when it beats the current plan"
    )

    def run(
        self, request: OperationRequest, progress: "ProgressCallback | None" = None
    ) -> OperationResult:
        options = self._options(request.params)

        # 1. One scenario for every candidate: the map plus demand built from the counts.
        scenario = prepare_scenario(request)

        # 2. The candidates: the current plan (the baseline) and the Webster plan.
        current, notes = baseline_plan(scenario, request.current_plan)
        try:
            webster = build_webster_plan(
                scenario.tmc_json, scenario.corridor, scenario.net_file, **options
            )
        except ValueError as exc:
            raise PluginInputError(f"cannot derive a Webster plan: {exc}") from exc

        # 3. Simulate both; the lower KPI cost wins (the baseline keeps a tie).
        scores = evaluate_plans(scenario, [current, webster], progress=progress)
        winner = best_plan(scores)
        if winner is scores[0]:
            notes += (
                "the Webster plan does not improve on the baseline, which stays the recommendation",
            )

        # 4. Hand off the winner as NEMA timing, with the KPI comparison.
        nema_plan = export_nema(scenario, winner.plan)
        return build_result(
            self,
            scenario,
            nema_plan=nema_plan,
            baseline=scores[0],
            optimized=winner,
            notes=notes,
            evaluated=scores,
            details={"webster": dict(webster.provenance.params)},
        )

    @staticmethod
    def _options(params) -> dict:
        unknown = sorted(set(params) - set(PARAMS))
        if unknown:
            raise PluginInputError(
                f"webster_fixed_time does not accept params {unknown}; accepted: {sorted(PARAMS)}"
            )
        options = dict(params)
        for key in ("cycle_min_s", "cycle_max_s"):
            if key in options:
                value = options[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise PluginInputError(
                        f"param {key!r} must be a number of seconds, got {value!r}"
                    )
                options[key] = float(value)
        return options
