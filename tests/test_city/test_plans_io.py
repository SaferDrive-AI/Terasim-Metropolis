"""Tests for plan materialisation (plans/materialize.py).

The round-trip builds a scenario in tmp against the bundled corridor demo map
and checks the trial config both structurally and by actually running it.
Seed plans come from the shared hardcoded builder (tests/fixtures).
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from terasim_city.plans import (
    CorridorPlan,
    PlanProvenance,
    materialize_plan,
)

from tests.fixtures.signal_plans import demo_alt_fixed_plan
from terasim_city.scenario.sumocfg_io import parse_sumocfg, write_sumocfg

REPO_ROOT = Path(__file__).resolve().parents[2]
CORRIDOR_DEMO_DIR = REPO_ROOT / "examples" / "corridor_demo"
DEMO_SUMOCFG = CORRIDOR_DEMO_DIR / "demo.sumocfg"
DEMO_TLS_XML = CORRIDOR_DEMO_DIR / "demo.tls.xml"
DEMO_NET_XML = CORRIDOR_DEMO_DIR / "demo.net.xml"
DEMO_ROU_XML = CORRIDOR_DEMO_DIR / "demo.rou.xml"

requires_demo_map = pytest.mark.skipif(
    not DEMO_SUMOCFG.exists(), reason=f"corridor demo map not present at {CORRIDOR_DEMO_DIR}"
)


def _read_programs(tls_xml: Path) -> dict[str, dict]:
    """Parse a materialised tls.xml: tls_id -> {program_id, offset, phases}."""
    from lxml import etree as ET

    out = {}
    for tl in ET.parse(str(tls_xml)).getroot().findall("tlLogic"):
        out[tl.get("id")] = {
            "program_id": tl.get("programID"),
            "offset_s": float(tl.get("offset", 0)),
            "phases": tuple(
                (float(ph.get("duration")), ph.get("state"))
                for ph in tl.findall("phase")
            ),
        }
    return out


@pytest.fixture()
def two_tls_trial(tmp_path):
    """A scenario sumocfg in tmp plus a 2-TLS plan materialised onto it."""
    scenario_dir = tmp_path / "scenario"
    scenario_cfg = write_sumocfg(
        scenario_dir / "scenario.sumocfg",
        net_file=DEMO_NET_XML,
        route_file=DEMO_ROU_XML,
        additional_files=(DEMO_TLS_XML,),
        sim_end=120.0,
        seed=7,
    )

    plan = CorridorPlan(
        plan_id="trial_plan",
        plans=(
            dataclasses.replace(demo_alt_fixed_plan("J1"), offset_s=0.0),
            dataclasses.replace(demo_alt_fixed_plan("J2"), offset_s=12.0),
        ),
        provenance=PlanProvenance(source="explicit"),
    )
    trial_dir = tmp_path / "trial_0000"
    trial_cfg = materialize_plan(plan, scenario_cfg, trial_dir)
    return plan, scenario_cfg, trial_dir, trial_cfg


@requires_demo_map
def test_materialize_writes_trial_inputs(two_tls_trial):
    plan, scenario_cfg, trial_dir, trial_cfg = two_tls_trial

    assert trial_cfg == trial_dir / "simulation.sumocfg"
    plan_tls = trial_dir / "plan.tls.xml"
    plan_json = trial_dir / "plan.json"
    assert plan_tls.exists() and plan_json.exists()

    # plan.json round-trips to the exact same CorridorPlan.
    payload = json.loads(plan_json.read_text(encoding="utf-8"))
    assert CorridorPlan.from_json_dict(payload) == plan

    # Trial cfg keeps net/routes and appends the plan file LAST in additionals.
    scenario_info = parse_sumocfg(scenario_cfg)
    trial_info = parse_sumocfg(trial_cfg)
    assert trial_info.net_file == scenario_info.net_file
    assert trial_info.route_files == scenario_info.route_files
    assert trial_info.additional_files == scenario_info.additional_files + (plan_tls,)
    assert trial_info.additional_files[-1] == plan_tls

    # plan.tls.xml carries the uniform programID and per-tls offsets.
    materialised = _read_programs(plan_tls)
    assert set(materialised) == {"J1", "J2"}
    assert all(p["program_id"] == "trial_plan" for p in materialised.values())
    assert materialised["J1"]["offset_s"] == 0.0
    assert materialised["J2"]["offset_s"] == 12.0
    expected_phases = tuple(
        (ph.duration_s, ph.state) for ph in plan.by_tls()["J2"].phases
    )
    assert materialised["J2"]["phases"] == expected_phases


@requires_demo_map
def test_materialize_does_not_mutate_scenario(tmp_path, two_tls_trial):
    plan, scenario_cfg, _, _ = two_tls_trial
    before = scenario_cfg.read_bytes()
    materialize_plan(plan, scenario_cfg, tmp_path / "trial_0001")
    assert scenario_cfg.read_bytes() == before


@requires_demo_map
def test_materialize_preserves_end_and_seed(two_tls_trial):
    _, _, _, trial_cfg = two_tls_trial
    from lxml import etree as ET

    root = ET.parse(str(trial_cfg)).getroot()
    assert root.find(".//time/end").attrib["value"] == "120"
    assert root.find(".//random_number/seed").attrib["value"] == "7"


@requires_demo_map
def test_materialize_rejects_scenario_without_routes(tmp_path):
    scenario_cfg = write_sumocfg(
        tmp_path / "scenario.sumocfg",
        net_file=DEMO_NET_XML,
        route_file="",  # written as empty route-files value
        sim_end=60.0,
    )
    plan = CorridorPlan(
        plan_id="p",
        plans=(demo_alt_fixed_plan("J1"),),
        provenance=PlanProvenance(source="explicit"),
    )
    with pytest.raises(ValueError, match="route-files"):
        materialize_plan(plan, scenario_cfg, tmp_path / "trial")


@requires_demo_map
@pytest.mark.skipif(shutil.which("sumo") is None, reason="needs the sumo binary on PATH")
def test_materialized_trial_runs_and_applies_program(two_tls_trial):
    plan, _, _, trial_cfg = two_tls_trial

    result = subprocess.run(
        ["sumo", "-c", str(trial_cfg), "-e", "5", "--no-step-log"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    # The uniform programID is selectable via setProgram at t=0. Run the SUMO
    # client in a subprocess so the singleton runtime cannot leak into this
    # process; libsumo when available, else traci (the terasim overlay makes
    # the same choice at runtime).
    script = textwrap.dedent(
        f"""
        try:
            import libsumo as client
        except ImportError:
            import traci as client
        client.start(["sumo", "-c", {str(trial_cfg)!r}, "--no-step-log", "true"])
        for tls_id in ("J1", "J2"):
            client.trafficlight.setProgram(tls_id, "trial_plan")
            got = client.trafficlight.getProgram(tls_id)
            assert got == "trial_plan", (tls_id, got)
        client.close()
        print("PROGRAM_OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "PROGRAM_OK" in result.stdout
