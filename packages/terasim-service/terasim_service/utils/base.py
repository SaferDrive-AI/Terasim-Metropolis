import importlib
import os
from loguru import logger
from omegaconf import OmegaConf
from pathlib import Path
from pydantic import BaseModel, Field
import redis
import sys
import yaml

from terasim.logger.infoextractor import InfoExtractor
from terasim.simulator import Simulator

from .messages import AgentCommand

from terasim_service.constants import (
    ENVIRONMENT_VARIABLE_DB,
    ENVIRONMENT_VARIABLE_HOST,
    ENVIRONMENT_VARIABLE_PASSWORD,
    ENVIRONMENT_VARIABLE_PORT,
)


class SimulationConfig(BaseModel):
    config_file: str = Field(
        ..., description="Path to the simulation configuration file"
    )
    auto_run: bool = Field(
        False,
        description="Whether to automatically run the simulation or wait for manual control",
    )


class SimulationStatus(BaseModel):
    id: str = Field(..., description="Unique identifier for the simulation")
    status: str = Field(..., description="Current status of the simulation")
    progress: float = Field(
        0.0, description="Progress of the simulation as a percentage"
    )


class SimulationCommand(BaseModel):
    command: str = Field(
        ...,
        description="Control command for the simulation (e.g., 'pause', 'resume', 'stop')",
    )


class AgentCommandBatch(BaseModel):
    commands: list[AgentCommand] = Field(
        ..., description="List of agent commands to execute"
    )


def load_config(config_file):
    """Load the configuration file.

    Args:
        config_file (str): Path to the configuration file.

    Returns:
        dict: The configuration dictionary.
    """
    with open(config_file, "r") as file:
        return yaml.safe_load(file)


def resolve_config_paths(config, config_file_path=None):
    """Resolve all relative paths in config to absolute paths based on path_resolution setting.
    
    Args:
        config (dict): The configuration dictionary.
        config_file_path (str): Path to the configuration file (for resolving relative paths).
    
    Returns:
        dict: The configuration with resolved absolute paths.
    """
    # Get path resolution mode from config
    path_resolution = config.get("path_resolution", "config_relative")
    
    def resolve_path(path_str):
        """Resolve a single path based on path_resolution configuration"""
        path = Path(path_str)
        if path.is_absolute():
            return str(path)
        else:
            if path_resolution == "cwd_relative":
                # Relative to current working directory
                return str(Path(path).resolve())
            else:  # path_resolution == "config_relative" or not set
                # Relative to config file location
                if config_file_path:
                    yaml_dir = Path(config_file_path).parent
                    return str((yaml_dir / path).resolve())
                else:
                    # Fallback to current directory if no config file path
                    return str(Path(path).resolve())
    
    # Resolve paths in the config
    if "input" in config:
        if "sumo_net_file" in config["input"]:
            config["input"]["sumo_net_file"] = resolve_path(config["input"]["sumo_net_file"])
        if "sumo_config_file" in config["input"]:
            config["input"]["sumo_config_file"] = resolve_path(config["input"]["sumo_config_file"])

    return config


def create_environment(config, base_dir):
    """Create the environment based on the configuration.

    Supports both NDE and CNDE modes. When a 'cnde' section is present in config
    and cnde.enabled is true, the NNDE simulator is built and passed to the
    environment constructor.

    Args:
        config (dict): The configuration dictionary.
        base_dir (str): Base directory for the environment.

    Returns:
        Environment: The environment object.
    """
    # Check for CNDE configuration and set env var BEFORE importing NADE modules.
    # This ensures the correct base class (NDE or NNDE) is selected in nade.py.
    cnde_enabled = False
    if "cnde" in config:
        cnde_enabled = config["cnde"].get("enabled", False)
    os.environ["TERASIM_CNDE_ENABLED"] = str(cnde_enabled).lower()
    logger.info(f"CNDE mode: {'enabled' if cnde_enabled else 'disabled'}")

    # Now import NADE modules (after environment variable is set)
    from terasim_nde_nade.vehicle import NDEVehicleFactory
    from terasim_nde_nade.vru import NDEVulnerableRoadUserFactory

    env_module = importlib.import_module(config["environment"]["module"])
    env_class = getattr(env_module, config["environment"]["class"])

    env_params = OmegaConf.create(config["environment"]["parameters"])

    kwargs = dict(
        vehicle_factory=NDEVehicleFactory(env_params),
        vru_factory=NDEVulnerableRoadUserFactory(env_params),
        info_extractor=InfoExtractor,
        log_flag=config["environment"]["parameters"]["log_flag"],
        log_dir=base_dir,
        warmup_time_lb=config["environment"]["parameters"]["warmup_time_lb"],
        warmup_time_ub=config["environment"]["parameters"]["warmup_time_ub"],
        run_time=config["environment"]["parameters"]["run_time"],
        configuration=env_params,
    )

    # Make av_cfg optional
    if OmegaConf.select(env_params, "AV_cfg") is not None:
        kwargs["av_cfg"] = env_params.AV_cfg

    # Pass the dynamic_traffic config: without it NADEWithAV defaults to
    # dynamic_traffic_cfg=None and self._dtm stays None, so DTM never spawns
    # any BVs in service/autoware mode (only background traffic from rou.xml
    # exists).
    dyn_cfg = OmegaConf.select(env_params, "dynamic_traffic")
    if dyn_cfg is not None:
        kwargs["dynamic_traffic_cfg"] = dyn_cfg

    # Build CNDE simulator if enabled
    if cnde_enabled:
        from csim.simulator.terasim_cnde_simulator import build_nnde_simulator

        cnde_cfg = config["cnde"]
        nnde_simulator = build_nnde_simulator(
            map_path=cnde_cfg["map_path"],
            desc=cnde_cfg.get("desc", "terasim-cnde"),
        )
        kwargs["nnde_simulator"] = nnde_simulator
        kwargs["map_id_mapping"] = cnde_cfg["map_id_mapping"]
        logger.info("CNDE simulator built and configured")
    else:
        logger.info("Running in NDE mode (no neural network control)")

    return env_class(**kwargs)


def create_simulator(config, base_dir):
    """Create the simulator based on the configuration.

    Args:
        config (dict): The configuration dictionary with resolved paths.
        base_dir (str): Base directory for the simulator.

    Returns:
        Simulator: The simulator object.
    """
    sim_params = config["simulator"]["parameters"]

    # Use additional_sumo_args from config if provided, otherwise default
    additional_sumo_args = sim_params.get("additional_sumo_args", None)
    if additional_sumo_args is not None:
        additional_sumo_args = list(additional_sumo_args)
    else:
        additional_sumo_args = ["--start", "--quit-on-end"]

    # Paths should already be resolved in config
    return Simulator(
        sumo_net_file_path=config["input"]["sumo_net_file"],
        sumo_config_file_path=config["input"]["sumo_config_file"],
        num_tries=sim_params["num_tries"],
        gui_flag=sim_params["gui_flag"],
        realtime_flag=sim_params.get("realtime_flag", False),
        output_path=base_dir,
        sumo_output_file_types=sim_params["sumo_output_file_types"],
        seed=sim_params.get("sumo_seed", None),
        additional_sumo_args=additional_sumo_args,
        traffic_scale=sim_params.get("traffic_scale", 1),
    )

def set_random_seed(seed):
    """Set the random seed for the simulation.
    """
    import random
    import numpy as np
    random.seed(seed)
    np.random.seed(seed)
    try:
        import torch
        torch.manual_seed(seed)
    except ImportError:
        pass
    logger.info(f"Setting random seed to {seed}")

def _int_env(name, default, *, minimum=None, maximum=None):
    """Read an integer env var, raising a clear error on a bad / out-of-range value."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {value}")
    return value


def redis_connection_kwargs_from_env():
    """Build ``redis.Redis(**kwargs)`` connection settings from the environment.

    Reads TERASIM_REDIS_HOST / PORT / DB / PASSWORD, defaulting to localhost:6379
    db 0 (so local dev and tests are unchanged). PORT (1–65535) and DB (>= 0)
    must be valid integers when set — a bad or out-of-range value raises
    ValueError rather than silently falling back. The password is included only
    when non-empty. This is the single Redis-config source used by
    check_redis_connection and intended for the city job stores.
    """
    kwargs = {
        "host": os.environ.get(ENVIRONMENT_VARIABLE_HOST, "localhost"),
        "port": _int_env(ENVIRONMENT_VARIABLE_PORT, 6379, minimum=1, maximum=65535),
        "db": _int_env(ENVIRONMENT_VARIABLE_DB, 0, minimum=0),
    }
    password = os.environ.get(ENVIRONMENT_VARIABLE_PASSWORD)
    if password:
        kwargs["password"] = password
    return kwargs


# Add this function to check Redis connection
def check_redis_connection():
    """Check the connection to Redis (connection settings from the environment)."""
    try:
        redis_client = redis.Redis(**redis_connection_kwargs_from_env())
        redis_client.ping()
        logger.info("Successfully connected to Redis")
    except redis.RedisError:
        logger.error("Failed to connect to Redis. Exiting...")
        sys.exit(1)