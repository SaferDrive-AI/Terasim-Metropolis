"""Integration tests for the full TMC → SUMO pipeline.

``run_tmc_pipeline`` is driven end-to-end through the topology / matcher /
boundary layers against a real SUMO network: the synthetic demo corridor
(``examples/corridor_demo``), fed with a one-hour payload for its first two
signals (``i1``/``i2``) and a production-shaped ten-minute payload for ``i2``
(both from ``tests/fixtures/tmc_templates.py``).

The tests are skipped automatically when ``sumolib`` is not importable — see
``conftest.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.tmc_templates import ten_minute_tmc
from tests.test_city.conftest import requires_sumolib


# Mark the whole module — every test here parses the demo net with sumolib.
pytestmark = [requires_sumolib]


def test_run_tmc_pipeline_writes_valid_metadata(
    tmp_path: Path,
    two_signal_tmc_json: Path,
    demo_sumocfg: Path,
):
    """metadata.json must be valid JSON with all expected top-level keys."""
    from terasim_city import run_tmc_pipeline

    scenario = run_tmc_pipeline(
        tmc_json=two_signal_tmc_json,
        base_sumocfg=demo_sumocfg,
        output_dir=tmp_path,
        sim_end=200.0,
        seed=42,
    )

    payload = json.loads(scenario.metadata_file.read_text(encoding="utf-8"))
    for key in ("generated_at", "source", "base_sumocfg", "net_file", "params", "output"):
        assert key in payload, f"missing top-level key {key!r}"

    # OD peel-off ("corridor_graph") is the default flow source.
    assert payload["source"]["type"] == "corridor_graph"
    assert payload["source"]["flow_source"] == "corridor_graph"
    assert payload["source"]["corridor_id"] == "demo_arterial"
    assert payload["source"]["n_intersections"] == 2
    assert payload["params"]["sim_end"] == 200.0
    assert payload["params"]["seed"] == 42
    assert payload["output"]["n_flows"] == scenario.n_flows
    assert payload["output"]["total_vehs_per_hour"] == pytest.approx(
        scenario.total_vehs_per_hour, abs=0.5
    )


def test_run_tmc_pipeline_generated_cfg_round_trips(
    tmp_path: Path,
    two_signal_tmc_json: Path,
    demo_sumocfg: Path,
):
    """The freshly written ``simulation.sumocfg`` must round-trip through parse_sumocfg.

    Renamed without the literal "sumocfg" substring because the parent
    ``tests/conftest.py`` auto-applies ``requires_sumo`` to any test whose
    name contains "sumo", which would force a skip on environments without
    ``$SUMO_HOME`` even though this test only needs ``sumolib``.
    """
    from terasim_city import run_tmc_pipeline
    from terasim_city.scenario.sumocfg_io import parse_sumocfg

    scenario = run_tmc_pipeline(
        tmc_json=two_signal_tmc_json,
        base_sumocfg=demo_sumocfg,
        output_dir=tmp_path,
        sim_end=300.0,
        seed=99,
    )

    info = parse_sumocfg(scenario.sumocfg_file)
    # The base sumocfg's net is preserved.
    assert info.net_file.exists()
    # The newly generated routes file is wired in.
    assert info.route_files == (scenario.routes_file,)


def test_run_tmc_pipeline_routes_xml_has_flows(
    tmp_path: Path,
    two_signal_tmc_json: Path,
    demo_sumocfg: Path,
):
    """Spot-check the generated rou.xml: vType present, at least one flow, sane attrs."""
    from xml.etree import ElementTree as ET

    from terasim_city import run_tmc_pipeline

    scenario = run_tmc_pipeline(
        tmc_json=two_signal_tmc_json,
        base_sumocfg=demo_sumocfg,
        output_dir=tmp_path,
        sim_end=200.0,
    )

    root = ET.parse(scenario.routes_file).getroot()
    assert root.tag == "routes"
    assert root.find("vType") is not None
    flows = root.findall("flow")
    assert flows, "the pipeline produced no flows"
    assert len(flows) == scenario.n_flows
    # Every flow must reference a real route and have a positive rate.
    routes_by_id = {r.attrib["id"]: r for r in root.findall("route")}
    for fl in flows:
        assert fl.attrib["route"] in routes_by_id
        assert float(fl.attrib["vehsPerHour"]) > 0


def test_production_tmc_sample_preserves_all_observed_demand(
    tmp_path: Path,
    ten_minute_tmc_json: Path,
    demo_sumocfg: Path,
):
    """A production-shaped ten-minute payload maps every direction and scales its window.

    The payload observes 267 vehicles in ten minutes over nine non-zero
    movements (three approaches without left turns); every movement becomes
    one flow and the window scales x6 to 1602 veh/h — nothing is dropped.
    """
    from terasim_city import run_tmc_pipeline

    counts = ten_minute_tmc()["intersections"][0]["counts"]
    observed = sum(n for turns in counts.values() for n in turns.values())
    movements = sum(1 for turns in counts.values() for n in turns.values() if n > 0)
    assert (observed, movements) == (267, 9)

    scenario = run_tmc_pipeline(
        tmc_json=ten_minute_tmc_json,
        base_sumocfg=demo_sumocfg,
        output_dir=tmp_path,
        sim_end=200.0,
    )

    assert scenario.n_flows == 9
    assert scenario.total_vehs_per_hour == pytest.approx(1602.0)
    assert scenario.total_vehs_per_hour == pytest.approx(observed * 3600.0 / 600.0)
    boundary = json.loads((tmp_path / "boundary.json").read_text(encoding="utf-8"))
    assert set(boundary["intersections"][0]["counts"]) == {"EB", "WB", "NB", "SB"}
    assert not boundary["injection_strategy"][
        "dropped_unmapped_or_unusable_directions"
    ]
