"""Control commands are applied to every commanded agent, in agent-list order.

``EnvTemplateComplete.execute_control_commands`` iterates the
insertion-ordered agent lists, not ``set(commands) & set(vehicle_list)``:
string agent ids hash differently in every Python process, so a set-based
order would make the order of the issued libsumo commands -- and with it the
whole run -- depend on PYTHONHASHSEED. ``AgentList`` iterates VALUES, so the
loop goes over ``.keys()``; the tests check both the order and that every
command actually reaches its agent.
"""

from __future__ import annotations

from terasim.envs.template_complete import EnvTemplateComplete
from terasim.params import AgentType
from terasim.vehicle.vehicle import VehicleList
from terasim.vulnerable_road_user.vulnerable_road_user import VulnerableRoadUserList


class _Agent:
    def __init__(self, agent_id: str, log: list):
        self.id = agent_id
        self._log = log

    def apply_control(self, command):
        self._log.append((self.id, command))


class _ConcreteEnv(EnvTemplateComplete):
    """The abstract hooks are irrelevant here; only execute_control_commands runs."""


_ConcreteEnv.__abstractmethods__ = frozenset()


def _env(vehicle_ids, vru_ids, log):
    env = _ConcreteEnv.__new__(_ConcreteEnv)
    env.vehicle_list = VehicleList({vid: _Agent(vid, log) for vid in vehicle_ids})
    env.vulnerable_road_user_list = VulnerableRoadUserList({pid: _Agent(pid, log) for pid in vru_ids})
    return env


def test_every_commanded_agent_gets_its_command_in_list_order():
    log: list = []
    vehicle_ids = ["flow_2.31", "flow_6.9", "flow_8.60", "flow_2.99"]
    env = _env(vehicle_ids, ["ped_1", "ped_2"], log)
    commands = {
        AgentType.VEHICLE: {vid: f"cmd-{vid}" for vid in reversed(vehicle_ids)},  # dict order != list order
        AgentType.VULNERABLE_ROAD_USER: {"ped_2": "cmd-ped_2", "ped_1": "cmd-ped_1"},
    }
    commands[AgentType.VEHICLE]["not_in_env"] = "ignored"  # commanded but absent -> skipped

    env.execute_control_commands(commands)

    assert log == [(vid, f"cmd-{vid}") for vid in vehicle_ids] + [("ped_1", "cmd-ped_1"), ("ped_2", "cmd-ped_2")]


def test_agents_without_a_command_are_left_alone():
    log: list = []
    env = _env(["a", "b", "c"], [], log)

    env.execute_control_commands({AgentType.VEHICLE: {"b": "only-b"}, AgentType.VULNERABLE_ROAD_USER: {}})

    assert log == [("b", "only-b")]
