"""A vehicle released from CNDE control must not be left at the CNDE speed mode.

``NDE.executeMove`` force-releases every vehicle still executing a CNDE trajectory that the
control filter has dropped (typically ~3 s before a stop line), then gives the trajectory
vehicles within 20 m of their lane end speed mode 56 (right of way + red light) for the move
and restores the CNDE mode 32 afterwards. The released vehicles must take no part in that:
SUMO drives them from here on, and mode 32 left on a SUMO-driven vehicle means no red-light
braking, no right of way and no speed limit (SUMO 1.23) for the rest of its approach.

GPU-free and SUMO-free: a fake traci records the speed modes in force when the simulation
moves and afterwards.
"""

from types import SimpleNamespace

import pytest

from terasim.params import AgentType
from terasim_nde_nade.utils import CommandType

_LANE_LENGTH = 100.0

#: veh_id -> (lane_id, lane_pos); every lane is 100 m long, so 90 m is 10 m before its end.
_WORLD = {
    "kept": ("e1_0", 90.0),       # still a CNDE control candidate, 10 m before the stop line
    "released": ("e2_0", 90.0),   # dropped by the filter, 10 m before the stop line
    "far": ("e3_0", 10.0),        # still a candidate, 90 m before its lane end
}


class _FakeTraci:
    speed_mode = {}
    modes_during_move = None

    class vehicle:
        @staticmethod
        def getIDList():
            return tuple(_WORLD)

        @staticmethod
        def getRoadID(v):
            return _WORLD[v][0].rsplit("_", 1)[0]

        @staticmethod
        def getLaneID(v):
            return _WORLD[v][0]

        @staticmethod
        def getLanePosition(v):
            return _WORLD[v][1]

        @staticmethod
        def setSpeedMode(v, mode):
            _FakeTraci.speed_mode[v] = mode

        @staticmethod
        def setSpeed(v, speed):  # pragma: no cover - only for stalled objects
            pass

    class lane:
        @staticmethod
        def getLength(lane_id):
            return _LANE_LENGTH

    class simulation:
        @staticmethod
        def getTime():
            return 12.3

        @staticmethod
        def executeMove():
            _FakeTraci.modes_during_move = dict(_FakeTraci.speed_mode)

    class person:
        @staticmethod
        def getIDList():
            return ()


class _Controller:
    """The two NDEController attributes executeMove reads, and all_checks_on."""

    def __init__(self, veh_id):
        self.veh_id = veh_id
        self.is_busy = True
        self.cached_control_command = SimpleNamespace(
            timestep=12.0,
            cached_command=SimpleNamespace(command_type=CommandType.CNDE_TRAJECTORY),
        )

    def all_checks_on(self, veh_id):
        _FakeTraci.vehicle.setSpeedMode(veh_id, 31)


class _Vehicle:
    def __init__(self, veh_id):
        self.id = veh_id
        self.controller = _Controller(veh_id)


class _NoVehicleFactory:
    def create_vehicle(self, veh_id, simulator):  # pragma: no cover - never called
        raise AssertionError("no vehicle is created in these tests")


class _FakeNndeSimulator:
    sim_resolution = 0.1
    history_step = 11


@pytest.fixture
def env(monkeypatch, tmp_path):
    import terasim.envs.base_complete as base_complete
    import terasim_city.envs.cnde_corridor_env as cnde_corridor_env
    import terasim_city.envs.corridor_env as corridor_env
    import terasim_nde_nade.envs.nde as nde_module
    from terasim_city.envs.cnde_corridor_env import CndeCorridorSignalEnv

    _FakeTraci.speed_mode = {vid: 32 for vid in _WORLD}  # all three are executing trajectories
    _FakeTraci.modes_during_move = None
    for module in (base_complete, cnde_corridor_env, corridor_env, nde_module):
        monkeypatch.setattr(module, "traci", _FakeTraci)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TERASIM_CITY_CNDE_DIAG_LOGS", raising=False)
    env = CndeCorridorSignalEnv(
        programs={},
        adversities=[],
        sim_end=10.0,
        nnde_simulator=_FakeNndeSimulator(),
        map_id_mapping=None,
        vehicle_factory=_NoVehicleFactory(),
    )
    env._maintain_all_vehicles = lambda ctx: None
    env._maintain_all_vulnerable_road_users = lambda ctx: None
    for vid in _WORLD:
        env.vehicle_list[vid] = _Vehicle(vid)
    env.control_ls = {"cnde_control_ids": ["kept", "far"]}
    return env


def _move(env):
    commands = {AgentType.VEHICLE: {}, AgentType.VULNERABLE_ROAD_USER: {}}
    observation = {AgentType.VEHICLE: {}, AgentType.VULNERABLE_ROAD_USER: {},
                   AgentType.TRAFFIC_LIGHT: {}}
    return env.executeMove({}, commands, observation)


def test_released_vehicle_keeps_sumo_speed_mode(env):
    _move(env)
    released = env.vehicle_list["released"].controller
    assert released.is_busy is False and released.cached_control_command is None
    # SUMO moved it with the default mode, and it stays there afterwards.
    assert _FakeTraci.modes_during_move["released"] == 31
    assert _FakeTraci.speed_mode["released"] == 31


def test_trajectory_vehicle_near_the_stop_line_still_gets_right_of_way(env):
    _move(env)
    # Unchanged behaviour for the vehicle CNDE still drives: right of way + red light for the
    # move (56), back to the CNDE mode afterwards.
    assert env.vehicle_list["kept"].controller.is_busy is True
    assert _FakeTraci.modes_during_move["kept"] == 56
    assert _FakeTraci.speed_mode["kept"] == 32
    # Far from its lane end: never touched.
    assert _FakeTraci.modes_during_move["far"] == 32
    assert _FakeTraci.speed_mode["far"] == 32


def test_restore_gives_sumo_mode_to_a_vehicle_no_longer_executing(env):
    """Belt and braces: whatever ends a trajectory during the move, no 32 is put back."""
    real_move = _FakeTraci.simulation.executeMove

    def move_and_finish():
        real_move()
        env.vehicle_list["kept"].controller.is_busy = False

    _FakeTraci.simulation.executeMove = staticmethod(move_and_finish)
    try:
        _move(env)
    finally:
        _FakeTraci.simulation.executeMove = staticmethod(real_move)
    assert _FakeTraci.modes_during_move["kept"] == 56
    assert _FakeTraci.speed_mode["kept"] == 31


def test_executing_trajectory(env):
    assert env._executing_trajectory("kept") is True
    env.vehicle_list["kept"].controller.is_busy = False
    assert env._executing_trajectory("kept") is False
    assert env._executing_trajectory("not-a-vehicle") is False


# --------------------------------------------------------------------------
# The audit's remediation runs whether or not the diagnostic logs are written.
# --------------------------------------------------------------------------


class _RemediationTraci(_FakeTraci):
    """Adds what the remediation reads: road (junction / route), route index, removal."""

    roads = {}
    routes = {}
    route_index = {}
    removed = []

    class vehicle(_FakeTraci.vehicle):
        @staticmethod
        def getRoadID(v):
            return _RemediationTraci.roads.get(v, _WORLD[v][0].rsplit("_", 1)[0])

        @staticmethod
        def getSpeedMode(v):
            return _FakeTraci.speed_mode[v]

        @staticmethod
        def getRoute(v):
            return _RemediationTraci.routes[v]

        @staticmethod
        def getRouteIndex(v):
            return _RemediationTraci.route_index[v]

        @staticmethod
        def remove(v):
            _RemediationTraci.removed.append(v)

        # What the audit record reads besides (diag logs on).
        @staticmethod
        def getPosition(v):
            return (0.0, 0.0)

        @staticmethod
        def getSpeed(v):
            return 5.0

        @staticmethod
        def getAngle(v):
            return 0.0

        @staticmethod
        def getAcceleration(v):
            return 0.0

        @staticmethod
        def getLaneChangeMode(v):
            return 0

        @staticmethod
        def getLeader(v, dist=100):
            return None


@pytest.mark.parametrize("diag_logs", [False, True])
def test_remediation_does_not_depend_on_the_diag_logs(env, monkeypatch, diag_logs):
    import terasim_nde_nade.envs.nde as nde_module

    monkeypatch.setattr(nde_module, "traci", _RemediationTraci)
    env.cnde_diag_logs = diag_logs
    _RemediationTraci.roads = {"kept": ":j_0"}             # inside a junction at mode 32
    _RemediationTraci.routes = {"kept": ("e1", "x"), "far": ("e9", "x"), "released": ("e2", "x")}
    _RemediationTraci.route_index = {"kept": 0, "far": 0, "released": 0}
    _RemediationTraci.removed = []
    _move(env)
    # "kept" sat in a junction at the CNDE mode: back to 31 before the move.
    assert _FakeTraci.modes_during_move["kept"] == 31
    # "far" drives on an edge outside its route: removed, with or without the audit log.
    assert _RemediationTraci.removed == ["far"]
