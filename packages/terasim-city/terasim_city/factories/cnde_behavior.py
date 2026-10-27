"""Neural NDE (``cnde``) behavior: a process-level model singleton and a fresh RouteTLSimulator per trial.

The RouteTLSimulator holds mutable cross-step state (``agent_record``) and is
never reused across trials, while the 972 MB model weights are always reused.
Under a spawn pool (``maxtasksperchild=1``) the singleton amounts to one load
per worker process, so GPU runs use inline evaluation or at most
``cnde_max_workers`` coarse-search workers (the orchestration layer validates
this; the default is 1, and the service raises it per request to
TERASIM_CITY_CNDE_MAX_WORKERS). ``missing_cnde_runtime`` lets the service check,
before it accepts a run, that this process has the CNDE runtime and its
checkpoint.

Module level stays torch/csim-free: both are imported lazily inside the
functions that actually need them, so importing this module (e.g. to read
``CndeVehicleFactory`` off the registry) never triggers a GPU-runtime import.

``TERASIM_CITY_CNDE_RELAX_QUEUE_FILTER`` (see
``terasim_city.envs.cnde_corridor_env.RELAX_QUEUE_FILTER_ENV_VAR``) is off by
default (the variable is unset). In a 3-seed A/B comparison (same
intersection and plan, the switch as the only variable) relaxing the
stop-line / low-speed queue filter made delay, stops, completion ratio,
collisions and hard braking all worse together, so the default keeps the
filter in place.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

__all__ = [
    "get_cnde_model",
    "build_trial_nnde_simulator",
    "missing_cnde_runtime",
    "CndeVehicleFactory",
    "CNDE_EGO_SENSOR_FIELD_NAMES",
    "FULL_OBSERVATION_ENV_VAR",
]

#: Set to "1" to give CNDE background vehicles the full NDE ego-sensor field set
#: (next links, foe lanes, ...) instead of :data:`CNDE_EGO_SENSOR_FIELD_NAMES`.
FULL_OBSERVATION_ENV_VAR = "TERASIM_CITY_CNDE_FULL_OBSERVATION"

#: TraCI getters the CNDE corridor pipeline actually reads from a vehicle's ``ego``
#: observation, per 0.1 s step for EVERY vehicle in the network: the kinematic state
#: that feeds the 11-step history synced into CNDE (``sync_terasim_history_to_cnde``),
#: ``get_cnde_vehicle_state`` and ``interpolate_control_command`` (position, heading,
#: velocity, acceleration) plus ``lane_id`` (``get_location``).  The default NDE set
#: additionally fetches next links and per-lane foe lanes, which only the adversity
#: models consume -- a corridor trial has none -- and which cost ~15 % of a CNDE
#: evaluation's wall time on a 1,000-vehicle network.
CNDE_EGO_SENSOR_FIELD_NAMES = ("velocity", "position", "heading", "lane_id", "acceleration")

#: Process-level MotionTransformer checkpoint singleton. Set once by
#: :func:`get_cnde_model`, shared by every trial this process ever evaluates.
_MODEL = None

#: Where ``csim.simulator.terasim_cnde_simulator.load_cnde_model`` reads the checkpoint,
#: relative to the directory holding ``csim`` (``packages/cnde``); not configurable there.
CHECKPOINT_RELATIVE_PATH = Path("assets") / "nn_weights.pth"
#: A checkpoint smaller than this is a placeholder (e.g. a git-lfs pointer), not the
#: ~972 MB weights.
_MIN_CHECKPOINT_BYTES = 1 << 20


def missing_cnde_runtime(*, find_spec=importlib.util.find_spec) -> list[str]:
    """What this process lacks to run the "cnde" behavior; empty when nothing is missing.

    For callers that must refuse a CNDE run up front rather than let every trial fail
    (the service checks before it reserves capacity). Imports nothing heavy: it only
    locates ``torch``, the CNDE model package ``cnde`` (``packages/cnde/network/cnde`` on
    the import path), the simulator package ``csim`` (``packages/cnde``) and the
    checkpoint csim loads from next to it. A visible GPU is not checked: without one a
    trial fails when the model moves to CUDA. ``find_spec`` is a test seam.
    """
    missing = []
    if find_spec("torch") is None:
        missing.append("torch is not installed")
    model = find_spec("cnde")
    if model is None or not any(
        (Path(location) / "models" / "model.py").is_file()
        for location in model.submodule_search_locations or ()
    ):
        missing.append(
            "the CNDE model package 'cnde' is not importable "
            "(the import path needs packages/cnde/network/cnde)"
        )
    simulator = find_spec("csim")
    # csim is a namespace package: an import resolves the first of its directories that
    # holds the simulator module, and load_cnde_model reads the checkpoint next to it.
    locations = simulator.submodule_search_locations if simulator is not None else None
    roots = [
        Path(location).parent
        for location in locations or ()
        if (Path(location) / "simulator" / "terasim_cnde_simulator.py").is_file()
    ]
    if not roots:
        missing.append(
            "the CNDE simulator package 'csim' is not importable "
            "(the import path needs packages/cnde)"
        )
        return missing
    checkpoint = roots[0] / CHECKPOINT_RELATIVE_PATH
    try:
        size = checkpoint.stat().st_size
    except OSError:
        missing.append(f"the CNDE checkpoint {checkpoint} is missing")
    else:
        if size < _MIN_CHECKPOINT_BYTES:
            missing.append(
                f"the CNDE checkpoint {checkpoint} is a {size}-byte placeholder, "
                "not the ~972 MB weights"
            )
    return missing


def get_cnde_model():
    """Load (once per process) and return the CNDE MotionTransformer checkpoint.

    The 972 MB weights are a process-level singleton: reloading per trial
    would dominate wall-clock time. The RouteTLSimulator built fresh per trial
    by :func:`build_trial_nnde_simulator` holds the mutable per-step state
    (``agent_record``) and must NEVER be reused across trials — only the model
    itself is shared. Under a spawn pool with ``maxtasksperchild=1`` this
    singleton degrades to "once per worker process" (one trial per process
    anyway), so it only pays off for inline evaluation or a pool that reuses
    workers across trials.
    """
    global _MODEL
    if _MODEL is None:
        import torch  # noqa: F401 -- preloads libc10 before csim's extension import

        from csim.simulator.terasim_cnde_simulator import load_cnde_model

        _MODEL = load_cnde_model()
    return _MODEL


def build_trial_nnde_simulator(map_dir, desc: str = "terasim-cnde"):
    """Build a fresh per-trial NNDE simulator against the process-level model.

    Args:
        map_dir: MapStore package directory (contains ``net.net.xml``;
            ``cnde_map.pkl`` / ``cnde_id_mapping.pkl`` are generated there on
            first use and cached — see
            :func:`terasim_city.maps.cnde_artifacts.ensure_cnde_artifacts`).
        desc: CNDE simulator description/tag, matching the Neural NDE
            runtime's ``cnde.desc`` config default ("terasim-cnde").

    Returns:
        ``(nnde_simulator, id_mapping_pkl)``: the fresh simulator and the
        PATH to the artifacts' id-mapping pickle. NNDE loads this itself from
        disk (``terasim_nde_nade.envs.nnde.NNDE.__init__`` does
        ``pickle.load(open(map_id_mapping, "rb"))`` when given a path — see
        nnde.py:141-144), so this is deliberately a path, not a loaded dict,
        matching how the service's Neural NDE configuration passes
        ``cnde_cfg["map_id_mapping"]`` straight from YAML
        (``terasim_service.utils.base``).
    """
    from csim.simulator.terasim_cnde_simulator import build_nnde_simulator

    from terasim_city.maps.cnde_artifacts import ensure_cnde_artifacts

    artifacts = ensure_cnde_artifacts(map_dir)
    sim = build_nnde_simulator(str(artifacts.map_pkl), desc, model=get_cnde_model())
    return sim, artifacts.id_mapping_pkl


class CndeVehicleFactory:
    """CNDE background-vehicle factory: ``NDEVehicleFactory`` with a minimal cfg.

    Signature-compatible with
    :class:`terasim.vehicle.factories.vehicle_factory.VehicleFactory`
    (``create_vehicle(veh_id, simulator)``); deliberately does NOT subclass it
    at module level to keep this module free of any ``terasim`` import (the
    registry only ever calls ``create_vehicle`` duck-typed, same as every
    other registered factory).

    Delegates entirely to ``NDEVehicleFactory``, which already builds exactly
    what a CNDE-controlled vehicle needs:

    * ``NDEEgoSensor(cache=True, cache_history=True, cache_history_duration=1)``
      so the last second of history is available to
      ``sync_terasim_history_to_cnde``.
    * ``ConflictGenerationModel`` for a DEFAULT decision every step — CNDE's
      merge only ever overrides a DEFAULT command (see
      ``CndeCorridorSignalEnv._merge_cnde_control_commands``), so a vehicle's
      base decision must stay DEFAULT for CNDE to actually drive it.
    * ``NDEController``, which already handles
      ``CommandType.CNDE_TRAJECTORY`` natively.

    ``cfg=SimpleNamespace(adversity_cfg=None)``:
    ``ConflictGenerationModel.__init__`` evaluates ``cfg.adversity_cfg.vehicle
    if hasattr(cfg.adversity_cfg, "vehicle") else None`` — ``cfg`` itself
    cannot be ``None`` (that attribute access would raise ``AttributeError``
    before ``hasattr`` ever runs), but ``cfg.adversity_cfg`` can be, which
    routes to ``AdversityManager(None)`` -> no adversities -> every vehicle
    gets the DEFAULT command every step. A corridor trial has no NDE-style
    per-vehicle adversity config of its own; the only adversarial behavior in
    a city scenario is ``StalledObjectAdversity``, injected by the env, not by
    this factory.
    """

    def __init__(self) -> None:
        import os

        self._cfg = SimpleNamespace(adversity_cfg=None)
        self._full_observation = os.environ.get(FULL_OBSERVATION_ENV_VAR, "").strip() == "1"
        self._factory = None

    def _ego_sensor_fields(self):
        """Slim getter mapping (see :data:`CNDE_EGO_SENSOR_FIELD_NAMES`), or None for the full set."""
        if self._full_observation:
            return None
        from terasim_nde_nade.vehicle.nde_ego_sensor import NDEEgoSensor

        defaults = NDEEgoSensor.DEFAULT_PARAMS["fields"]
        return {name: defaults[name] for name in CNDE_EGO_SENSOR_FIELD_NAMES}

    def create_vehicle(self, veh_id, simulator):
        from terasim_nde_nade.vehicle.nde_vehicle_factory import NDEVehicleFactory

        if self._factory is None:
            self._factory = NDEVehicleFactory(
                cfg=self._cfg, ego_sensor_fields=self._ego_sensor_fields()
            )
        return self._factory.create_vehicle(veh_id, simulator)
