"""Flow source that reuses the base scenario's own route files verbatim.

Customers who already have a calibrated route file need that demand used
as-is, not re-synthesised from turning counts. Every element of the base
sumocfg's route files is copied, attributes and ids untouched, into one
``routes.rou.xml``.

SUMO loads several route files side by side and merges them by departure
time, but it requires one file to be sorted by departure: an element that goes
back in time is dropped with a warning ("Route file should be sorted by
departure time, ignoring ..."). Plain concatenation of per-mode or
per-interval files would therefore lose demand silently. When the files
are not already in departure order, the merged file lists all definitions
(``vType``, ``route``, ...) first and every demand element ordered by its
start time, the order in which SUMO itself merges separate route files.
A single route file is always copied in its own order.
"""
from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree as ET

from terasim_city.flow.descriptors import TMCData
from terasim_city.flow.sources.base import FlowSource
from terasim_city.scenario.sumocfg_io import parse_sumocfg

logger = logging.getLogger(__name__)

_DEMAND_TAGS = ("flow", "vehicle", "trip", "person", "personFlow", "container", "containerFlow")
#: Demand elements that carry their start time in ``begin`` rather than ``depart``.
_FLOW_TAGS = frozenset(("flow", "personFlow", "containerFlow"))
#: Seconds per field of SUMO's ``H:M:S`` / ``D:H:M:S`` clock times.
_CLOCK_WEIGHTS_S = (86400.0, 3600.0, 60.0, 1.0)


def _departure_s(element) -> float | None:
    """Start time of a demand element in seconds, ``None`` when it has no clock time.

    SUMO writes times as plain seconds or as ``H:M:S`` / ``D:H:M:S``; symbolic
    values such as ``triggered`` carry no time. A flow without ``begin`` starts
    with the simulation, at 0.
    """
    raw = element.get("begin", "0") if element.tag in _FLOW_TAGS else element.get("depart")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    fields = raw.split(":")
    if len(fields) not in (3, 4):
        return None
    try:
        values = [float(field) for field in fields]
    except ValueError:
        return None
    return sum(value * weight for value, weight in zip(values, _CLOCK_WEIGHTS_S[-len(values):]))


def _in_departure_order(elements) -> bool:
    """True if no demand element starts before an earlier one (SUMO's own check)."""
    latest = float("-inf")
    for element in elements:
        seconds = _departure_s(element) if element.tag in _DEMAND_TAGS else None
        if seconds is None:
            continue
        if seconds < latest:
            return False
        latest = seconds
    return True


def _by_departure(elements) -> list:
    """Definitions first, in file order, then the demand ordered by start time.

    The sort is stable, so equal times keep file order. An element without a
    clock time takes the time of the element before it and stays right behind
    it.
    """
    definitions = [element for element in elements if element.tag not in _DEMAND_TAGS]
    timed = []
    latest = 0.0
    for element in elements:
        if element.tag not in _DEMAND_TAGS:
            continue
        seconds = _departure_s(element)
        latest = latest if seconds is None else seconds
        timed.append((latest, element))
    timed.sort(key=lambda pair: pair[0])
    return definitions + [element for _, element in timed]


class ScenarioRoutesFlowSource(FlowSource):
    """Copy every element of the base sumocfg's route files into one ``routes.rou.xml``.

    The TMC payload is still parsed (``tmc``) because ``run_tmc_pipeline``
    records its corridor in ``metadata.json``, but no demand is derived from
    its counts. The demand-synthesis tuning options of ``run_tmc_pipeline``
    (hops, ``flow_scale``, ...) do not apply and are ignored.
    """

    def __init__(self, tmc_json: Path | str, *, base_sumocfg: Path | str | None = None, **_ignored):
        if base_sumocfg is None:
            raise ValueError("scenario_routes flow source needs base_sumocfg")
        self.tmc_json = Path(tmc_json)
        self.tmc = TMCData.from_json(self.tmc_json)
        self._base_sumocfg = Path(base_sumocfg)
        #: Demand elements (flows, vehicles, trips, persons, ...) copied by the
        #: last :meth:`generate`. Informational: ``GeneratedScenario.n_flows``
        #: counts ``<flow>`` elements of the written file instead.
        self.n_flows = 0

    @property
    def source_name(self) -> str:
        return "scenario_routes"

    def generate(self, net_path: Path, output_path: Path, sim_end: float = 1800.0) -> Path:
        """Write the base scenario's route files to ``output_path`` as one ``rou.xml``.

        Several files are concatenated, or merged by departure time when
        concatenation would not be in departure order (see the module
        docstring). ``net_path`` and ``sim_end`` are part of the
        :class:`FlowSource` contract and unused: the scenario's own demand
        needs neither.

        Raises:
            ValueError: The base sumocfg declares no ``<route-files>``.
        """
        info = parse_sumocfg(self._base_sumocfg)
        if not info.route_files:
            raise ValueError(
                f"{self._base_sumocfg}: declares no <route-files>; the scenario_routes "
                "flow source needs the scenario's own demand"
            )
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        elements = [
            child
            for route_file in info.route_files
            for child in ET.parse(str(route_file)).getroot()
            if isinstance(child.tag, str)  # skips comments and processing instructions
        ]
        if len(info.route_files) > 1 and not _in_departure_order(elements):
            elements = _by_departure(elements)
        self.n_flows = sum(1 for element in elements if element.tag in _DEMAND_TAGS)
        if not self.n_flows:
            logger.warning(
                "%s: the route files %s hold no demand (no flow, vehicle, trip, person "
                "or container element); every trial will simulate an empty network",
                self._base_sumocfg,
                ", ".join(str(route_file) for route_file in info.route_files),
            )
        root = ET.Element("routes")
        root.extend(elements)
        # The copied elements bring their own whitespace; without this
        # pretty_print would leave the merged file ragged.
        ET.indent(root, space="  ")
        ET.ElementTree(root).write(
            str(output_path), xml_declaration=True, encoding="UTF-8", pretty_print=True
        )
        return output_path


__all__ = ["ScenarioRoutesFlowSource"]
