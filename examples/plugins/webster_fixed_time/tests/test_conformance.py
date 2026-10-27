"""Conformance test of the plugin, written the way a third party would write it.

It runs the plugin on the synthetic three-signal demo corridor through the SDK's
conformance kit. ``TERASIM_CORRIDOR_DEMO`` points at ``examples/corridor_demo`` of a
TeraSim Metropolis checkout; inside the repository it is found automatically.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from terasim_plugin_sdk.conformance import demo_request, run_conformance
from webster_fixed_time import WebsterFixedTimePlugin

CORRIDOR_DEMO = Path(
    os.environ.get("TERASIM_CORRIDOR_DEMO", Path(__file__).resolve().parents[3] / "corridor_demo")
)

needs_runtime = pytest.mark.skipif(
    shutil.which("sumo") is None or not (CORRIDOR_DEMO / "demo.sumocfg").is_file(),
    reason="needs the sumo binary and the demo corridor (set TERASIM_CORRIDOR_DEMO)",
)


@needs_runtime
def test_webster_fixed_time_conforms(tmp_path):
    request = demo_request(CORRIDOR_DEMO, tmp_path / "out")

    result = run_conformance(WebsterFixedTimePlugin(), request)

    assert result.plugin == "webster_fixed_time"
    assert [item["tls_id"] for item in result.nema_plan["intersections"]] == ["J1", "J2", "J3"]
    assert result.kpis["optimized"]["cost"] <= result.kpis["baseline"]["cost"]
    assert result.details["recommended_plan_id"] in ("baseline", "webster_init")
