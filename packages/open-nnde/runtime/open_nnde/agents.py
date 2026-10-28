"""Per-vehicle state history, with the members TeraSim's NNDE environment reads and writes.

Every row of an agent's states is [x, y, heading, vx, vy, ax, ay]: position relative to the
map's ``pos_offset``, heading in radians (east = 0, counter-clockwise). The last row is the
current step. NNDE overwrites the last rows with SUMO's own history before every prediction
(``agent._states_per_step.states[-n:] = ...``), so the rows are 0.1 s apart where it matters.
"""

from __future__ import annotations

import numpy as np

DEFAULT_STATIC = (3.8, 1.8, 1.5)  # length, width, height (m), as NNDE adds new vehicles
STATE_DIM = 7


class StatesPerStep:
    __slots__ = ("states",)

    def __init__(self, states: np.ndarray):
        self.states = states


class Agent:
    def __init__(self, agent_id, states, static, first_valid_step: int):
        self.id = agent_id
        self._states_per_step = StatesPerStep(
            np.array(states, dtype=np.float64).reshape(-1, STATE_DIM)
        )
        self.static = np.asarray(static if static is not None else DEFAULT_STATIC, dtype=np.float64)
        self.first_valid_step = first_valid_step

    @property
    def states(self) -> np.ndarray:
        return self._states_per_step.states

    def append(self, state, max_rows: int) -> None:
        rows = np.vstack(
            [self._states_per_step.states, np.asarray(state, dtype=np.float64)[:STATE_DIM][None, :]]
        )
        self._states_per_step.states = rows[-max_rows:]


class AgentRecord:
    """The tracked vehicles, keyed by SUMO vehicle id (insertion order kept)."""

    def __init__(self, history_step: int, max_rows: int | None = None):
        self.history_step = history_step
        self.max_rows = max_rows or 2 * history_step
        self.current_step = -1
        self._id_agent_mapping: dict = {}

    # -- read --------------------------------------------------------------------------------
    @property
    def current_track_agent_ids(self) -> list:
        return list(self._id_agent_mapping)

    @property
    def all_recorded_agent_ids(self) -> list:
        return list(self._id_agent_mapping)

    def get_agents(self, agent_id):
        return self._id_agent_mapping.get(agent_id)

    def get_valid_agent_ids(self, timestep: int = -1) -> list:
        """Every tracked agent has a row for the current step."""
        if timestep != -1:
            raise NotImplementedError("only the current step (-1) is supported")
        return list(self._id_agent_mapping)

    def __getitem__(self, key):
        """``record[-1, ids, :7]`` -> (ids, states (n, 7), valid mask (n,))."""
        timestep, ids, columns = key
        ids = list(ids)
        if not ids:
            return ids, np.zeros((0, STATE_DIM)), np.zeros(0, dtype=bool)
        states = np.stack([self._id_agent_mapping[i].states[timestep] for i in ids])[:, columns]
        return ids, states, np.ones(len(ids), dtype=bool)

    def complete_history_ids(self) -> list:
        return [i for i, a in self._id_agent_mapping.items() if len(a.states) >= self.history_step]

    # -- write -------------------------------------------------------------------------------
    def add_agents_from_history_states(self, vehicles_id, vehicles_states, vehicles_static=None):
        states = np.asarray(vehicles_states, dtype=np.float64).reshape(-1, STATE_DIM)
        self._id_agent_mapping[vehicles_id] = Agent(
            vehicles_id, states, vehicles_static, self.current_step - len(states) + 1
        )

    def reset_agent_history(self, agent_id, fresh_history) -> None:
        states = np.asarray(fresh_history, dtype=np.float64).reshape(-1, STATE_DIM)
        agent = self._id_agent_mapping.get(agent_id)
        if agent is None:
            self.add_agents_from_history_states(agent_id, states)
            return
        agent._states_per_step.states = states.copy()
        agent.first_valid_step = self.current_step - len(states) + 1

    def force_remove_agents(self, agent_ids) -> None:
        for agent_id in list(agent_ids):
            self._id_agent_mapping.pop(agent_id, None)

    def proceed(self, ids, states) -> None:
        """Advance one step: the given vehicles take the given state, the others keep theirs."""
        self.current_step += 1
        given = {i: s for i, s in zip(list(ids), np.asarray(states, dtype=np.float64))}
        for agent_id, agent in self._id_agent_mapping.items():
            agent.append(
                given.pop(agent_id) if agent_id in given else agent.states[-1], self.max_rows
            )
        for agent_id, state in given.items():
            self._id_agent_mapping[agent_id] = Agent(
                agent_id, state[None, :STATE_DIM], None, self.current_step
            )
