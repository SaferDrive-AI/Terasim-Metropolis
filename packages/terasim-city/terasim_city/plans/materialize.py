"""Materialise a :class:`CorridorPlan` into runnable SUMO trial inputs.

``materialize_plan`` turns a plan into three files under ``trial_dir``:

* ``plan.tls.xml`` — one static ``<tlLogic>`` per TLS. Every tlLogic carries
  ``programID = plan.plan_id`` (uniform across the corridor), so the
  evaluation env applies the plan with ``setProgram(tls, plan_id)`` for each
  TLS.
* ``plan.json`` — ``plan.to_json_dict()`` for the trials ledger / audit.
* ``simulation.sumocfg`` — a copy of the scenario config with ``plan.tls.xml``
  appended LAST to the additional files, so its tlLogic definitions win over
  any earlier program of the same (tls, programID).

The input scenario is never mutated.
"""

from __future__ import annotations

import json
from pathlib import Path

from lxml import etree as ET

from terasim_city.plans.model import CorridorPlan
from terasim_city.scenario.sumocfg_io import parse_sumocfg, write_sumocfg

PLAN_TLS_FILENAME = "plan.tls.xml"
PLAN_JSON_FILENAME = "plan.json"
TRIAL_SUMOCFG_FILENAME = "simulation.sumocfg"


def _write_plan_tls_xml(plan: CorridorPlan, out_path: Path) -> Path:
    root = ET.Element("additional")
    for signal_plan in plan.plans:
        tl = ET.SubElement(
            root,
            "tlLogic",
            {
                "id": signal_plan.tls_id,
                "programID": plan.plan_id,
                "offset": f"{signal_plan.offset_s:g}",
                "type": "static",
            },
        )
        for phase in signal_plan.phases:
            attrs = {"duration": f"{phase.duration_s:g}", "state": phase.state}
            if phase.name:
                attrs["name"] = phase.name
            ET.SubElement(tl, "phase", attrs)
    ET.ElementTree(root).write(
        str(out_path), encoding="utf-8", xml_declaration=True, pretty_print=True
    )
    return out_path


def _read_end_and_seed(sumocfg_path: Path) -> tuple[float, int]:
    """Read <time><end> and <random_number><seed> from the scenario sumocfg.

    ``parse_sumocfg`` deliberately does not capture these, but the trial must
    preserve the scenario's horizon and seed. Absent values fall back to the
    ``write_sumocfg`` defaults (1800 s / seed 42).
    """
    root = ET.parse(str(sumocfg_path)).getroot()
    sim_end, seed = 1800.0, 42
    end_node = root.find(".//time/end")
    if end_node is not None and end_node.attrib.get("value"):
        sim_end = float(end_node.attrib["value"])
    seed_node = root.find(".//random_number/seed")
    if seed_node is not None and seed_node.attrib.get("value"):
        seed = int(seed_node.attrib["value"])
    return sim_end, seed


def materialize_plan(plan: CorridorPlan, scenario_sumocfg: Path, trial_dir: Path) -> Path:
    """Write ``plan.tls.xml`` + ``plan.json`` + a trial sumocfg into ``trial_dir``.

    Returns:
        The path of the trial sumocfg (``trial_dir/simulation.sumocfg``).

    Raises:
        ValueError: The scenario sumocfg is missing/unparseable or declares no
            route files (a scenario without demand cannot be evaluated).
    """
    scenario_sumocfg = Path(scenario_sumocfg)
    trial_dir = Path(trial_dir)
    trial_dir.mkdir(parents=True, exist_ok=True)

    info = parse_sumocfg(scenario_sumocfg)
    if not info.route_files:
        raise ValueError(
            f"scenario sumocfg {scenario_sumocfg} declares no <route-files>; "
            "cannot materialise a trial without demand"
        )
    sim_end, seed = _read_end_and_seed(scenario_sumocfg)

    plan_tls_path = _write_plan_tls_xml(plan, trial_dir / PLAN_TLS_FILENAME)
    (trial_dir / PLAN_JSON_FILENAME).write_text(
        json.dumps(plan.to_json_dict(), indent=2), encoding="utf-8"
    )

    # write_sumocfg takes one route-files value; a comma-joined string keeps
    # multiple route files intact (parse_sumocfg splits on commas).
    route_value = ",".join(str(p) for p in info.route_files)
    return write_sumocfg(
        trial_dir / TRIAL_SUMOCFG_FILENAME,
        net_file=info.net_file,
        route_file=Path(route_value) if len(info.route_files) == 1 else route_value,  # type: ignore[arg-type]
        additional_files=tuple(info.additional_files) + (plan_tls_path,),
        gui_settings_file=info.gui_settings_file,
        processing_params=dict(info.processing_params),
        routing_params=dict(info.routing_params),
        report_params=dict(info.report_params),
        sim_end=sim_end,
        seed=seed,
        step_length=info.step_length,
    )
