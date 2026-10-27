"""Structure + behaviour tests for the CNDE corridor env and the signal mixin.

Deliberately GPU-free and SUMO-free. The first three tests assert the class
graph (``SignalProgramMixin`` shared by both envs, the CNDE env sitting on the
NNDE chain) and that the CNDE context provider carries no AV anchor. The rest
drive the context provider and the command merge against a fake traci, which
is what the queue-filter relax switch (``TERASIM_CITY_CNDE_RELAX_QUEUE_FILTER``)
and the CNDE overlay actually depend on.
"""

import inspect

import pytest


def test_mixin_shared_by_both_envs():
    from terasim_city.envs.corridor_env import CorridorSignalEnv, SignalProgramMixin
    from terasim_city.envs.cnde_corridor_env import CndeCorridorSignalEnv
    assert issubclass(CorridorSignalEnv, SignalProgramMixin)
    assert issubclass(CndeCorridorSignalEnv, SignalProgramMixin)


def test_cnde_env_on_nnde_chain():
    from terasim_nde_nade.envs.nnde import NNDE
    from terasim_city.envs.cnde_corridor_env import CndeCorridorSignalEnv
    assert issubclass(CndeCorridorSignalEnv, NNDE)


def test_context_provider_has_no_av_anchor():
    from terasim_city.envs.cnde_corridor_env import CndeCorridorSignalEnv
    src = inspect.getsource(CndeCorridorSignalEnv.get_context_for_control)
    assert "AV_ID" not in src and "subscribeContext" not in src


# --------------------------------------------------------------------------
# Behaviour of the globalized context provider / relax switch / merge.
# --------------------------------------------------------------------------

#: veh_id -> (lane_id, lane_pos, speed, leader). Lanes are 100 m long.
_WORLD = {
    "free": ("e1_0", 40.0, 10.0, ("lead", 30.0)),  # included
    "injunc": (":j_0", 1.0, 8.0, ("lead", 30.0)),  # junction
    "nearstop": ("e1_0", 80.0, 10.0, ("lead", 30.0)),  # 2 s to stop bar
    "slow": ("e1_0", 40.0, 1.0, ("lead", 30.0)),  # queued
    "postjunc": ("e1_0", 3.0, 9.0, ("", -1)),  # first after junction
}


class _FakeTraci:
    """Just enough traci for the CNDE control filter."""

    world = dict(_WORLD)

    #: What getDepartedNumber()/getArrivedNumber() report. Non-equal, non-zero
    #: values so a test can tell the two counters apart and spot a double read.
    departed_per_step = 2
    arrived_per_step = 1

    class TraCIException(Exception):
        pass

    class vehicle:
        @staticmethod
        def getIDList():
            return tuple(_FakeTraci.world)

        @staticmethod
        def getLaneID(v):
            return _FakeTraci.world[v][0]

        @staticmethod
        def getLanePosition(v):
            return _FakeTraci.world[v][1]

        @staticmethod
        def getSpeed(v):
            return _FakeTraci.world[v][2]

        @staticmethod
        def getLeader(v, dist=100):
            return _FakeTraci.world[v][3]

        @staticmethod
        def getRoadID(v):
            return _FakeTraci.world[v][0].rsplit("_", 1)[0]

    class lane:
        @staticmethod
        def getLength(lane_id):
            return 100.0

    class simulation:
        @staticmethod
        def getTime():
            return 0.0

        @staticmethod
        def getDepartedNumber():
            return _FakeTraci.departed_per_step

        @staticmethod
        def getArrivedNumber():
            return _FakeTraci.arrived_per_step


class _NoVehicleFactory:
    def create_vehicle(self, veh_id, simulator):  # pragma: no cover - never called
        raise AssertionError("no vehicle is created in these tests")


class _FakeNndeSimulator:
    sim_resolution = 0.1
    history_step = 11


@pytest.fixture
def cnde_env(monkeypatch, tmp_path):
    """Factory for a constructed CndeCorridorSignalEnv wired to the fake traci."""
    import terasim.envs.base_complete as base_complete
    import terasim_city.envs.cnde_corridor_env as cnde_corridor_env
    from terasim_city.envs.cnde_corridor_env import CndeCorridorSignalEnv

    _FakeTraci.world = dict(_WORLD)
    monkeypatch.setattr(base_complete, "traci", _FakeTraci)
    monkeypatch.setattr(cnde_corridor_env, "traci", _FakeTraci)
    # NNDE opens its trajectory log relative to cwd.
    monkeypatch.chdir(tmp_path)

    def _build(*, relax: bool = False):
        if relax:
            monkeypatch.setenv("TERASIM_CITY_CNDE_RELAX_QUEUE_FILTER", "1")
        else:
            monkeypatch.delenv(
                "TERASIM_CITY_CNDE_RELAX_QUEUE_FILTER", raising=False
            )
        return CndeCorridorSignalEnv(
            programs={},
            adversities=[],
            sim_end=10.0,
            nnde_simulator=_FakeNndeSimulator(),
            map_id_mapping=None,
            vehicle_factory=_NoVehicleFactory(),
        )

    return _build


def test_context_is_global_and_keeps_junction_filter(cnde_env):
    env = cnde_env()
    ctx = env.get_context_for_control()

    all_ids = list(_FakeTraci.world)
    assert ctx["terasim_controlled_vehicle_ids"] == all_ids
    assert ctx["control_radius_vehicle_ids"] == all_ids
    assert ctx["cnde_control_ids"] == ["free"]
    assert env.control_ls is ctx


def test_relax_switch_drops_queue_filters_only(cnde_env):
    strict = cnde_env(relax=False).get_context_for_control()["cnde_control_ids"]
    relaxed = cnde_env(relax=True).get_context_for_control()["cnde_control_ids"]

    assert set(strict) == {"free"}
    # The two queue-shaped filters are gone...
    assert set(relaxed) == {"free", "nearstop", "slow"}
    # ...but everything junction-related still excludes, deadlock protection.
    assert "injunc" not in relaxed
    assert "postjunc" not in relaxed


def test_cnde_control_ids_recomputed_every_five_steps(cnde_env):
    env = cnde_env()
    first = env.get_context_for_control()["cnde_control_ids"]
    assert first == ["free"]

    # "free" drives into the junction — the filter must not notice until the
    # next inference cycle, so one CNDE trajectory window sees one id set.
    _FakeTraci.world["free"] = (":j_0", 1.0, 10.0, ("lead", 30.0))
    for _ in range(4):
        assert env.get_context_for_control()["cnde_control_ids"] == ["free"]
    assert env.get_context_for_control()["cnde_control_ids"] == []


def test_stalled_objects_never_reach_cnde_control(cnde_env):
    """Stalls are pinned every step by the adversity; CNDE must not touch them.

    The stall used here (mid-block, speed 0, has a leader) is excluded by the
    plain filter only through its "low_speed" condition — the very condition
    the relax switch removes. So the exclusion has to happen in the provider,
    not in the filter.
    """
    _FakeTraci.world["BV_stall"] = ("e1_0", 40.0, 0.0, ("lead", 30.0))

    for relax in (False, True):
        env = cnde_env(relax=relax)
        # What SignalProgramMixin._signal_mixin_on_start would have recorded.
        env._stalled_vehicle_ids.add("BV_stall")
        ctx = env.get_context_for_control()

        assert "BV_stall" not in ctx["terasim_controlled_vehicle_ids"]
        assert "BV_stall" not in ctx["control_radius_vehicle_ids"]
        assert "BV_stall" not in ctx["cnde_control_ids"]
        assert ctx["static_adversarial_object_id_list"] == ["BV_stall"]
        # Nothing else changed.
        assert "free" in ctx["cnde_control_ids"]

    # Sanity: without the stall bookkeeping the relaxed filter WOULD take it,
    # which is exactly the regression this test guards.
    env = cnde_env(relax=True)
    assert "BV_stall" in env.get_context_for_control()["cnde_control_ids"]


def test_vehicle_coloring_is_disabled(cnde_env):
    """The parent's per-step repaint is O(N^2) TraCI on a whole-network cache."""
    assert cnde_env().cnde_vehicle_coloring is False


def test_warmup_hook_is_a_noop(cnde_env, monkeypatch):
    """City trials start at t=0: no warmup step, no reload, no traci at all.

    Named without "sumo"/"simulation": ``tests/conftest.py:141-143`` auto-marks
    any such test ``requires_sumo`` and skips it when SUMO_HOME is unset, which
    would have silently disabled this (SUMO-free) test.
    """
    import terasim_nde_nade.envs.nde as nde_module
    from terasim_city.envs.cnde_corridor_env import CndeCorridorSignalEnv

    assert "sumo_warmup" in CndeCorridorSignalEnv.__dict__, "must override NDE's"

    env = cnde_env()

    class _ExplodingTraci:
        def __getattr__(self, name):
            raise AssertionError(f"sumo_warmup touched traci.{name}")

    monkeypatch.setattr(nde_module, "traci", _ExplodingTraci())
    assert env.sumo_warmup(900) is None


def test_on_step_runs_the_cnde_sequence_in_order(cnde_env):
    """Pin the choreography lifted from NADE_decision_and_control."""
    from terasim.params import AgentType

    env = cnde_env()
    calls = []

    class _RecordingVehicle:
        @property
        def observation(self):
            calls.append("sensor_refresh")
            return {}

    env.vehicle_list["v1"] = _RecordingVehicle()
    env.control_ls = {
        "terasim_controlled_vehicle_ids": ["v1"],
        "control_radius_vehicle_ids": ["v1"],
        "cnde_control_ids": ["v1"],
        "static_adversarial_object_id_list": [],
    }
    observation = {AgentType.VEHICLE: {"v1": {}}}
    base_commands = {AgentType.VEHICLE: {}, AgentType.VULNERABLE_ROAD_USER: {}}

    def _record(name, result=None):
        def _fn(*args, **kwargs):
            calls.append(name)
            return result
        return _fn

    env._signal_mixin_on_step = _record("_signal_mixin_on_step")
    env.cache_history_tls_data = _record("cache_history_tls_data")
    env.make_decisions = _record("make_decisions", (base_commands, "cmd_info"))
    env.get_env_observation = _record("get_env_observation", observation)
    env.executeMove = _record("executeMove", ("cmd_info", observation, True))
    env.nnde_make_decisions = _record("nnde_make_decisions", {})
    env._merge_cnde_control_commands = _record("merge")
    env.refresh_control_commands_state = _record("refresh_control_commands_state")
    env.execute_control_commands = _record("execute_control_commands")

    assert env.on_step({}) is True, "must return executeMove's continue flag"
    assert calls == [
        "_signal_mixin_on_step",
        "cache_history_tls_data",
        "make_decisions",
        "get_env_observation",
        "executeMove",
        "sensor_refresh",  # AFTER executeMove, else the history is half-step stale
        "nnde_make_decisions",
        "merge",
        "refresh_control_commands_state",
        "execute_control_commands",
    ]


def test_on_step_counts_departures_after_the_half_step(cnde_env):
    """The backlog counters must be read AFTER ``executeMove``, not before.

    ``SignalProgramMixin._signal_mixin_on_step`` accumulates
    ``getDepartedNumber()``/``getArrivedNumber()`` before the half step — right
    for ``CorridorSignalEnv``, but a structural 0 here: libsumo's
    ``executeMove()`` consumes the pending vehicle-state changes and the
    trailing ``simulationStep()`` clears them again. Measured on the corridor
    scenario over 60 s: a plain ``simulationStep`` loop counts 37 departed /
    15 arrived; an ``executeMove`` + ``simulationStep`` loop counts 0/0 read
    before ``executeMove`` and 37/15 read right after it. Without this
    post-half-step read, every CNDE trial's ``kpi.json`` backlog reported
    ``inserted_vehicles=0, arrived_vehicles=0`` while the SUMO-passive arm on
    the same scenario reported 152/130.
    """
    from terasim.params import AgentType

    env = cnde_env()
    env.control_ls = {
        "terasim_controlled_vehicle_ids": [],
        "control_radius_vehicle_ids": [],
        "cnde_control_ids": [],
        "static_adversarial_object_id_list": [],
    }
    observation = {AgentType.VEHICLE: {}}
    base_commands = {AgentType.VEHICLE: {}, AgentType.VULNERABLE_ROAD_USER: {}}
    # Stub out everything except the counter read; _signal_mixin_on_step in
    # particular, so the assertion below can only be satisfied by the
    # post-executeMove accumulation.
    env._signal_mixin_on_step = lambda *a, **k: None
    env.cache_history_tls_data = lambda *a, **k: None
    env.make_decisions = lambda *a, **k: (base_commands, "cmd_info")
    env.get_env_observation = lambda *a, **k: observation
    env.executeMove = lambda *a, **k: ("cmd_info", observation, True)
    env.refresh_control_commands_state = lambda *a, **k: None
    env.execute_control_commands = lambda *a, **k: None

    assert (env.cum_inserted, env.cum_arrived) == (0, 0)
    env.on_step({})
    env.on_step({})
    assert (env.cum_inserted, env.cum_arrived) == (
        2 * _FakeTraci.departed_per_step,
        2 * _FakeTraci.arrived_per_step,
    )


def test_merge_only_replaces_default_commands():
    import addict
    from terasim.params import AgentType
    from terasim_nde_nade.utils import CommandType, NDECommand

    from terasim_city.envs.cnde_corridor_env import CndeCorridorSignalEnv

    def _cnde_command():
        return NDECommand(
            command_type=CommandType.CNDE_TRAJECTORY,
            duration=0.5,
            future_trajectory=[[1, 1, 1, 1, 1]],
        )

    base = addict.Dict()
    base[AgentType.VEHICLE] = addict.Dict(
        {
            "v_default": NDECommand(command_type=CommandType.DEFAULT),
            "v_traj": NDECommand(
                command_type=CommandType.TRAJECTORY,
                duration=0.5,
                future_trajectory=[[0, 0, 0, 0, 0]],
            ),
            "v_passive": None,  # SUMO-passive vehicle: no NDE command at all
        }
    )
    CndeCorridorSignalEnv._merge_cnde_control_commands(
        base,
        {
            "v_default": _cnde_command(),
            "v_traj": _cnde_command(),
            "v_passive": _cnde_command(),
            "v_gone": _cnde_command(),  # left the network before the merge
        },
    )

    merged = base[AgentType.VEHICLE]
    assert merged["v_default"].command_type == CommandType.CNDE_TRAJECTORY
    assert merged["v_traj"].command_type == CommandType.TRAJECTORY
    assert merged["v_passive"] is None
    assert "v_gone" not in merged
