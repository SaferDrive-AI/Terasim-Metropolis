# Configuration system

TeraSim Metropolis is configured on four levels. Each has one home:

| Level | Format | Read by | Scope |
| --- | --- | --- | --- |
| Simulation experiments | YAML, OmegaConf / Hydra | `terasim_service.utils.base`, `terasim_nde_nade` | Environment class, simulator options, behavior and adversity parameters |
| Deployment | Environment variables | the three services | Ports, storage, Redis, concurrency, scheduler, downloads |
| Corridors | YAML (`TERASIM_CITY_SCHEDULED_CONFIG`) | `terasim-service` | Per-corridor defaults: map, signals, timing sheet, trial budget |
| Requests | JSON | the REST API and MCP tools | One optimization: target, KPI weights, and on the asynchronous API the search options |

## Simulation experiment configuration

An experiment configuration is a YAML document with these sections (the files in `configs/`
are the reference values):

```yaml
input:                                  # the scenario
  sumo_net_file: path/to/map.net.xml
  sumo_config_file: path/to/map.sumocfg
path_resolution: config_relative        # or cwd_relative: how relative input paths resolve

seed: 42
simulator:                              # configs/base.yaml
  module: "terasim.simulator"
  class: "Simulator"
  parameters:
    num_tries: 10
    gui_flag: false
    realtime_flag: false
    sumo_seed: 42
    sumo_output_file_types: ["fcd_all", "collision", "tripinfo"]

environment:                            # configs/environment.yaml
  module: "terasim_nde_nade.envs"
  class: "NADEWithAV"                   # NADE for runs without an ego vehicle
  parameters:
    vehicle_factory: "terasim_nde_nade.vehicle.nde_vehicle_factory.NDEVehicleFactory"
    info_extractor: "terasim.logger.infoextractor.InfoExtractor"
    log_flag: true
    warmup_time_lb: 749                 # warm-up drawn uniformly from [lb, ub) seconds
    warmup_time_ub: 750
    run_time: 1200                      # seconds simulated after the warm-up
    MOBIL_lc_flag: true
    stochastic_acc_flag: false
    drive_rule: "righthand"
    adversity_sampling_probability: 0.1
    adversity_cfg:                      # merged from configs/adversities/*.yaml
      vehicle: {...}
      vulnerable_road_user: {...}
      static: {...}
    AV_cfg: {...}                       # configs/av_config.yaml, NADEWithAV only
```

Two helpers turn such a document into running objects:

```python
from terasim_service.utils.base import create_environment, create_simulator, resolve_config_paths

config = resolve_config_paths(config, "experiment.yaml")   # relative input paths -> absolute
env = create_environment(config, "/tmp/terasim/experiment")
sim = create_simulator(config, "/tmp/terasim/experiment")
sim.bind_env(env)
sim.run()
```

`create_environment()` imports `environment.module` / `environment.class`, wraps
`environment.parameters` in an OmegaConf `DictConfig` and passes it to the environment as its
`configuration`, together with the NDE vehicle and vulnerable-road-user factories.
`create_simulator()` builds the `Simulator` from `input` and `simulator.parameters`. The same
helpers back the co-simulation endpoint `POST /start_simulation`, which takes the path of such
a YAML file.

Adversity entries are instantiated with Hydra from their `_target_` class; every other key of an
entry is a constructor argument. See [Adversarial environment](adversarial-environment.md) for
the parameters.

## Deployment configuration

Each service reads its settings from environment variables, with defaults that suit the
container image. The complete list, with defaults and the matching Helm values, is in the
[Configuration reference](../deployment/configuration.md).

## Corridor configuration

`TERASIM_CITY_SCHEDULED_CONFIG` names a YAML file of corridor entries. An entry supplies the
defaults of every optimization of that corridor (explicit request fields win) and, with the
built-in scheduler on, the corridors the scheduler optimizes.

```yaml
corridors:
  demo_arterial:                                  # the corridor_id of its TMC snapshots
    base_sumocfg: examples/corridor_demo/demo.sumocfg
    corridor:
      tls_ids: [J1, J2, J3]                       # or {from: i1, to: i3}, or [i1, i3]
    baseline_sheet: examples/corridor_demo/baseline_sheet_uncoordinated.json
    sim_end: 600
    max_trials: 6
    n_workers: 2
```

| Key | Required | Meaning |
| --- | --- | --- |
| `base_sumocfg` | yes | The corridor's map (a SUMO configuration the service can read) |
| `corridor` | yes | `tls_ids`, or `from`/`to` (TMC intersection ids or `"lat,lon"`), or a two-element list `[from, to]` |
| `baseline_sheet` | no | The current timing as a [timing sheet](../reference/timing-sheet-format.md); uploaded NEMA plans take precedence when they cover every signal |
| `rerun_unchanged` | no | Scheduler only: rerun an entry whose inputs did not change (entries fed by scenario routes) |
| any other key | no | A field of the optimization request (`optimizer`, `max_trials`, `patience`, `sim_end`, `n_workers`, `seed`, `kpi_weights`, `plan_time`, `stage_mode`, `search_cycle`, `cycle_min_s`, `cycle_max_s`, `flow_source`, `counts_source`, `seed_from_scenario_programs`, `use_stored_plans`) |

Relative paths resolve against the service's working directory. The entry's map is used as
given, so the corridor needs no map download.

`n_workers` must not exceed `TERASIM_CITY_MAX_CONCURRENCY`; each run holds that many worker
slots (one SUMO process each) while it runs.

## Request parameters

The synchronous call (`POST /v1/city/optimize`, used by the MCP tools) carries only the target
and the KPI weights; the search options come from the corridor entry and the server defaults
(`optimizer: auto`, `max_trials: 20`, `patience: 8`, `sim_end: 1800`, `n_workers: 2`,
`seed: 42`). The asynchronous call (`POST /v1/city/optimizations`) accepts every option of
`OptimizationRequest`. See the [HTTP API](../reference/http-api.md#schema-optimizationrequest).

## TMC corridor specification

The TMC collector reads its corridor from a `Metadata.json` file (`TMC_SPEC_FILE`): which camera
watches which approach, and how road segments map to headings and turns. The format is
described in [VSS integration](../deployment/vss-integration.md#corridor-specification-metadatajson).
