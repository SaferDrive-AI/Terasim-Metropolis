"""CNDE (neural) evaluation env for a signalized corridor.

Same evaluation contract as :class:`~terasim_city.envs.corridor_env.CorridorSignalEnv`
— force one signal program per TLS, inject the stalled objects, keep the
backlog counters, end at ``sim_end`` — but the background traffic is driven by
the CNDE neural simulator instead of SUMO's car-following model. That requires
a different base chain (``NNDE`` -> ``NDE`` -> ``EnvTemplateComplete``), so the
corridor behaviour is shared through :class:`SignalProgramMixin` rather than
through a common env base class.

Two deliberate departures from the CNDE-enabled NADE environments (built
around an ego AV), both because this scenario has no ego AV:

* :meth:`CndeCorridorSignalEnv.get_context_for_control` is the globalized
  provider — every SUMO vehicle is cached and controlled. The AV-anchored
  version in ``BaseEnvComplete`` returns ``{}`` when no vehicle is named "AV",
  which would silently disable the whole pipeline here.
* ``on_step`` runs only the CNDE-necessary sub-sequence of
  ``NADEWithAV.NADE_decision_and_control``. No adversarial decision, no
  importance sampling, no AV command cache — see :meth:`on_step`.
"""

from __future__ import annotations

import os
from typing import Mapping, Sequence

from loguru import logger

from terasim.logger.infoextractor import InfoExtractor
from terasim.overlay import traci
from terasim.params import AgentType
from terasim.vulnerable_road_user.controllers.sumo_move_controller import (
    SUMOMOVEController as VulnerableRoadUserSUMOMOVEController,
)
from terasim.vulnerable_road_user.decision_models.sumo_model import (
    SUMOModel as VulnerableRoadUserSUMOModel,
)
from terasim.vulnerable_road_user.factories.vulnerable_road_user_factory import (
    VulnerableRoadUserFactory,
)
from terasim.vulnerable_road_user.vulnerable_road_user import VulnerableRoadUser
from terasim_nde_nade.envs.nnde import NNDE
from terasim_nde_nade.utils import CommandType

from terasim_city.envs.corridor_env import SignalProgramMixin

__all__ = [
    "DIAG_LOGS_ENV_VAR",
    "PREDICT_CONTROL_ONLY_ENV_VAR",
    "RELAX_QUEUE_FILTER_ENV_VAR",
    "RELAXABLE_CNDE_FILTER_REASONS",
    "CndeCorridorSignalEnv",
]

#: Set to "1" to drop the two queue-shaped CNDE filters (see
#: :meth:`CndeCorridorSignalEnv._should_include_in_cnde_control`).
RELAX_QUEUE_FILTER_ENV_VAR = "TERASIM_CITY_CNDE_RELAX_QUEUE_FILTER"

#: Set to "1" to write the per-step CNDE diagnostic jsonl logs
#: (``output/cnde_trajectory_log.jsonl``, ``heading_tracker.jsonl``, ``cnde_audit.jsonl``:
#: ~6 GB per 1800 s evaluation). Off by default for city trials; ``kpi.json`` is unaffected.
DIAG_LOGS_ENV_VAR = "TERASIM_CITY_CNDE_DIAG_LOGS"

#: Set to "1" to run the neural model only for the ~1/3 of vehicles that pass the CNDE control
#: filter (``cnde_control_ids``) instead of every vehicle with a complete history. All vehicles
#: stay in the scene as context. Opt-in: the predicted set changes the route-assignment random
#: stream and the batch composition, so the trajectories are not bit-identical to the default.
PREDICT_CONTROL_ONLY_ENV_VAR = "TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY"

#: Exclusion reasons that :data:`RELAX_QUEUE_FILTER_ENV_VAR` may override.
#: "junction" is NOT here and never will be: handing a vehicle already inside
#: an intersection to CNDE deadlocks the junction.
RELAXABLE_CNDE_FILTER_REASONS = frozenset({"approaching_edge_end", "low_speed"})


class _PassiveVulnerableRoadUserFactory(VulnerableRoadUserFactory):
    """VRUs stay under SUMO control; CNDE only ever drives vehicles.

    ``NDE.__init__`` requires a VRU factory, and ``_maintain_all_vulnerable_road_users``
    instantiates one object per ``traci.person`` in the network, so a corridor
    scenario that happens to contain pedestrians must not crash. ``SUMOModel``
    returns ``(None, None)``, which short-circuits ``apply_control`` and leaves
    SUMO's own pedestrian model untouched.
    """

    def create_vulnerable_road_user(self, vru_id, simulator):
        return VulnerableRoadUser(
            vru_id,
            simulator,
            sensors=[],
            decision_model=VulnerableRoadUserSUMOModel(),
            controller=VulnerableRoadUserSUMOMOVEController(simulator),
        )


class CndeCorridorSignalEnv(SignalProgramMixin, NNDE):
    """CNDE high-fidelity evaluation env for a signalized corridor.

    The NNDE chain carries the inference orchestration; the mixin carries the
    signal program application, the stalled-object injection, the backlog
    counters and the ``sim_end`` termination.
    """

    #: Recompute the CNDE control filter every N steps, so one decision holds
    #: for a whole 0.5 s CNDE trajectory window (same value/rhythm as
    #: ``BaseEnvComplete.get_context_for_control``).
    CNDE_FILTER_INTERVAL = 5

    def __init__(
        self,
        *,
        programs: Mapping[str, str],
        adversities: Sequence,
        sim_end: float,
        nnde_simulator,
        map_id_mapping,
        vehicle_factory,
        info_extractor=InfoExtractor,
    ):
        super().__init__(
            vehicle_factory=vehicle_factory,
            vru_factory=_PassiveVulnerableRoadUserFactory(),
            info_extractor=info_extractor,
            # City trials are driven by a generated demand profile that starts
            # at t=0, so there is nothing to warm up. lb/ub give
            # warmup_time == 0, but that alone still leaves NDE's warmup loop
            # running one un-instrumented `traci.simulationStep()` outside the
            # env's own step hooks, plus a latent >2500-vehicle `traci.load`
            # reload; `sumo_warmup` below is therefore a documented no-op.
            warmup_time_lb=0,
            warmup_time_ub=1,
            run_time=float(sim_end),
            nnde_simulator=nnde_simulator,
            map_id_mapping=map_id_mapping,
            cnde_diag_logs=os.environ.get(DIAG_LOGS_ENV_VAR, "").strip() == "1",
        )
        self._signal_mixin_init(
            programs=programs, adversities=adversities, sim_end=sim_end
        )
        # The parent's per-step vehicle recoloring is an AV-demo affordance:
        # it calls traci.vehicle.getIDList() once per vehicle per step and
        # repaints the whole cache circle (BaseEnvComplete._step:266-280).
        # With a whole-network cache list that is O(N^2) TraCI traffic per
        # step for zero evaluation value.
        self.cnde_vehicle_coloring = False
        # Read once at construction: the switch selects an experiment arm, it
        # is not meant to flip mid-run.
        self._relax_queue_filter = (
            os.environ.get(RELAX_QUEUE_FILTER_ENV_VAR, "").strip() == "1"
        )
        # Read by NNDE.nnde_make_decisions: predict only cnde_control_ids (opt-in).
        self.cnde_predict_control_only = (
            os.environ.get(PREDICT_CONTROL_ONLY_ENV_VAR, "").strip() == "1"
        )
        if self.cnde_predict_control_only:
            logger.info(f"[CNDE] {PREDICT_CONTROL_ONLY_ENV_VAR}=1 -> the model predicts only the control candidates")
        if self._relax_queue_filter:
            logger.info(
                f"[CNDE] {RELAX_QUEUE_FILTER_ENV_VAR}=1 -> queue filters "
                f"({', '.join(sorted(RELAXABLE_CNDE_FILTER_REASONS))}) relaxed; "
                "junction filter still enforced"
            )

    ########## lifecycle ##########

    def sumo_warmup(self, warmup_time):
        """No warmup for city trials (see ``__init__``); NDE's hook, neutered."""
        logger.info("[CNDE] warmup skipped: city corridor trials start at t=0")

    def on_start(self, ctx):
        result = super().on_start(ctx)
        self._signal_mixin_on_start()
        return result

    def on_step(self, ctx):
        """One CNDE step: the CNDE-necessary slice of NADE's choreography.

        Follows ``NADEWithAV.NADE_decision_and_control`` and
        ``NADE.base_decision`` of the CNDE-enabled NADE environments, with
        every adversarial/importance-sampling/AV branch dropped.

        1. mixin bookkeeping — stall updates + backlog counters, read before
           the half step exactly like ``CorridorSignalEnv.on_step`` does.
        2. ``cache_history_tls_data`` — CNDE consumes traffic light state out
           of this cache (``nade.preparation`` :138).
        3. base decisions + half-step ``executeMove`` (``base_decision`` :157-163).
        3b. re-read the departure/arrival counters, which are only visible in
           the window right after ``executeMove`` (see below).
        4. refresh cached sensors for every cache-radius vehicle so their
           history carries the post-``executeMove`` time (:1043-1045).
        5. ``nnde_make_decisions`` (:1048-1052).
        6. merge: a CNDE trajectory only replaces a DEFAULT command
           (``merge_NADE_NeuralNDE_control_commands`` nade.py:353-359,
           reimplemented locally — nade.py is not imported).
        7. ``refresh_control_commands_state`` + ``execute_control_commands`` (:1070-1074).
        """
        # 1. Signal/stall/backlog bookkeeping.
        self._signal_mixin_on_step()

        # 2. Traffic light history — CNDE's traffic light input.
        self.cache_history_tls_data()

        # 3. Base (SUMO-level) decisions, then move SUMO half a step forward so
        #    the CNDE trajectories are computed against post-move positions.
        base_control_commands, env_command_information = self.make_decisions(ctx)
        env_observation = self.get_env_observation(ctx)
        (
            env_command_information,
            env_observation,
            should_continue_simulation_flag,
        ) = self.executeMove(ctx, env_command_information, env_observation)

        # 3b. Departure/arrival counters, re-read AFTER the half step.
        #
        # ``SignalProgramMixin._signal_mixin_on_step`` accumulates
        # ``getDepartedNumber()``/``getArrivedNumber()`` BEFORE the half step,
        # which is right for ``CorridorSignalEnv`` (its only SUMO step is the
        # simulator's own) but reads a structural 0 here: libsumo's
        # ``executeMove()`` consumes the pending vehicle-state changes, and the
        # trailing ``simulationStep()`` (Simulator's ``sumo_step``) clears them
        # again, so nothing is ever left to see at the top of the next
        # ``on_step``. Measured on this corridor's own scenario over 60 s:
        # a plain ``simulationStep`` loop counts (37 departed, 15 arrived);
        # an ``executeMove`` + ``simulationStep`` loop counts (0, 0) reading
        # before ``executeMove`` and (37, 15) reading right after it.
        # Accumulating here is therefore additive, not double-counting: the
        # mixin's own read above contributes exactly 0 in this env.
        self.cum_inserted += traci.simulation.getDepartedNumber()
        self.cum_arrived += traci.simulation.getArrivedNumber()

        if self.cnde_enabled and env_observation.get(AgentType.VEHICLE):
            cache_veh_ids = set(self.control_ls.get("terasim_controlled_vehicle_ids", []))
            control_veh_ids = set(self.control_ls.get("control_radius_vehicle_ids", []))

            # 4. Refresh the cached ego sensors AFTER executeMove, otherwise
            #    the history CNDE syncs from is one half-step stale.
            for veh_id in cache_veh_ids:
                if veh_id in self.vehicle_list:
                    _ = self.vehicle_list[veh_id].observation

            # 5. Neural inference (internally a no-op except every 5th step).
            nnde_control_commands = self.nnde_make_decisions(
                env_observation,
                controlled_vehicle_ids=control_veh_ids,
                cache_vehicle_ids=cache_veh_ids,
            )

            # 6. Merge.
            self._merge_cnde_control_commands(
                base_control_commands, nnde_control_commands
            )

        # 7. Execute.
        self.refresh_control_commands_state()
        self.execute_control_commands(base_control_commands)
        return should_continue_simulation_flag

    @staticmethod
    def _merge_cnde_control_commands(base_control_commands, cnde_control_commands):
        """Overlay CNDE commands onto DEFAULT base commands, in place.

        Verbatim semantics of ``NADE.merge_NADE_NeuralNDE_control_commands``
        (``terasim_nde_nade.envs.nade``): only a command whose type is DEFAULT may
        be replaced, so a base decision that actually asked for something
        (a lane change, an adversity-driven maneuver) always wins. A base
        command of ``None`` — a SUMO-passive vehicle whose controller cannot
        execute trajectories at all — is not DEFAULT and is therefore left
        alone.
        """
        vehicle_commands = base_control_commands[AgentType.VEHICLE]
        for veh_id, cnde_command in cnde_control_commands.items():
            if veh_id not in vehicle_commands:
                continue
            base_command = vehicle_commands[veh_id]
            if getattr(base_command, "command_type", None) == CommandType.DEFAULT:
                vehicle_commands[veh_id] = cnde_command

    ########## context / filtering ##########

    def get_context_for_control(self):
        """Whole-network vehicle context (this scenario has no ego vehicle).

        The parent anchors both radii on the ego vehicle via a SUMO context
        subscription. A corridor timing scenario has no ego at all, and its map
        is one intersection plus ~250 m of approach, so the network itself is
        the bound: cache = control = every SUMO vehicle.

        ``cnde_control_ids`` keeps the parent's filter and the parent's rhythm
        (recomputed every ``CNDE_FILTER_INTERVAL`` steps, so the set stays
        stable across a whole CNDE trajectory window).

        Injected stalled objects are stripped from all three published lists
        and republished under ``static_adversarial_object_id_list``, exactly as
        ``NADEWithAV.get_context_for_control`` does. A stall is not a normal
        background vehicle: the adversity re-pins it every step with ``moveToXY`` +
        ``setSpeed(0)`` (stalled_object.py:265-283), so letting CNDE issue
        trajectories for it would put two writers on the same vehicle. The
        filter alone would not catch it — a mid-block stall (speed 0, no
        leader, lane_pos > margin) is excluded only by the "low_speed"
        condition, which is exactly what
        ``TERASIM_CITY_CNDE_RELAX_QUEUE_FILTER=1`` turns off.
        """
        # Removed BEFORE the filter loop, so the cached cnde_control_ids can
        # never contain a stall either.
        stalled_ids = self._stalled_vehicle_ids
        all_ids = [
            veh_id
            for veh_id in traci.vehicle.getIDList()
            if veh_id not in stalled_ids
        ]

        if not hasattr(self, "_cnde_filter_counter"):
            self._cnde_filter_counter = 0
        if not hasattr(self, "_cached_cnde_control_ids"):
            self._cached_cnde_control_ids = []

        cnde_control_ids = self._cached_cnde_control_ids
        if self.cnde_enabled:
            self._cnde_filter_counter += 1
            should_recompute = (
                self._cnde_filter_counter % self.CNDE_FILTER_INTERVAL == 1
                or not self._cached_cnde_control_ids
            )

            if should_recompute:
                cnde_control_ids = []
                filter_reasons: dict[str, int] = {}
                for veh_id in all_ids:
                    included, reason = self._should_include_in_cnde_control(veh_id)
                    if included:
                        cnde_control_ids.append(veh_id)
                    else:
                        filter_reasons[reason] = filter_reasons.get(reason, 0) + 1
                self._cached_cnde_control_ids = cnde_control_ids

                sim_time = traci.simulation.getTime()
                reason_str = (
                    ", ".join(f"{k}={v}" for k, v in sorted(filter_reasons.items()))
                    if filter_reasons
                    else "none"
                )
                logger.info(
                    f"[CNDE] t={sim_time:.1f} sumo={len(all_ids) + len(stalled_ids)} "
                    f"cache={len(all_ids)} "
                    f"control={len(cnde_control_ids)} "
                    f"stalled={len(stalled_ids)} "
                    f"filtered({reason_str})"
                )

        self.control_ls = {
            "terasim_controlled_vehicle_ids": all_ids,
            "control_radius_vehicle_ids": all_ids,
            "cnde_control_ids": cnde_control_ids,
            # Consumed by NDE.executeMove (nde.py:218); also the crib's
            # republish contract (nade_with_av.py:832).
            "static_adversarial_object_id_list": list(stalled_ids),
        }
        return self.control_ls

    def _should_include_in_cnde_control(self, veh_id: str) -> tuple:
        """Parent filter, optionally without its two queue-shaped conditions.

        With :data:`RELAX_QUEUE_FILTER_ENV_VAR` unset this is exactly
        ``BaseEnvComplete._should_include_in_cnde_control``. With it set to
        "1", a vehicle rejected for "approaching_edge_end" (< 3 s to the stop
        bar) or "low_speed" (queued below 3 m/s) is admitted instead — those
        two conditions exclude nearly every vehicle that matters in a
        signalized corridor, and this switch exists to A/B-test exactly that.

        The junction conditions are never relaxed. Because the parent returns
        on its FIRST failing condition, a vehicle rejected for a relaxable
        reason may never have been tested against the conditions that follow
        it, so those are re-checked here before the vehicle is admitted.
        """
        included, reason = super()._should_include_in_cnde_control(veh_id)
        if included or not self._relax_queue_filter:
            return included, reason
        if reason not in RELAXABLE_CNDE_FILTER_REASONS:
            return False, reason
        return self._non_queue_filters(veh_id)

    def _non_queue_filters(self, veh_id: str) -> tuple:
        """The non-relaxable half of the parent filter, re-checked.

        Mirrors ``BaseEnvComplete._should_include_in_cnde_control`` filters 1
        (junction), 3 (first vehicle after a junction), 5 (lane mismatch
        cooldown) and 6 (TWLTL center lane); only filters 2 and 4 — the two
        queue conditions — are left out. Keep in sync with the parent.
        """
        TIME_TO_STOP_BAR_THRESHOLD = 3.0
        EDGE_END_MARGIN = 5.0

        try:
            veh_lane_id = traci.vehicle.getLaneID(veh_id)

            # Filter 1: already inside a junction.
            if veh_lane_id.startswith(":"):
                return False, "junction"

            veh_speed = traci.vehicle.getSpeed(veh_id)
            veh_lane_pos = traci.vehicle.getLanePosition(veh_id)

            # Filter 3: first vehicle after a junction — may still be yielding
            # to junction traffic; let SUMO clear it.
            leader_info = traci.vehicle.getLeader(veh_id)
            if leader_info is None or leader_info[0] == "":
                if veh_lane_pos < EDGE_END_MARGIN:
                    return False, "first_after_junction"
                if (
                    veh_speed > 0.1
                    and veh_lane_pos / veh_speed < TIME_TO_STOP_BAR_THRESHOLD
                ):
                    return False, "first_after_junction"

            # Filter 5: lane mismatch cooldown.
            if veh_id in self.vehicle_list:
                ctrl = self.vehicle_list[veh_id].controller
                if hasattr(ctrl, "cnde_cooldown_until"):
                    if traci.simulation.getTime() < ctrl.cnde_cooldown_until:
                        return False, "lane_mismatch_cooldown"

            # Filter 6: TWLTL center lane.
            twltl_manager = getattr(self, "_twltl_manager", None)
            if twltl_manager is not None:
                edge_id = traci.vehicle.getRoadID(veh_id)
                if edge_id in twltl_manager._twltl_edges:
                    center_idx = twltl_manager._twltl_edges[edge_id]
                    if int(veh_lane_id.split("_")[-1]) == center_idx:
                        return False, "twltl_center_lane"

            return True, ""

        except Exception as e:  # noqa: BLE001 — parity with the parent filter
            logger.debug(f"Error re-checking CNDE control inclusion for {veh_id}: {e}")
            return True, "error"
