"""The scenario self-test's result checks (apps/deploy/city/phase1/scenario_e2e.py).

The script runs inside the image against a live service; its result checks are plain
functions over the result JSON, exercised here without a service.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "apps" / "deploy" / "city" / "phase1" / "scenario_e2e.py"


@pytest.fixture(scope="module")
def e2e():
    spec = importlib.util.spec_from_file_location("scenario_e2e_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result(e2e) -> dict:
    signals = e2e.SIGNALS
    timing = {"cycle_s": {tls: 66.0 for tls in signals}}

    def moe(delay: float, junction: float) -> dict:
        return {
            "control_delay_s_per_veh": delay,
            "corridor_junction_delay_s": junction,
            "junction_delay_s": {tls: junction for tls in signals},
        }

    config = {key: e2e.REQUEST[key] for key in e2e.ECHOED}
    config["tmc_json"] = "/data/output/run/derived_tmc.json"
    return {
        "seed": {"source": "sumo_tls"},
        "baseline": {"timing": timing, "moe": moe(54.98, 23.21)},
        "best": {"timing": timing, "moe": moe(46.96, 23.99)},
        "search": {"ran": True, "reason": None},
        "nema_export": {"exported": True},
        "config": config,
    }


def test_a_complete_result_passes(e2e):
    result = _result(e2e)
    e2e.check_result(result, result["config"])


@pytest.mark.parametrize("side", ["baseline", "best"])
@pytest.mark.parametrize("key", ["control_delay_s_per_veh", "corridor_junction_delay_s"])
@pytest.mark.parametrize("value", [None, float("nan"), "12.0", True, -1.0], ids=repr)
def test_both_delay_metrics_must_be_numbers_on_both_sides(e2e, side, key, value):
    result = copy.deepcopy(_result(e2e))
    result[side]["moe"][key] = value
    with pytest.raises(SystemExit):
        e2e.check_result(result, result["config"])


@pytest.mark.parametrize("side", ["baseline", "best"])
def test_a_missing_delay_metric_fails(e2e, side):
    result = copy.deepcopy(_result(e2e))
    del result[side]["moe"]["control_delay_s_per_veh"]
    with pytest.raises(SystemExit):
        e2e.check_result(result, result["config"])
