# Python API

The services are thin layers over Python packages that can be used directly. This page covers
the entry points developers use most; the examples run from the repository root of a
[source installation](../getting-started/installation.md#option-2-python-packages-from-source)
and use the synthetic demo corridor.

| Task | Entry point |
| --- | --- |
| Run a SUMO scenario with TeraSim agents | `terasim.simulator.Simulator` + an environment |
| Turn TMC counts into a runnable scenario | `terasim_city.run_tmc_pipeline()`, `terasim_city.generate_flow()` |
| Store and look up maps | `terasim_city.maps.MapStore` |
| Store and look up timing plans | `terasim_city.plans.PlanStore` |
| Optimize a corridor's signal timing | `terasim_city.orchestration.run_corridor_optimization()` |
| Compute KPIs from a SUMO run | `terasim_city.evaluation.extract_moe()`, `score_moe()` |
| Call a running service | `terasim_agent_skill.client.CityOptimizationClient` |

## The simulation core

`Simulator` owns the SUMO process and runs three pipelines (start, step, stop). An environment
bound to it decides, at every step, which agents act and how. `EnvTemplate` implements the
usual loop (agents make decisions, the environment executes them) and stops when
`should_continue_simulation()` returns `False`.

```python
from terasim.envs.template import EnvTemplate
from terasim.logger.infoextractor import InfoExtractor
from terasim.overlay import traci
from terasim.simulator import Simulator
from terasim_city.factories import PassiveVehicleFactory


class TimedEnv(EnvTemplate):
    """Runs a scenario for a fixed horizon; SUMO drives every vehicle."""

    def __init__(self, vehicle_factory, info_extractor, *, end_s: float):
        super().__init__(vehicle_factory, info_extractor)
        self.end_s = end_s

    def should_continue_simulation(self):
        return traci.simulation.getTime() < self.end_s


env = TimedEnv(PassiveVehicleFactory(), InfoExtractor, end_s=300.0)
sim = Simulator(
    sumo_config_file_path="examples/corridor_demo/demo_scenario.sumocfg",
    sumo_net_file_path="examples/corridor_demo/demo.net.xml",
    output_path="/tmp/terasim/sim",
    sumo_output_file_types=["tripinfo"],   # also: fcd, fcd_all, collision, lc, traj
)
sim.bind_env(env)
sim.run()                                  # start(), step() until the env stops, stop()
```

Key points:

- `Simulator(...)` takes the sumocfg and the net, an optional `output_path` for SUMO outputs,
  `step_length`, `seed`, `gui_flag` (runs `sumo-gui`), `realtime_flag`, `traffic_scale` and
  `additional_sumo_args`. It uses `libsumo` when available and TraCI otherwise
  (`USE_LIBSUMO=0` forces TraCI).
- Agents come from factories: `PassiveVehicleFactory` leaves driving to SUMO's own models; the
  NDE/NADE factories of `terasim-nde-nade` attach naturalistic and adversarial behavior (see
  [Adversarial environment](adversarial-environment.md)).
- `sim.add_plugin(plugin)` adds a plugin that hooks into the pipelines; environments hook in
  through `on_start`, `on_step` and `on_stop`.

## From traffic counts to a scenario

`run_tmc_pipeline()` turns one TMC file and a base scenario into a runnable scenario directory:
it matches the TMC intersections to the map, generates routes from the turning counts
(`routes.rou.xml`), writes `simulation.sumocfg` and records the run context in
`metadata.json`.

```python
from terasim_city import run_tmc_pipeline

scenario = run_tmc_pipeline(
    "examples/corridor_demo/demo_tmc.json",   # 1 h of turning counts for i1, i2, i3
    "examples/corridor_demo/demo.sumocfg",    # base scenario: net and signal programs
    "/tmp/terasim/scenario",
    sim_end=600.0,
)
print(scenario.n_flows, scenario.total_vehs_per_hour, scenario.sumocfg_file)
```

`flow_source` selects how the demand is produced: `"corridor_graph"` (default) generates it
from the counts; `"scenario_routes"` reuses the base scenario's own route files unchanged.
Further parameters (`seed`, `flow_scale`, `max_match_distance_m`, hop limits) are documented
in the function's docstring.

`generate_flow()` does the same with the map found automatically: it looks up the stored map
whose bounding box covers every intersection of the TMC file.

```python
from terasim_city import generate_flow
from terasim_city.maps import MapStore

store = MapStore("/tmp/terasim/store")
result = generate_flow("examples/corridor_demo/demo_tmc.json", "/tmp/terasim/flow",
                       store=store, sim_end=600.0)
print(result.map_id, result.scenario.n_flows)
```

## Maps

`MapStore` is the map adapter behind `POST /v1/city/maps`. It detects the format, converts HD
maps to a SUMO network once and keeps one package per map under `<data_root>/maps/<map_id>/`.

```python
from terasim_city.maps import MapStore, detect_map_format

store = MapStore("/tmp/terasim/store")          # default: $TERASIM_CITY_DATA_ROOT
print(detect_map_format("examples/corridor_demo/demo.sumocfg"))   # "sumocfg"

result = store.ingest("examples/corridor_demo/demo.sumocfg")      # .osm, .xodr or .sumocfg
print(result.map_id, result.duplicate, result.package.source, result.summary["traffic_lights"])

# An OpenDRIVE file without a geo-reference takes its projection explicitly:
# store.ingest("corridor.xodr", proj="+proj=tmerc +lat_0=37.335 +lon_0=-121.9 +k=1 "
#              "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")

for map_id, package in store.find_covering(lat=37.335, lon=-121.9):  # best source first
    print(map_id, package.source, package.base_sumocfg)
```

`store.get(map_id)` loads one package (`MapPackage`: `net_file`, `base_sumocfg`, `tls_files`,
`geo_referenced`, `source`, `metadata`); `store.list_ids()` lists them. Formats, conversion and
the projection override are described in [Map formats](../reference/map-formats.md).

## Timing plans

`PlanStore` keeps the cities' current plans (`terasim.nema_plan.v1`) per intersection, the store
behind `POST /v1/city/plans`. The last upload for an intersection wins; the replaced version is
archived.

```python
from terasim_city.plans import PlanStore

plans = PlanStore("/tmp/terasim/store")          # default: $TERASIM_CITY_DATA_ROOT
result = plans.ingest("my_plan.json")            # a path or a dict
print(result.stored, result.notes)

# Look a plan up by key, or by the location of a map junction (within 60 m):
stored = plans.find_for(None, None, lat=37.335, lon=-121.9)
if stored is not None:
    print(stored.corridor_id, stored.intersection_id, stored.matched_by, stored.match_distance_m)
```

See [NEMA plan formats](../reference/nema-plan-format.md) for the document.

## Corridor optimization

`run_corridor_optimization()` is the whole optimization run of the service, in process: resolve
the corridor, generate the scenario, build the seed plans, evaluate them and the Bayesian
candidates in parallel SUMO processes, export the winner as NEMA timing.

```python
from pathlib import Path

from terasim_city.evaluation import KpiConfig
from terasim_city.optimize import Budget
from terasim_city.orchestration import CorridorOptimizationConfig, run_corridor_optimization


def main() -> None:
    cfg = CorridorOptimizationConfig(
        tmc_json=Path("examples/corridor_demo/demo_tmc.json"),
        base_sumocfg=Path("examples/corridor_demo/demo.sumocfg"),
        output_dir=Path("/tmp/terasim/optimization"),
        tls_ids=("J1", "J2", "J3"),               # or from_ref="i1", to_ref="i3"
        baseline_sheet=Path("examples/corridor_demo/baseline_sheet_uncoordinated.json"),
        budget=Budget(max_trials=6, patience=8),
        kpi=KpiConfig.from_dict({"control_delay": 0.7, "stops": 0.3}),
        sim_end=600.0,
        n_workers=2,
    )
    result = run_corridor_optimization(cfg)
    print(result["baseline"]["score"], result["best"]["score"], result["nema_export"])


if __name__ == "__main__":   # required: trials run in a spawn-based process pool
    main()
```

Two rules for scripts:

1. **Keep the call behind `if __name__ == "__main__":`.** Trials run in a spawn-based process
   pool and spawn re-imports the calling script; module-level code would run again in every
   worker.
2. **Name the corridor on a map with several signals**: explicit `tls_ids`, or a
   `from_ref`/`to_ref` pair (TMC intersection ids or `"lat,lon"` strings). A single-signal map
   needs neither.

The main fields of `CorridorOptimizationConfig`:

| Field | Default | Meaning |
| --- | --- | --- |
| `tmc_json` | required | Demand file (TMC counts) |
| `base_sumocfg` | required (may be `None`) | The corridor's map; `None` selects a stored map covering the TMC intersections |
| `output_dir` | required | Run directory |
| `from_ref`, `to_ref`, `tls_ids` | `None`, `None`, `()` | Corridor selection |
| `baseline_sheet` | `None` | Timing sheet used as the baseline (see [Timing sheet format](../reference/timing-sheet-format.md)) |
| `plan_data_root` | `None` | Plan store root; when it covers every corridor signal, the uploaded NEMA plans are the baseline |
| `plan_time` | TMC `timestamp_start` | Time that selects each stored plan's time-of-day pattern (local time of the corridor) |
| `optimizer` | `"auto"` | `auto`, `exhaustive`, `bayes_offsets`, `bayes_splits`, `bayes_corridor` |
| `budget` | `Budget(max_trials=20, patience=8)` | Bayesian trials and early stopping |
| `kpi` | weights 0.7 / 0.3 | KPI weights of the cost |
| `sim_end` | `1800.0` | Simulated seconds per candidate |
| `seed` | `42` | SUMO seed (identical for every candidate) |
| `n_workers` | `4` | Parallel SUMO processes |
| `stall_data` | `None` | Stalled vehicles to place in every trial (incident runs) |
| `vehicle_behavior` | `"sumo_passive"` | Background-vehicle behavior (`"cnde"` with the Neural NDE runtime) |
| `cnde_max_workers` | `1` | Most concurrent trial processes of a `"cnde"` search (`n_workers` above it is rejected unless evaluation is inline) |
| `flow_source`, `counts_source` | `"corridor_graph"`, `"tmc"` | Demand and turning counts from the counts, or from the scenario's own routes (`"scenario_routes"`) |
| `seed_from_scenario_programs` | `False` | Use the scenario's own signal programs as the baseline |
| `stage_mode` | `"two_stage"` | Webster seed stages: `two_stage` or `per_approach` |
| `search_cycle`, `cycle_min_s`, `cycle_max_s` | `False`, `45.0`, `150.0` | Let the search vary the cycle within the bounds |
| `refine_top_k`, `refine_vehicle_behavior`, `refine_max_collisions`, `refine_n_workers` | `0`, `None`, `None`, `1` | Optional re-evaluation of the best candidates under another behavior |

`optimizer="auto"` searches offsets on a corridor of two or more signals and green splits on a
single signal; with stored NEMA plans it searches the NEMA offsets or splits of the stored
pattern. `bayes_corridor` searches splits and offsets jointly (and the common cycle with
`search_cycle`).

The function returns the result document (also written as
`corridor_optimization_result.json`, schema `terasim.corridor_optimization.v1`) with, among
others, `resolved_corridor`, `seed`, `trials`, `baseline` and `best` (each with `plan_id`,
`score`, `moe` and `timing`), `search`, `nema_export` and `unserved_movements`; the NEMA export
is written to `nema_plan.json`. A `progress_callback(done, total, last_plan_id)` reports
progress after every evaluated plan.

## KPIs

```python
from terasim_city.evaluation import KpiConfig, extract_moe, score_moe

moe = extract_moe("/tmp/terasim/sim/tripinfo.xml")
print(moe["control_delay_s_per_veh"], moe["stops_per_veh"], moe["vehicles_total"])

weights = KpiConfig.from_dict({"control_delay": 0.7, "stops": 0.3})
print(score_moe(moe, moe, weights))   # a plan identical to the baseline scores 1.0
```

`extract_moe()` reads a SUMO `tripinfo.xml` written with unfinished and undeparted vehicles
included, so the means cover all demand. See [KPIs](../reference/kpis.md).

## Calling a running service

`CityOptimizationClient` is a small synchronous client of the REST API (it is what the MCP server
and the TMC collector use). Every non-2xx answer raises `RuntimeError` with the server's
message.

```python
from terasim_agent_skill.client import CityOptimizationClient

client = CityOptimizationClient("http://localhost:8000")   # timeout 1800 s by default
client.upload_map("/data/input/map/demo.sumocfg")          # a path the service can read
result = client.optimize_corridor(
    "demo_arterial",
    {"intersection_id": "i1", "name": "Arterial Ave & 1st Cross St", "lat": 37.335, "lon": -121.90339},
    {"intersection_id": "i3", "name": "Arterial Ave & 3rd Cross St", "lat": 37.335, "lon": -121.89661},
    cost={"weights": {"control_delay": 0.7, "stops": 0.3}},
)
print(result["kpis"]["optimized"]["cost"])
client.close()
```

Other methods: `push_snapshot(payload)`, `upload_plan(plan, corridor_id=None)`, `list_plans()`,
`optimize_intersection(corridor_id, intersection, cost=None)`, and for the asynchronous API
`get_status(run_id)`, `get_result(run_id)`, `get_nema(run_id)` and `wait(run_id)`.
