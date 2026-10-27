"""Tests for terasim_city.evaluation.moe (extract_moe / KpiConfig / score_moe).

The unit tests use hand-written tripinfo XML covering all three row kinds
SUMO emits under ``--tripinfo-output.write-unfinished`` /
``--tripinfo-output.write-undeparted``. One integration test generates real
demand on the bundled corridor demo map and runs the actual binary via
subprocess (name deliberately avoids the repo-root ``requires_sumo``
auto-marker substrings; it self-skips when the runtime is absent).
"""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

import pytest

from terasim_city.evaluation.moe import EPS, KpiConfig, extract_moe, score_moe
from terasim_city.optimize.evaluator import moe_validity_errors
from tests.fixtures.tmc_templates import demo_tmc, write_tmc
from tests.test_city.conftest import REPO_ROOT, requires_sumolib

DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_CFG = DEMO_DIR / "demo.sumocfg"


# ---------------------------------------------------------------------------
# extract_moe — hand-written fixtures
# ---------------------------------------------------------------------------

MIXED_TRIPINFO = """<?xml version="1.0" encoding="UTF-8"?>
<tripinfos>
    <tripinfo id="done_a" depart="0.00" arrival="100.00" duration="100.00"
              waitingCount="2" waitingTime="20.00" timeLoss="12.5" departDelay="1.5"/>
    <tripinfo id="done_b" depart="10.00" arrival="90.00" duration="80.00"
              waitingCount="0" waitingTime="0.00" timeLoss="5.0" departDelay="0.0"/>
    <tripinfo id="unfinished" depart="20.00" arrival="-1.00" duration="40.00"
              waitingCount="3" waitingTime="30.00" timeLoss="40.0" departDelay="2.0"/>
    <tripinfo id="undeparted" depart="-1" departDelay="9.0"/>
</tripinfos>
"""


@pytest.fixture
def mixed_tripinfo(tmp_path: Path) -> Path:
    path = tmp_path / "tripinfo.xml"
    path.write_text(MIXED_TRIPINFO, encoding="utf-8")
    return path


def test_extract_moe_count_buckets(mixed_tripinfo: Path):
    moe = extract_moe(mixed_tripinfo)
    assert moe["vehicles_total"] == 4
    assert moe["vehicles_completed"] == 2
    assert moe["vehicles_unfinished"] == 1
    assert moe["vehicles_undeparted"] == 1


def test_extract_moe_delay_math(mixed_tripinfo: Path):
    moe = extract_moe(mixed_tripinfo)
    # Per-vehicle delay = timeLoss + departDelay; undeparted row has no
    # timeLoss attribute -> treated as 0.0.
    # done_a: 12.5+1.5=14, done_b: 5+0=5, unfinished: 40+2=42, undeparted: 0+9=9
    assert moe["total_delay_s"] == pytest.approx(70.0)
    assert moe["control_delay_s_per_veh"] == pytest.approx(70.0 / 4)
    assert moe["mean_time_loss_s"] == pytest.approx((12.5 + 5.0 + 40.0) / 4)
    assert moe["mean_depart_delay_s"] == pytest.approx((1.5 + 0.0 + 2.0 + 9.0) / 4)


def test_extract_moe_stop_math(mixed_tripinfo: Path):
    moe = extract_moe(mixed_tripinfo)
    # waitingCount 2+0+3, undeparted row is standing at the insertion gate ->
    # counts max(waitingCount, 1) = 1 stop (anti-gaming: gate-holding must not
    # dilute stops_per_veh).
    assert moe["total_stops"] == 6
    assert moe["stops_per_veh"] == pytest.approx(6 / 4)


def test_extract_moe_ratio_fields(mixed_tripinfo: Path):
    moe = extract_moe(mixed_tripinfo)
    # 4 total, 1 undeparted -> 3 departed; 2 completed.
    assert moe["departed_ratio"] == pytest.approx(3 / 4)
    assert moe["completion_ratio"] == pytest.approx(2 / 4)


def test_extract_moe_undeparted_row_with_only_depart(tmp_path: Path):
    # Real SUMO always writes departDelay on write-undeparted rows; a row
    # missing it means a hand-rolled/truncated file and must fail loudly
    # (zero-filling it would erase the delay penalty of held-back demand).
    path = tmp_path / "tripinfo.xml"
    path.write_text(
        '<tripinfos><tripinfo id="ghost" depart="-1"/></tripinfos>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="ghost"):
        extract_moe(path)


def test_extract_moe_undeparted_row_counts_at_least_one_stop(tmp_path: Path):
    # SUMO reports waitingCount=0 for never-inserted vehicles; extract_moe
    # counts them as standing (>= 1 stop) so gate-holding cannot win the
    # stops axis. An explicit larger waitingCount is kept as-is.
    path = tmp_path / "tripinfo.xml"
    path.write_text(
        "<tripinfos>"
        '<tripinfo id="held" depart="-1" departDelay="60.0" waitingCount="0"/>'
        '<tripinfo id="held_no_count" depart="-1" departDelay="60.0"/>'
        '<tripinfo id="held_big" depart="-1" departDelay="60.0" waitingCount="3"/>'
        "</tripinfos>",
        encoding="utf-8",
    )
    moe = extract_moe(path)
    assert moe["vehicles_undeparted"] == 3
    assert moe["total_stops"] == 1 + 1 + 3
    assert moe["stops_per_veh"] == pytest.approx(5 / 3)
    assert moe["total_delay_s"] == pytest.approx(180.0)


def test_extract_moe_missing_file_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="not found"):
        extract_moe(tmp_path / "nope.xml")


def test_extract_moe_truncated_xml_raises(tmp_path: Path):
    path = tmp_path / "tripinfo.xml"
    path.write_text('<tripinfos><tripinfo id="v0" depart="0.0"', encoding="utf-8")
    with pytest.raises(ValueError, match="not parseable"):
        extract_moe(path)


def test_extract_moe_zero_entries_raises(tmp_path: Path):
    path = tmp_path / "tripinfo.xml"
    path.write_text("<tripinfos></tripinfos>", encoding="utf-8")
    with pytest.raises(ValueError, match="no <tripinfo> entries"):
        extract_moe(path)


def test_extract_moe_non_numeric_attr_raises(tmp_path: Path):
    path = tmp_path / "tripinfo.xml"
    path.write_text(
        '<tripinfos><tripinfo id="v0" depart="0.0" arrival="10.0" '
        'timeLoss="abc"/></tripinfos>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="timeLoss"):
        extract_moe(path)


# ---------------------------------------------------------------------------
# moe_validity_errors — evaluator-side anti-gaming guard
# ---------------------------------------------------------------------------

GATE_HOLDING_TRIPINFO = """<?xml version="1.0" encoding="UTF-8"?>
<tripinfos>
    <tripinfo id="held_0" depart="-1" departDelay="60.00" waitingCount="0"/>
    <tripinfo id="held_1" depart="-1" departDelay="55.00" waitingCount="0"/>
    <tripinfo id="held_2" depart="-1" departDelay="50.00" waitingCount="0"/>
</tripinfos>
"""


def test_gate_holding_run_is_flagged_invalid(tmp_path: Path):
    # All demand held at the insertion gate (real SUMO shape: departDelay
    # written, waitingCount=0): the MOE extracts fine but the evaluator-side
    # guard must reject it as "no vehicle departed".
    path = tmp_path / "tripinfo.xml"
    path.write_text(GATE_HOLDING_TRIPINFO, encoding="utf-8")
    moe = extract_moe(path)
    assert moe["vehicles_undeparted"] == moe["vehicles_total"] == 3
    assert moe["departed_ratio"] == 0.0
    assert moe["completion_ratio"] == 0.0
    assert moe_validity_errors(moe) == ["no vehicle departed"]


def test_moe_validity_errors_accepts_partial_departure(mixed_tripinfo: Path):
    # Any departure at all passes the guard.
    assert moe_validity_errors(extract_moe(mixed_tripinfo)) == []


def test_moe_validity_errors_does_not_gate_on_zero_completion():
    # Legitimate short runs: vehicles departed but none finished. The guard
    # must only reject all-undeparted, never completion_ratio == 0.
    moe = {
        "vehicles_total": 5,
        "vehicles_undeparted": 0,
        "vehicles_completed": 0,
        "departed_ratio": 1.0,
        "completion_ratio": 0.0,
    }
    assert moe_validity_errors(moe) == []


# ---------------------------------------------------------------------------
# KpiConfig
# ---------------------------------------------------------------------------


def test_kpi_config_unknown_key_rejected():
    with pytest.raises(ValueError, match="unknown weight key"):
        KpiConfig(weights=(("throughput", 1.0),))


def test_kpi_config_non_positive_weight_rejected():
    with pytest.raises(ValueError, match="must be > 0"):
        KpiConfig(weights=(("control_delay", 0.0),))
    with pytest.raises(ValueError, match="must be > 0"):
        KpiConfig(weights=(("stops", -0.5),))


def test_kpi_config_empty_rejected():
    with pytest.raises(ValueError, match="empty"):
        KpiConfig(weights=())


def test_kpi_config_dict_round_trip():
    cfg = KpiConfig.from_dict({"control_delay": 0.9, "stops": 0.1})
    assert cfg.weights == (("control_delay", 0.9), ("stops", 0.1))
    assert KpiConfig.from_dict(cfg.to_dict()) == cfg


# ---------------------------------------------------------------------------
# score_moe
# ---------------------------------------------------------------------------

BASE_MOE = {"control_delay_s_per_veh": 20.0, "stops_per_veh": 2.0}


def test_score_moe_baseline_scores_sum_of_weights():
    cfg = KpiConfig()
    assert score_moe(BASE_MOE, BASE_MOE, cfg) == pytest.approx(0.7 + 0.3)
    lopsided = KpiConfig.from_dict({"control_delay": 2.5, "stops": 1.5})
    assert score_moe(BASE_MOE, BASE_MOE, lopsided) == pytest.approx(4.0)


def test_score_moe_lower_is_better():
    cfg = KpiConfig()
    better = {"control_delay_s_per_veh": 10.0, "stops_per_veh": 1.0}
    assert score_moe(better, BASE_MOE, cfg) < score_moe(BASE_MOE, BASE_MOE, cfg)


def test_score_moe_zero_baseline_uses_eps_guard():
    cfg = KpiConfig.from_dict({"stops": 1.0})
    zero_baseline = {"control_delay_s_per_veh": 20.0, "stops_per_veh": 0.0}
    moe = {"control_delay_s_per_veh": 20.0, "stops_per_veh": 1.0}
    score = score_moe(moe, zero_baseline, cfg)
    assert math.isfinite(score)
    assert score == pytest.approx(1.0 / EPS)


def test_score_moe_missing_metric_raises():
    cfg = KpiConfig()
    with pytest.raises(ValueError, match="control_delay_s_per_veh"):
        score_moe({"stops_per_veh": 1.0}, BASE_MOE, cfg)
    with pytest.raises(ValueError, match="baseline_moe"):
        score_moe(BASE_MOE, {"control_delay_s_per_veh": 20.0}, cfg)


# ---------------------------------------------------------------------------
# Integration: real tripinfo produced on the corridor demo map
# ---------------------------------------------------------------------------

requires_demo_assets = pytest.mark.skipif(
    not DEMO_CFG.exists(),
    reason=f"corridor demo assets not present at {DEMO_DIR} "
           "(run examples/corridor_demo/build_demo_map.py)",
)

requires_runtime_binary = pytest.mark.skipif(
    shutil.which("sumo") is None,
    reason="needs the `sumo` binary on PATH",
)


@requires_demo_assets
@requires_sumolib
@requires_runtime_binary
def test_extract_moe_on_demo_corridor_run(tmp_path: Path):
    from terasim_city.api import run_tmc_pipeline

    demo_tmc_path = write_tmc(tmp_path, demo_tmc())
    scenario = run_tmc_pipeline(
        demo_tmc_path, DEMO_CFG, tmp_path / "scenario", sim_end=60.0
    )
    tripinfo = tmp_path / "tripinfo.xml"
    result = subprocess.run(
        [
            "sumo",
            "-c", str(scenario.sumocfg_file),
            "--tripinfo-output", str(tripinfo),
            "--tripinfo-output.write-unfinished", "true",
            "--tripinfo-output.write-undeparted", "true",
            "--no-step-log",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"demo run failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    moe = extract_moe(tripinfo)
    assert moe["vehicles_total"] > 0
    assert moe["vehicles_total"] == (
        moe["vehicles_completed"]
        + moe["vehicles_unfinished"]
        + moe["vehicles_undeparted"]
    )
    for key in (
        "control_delay_s_per_veh",
        "stops_per_veh",
        "mean_time_loss_s",
        "mean_depart_delay_s",
        "total_delay_s",
    ):
        assert math.isfinite(moe[key]), f"{key} not finite: {moe[key]}"
        assert moe[key] >= 0.0
    assert moe["departed_ratio"] == pytest.approx(
        (moe["vehicles_total"] - moe["vehicles_undeparted"]) / moe["vehicles_total"]
    )
    assert moe["completion_ratio"] == pytest.approx(
        moe["vehicles_completed"] / moe["vehicles_total"]
    )
    assert 0.0 <= moe["completion_ratio"] <= moe["departed_ratio"] <= 1.0
