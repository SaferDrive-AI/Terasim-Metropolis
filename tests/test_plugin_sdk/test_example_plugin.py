"""The third-party example ``webster_fixed_time`` (examples/plugins) through the SDK."""

from __future__ import annotations

import dataclasses
import importlib.util
import json

import pytest
from terasim_plugin_sdk import PluginInputError, TrafficSnapshot, run_operation
from terasim_plugin_sdk.conformance import check_plugin, demo_request
from webster_fixed_time import PARAMS, WebsterFixedTimePlugin

from tests.test_plugin_sdk.conftest import DEMO_DIR, EXAMPLE_DIR, apply, needs_demo, simulates

pytestmark = [
    needs_demo,
    pytest.mark.skipif(importlib.util.find_spec("sumolib") is None, reason="needs sumolib"),
]


def test_example_is_a_valid_plugin():
    plugin = WebsterFixedTimePlugin()
    check_plugin(plugin)
    assert (plugin.name, plugin.version) == ("webster_fixed_time", "0.1.0")
    assert sorted(PARAMS) == ["cycle_max_s", "cycle_min_s", "stage_mode"]


@pytest.mark.parametrize(
    "params, message",
    [
        ({"offsets": [1, 2]}, "does not accept params"),
        ({"cycle_min_s": "short"}, "number of seconds"),
        ({"stage_mode": "eight_phase"}, "cannot derive a Webster plan"),
    ],
)
def test_example_rejects_bad_params(tmp_path, params, message):
    request = demo_request(DEMO_DIR, tmp_path / "out", params=params)
    with pytest.raises(PluginInputError, match=message):
        run_operation(WebsterFixedTimePlugin(), request)


def test_example_lets_cancellation_through(tmp_path):
    class Cancelled(Exception):
        pass

    def progress(done, total, label):
        raise Cancelled(f"stop at {done}/{total}")

    with pytest.raises(Cancelled, match="stop at 0/2"):
        run_operation(
            WebsterFixedTimePlugin(), demo_request(DEMO_DIR, tmp_path / "out"), progress=progress
        )
    assert not list((tmp_path / "out").glob("trials/trial_*"))


def _load_example_test_module():
    """The example's own test file, as its authors run it (``pytest examples/plugins/...``)."""
    path = EXAMPLE_DIR / "tests" / "test_conformance.py"
    spec = importlib.util.spec_from_file_location("webster_fixed_time_conformance_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@apply(simulates)
def test_the_example_own_conformance_test_passes(tmp_path):
    _load_example_test_module().test_webster_fixed_time_conforms(tmp_path)


@apply(simulates)
def test_example_is_deterministic_and_reads_payload_snapshots(tmp_path):
    """Same inputs and seed -> the same recommendation and KPIs, from a file or a payload."""
    from_file = run_operation(WebsterFixedTimePlugin(), demo_request(DEMO_DIR, tmp_path / "a"))

    payload = json.loads((DEMO_DIR / "demo_tmc.json").read_text(encoding="utf-8"))
    request = dataclasses.replace(
        demo_request(DEMO_DIR, tmp_path / "b"), traffic=TrafficSnapshot.from_payload(payload)
    )
    from_payload = run_operation(WebsterFixedTimePlugin(), request)

    assert from_payload.kpis == from_file.kpis
    assert from_payload.details["evaluations"] == from_file.details["evaluations"]
    strip = lambda plan: {key: value for key, value in plan.items() if key != "generated_at"}  # noqa: E731
    assert strip(from_payload.nema_plan) == strip(from_file.nema_plan)
    assert (
        from_payload.artifacts["inputs"] == "inputs"
    )  # the payload was written to inputs/tmc.json

    # The 90 s timing sheet is the baseline; on the demo demand the 45 s Webster plan wins.
    assert from_file.details["baseline_plan_id"] == "baseline"
    assert from_file.details["recommended_plan_id"] == "webster_init"
    assert from_file.kpis["optimized"]["cost"] < from_file.kpis["baseline"]["cost"]
    assert from_file.nema_plan["coordination"]["cycle_length_s"] == pytest.approx(
        from_file.details["webster"]["cycle_s"]
    )


@apply(simulates)
def test_without_a_current_plan_the_baseline_stays(tmp_path):
    """No current plan: the reference is the same Webster plan, so the baseline is kept."""
    result = run_operation(
        WebsterFixedTimePlugin(), demo_request(DEMO_DIR, tmp_path / "out", current_plan=None)
    )
    assert (
        result.details["baseline_plan_id"]
        == result.details["recommended_plan_id"]
        == "webster_init"
    )
    assert result.kpis["optimized"]["cost"] == result.kpis["baseline"]["cost"]
    assert result.notes == (
        "the Webster plan does not improve on the baseline, which stays the recommendation",
    )
