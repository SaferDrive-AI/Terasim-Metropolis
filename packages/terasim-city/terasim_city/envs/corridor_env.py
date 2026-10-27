"""TeraSim environment that forces signal programs while evaluating a corridor.

``CorridorSignalEnv`` applies one signal program per TLS at start (fail-loud
when the program is not available), keeps end-of-run counters (insertions,
arrivals, backlog, halting on stall edges) and handles stalled-object
initialization. The stall constants and
are defined HERE (the env layer); evaluation
and orchestration layers import them from this module.

The signal-program application, the stalled-object injection, the backlog
counters and the ``sim_end`` termination all live in :class:`SignalProgramMixin`
so the CNDE evaluation env (:mod:`terasim_city.envs.cnde_corridor_env`, which
sits on a completely different base chain — NNDE instead of ``EnvTemplate``)
does all four in exactly the same way.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from terasim.envs.template import EnvTemplate
from terasim.logger.infoextractor import InfoExtractor
from terasim.overlay import traci

from terasim_city.factories import PassiveVehicleFactory

logger = logging.getLogger(__name__)

#: End time handed to StalledObjectAdversity: -1 keeps the stall for the whole run.
STALL_END_TIME = -1.0

#: SUMO vehicle type used for injected stalled objects.
STALL_OBJECT_TYPE = "DEFAULT_VEHTYPE"


__all__ = [
    "STALL_END_TIME",
    "STALL_OBJECT_TYPE",
    "SignalProgramMixin",
    "CorridorSignalEnv",
]


class SignalProgramMixin:
    """Forced signal programs + stall injection + corridor backlog counters.

    Base-class agnostic on purpose: it defines no ``on_start``/``on_step`` of
    its own, only the hooks :meth:`_signal_mixin_on_start` and
    :meth:`_signal_mixin_on_step` that the concrete env calls around its own
    ``super()`` chain, plus :meth:`should_continue_simulation` (run ends at
    ``sim_end``). State is installed by :meth:`_signal_mixin_init`, called from
    the env's ``__init__`` AFTER its ``super().__init__`` — mirroring the
    original ordering exactly.

    ``programs`` maps ``tls_id -> program_id``; every entry is applied via
    ``traci.trafficlight.setProgram`` during ``on_start`` after validating the
    program is available for that TLS (RuntimeError otherwise). Applied
    decisions are recorded in ``signal_decisions`` (in ``programs`` order);
    ``signal_decision`` keeps the first decision for single-TLS callers.
    """

    def _signal_mixin_init(
        self,
        *,
        programs: Mapping[str, str],
        adversities: Sequence,
        sim_end: float,
    ):
        self._programs = dict(programs)
        self._adversities = list(adversities)
        self._initialized: set = set()
        self._stalled_vehicle_ids: set[str] = set()
        self._sim_end = float(sim_end)
        self.stalled_edge_ids: list[str] = []
        self.stalled_lane_ids: list[str] = []
        self.cum_inserted = 0
        self.cum_arrived = 0
        self.end_running = 0
        self.end_pending = 0
        self.end_halting_on_stall_edge = 0
        self.end_stalled_objects_present = 0

    def _signal_mixin_on_start(self):
        for adv in self._adversities:
            edge_id, _lane_idx, lane_id = _resolve_lane_and_align_heading(adv)
            if not adv.is_effective():
                raise RuntimeError(f"stalled object is not effective: {adv.adversity_id}")
            adv.initialize(time=0.0)
            self._initialized.add(adv.adversity_id)
            self.stalled_edge_ids.append(edge_id)
            self.stalled_lane_ids.append(lane_id)
            sid = getattr(adv, "stalled_object_id", None)
            if sid:
                self._stalled_vehicle_ids.add(sid)

        for tls_id, program_id in self._programs.items():
            available = [
                logic.programID
                for logic in traci.trafficlight.getAllProgramLogics(tls_id)
            ]
            if program_id not in available:
                raise RuntimeError(
                    f"program {program_id!r} is not available for TLS {tls_id!r}: "
                    f"{available}"
                )
            traci.trafficlight.setProgram(tls_id, program_id)

    def _signal_mixin_on_step(self):
        t = traci.simulation.getTime()
        for adv in self._adversities:
            if adv.adversity_id in self._initialized:
                adv.update(time=t)
        self.cum_inserted += traci.simulation.getDepartedNumber()
        self.cum_arrived += traci.simulation.getArrivedNumber()
        active_ids = traci.vehicle.getIDList()
        self.end_running = len(active_ids)
        self.end_pending = max(
            traci.simulation.getMinExpectedNumber() - self.end_running,
            0,
        )
        self.end_stalled_objects_present = sum(
            1 for vehicle_id in active_ids if vehicle_id in self._stalled_vehicle_ids
        )
        if self.stalled_edge_ids:
            # Sum over every distinct stalled edge so multi-incident runs
            # are not undercounted (was: only stalled_edge_ids[0]).
            halting = 0
            for edge_id in dict.fromkeys(self.stalled_edge_ids):
                try:
                    halting += traci.edge.getLastStepHaltingNumber(edge_id)
                except traci.TraCIException:
                    pass
            self.end_halting_on_stall_edge = halting

    def should_continue_simulation(self):
        return traci.simulation.getTime() < self._sim_end


class CorridorSignalEnv(SignalProgramMixin, EnvTemplate):
    """Evaluation env applying one forced signal program per TLS.

    Signal application, stall injection, backlog counters and the ``sim_end``
    termination all come from :class:`SignalProgramMixin`; this class only
    wires them into the ``EnvTemplate`` lifecycle.
    """

    def __init__(
        self,
        *,
        programs: Mapping[str, str],
        adversities: Sequence,
        sim_end: float,
        vehicle_factory=None,
        info_extractor=InfoExtractor,
    ):
        super().__init__(
            vehicle_factory=(
                vehicle_factory if vehicle_factory is not None else PassiveVehicleFactory()
            ),
            info_extractor=info_extractor,
        )
        self._signal_mixin_init(
            programs=programs, adversities=adversities, sim_end=sim_end
        )

    def on_start(self, ctx):
        result = super().on_start(ctx)
        self._signal_mixin_on_start()
        return result

    def on_step(self, ctx):
        self._signal_mixin_on_step()
        return super().on_step(ctx)


def _resolve_lane_and_align_heading(adv) -> tuple[str, int, str]:
    """Map a lat/lon stall to a SUMO edge/lane and align its heading to the lane."""
    x, y = traci.simulation.convertGeo(adv._lon, adv._lat, fromGeo=True)
    edge_id, _, lane_idx = traci.simulation.convertRoad(x, y, isGeo=False)
    if not edge_id:
        raise RuntimeError(f"({adv._lat}, {adv._lon}) does not map to any SUMO edge")
    lane_id = f"{edge_id}_{lane_idx}"
    shape = traci.lane.getShape(lane_id)
    if len(shape) < 2:
        raise RuntimeError(f"lane {lane_id!r} has degenerate shape")
    dx = shape[1][0] - shape[0][0]
    dy = shape[1][1] - shape[0][1]
    adv._degree = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
    return edge_id, lane_idx, lane_id
