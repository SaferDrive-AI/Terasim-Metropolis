"""Vehicle factory that hands all driving back to SUMO.

Why this instead of HighEfficiencyController + IDMModel?

- ``HighEfficiencyController`` hard-codes ``speedMode=0`` (disables every
  SUMO safety check: safe-speed, accel/decel limits, right-of-way,
  red-light compliance). It was designed for NADE-style adversarial
  scenarios where vehicles must be able to break traffic rules.
- For an empirical pipeline whose goal is to reproduce real-world flow,
  we want SUMO's safety net intact. ``SUMOModel`` returns ``(None, None)``
  from ``derive_control_command_from_observation``, which makes
  ``Agent.apply_control`` short-circuit via ``_is_command_legal`` —
  ``SUMOMOVEController.execute_control_command`` is therefore never
  invoked and ``setSpeedMode`` is never called. The vehicle keeps SUMO's
  default behaviour end-to-end (default ``speedMode``, default
  ``carFollowModel`` from its vType, default lane-change model).
"""

from terasim.vehicle.controllers.sumo_move_controller import SUMOMOVEController
from terasim.vehicle.decision_models.sumo_model import SUMOModel
from terasim.vehicle.factories.vehicle_factory import VehicleFactory
from terasim.vehicle.vehicle import Vehicle


class PassiveVehicleFactory(VehicleFactory):
    """Background-vehicle factory that leaves driving entirely to SUMO.

    Use when you want TeraSim's orchestration layer (env hooks, adversity
    injection, KPI plumbing) but SUMO's built-in driver behaviour for
    every background vehicle.
    """

    def create_vehicle(self, veh_id, simulator):
        return Vehicle(
            veh_id,
            simulator,
            sensors=[],
            decision_model=SUMOModel(),
            controller=SUMOMOVEController(simulator),
        )
