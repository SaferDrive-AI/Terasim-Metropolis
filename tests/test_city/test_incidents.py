"""Integration test for stalled-vehicle incident injection.

Drives the SAME building blocks the trial evaluator uses
(:class:`CorridorSignalEnv` + ``StalledObjectAdversity`` + ``Simulator``) and
asserts against the **FCD output**, so the test depends only on the
externally observable behaviour: the stall lands on the right edge, never
moves, and faces along the lane (the "sideways across the lane" regression).
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from terasim_city.evaluation.diagnostics import disable_teleport, load_stalls
from tests.test_city.conftest import (
    DEMO_SUMOCFG,
    DEMO_TMC_JSON,
    requires_sumo_runtime,
)


# One stalled vehicle on the demo arterial, ~40 m west of J2 on the
# eastbound right lane ``EB_2_0`` (the coordinate is the lane's centre line
# 33 m before its end at the J2 junction, converted with sumolib).
_STALL = {"latitude": 37.334957, "longitude": -121.900454}

# Expected SUMO edge where that coordinate map-matches on
# ``examples/corridor_demo/demo.net.xml``. Verified via
# ``traci.simulation.convertRoad`` during development.
_EXPECTED_STALL_EDGE = "EB_2"

# Lane ``EB_2_0`` runs ~89.3° clockwise from north (computed from the lane
# shape via sumolib: (607.80, 595.27) -> (893.72, 598.60)). If the heading
# override in the env's ``on_start`` is bypassed, the recorded angle would
# default to ~0 (north) -- the "sideways across the lane" bug.
_EXPECTED_LANE_ANGLE_DEG = 89.33
_ANGLE_TOLERANCE_DEG = 5.0

# Vehicle-ID prefix used by ``StalledObjectAdversity`` for non-VRU
# vehicles. See ``terasim_nde_nade/adversity/static/stalled_object.py``.
_STALLED_VEH_PREFIX = "BV_DEFAULT_VEHTYPE_stalled_object_"


@pytest.fixture
def stall_json(tmp_path: Path) -> Path:
    """The stalled-vehicle incident file (``{"stalled_vehicles": [...]}``)."""
    path = tmp_path / "stalled_vehicles.json"
    path.write_text(json.dumps({"stalled_vehicles": [_STALL]}), encoding="utf-8")
    return path


def _run_stall_scenario(
    tmc_json: Path,
    base_sumocfg: Path,
    stall_data: Path,
    output_dir: Path,
    sim_end: float,
) -> Path:
    """Generate a scenario and run it with one injected stall; return sim dir."""
    from terasim.logger.infoextractor import InfoExtractor
    from terasim.simulator import Simulator
    from terasim_nde_nade.adversity import StalledObjectAdversity

    from terasim_city.api import run_tmc_pipeline
    from terasim_city.envs.corridor_env import (
        STALL_END_TIME,
        STALL_OBJECT_TYPE,
        CorridorSignalEnv,
    )

    scenario = run_tmc_pipeline(
        tmc_json, base_sumocfg, output_dir, sim_end=sim_end, seed=42
    )
    # Incident runs must not teleport the stall away — same patch the
    # corridor orchestration applies.
    disable_teleport(Path(scenario.sumocfg_file), sim_end=sim_end, seed=42)

    stalls = load_stalls(stall_data)
    adversities = [
        StalledObjectAdversity(
            placement_mode="latlon_degree",
            lat=stall["latitude"],
            lon=stall["longitude"],
            degree=0.0,
            start_time=0.0,
            end_time=STALL_END_TIME,
            object_type=STALL_OBJECT_TYPE,
        )
        for stall in stalls
    ]
    env = CorridorSignalEnv(
        programs={},  # signal program irrelevant to stall placement
        adversities=adversities,
        sim_end=sim_end,
        info_extractor=InfoExtractor,
    )
    sim_output_dir = output_dir / "sim_output"
    sim = Simulator(
        sumo_net_file_path=Path(scenario.net_file),
        sumo_config_file_path=Path(scenario.sumocfg_file),
        num_tries=10,
        gui_flag=False,
        output_path=sim_output_dir,
        sumo_output_file_types=["fcd_all"],
    )
    sim.bind_env(env)
    sim.run()
    return sim_output_dir


@pytest.mark.integration
@pytest.mark.requires_sumo
@requires_sumo_runtime
def test_stalled_vehicle_places_and_faces_correctly(
    tmp_path: Path,
    stall_json: Path,
):
    """Real e2e for the stalled-vehicle path used by the trial evaluator.

    Note the double gating: ``@requires_sumo_runtime`` is our skipif-based
    helper (checks ``shutil.which("sumo")`` + ``traci`` importable).
    ``@pytest.mark.requires_sumo`` is the repo-wide marker so that
    ``pytest -m "not requires_sumo"`` cleanly deselects this test on
    SUMO-less environments.
    """
    sim_output_dir = _run_stall_scenario(
        tmc_json=DEMO_TMC_JSON,
        base_sumocfg=DEMO_SUMOCFG,
        stall_data=stall_json,
        output_dir=tmp_path / "run",
        sim_end=15.0,
    )

    # Confirm the teleport patch landed in the generated sumocfg. Without it,
    # a long-running sim would teleport the stalled vehicle at t≈300s and the
    # test would silently appear to work for short runs but fail in production.
    from terasim_city.scenario.sumocfg_io import parse_sumocfg

    info = parse_sumocfg(tmp_path / "run" / "simulation.sumocfg")
    assert info.processing_params.get("time-to-teleport") == "-1", (
        "teleport patch did not apply to the generated sumocfg"
    )

    # ---- Assertions against FCD output --------------------------------
    fcd_file = sim_output_dir / "fcd_all.xml"
    assert fcd_file.exists(), f"fcd_all.xml not written to {sim_output_dir}"
    fcd_root = ET.parse(fcd_file).getroot()

    stalled_records: list[tuple[str, dict]] = []
    for ts in fcd_root.findall("timestep"):
        for v in ts.findall("vehicle"):
            if v.attrib.get("id", "").startswith(_STALLED_VEH_PREFIX):
                stalled_records.append((ts.attrib["time"], v.attrib))
    assert stalled_records, (
        f"no vehicle with id starting {_STALLED_VEH_PREFIX!r} found in "
        f"fcd_all.xml — no stalled vehicle was spawned."
    )

    # Property 1 — edge placement (derived from FCD's ``lane`` attribute).
    # Catches the ``StalledObjectAdversity._get_edge_from_latlon`` silent
    # fallback to ``"1"`` when lat/lon conversion fails.
    first_lane = stalled_records[0][1].get("lane", "")
    derived_edge = (
        first_lane.rsplit("_", 1)[0] if "_" in first_lane else first_lane
    )
    assert derived_edge != "1", (
        f"Stalled vehicle fell back to default edge '1' (lane={first_lane!r}). "
        "The stall lat/lon may be outside the network bounds or the lat/lon → "
        "edge conversion failed."
    )
    assert derived_edge == _EXPECTED_STALL_EDGE, (
        f"Stalled vehicle placed on unexpected edge {derived_edge!r} "
        f"(lane={first_lane!r}); expected {_EXPECTED_STALL_EDGE!r} for the "
        "demo stall coordinate. Has the demo network been regenerated?"
    )

    # Property 2 — speed == 0 in every recorded frame.
    for t, vattrs in stalled_records:
        speed = float(vattrs["speed"])
        assert speed == pytest.approx(0.0, abs=1e-6), (
            f"stalled vehicle moved at t={t}: speed={speed} (expected 0)"
        )

    # Property 3 — heading ≈ lane direction.
    # Catches the bypass of ``_align_adversity_heading_to_lane`` in the
    # env's on_start (without it, latlon_degree mode forces angle=0=north
    # and the GUI shows the car sideways across the lane).
    angles = [float(v["angle"]) for _, v in stalled_records]
    assert len(set(angles)) == 1, (
        f"stalled vehicle's angle is not constant across frames: {set(angles)}"
    )
    assert abs(angles[0] - _EXPECTED_LANE_ANGLE_DEG) < _ANGLE_TOLERANCE_DEG, (
        f"stalled vehicle's recorded angle {angles[0]:.2f} differs sharply "
        f"from the lane's expected heading {_EXPECTED_LANE_ANGLE_DEG:.2f}. "
        "Did _align_adversity_heading_to_lane fail to run?"
    )
