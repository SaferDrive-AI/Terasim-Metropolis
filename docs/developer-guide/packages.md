# Packages

The release consists of seven Python packages. Each lives in `packages/<name>/` with its own
`pyproject.toml` and is installed on its own; the dependency arrows below are package
dependencies.

```text
terasim  ◄── terasim-nde-nade  ◄── terasim-city  ◄── terasim-service
                                                      ▲
terasim-agent-skill  ◄── terasim-tmc-service          │ (REST)
        └──────────────────────(REST)─────────────────┘
terasim-plugin-sdk   (plugins for operations such as signal timing)
```

| Package | Import name | Console script | Module |
| --- | --- | --- | --- |
| `terasim` | `terasim` | | A3 simulation core |
| `terasim-nde-nade` | `terasim_nde_nade` | | A1 behavior models, A2 adversarial environment |
| `terasim-city` | `terasim_city` | | A4 map adapter, B KPIs, C signal optimization |
| `terasim-service` | `terasim_service` | `terasim-service` | REST service |
| `terasim-agent-skill` | `terasim_agent_skill` | `terasim-agent-skill` | MCP server |
| `terasim-tmc-service` | `terasim_tmc_service` | `terasim-tmc-service` | TMC collector |
| `terasim-plugin-sdk` | `terasim_plugin_sdk` | `terasim-plugin` | Plugin SDK |

## terasim

The simulation core. `terasim.simulator.Simulator` owns the SUMO process (through `libsumo` by
default, TraCI otherwise) and runs a pipeline of start, step and stop hooks; environments
(`terasim.envs`) decide what the agents do at every step; vehicles and vulnerable road users
are agents built by factories from a sensor set, a decision model and a controller
(`terasim.vehicle`, `terasim.vulnerable_road_user`); `terasim.traffic_light` does the same for
signals. SUMO 1.23.1 (`eclipse-sumo`, `libsumo`, `traci`, `sumolib`) is a pinned dependency.

## terasim-nde-nade

Naturalistic and adversarial driving environments built on the core:

- `terasim_nde_nade.envs`: `NDE` (naturalistic driving environment), `NADE` (naturalistic and
  adversarial driving environment with importance sampling) and `NADEWithAV` (NADE around an
  ego vehicle).
- `terasim_nde_nade.vehicle`, `terasim_nde_nade.vru`: behavior models and factories
  (`NDEVehicleFactory`, `NDEVulnerableRoadUserFactory`).
- `terasim_nde_nade.adversity`: the adversity library (vehicle, vulnerable road user and static
  adversities), configured by the files in `configs/adversities/`.

It contains Cython extensions (collision checks, geometry and trajectory utilities) built by
`python setup.py build_ext --inplace`. Configuration objects are OmegaConf `DictConfig`s and
adversities are instantiated from `_target_` entries with Hydra. See
[Adversarial environment](adversarial-environment.md).

## terasim-city

The signal-optimization pipeline:

- the map adapter (`terasim_city.maps`) that turns OpenDRIVE, Lanelet2, OpenStreetMap and SUMO
  maps into stored map packages;
- traffic-count ingestion and route generation (`terasim_city.flow`, `run_tmc_pipeline`);
- the plan model, seeds and stores (`terasim_city.plans`);
- the Bayesian optimizers and the parallel evaluator (`terasim_city.optimize`);
- KPIs and the simulated signal-performance measures (`terasim_city.evaluation`);
- the NEMA export (`terasim_city.nema`) and the run orchestration (`run_corridor_optimization`).

Dependencies include Optuna (TPE sampler), lxml, sumolib and `tzfpy` (offline time-zone
lookup). The package works offline; network access (OpenStreetMap download) is a service
concern.

## terasim-service

A FastAPI application started with `terasim-service` (port 8000). With
`TERASIM_ENABLE_CITY_API` set it mounts the city API under `/v1/city/*`: snapshot ingestion,
map and plan upload, synchronous and asynchronous optimization, the run listing and per-run
outputs, the audit log and the optional built-in scheduler. Optimization runs execute in worker
processes and hold worker slots in a Redis-backed job store. The package also carries the
co-simulation endpoints (`/start_simulation`, `/simulation_tick/...`) that drive single TeraSim
runs from an external simulator. See [HTTP API](../reference/http-api.md).

## terasim-agent-skill

The MCP server started with `terasim-agent-skill` (stdio by default, streamable HTTP for
deployments) and `CityOptimizationClient`, a small Python client of the city API. The server
validates tool arguments with Pydantic models, forwards each call to the service, saves the
result files (NEMA plan, timing report PDF) when an artifact store is configured and serves them
at `/files`. Extras: `mcp` (the MCP runtime) and `report` (the PDF renderer). See
[MCP tools](../reference/mcp-tools.md).

## terasim-tmc-service

The TMC collector started with `terasim-tmc-service` (port 8200). It loads the corridor spec
(`Metadata.json`), keeps the registry of active intersections, pulls the VSS behavior objects of
their cameras every observation window and counts the turning movements with the vendored
NVIDIA reference functions (`terasim_tmc_service.nv_generate_tmc`), then pushes each snapshot
to `terasim-service` through `CityOptimizationClient`. See
[VSS integration](../deployment/vss-integration.md).

## terasim-plugin-sdk

The SDK for operation plugins, discovered through the entry-point group
`terasim.operation_plugins`; the signal-timing optimization is the built-in plugin
`signal_timing`. The `terasim-plugin` command line works with installed plugins. See
[Plugin SDK](../plugins/index.md) and the [example plugin](../plugins/example-plugin.md).

## Optional: Neural NDE runtime

The vehicle-behavior registry of `terasim-city` has a slot named `cnde`, the inference
interface of the Neural NDE model; its runtime module and checkpoint are installed separately
from this repository (see [Behavior models](behavior-models.md)). When they are installed, a
request can ask for Neural NDE traffic for its own run (`traffic_model="cnde"`), and a
deployment can evaluate every candidate plan with it (`TERASIM_CITY_VEHICLE_BEHAVIOR=cnde`) or
re-evaluate the best candidates with it (`TERASIM_CITY_REFINE_TOP_K` and
`TERASIM_CITY_REFINE_BEHAVIOR`); see [Configuration reference](../deployment/configuration.md).
`apps/deploy/city/Dockerfile.cnde` builds the image for that. The default behavior,
`sumo_passive`, needs no GPU.
