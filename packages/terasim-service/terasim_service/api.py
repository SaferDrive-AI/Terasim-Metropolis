import asyncio
import inspect
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from multiprocessing import Process
from pathlib import Path
from typing import Annotated

import redis
from fastapi import Body, Depends, FastAPI, HTTPException
from loguru import logger
from pydantic import Field

from terasim_service.plugins import (
    DEFAULT_COSIM_PLUGIN_CONFIG, 
    TeraSimCoSimPlugin
)
from terasim_service.utils import (
    check_redis_connection,
    create_environment,
    create_simulator,
    load_config,
    resolve_config_paths,
    SimulationConfig,
    SimulationCommand,
    SimulationStatus,
    AgentCommand,
    AgentCommandBatch,
    set_random_seed,
)


def get_map_metadata(config, simulation_id):
    """Get the metadata for the simulation. Store it in redis.
    """
    # Paths should already be resolved in config
    map_path = Path(config["input"]["sumo_net_file"])
    # the metadata.json is in the same directory as the sumo net file
    metadata_path = map_path.parent / "metadata.json"
    with open(metadata_path, "r") as file:
        metadata = json.load(file)
        av_route = metadata["av_route_sumo"]
        try:
            redis_client = redis.Redis()
            redis_client.set(f"simulation:{simulation_id}:map_metadata", json.dumps(metadata))
            redis_client.set(f"simulation:{simulation_id}:av_route", json.dumps(av_route))
        except Exception as e:
            logger.exception(f"Failed to set metadata: {e}")
    return True


def get_simulation_id(simulation_id: str):
    if simulation_id not in running_simulations:
        raise HTTPException(status_code=404, detail="Simulation not found")
    return simulation_id


def check_simulation_running(simulation_id: str, redis_client: redis.Redis) -> bool:
    """Check if a simulation is still running in Redis

    Returns:
        bool: True if simulation is running, False if stopped or doesn't exist
    """
    status = redis_client.get(f"simulation:{simulation_id}:status")
    return status is not None


def run_simulation_process(simulation_id: str, config: dict, auto_run: bool, enable_viz: bool = False, viz_port: int = 8050, viz_update_freq: int = 5):
    # This function will run in a separate process
    asyncio.run(run_simulation_task(simulation_id, config, auto_run, enable_viz, viz_port, viz_update_freq))


description = """
TeraSim Metropolis service API. The `/v1/city` endpoints take traffic snapshots, maps and
the city's current signal plans, run simulation-based signal timing optimization and return
NEMA timing plans with their KPIs. The co-simulation endpoints start and control individual
TeraSim simulations.
"""


tags_metadata = [
    {
        "name": "simulations",
        "description": "Operations with simulations. You can start, stop, and control simulations.",
    },
]


# Startup/shutdown hooks registered AFTER ``app`` exists (the opt-in city
# scheduler adds its own from create_app()). FastAPI >= 0.141 / Starlette 1.x
# removed ``@app.on_event`` and ``app.add_event_handler``, so the lifespan below
# is the single place that runs them: startup hooks in registration order once
# the server starts, shutdown hooks in reverse order when it stops.
_startup_hooks: list = []
_shutdown_hooks: list = []


def add_startup_hook(hook) -> None:
    """Run ``hook`` (sync or async, no arguments) when the ASGI app starts."""
    _startup_hooks.append(hook)


def add_shutdown_hook(hook) -> None:
    """Run ``hook`` (sync or async, no arguments) when the ASGI app stops."""
    _shutdown_hooks.append(hook)


async def _run_hook(hook) -> None:
    result = hook()
    if inspect.isawaitable(result):
        await result


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await startup_event()
    for hook in list(_startup_hooks):
        await _run_hook(hook)
    try:
        yield
    finally:
        for hook in reversed(list(_shutdown_hooks)):
            try:
                await _run_hook(hook)
            except Exception:  # noqa: BLE001 - one failing hook must not skip the rest
                logger.exception("shutdown hook failed")
        await shutdown_event()


app = FastAPI(
    title="TeraSim Metropolis service",
    description=description,
    version="0.1.0",
    openapi_tags=tags_metadata,
    lifespan=_lifespan,
)


# Use a thread pool to limit the number of concurrent simulations
executor = ThreadPoolExecutor(
    max_workers=1
)  # Adjust max_workers based on your system's capacity


# Store running simulation tasks with process references
running_simulations = {}  # {simulation_id: {"process": process, "start_time": time, "status": status}}


@app.get(
    "/simulation_result/{simulation_id}",
    tags=["simulations"],
    summary="Get detailed simulation result data",
    responses={
        200: {"description": "Simulation result retrieved successfully"},
        404: {"description": "Simulation not found"},
    },
)
async def get_simulation_result(
    simulation_id: Annotated[str, Field(description="UUID of simulation to get results from")] = Depends(get_simulation_id)
):
    """
    Retrieve detailed result data from Redis for a completed simulation.
    
    Accesses simulation results stored in Redis during execution.
    Provides structured access to key simulation outcomes and metrics.
    
    Args:
        simulation_id: UUID of the target simulation
        
    Returns:
        dict: Structured simulation result data from Redis storage
        
    Note:
        This endpoint retrieves results cached in Redis during simulation execution.
        For comprehensive file-based results, use get_simulation_results instead.
    """
    try:
        redis_client = redis.Redis()
        simulation_result = redis_client.get(f"simulation:{simulation_id}:result")
        if not simulation_result:
            raise HTTPException(status_code=404, detail="Simulation result not found")
        return json.loads(simulation_result.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/av_route/{simulation_id}",
    tags=["simulations"],
    summary="Get autonomous vehicle route coordinates"
)
async def get_av_route(
    simulation_id: Annotated[str, Field(description="Simulation UUID to get AV route from")]
):
    """
    Retrieve the autonomous vehicle's planned route in geographic coordinates.
    
    Returns the complete AV route as latitude/longitude coordinate pairs.
    Essential for understanding the AV's intended path and analyzing
    scenario interactions along the route.
    
    Args:
        simulation_id: UUID of simulation containing the AV route
        
    Returns:
        list: Array of [latitude, longitude] coordinate pairs defining the AV route
        
    Example:
        Returns route like: [[40.7128, -74.0060], [40.7140, -74.0070], ...]
    """
    try:
        redis_client = redis.Redis()
        av_route = redis_client.get(f"simulation:{simulation_id}:av_route")
        if not av_route:
            raise HTTPException(status_code=404, detail="AV route not found")
        
        return json.loads(av_route.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def run_simulation_task(simulation_id: str, config: dict, auto_run: bool, enable_viz: bool = False, viz_port: int = 8050, viz_update_freq: int = 5):
    try:
        base_dir = (
            Path(config["output"]["dir"])
            / config["output"]["name"]
            / "raw_data"
            / config["output"]["nth"]
            /simulation_id
        )
        base_dir.mkdir(parents=True, exist_ok=True)

        # set random seed
        set_random_seed(config["seed"])
        
        env = create_environment(config, base_dir)
        sim = create_simulator(config, base_dir)
        try:
            get_map_metadata(config, simulation_id) # get the map metadata and store it in redis
        except Exception as e:
            logger.info(f"Failed to get map metadata: {e}")
        sim.bind_env(env)

        # Create and inject TeraSimControlPlugin with visualization support
        terasim_cosim_plugin_config = DEFAULT_COSIM_PLUGIN_CONFIG
        terasim_cosim_plugin_config["centered_agent_ID"] = "AV"
        terasim_cosim_plugin = TeraSimCoSimPlugin(
            simulation_uuid=simulation_id, 
            plugin_config=terasim_cosim_plugin_config,
            base_dir=str(base_dir),
            auto_run=auto_run,
            enable_viz=enable_viz,
            viz_port=viz_port,
            viz_update_freq=viz_update_freq
        )
        terasim_cosim_plugin.inject(sim, {})
        

        # Run the simulation
        await asyncio.get_event_loop().run_in_executor(executor, sim.run)

        if simulation_id in running_simulations:
            running_simulations[simulation_id]["status"] = "completed"
    except Exception as e:
        logger.exception(f"Simulation {simulation_id} failed: {str(e)}")
        if simulation_id in running_simulations:
            running_simulations[simulation_id]["status"] = "failed"
    finally:
        # Remove the simulation from running_simulations
        if simulation_id in running_simulations:
            del running_simulations[simulation_id]

        # Force garbage collection
        import gc

        gc.collect()


@app.post("/start_simulation", tags=["simulations"], summary="Start a new TeraSim simulation")
async def start_simulation(
    config: Annotated[SimulationConfig, Field(description="TeraSim simulation configuration including config file path and auto-run setting")],
    enable_viz: bool = False,
    viz_port: int = 8050,
    viz_update_freq: int = 5
):
    """
    Start a new TeraSim simulation with advanced scenario generation capabilities.
    
    This endpoint initializes a complete TeraSim simulation instance with support for:
    - Multi-agent autonomous vehicle scenarios
    - Real-time simulation control and monitoring
    - Corner case and adversarial scenario testing
    - Redis-based state management for distributed control
    
    Args:
        config: SimulationConfig containing the YAML configuration file path and execution settings
        
    Returns:
        dict: Response containing unique simulation_id for tracking and control
        
    Example:
        Start a highway merge scenario:
        {
            "config_file": "/path/to/highway_merge.yaml",
            "auto_run": true
        }
    """
    config_data = load_config(config.config_file)
    config_data = resolve_config_paths(config_data, config.config_file)
    simulation_id = str(uuid.uuid4())

    # Start the simulation in a new process
    process = Process(
        target=run_simulation_process,
        args=(simulation_id, config_data, config.auto_run, enable_viz, viz_port, viz_update_freq),
    )
    process.start()

    # Store process reference and metadata
    running_simulations[simulation_id] = {
        "process": process,
        "start_time": time.time(),
        "status": "started"
    }

    response = {"simulation_id": simulation_id, "message": "Simulation started"}
    
    if enable_viz:
        response["visualization_url"] = f"http://localhost:{viz_port}"
        response["message"] += " with visualization"

    return response


@app.get(
    "/simulation_status/{simulation_id}",
    tags=["simulations"],
    summary="Get real-time simulation status",
    responses={
        200: {"description": "Successful response", "model": SimulationStatus},
        404: {"description": "Simulation not found"},
    },
)
async def get_simulation_status(
    simulation_id: Annotated[str, Field(description="Unique simulation identifier returned from start_simulation")] = Depends(get_simulation_id)
):
    """
    Retrieve real-time status information for an active TeraSim simulation.
    
    Provides comprehensive status monitoring including execution state, progress tracking,
    and health information. Uses Redis backend for distributed state management.
    
    Args:
        simulation_id: UUID string identifier for the target simulation instance
        
    Returns:
        SimulationStatus: Detailed status object with current execution state
        
    Status values:
        - "started": Simulation initialized but not yet running
        - "running": Active execution in progress
        - "paused": Temporarily suspended, can be resumed
        - "completed": Successfully finished execution
        - "failed": Encountered error during execution
    """
    try:
        redis_client = redis.Redis()
        if not check_simulation_running(simulation_id, redis_client):
            raise HTTPException(
                status_code=404, detail="Simulation not found or finished"
            )
        redis_status = redis_client.get(f"simulation:{simulation_id}:status")
        if redis_status:
            running_simulations[simulation_id]["status"] = redis_status.decode("utf-8")
        
        # Return a SimulationStatus compatible response
        return {
            "id": simulation_id,
            "status": running_simulations[simulation_id]["status"],
            "progress": 0.0
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/simulation_control/{simulation_id}",
    tags=["simulations"],
    summary="Control simulation execution",
    responses={
        200: {"description": "Command sent successfully"},
        404: {"description": "Simulation not found"},
        500: {"description": "Failed to send command"},
    },
)
async def control_simulation(
    command: Annotated[SimulationCommand, Field(description="Control command: pause, resume, or stop")],
    simulation_id: Annotated[str, Field(description="Target simulation UUID")] = Depends(get_simulation_id),
):
    """
    Send real-time control commands to manage simulation execution.
    
    Provides precise control over simulation lifecycle with immediate effect.
    Commands are processed through Redis messaging for distributed control.
    
    Args:
        simulation_id: UUID of the target simulation instance
        command: Control operation to execute
        
    Available commands:
        - pause: Temporarily suspend simulation execution
        - resume: Continue paused simulation from current state
        - stop: Terminate simulation and clean up resources
        
    Returns:
        dict: Confirmation message with command execution status
        
    Example:
        Pause a running simulation:
        {
            "command": "pause"
        }
    """
    try:
        redis_client = redis.Redis()
        if not check_simulation_running(simulation_id, redis_client):
            raise HTTPException(
                status_code=404, detail="Simulation not found or finished"
            )

        if command.command == "resume":
            redis_client.delete(f"simulation:{simulation_id}:paused")
        elif command.command == "pause":
            redis_client.set(f"simulation:{simulation_id}:paused", "1")
        elif command.command == "stop":
            redis_client.set(f"simulation:{simulation_id}:control", command.command)

        return {
            "message": f"Command '{command.command}' sent to simulation {simulation_id}"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send command: {str(e)}")


@app.post(
    "/simulation_tick/{simulation_id}",
    tags=["simulations"],
    summary="Single-step simulation execution",
    responses={
        200: {"description": "Tick command sent successfully"},
        404: {"description": "Simulation not found"},
        500: {"description": "Failed to send tick command"},
    },
)
async def tick_simulation(
    simulation_id: Annotated[str, Field(description="UUID of simulation to advance")] = Depends(get_simulation_id)
):
    """
    Advance simulation by exactly one time step for precise control.
    
    Enables step-by-step debugging and detailed scenario analysis.
    Only functions when simulation was started with auto_run=false.
    Essential for detailed behavioral analysis and corner case investigation.
    
    Args:
        simulation_id: UUID of the target simulation instance
        
    Returns:
        dict: Confirmation that single step command was executed
        
    Note:
        This command requires the simulation to be started with auto_run=false.
        Use this for detailed step-by-step analysis of vehicle behaviors.
    """
    try:
        redis_client = redis.Redis()
        if not check_simulation_running(simulation_id, redis_client):
            raise HTTPException(
                status_code=404, detail="Simulation not found or finished"
            )

        redis_client.set(f"simulation:{simulation_id}:control", "tick")
        return {"message": f"Tick command sent to simulation {simulation_id}"}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to send tick command: {str(e)}"
        )


@app.get(
    "/simulation_results/{simulation_id}",
    tags=["simulations"],
    summary="Get comprehensive simulation results",
    responses={
        200: {"description": "Simulation results retrieved successfully"},
        404: {"description": "Simulation not found"},
    },
)
async def get_simulation_results(
    simulation_id: Annotated[str, Field(description="UUID of completed simulation")] = Depends(get_simulation_id)
):
    """
    Retrieve comprehensive results and analytics from completed simulation.
    
    Provides access to complete simulation output including vehicle trajectories,
    collision data, performance metrics, and generated scenario files.
    Results include both quantitative metrics and qualitative behavioral analysis.
    
    Args:
        simulation_id: UUID of the simulation to retrieve results for
        
    Returns:
        dict: Complete simulation results including:
            - Execution status and completion state
            - Vehicle trajectory data (FCD files)
            - Collision detection results
            - Performance metrics and KPIs
            - Generated OpenSCENARIO files
            - Output directory paths for detailed files
            
    Result states:
        - running: Simulation still executing, partial results available
        - completed: Full results available for analysis
        - failed: Error occurred, diagnostic information provided
    """
    status = running_simulations[simulation_id]["status"]
    if status == "running":
        return {
            "simulation_id": simulation_id,
            "status": status,
            "message": "Simulation still running",
        }
    elif status == "failed":
        return {
            "simulation_id": simulation_id,
            "status": status,
            "message": "Simulation failed",
        }
    elif status == "completed":
        # Implement result retrieval logic here
        # This might involve reading files from the output directory or querying a database
        results = (
            "Simulation results here"  # Replace with actual result retrieval logic
        )
        return {"simulation_id": simulation_id, "status": status, "results": results}


@app.get(
    "/simulation/{simulation_id}/state",
    tags=["simulations"],
    summary="Get complete simulation state snapshot",
)
async def get_simulation_state(
    simulation_id: Annotated[str, Field(description="UUID of simulation to query state from")] = Depends(get_simulation_id)
):
    """
    Retrieve comprehensive real-time state information for active simulation.
    
    Provides complete snapshot of current simulation state including:
    vehicle positions, speeds, accelerations, and behavioral states.
    Essential for real-time monitoring and analysis.
    
    Args:
        simulation_id: UUID of the target simulation
        
    Returns:
        dict: Complete simulation state including:
            - All vehicle states (position, velocity, acceleration)
            - Traffic light states
            - Environmental conditions
            - Simulation time and step information
            - Agent behavioral states
    """
    try:
        redis_client = redis.Redis()
        if not check_simulation_running(simulation_id, redis_client):
            raise HTTPException(
                status_code=404, detail="Simulation not found or finished"
            )

        state = redis_client.get(f"simulation:{simulation_id}:state")
        if not state:
            raise HTTPException(status_code=404, detail="Simulation state not found")

        return json.loads(state.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/simulation/{simulation_id}/agent_command",
    tags=["agents"],
    summary="Send real-time command to control specific agent",
)
async def control_vehicle(
    command: Annotated[AgentCommand, Field(description="Control command for specific agent (vehicle or pedestrian)")],
    simulation_id: Annotated[str, Field(description="UUID of simulation containing the agent")] = Depends(get_simulation_id),
):
    """
    Send real-time control commands to specific agents during simulation.
    
    Enables precise control over individual vehicle or pedestrian behavior
    for testing specific scenarios and edge cases. Commands are queued
    and executed in the next simulation step.
    
    Args:
        simulation_id: UUID of the simulation containing the target agent
        command: AgentCommand containing agent ID and control parameters
        
    Returns:
        dict: Confirmation that command was queued for execution
        
    Command types:
        - Lane change commands
        - Speed control (acceleration/deceleration)
        - Emergency braking
        - Route modifications
        - Behavioral parameter changes
        
    Example:
        Force emergency braking for vehicle "car_1":
        {
            "agent_id": "car_1",
            "command_type": "emergency_brake",
            "parameters": {"deceleration": 8.0}
        }
    """
    try:
        redis_client = redis.Redis()
        if not check_simulation_running(simulation_id, redis_client):
            raise HTTPException(
                status_code=404, detail="Simulation not found or finished"
            )

        redis_client.rpush(
            f"simulation:{simulation_id}:agent_commands", json.dumps(command.model_dump())
        )
        return {"message": f"Agent command sent for agent {command.agent_id}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/simulation/{simulation_id}/agent_commands_batch",
    tags=["agents"],
    summary="Send coordinated batch commands to multiple agents",
)
async def control_agents_batch(
    command_batch: Annotated[AgentCommandBatch, Field(description="Collection of commands for multiple agents to execute simultaneously")],
    simulation_id: Annotated[str, Field(description="UUID of simulation containing target agents")] = Depends(get_simulation_id),
):
    """
    Execute coordinated control commands across multiple agents simultaneously.
    
    Enables complex scenario orchestration by controlling multiple vehicles
    and pedestrians in coordination. Commands are executed in the order provided
    for precise timing control.
    
    Args:
        simulation_id: UUID of simulation containing the target agents
        command_batch: Collection of AgentCommand objects for batch execution
        
    Returns:
        dict: Confirmation with count of commands successfully queued
        
    Use cases:
        - Coordinated lane changes for convoy behavior
        - Simultaneous emergency responses
        - Traffic pattern manipulation
        - Multi-agent corner case generation
        
    Example:
        Coordinate emergency braking across multiple vehicles:
        {
            "commands": [
                {"agent_id": "car_1", "command_type": "emergency_brake"},
                {"agent_id": "car_2", "command_type": "lane_change", "target_lane": 2}
            ]
        }
    """
    try:
        redis_client = redis.Redis()
        if not check_simulation_running(simulation_id, redis_client):
            raise HTTPException(
                status_code=404, detail="Simulation not found or finished"
            )

        # Add all commands to the queue
        for command in command_batch.commands:
            redis_client.rpush(
                f"simulation:{simulation_id}:agent_commands", json.dumps(command.model_dump())
            )
        return {
            "message": f"Batch of {len(command_batch.commands)} agent commands sent successfully"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health_check():
    return {"status": "healthy"}


async def simple_cleanup_task():
    """Check every minute and clean up problematic processes"""
    while True:
        await asyncio.sleep(60)  # Check every minute
        
        for sim_id, info in list(running_simulations.items()):
            try:
                # Check 1: Is the process dead?
                if not info["process"].is_alive():
                    logger.info(f"Process dead for simulation {sim_id}, cleaning up")
                    del running_simulations[sim_id]
                    continue
                
                # Check 2: Force terminate if running over 1 hour
                runtime = time.time() - info["start_time"]
                if runtime > 18000:  # 5 hours
                    logger.warning(f"Simulation {sim_id} exceeded 1 hour runtime, terminating")
                    terminate_simulation_process(sim_id, info)
                    continue
                    
                # Check 3: Check for error flags in Redis
                redis_client = redis.Redis()
                error_flag = redis_client.get(f"simulation:{sim_id}:error_stop")
                if error_flag:
                    logger.warning(f"Error flag detected for {sim_id}, terminating")
                    terminate_simulation_process(sim_id, info)
                    continue
                    
            except Exception as e:
                logger.error(f"Error in cleanup task for {sim_id}: {e}")


def terminate_simulation_process(sim_id, info):
    """Terminate the specified simulation process"""
    try:
        # Try graceful termination first
        info["process"].terminate()
        info["process"].join(timeout=5)
        
        # Force kill if still alive
        if info["process"].is_alive():
            info["process"].kill()
            logger.warning(f"Force killed process for {sim_id}")
        
        # Clean up Redis data
        redis_client = redis.Redis()
        keys = redis_client.keys(f"simulation:{sim_id}:*")
        if keys:
            redis_client.delete(*keys)
            
    except Exception as e:
        logger.error(f"Error terminating {sim_id}: {e}")
    finally:
        # Remove from memory
        if sim_id in running_simulations:
            del running_simulations[sim_id]


async def startup_event():
    # Start the cleanup task (runs from the lifespan when the server starts)
    asyncio.create_task(simple_cleanup_task())
    logger.info("Simple cleanup task started")


async def shutdown_event():
    # Clean up all processes when service shuts down
    for sim_id, info in list(running_simulations.items()):
        terminate_simulation_process(sim_id, info)


_city_router_mounted = False


def _mount_city_router_once():
    """Mount the city APIs exactly once, and only when enabled.

    The city API is OPT-IN: by default create_app() mounts nothing (and never
    requires the city env vars), so existing terasim-service users are unaffected.
    Only when TERASIM_ENABLE_CITY_API is truthy are mounted:
      * synchronous VSS optimization at /v1/city/optimize,
      * the legacy/scheduler submit-and-poll API at /v1/city/optimizations
        (incident-expedited runs are an optional inline field of that POST,
        not a separate endpoint),
      * the snapshot-ingestion API at /v1/city/snapshots,
      * GET /v1/city/scheduled/{corridor_id}/latest,
      * the console's audit log at /v1/city/audit/* (on the v1 router; kept under
        TERASIM_CITY_OUTPUT_ROOT/audit/, off with TERASIM_CITY_AUDIT_ENABLED=0).
    TERASIM_CITY_OUTPUT_ROOT is then required (the builders fail loud if it is
    unset). All surfaces share ONE store config (same Redis keys / cap / TTL /
    emergency headroom), so the concurrency cap is global, and ONE snapshot
    buffer (built by build_v1_production_router) is shared by the ingest
    router, the v1 router's corridor_id mode, and the scheduler.

    When TERASIM_CITY_SCHEDULED_CONFIG is set, the v1 router accepts
    corridor_id requests over the corridors from that config — independent of
    the cadence scheduler, so an incident reported by the video analytics works
    with the periodic loop off.

    When TERASIM_CITY_SCHEDULER_ENABLED is additionally truthy, a CityScheduler
    task is created at app startup (config from TERASIM_CITY_SCHEDULED_CONFIG,
    cadence/window from TERASIM_CITY_CADENCE_S / TERASIM_CITY_WINDOW_S). Its
    submit callback reuses the SAME store + launcher as the v1 router (a direct
    function call, not HTTP) and records city_sched:last:{corridor_id} in Redis.
    """
    global _city_router_mounted
    if _city_router_mounted:
        return
    import os

    from terasim_service.constants import ENVIRONMENT_VARIABLE_ENABLE_CITY_API

    if os.environ.get(ENVIRONMENT_VARIABLE_ENABLE_CITY_API, "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        # City API disabled (default): mount nothing, require no city env vars.
        _city_router_mounted = True
        return

    from terasim_service.city.asset_api import build_asset_router
    from terasim_service.city.ingest_api import create_snapshot_router
    from terasim_service.city.optimize_api import build_v1_production_router

    v1 = build_v1_production_router()
    app.include_router(v1["router"])

    # Plan / map uploads live on the asset volume; without a data root the
    # endpoints are not mounted (uploads have nowhere to go).
    data_root = os.environ.get("TERASIM_CITY_DATA_ROOT")
    if data_root:
        app.include_router(build_asset_router(Path(data_root)))

    # ONE buffer (built inside build_v1_production_router, backing the v1
    # router's corridor_id mode) is shared with the ingest router and the
    # scheduler, so all three see the same buffered snapshots.
    redis_client = v1["redis_client"]
    snapshot_buffer = v1["buffer"]
    app.include_router(create_snapshot_router(snapshot_buffer))

    @app.get("/v1/city/scheduled/{corridor_id}/latest", tags=["city-scheduled"])
    def get_latest_scheduled_run(corridor_id: str):
        """run_id of the most recent scheduler-submitted run for this corridor."""
        try:
            value = redis_client.get(f"city_sched:last:{corridor_id}")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="job store unavailable") from exc
        run_id = value.decode("utf-8") if isinstance(value, bytes) else value
        return {"corridor_id": corridor_id, "run_id": run_id}

    _register_city_scheduler_if_enabled(v1, snapshot_buffer, redis_client)
    _city_router_mounted = True


def _register_city_scheduler_if_enabled(v1: dict, snapshot_buffer, redis_client) -> None:
    """Register startup/shutdown handlers for the scheduled-optimization loop."""
    import os

    from terasim_service.constants import (
        ENVIRONMENT_VARIABLE_CITY_CADENCE_S,
        ENVIRONMENT_VARIABLE_CITY_SCHEDULED_CONFIG,
        ENVIRONMENT_VARIABLE_CITY_SCHEDULER_ENABLED,
        ENVIRONMENT_VARIABLE_CITY_WINDOW_S,
    )

    if os.environ.get(ENVIRONMENT_VARIABLE_CITY_SCHEDULER_ENABLED, "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        return

    config_path = os.environ.get(ENVIRONMENT_VARIABLE_CITY_SCHEDULED_CONFIG)
    if not config_path:
        raise RuntimeError(
            f"{ENVIRONMENT_VARIABLE_CITY_SCHEDULED_CONFIG} must be set when "
            f"{ENVIRONMENT_VARIABLE_CITY_SCHEDULER_ENABLED} is on"
        )
    cadence_s = float(os.environ.get(ENVIRONMENT_VARIABLE_CITY_CADENCE_S) or 3600)
    window_s = float(os.environ.get(ENVIRONMENT_VARIABLE_CITY_WINDOW_S) or 3600)

    from terasim_service.city.optimize_api import make_run_status, make_scheduled_submit
    from terasim_service.city.scheduler import CityScheduler, load_scheduled_config

    corridors = load_scheduled_config(config_path)
    store = v1["store"]
    launcher = v1["launcher"]
    output_root = v1["output_root"]

    # A rejected submission raises SubmissionRejected; the scheduler logs its
    # HTTP status and detail and retries next tick.
    submit = make_scheduled_submit(
        store,
        launcher,
        output_root=output_root,
        on_submitted=lambda corridor_id, run_id: redis_client.set(
            f"city_sched:last:{corridor_id}", run_id
        ),
    )

    scheduler = CityScheduler(
        snapshot_buffer,
        submit,
        corridors,
        output_root=output_root,
        cadence_s=cadence_s,
        window_s=window_s,
        run_status=make_run_status(store),
    )
    state: dict = {}

    async def _start_city_scheduler() -> None:
        state["stop"] = asyncio.Event()
        state["task"] = asyncio.create_task(scheduler.run(state["stop"]))
        logger.info(
            f"city scheduler started: {len(corridors)} corridor(s), "
            f"cadence {cadence_s:.0f}s, window {window_s:.0f}s"
        )

    async def _stop_city_scheduler() -> None:
        stop = state.get("stop")
        if stop is not None:
            stop.set()
        task = state.get("task")
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                task.cancel()

    add_startup_hook(_start_city_scheduler)
    add_shutdown_hook(_stop_city_scheduler)


def create_app():
    """Create and return the FastAPI application with MCP support."""
    # Check Redis connection
    check_redis_connection()
    _mount_city_router_once()
    return app
