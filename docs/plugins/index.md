# Plugin Interface / SDK

Traffic Operation plug-ins are installable Python modules that turn a road network,
a turning-movement-count (TMC) snapshot, the signal timing running today and the
caller's KPI weights into a recommended NEMA timing plan with a before/after KPI
comparison. They ship in the same repository as the core and are built against a
documented interface, so NVIDIA, cities and integrators can author and publish their
own operations modules without modifying the core.

TeraSim Metropolis v0.1.0 provides:

| Part | Where | What it is |
|---|---|---|
| Plugin SDK | `packages/terasim-plugin-sdk` (import `terasim_plugin_sdk`) | Typed request and result, the plugin protocol, the registry, the run harness, building blocks on top of the core pipeline, the conformance kit, the `terasim-plugin` command line |
| Built-in plugin `signal_timing` | `terasim_plugin_sdk.builtin` | The core corridor signal-timing optimizer behind the plugin interface |
| Example plugin `webster_fixed_time` | `examples/plugins/webster_fixed_time` | A third-party operations module in its own distribution, discovered through an entry point; see [the walkthrough](example-plugin.md) |
| Tests | `tests/test_plugin_sdk` | Registry, contract, conformance kit, both plugins end to end on the synthetic demo corridor, command line |

The interface version is `terasim_plugin_sdk.API_VERSION = "1.0"`; the distribution
version is `0.1.0`.

## The core today

The SDK is a layer on top of interfaces the core already has. This section lists them,
because a plugin reuses them and its output has the same shape.

### The corridor optimization contract

`terasim_city.orchestration.corridor_optimization.run_corridor_optimization(cfg,
progress_callback=None)` runs one optimization from a `CorridorOptimizationConfig`:

| Config field | Meaning |
|---|---|
| `tmc_json` | The TMC JSON file (demand and seed counts) |
| `base_sumocfg` | The map's SUMO config; `None` selects the stored map covering every TMC intersection |
| `tls_ids` or `from_ref` / `to_ref` | The corridor: explicit SUMO traffic-light ids, or two endpoints (TMC intersection ids or `"lat,lon"`) |
| `baseline_sheet` | A timing sheet (`terasim.timing_sheet.v1`) as the current plan |
| `plan_data_root` + `plan_time` | Uploaded NEMA plans (`terasim.nema_plan.v1`, plan store) as the current plan, at a time of day |
| `seed_from_scenario_programs` | The map's own signal programs as the current plan |
| `kpi` | `KpiConfig` weights of the cost (default control delay 0.7, stops 0.3) |
| `sim_end` | Simulated horizon in seconds (default 1800, the 30-minute evaluation window) |
| `optimizer`, `budget` | Search strategy (see below) and trial budget (`Budget(max_trials, patience)`) |
| `n_workers`, `seed`, `vehicle_behavior` | Parallel SUMO processes, random seed, background-vehicle behavior |
| `stage_mode`, `search_cycle`, `cycle_min_s`, `cycle_max_s`, `flow_source`, `counts_source` | Seed structure, cycle search and demand switches |

It generates one shared scenario from the TMC counts, evaluates the current plan (or a
Webster plan computed from the counts when no current plan is given) and the plans of
a Bayesian search in SUMO, one trial directory per plan, and returns the result dict
(schema `terasim.corridor_optimization.v1`). It writes into the output directory:

* `corridor_optimization_result.json` -- the result (`baseline` and `best` with their
  `score`, `moe` and `timing`, the `trials`, `search`, `nema_export`,
  `unserved_movements`, `resolved_corridor`, `config`);
* `trials.jsonl` -- one line per evaluated plan;
* `nema_plan.json` -- the winning plan as `terasim.nema_timing.v1`;
* `scenario/` and `trials/trial_NNNN/` -- the generated scenario and each trial's
  plan, SUMO config, outputs and `kpi.json`.

The progress callback is called as `(done, total, last_plan_id)`, first with
`(0, total, None)`; exceptions it raises propagate, which is how the service cancels a
run between evaluation batches.

### Extension points that exist today

| Extension point | Where | What it selects |
|---|---|---|
| Flow sources | `terasim_city.api.FLOW_SOURCES`, `build_flow_source()`; base class `terasim_city.flow.sources.base.FlowSource` | How demand is generated: `corridor_graph` (default, from the TMC counts) or `scenario_routes` (the map's own route files) |
| Vehicle behaviors | `terasim_city.factories.VEHICLE_BEHAVIORS`, `register_vehicle_behavior()`, `resolve_vehicle_factory()` | Background-vehicle behavior by name: `sumo_passive` (default, SUMO's driver model) and the `cnde` integration point for the Neural NDE runtime, which is distributed separately |
| Optimizer choices | `OPTIMIZER_CHOICES` in `corridor_optimization` | The search after the seed plans: `auto`, `exhaustive`, `bayes_offsets`, `bayes_splits`, `bayes_corridor` |
| Map adapter | `terasim_city.maps.MapStore.ingest()` | Converts OpenStreetMap and Lanelet2 (`.osm`), OpenDRIVE (`.xodr`) or prebuilt SUMO (`.sumocfg`) maps into a stored map package (`terasim.map_package.v1`) with a base sumocfg |
| Plan inputs | `terasim_city.plans.load_timing_sheet()`, `terasim_city.plans.PlanStore` | Timing sheets (`terasim.timing_sheet.v1`) and uploaded NEMA plans (`terasim.nema_plan.v1`) |
| KPIs | `terasim_city.evaluation.moe`: `extract_moe()`, `KpiConfig`, `score_moe()` | The measures of effectiveness and the weighted cost |

The KPI measures come from SUMO's trip information over all demand, including vehicles
still travelling or not yet inserted at the end of the run:
`control_delay_s_per_veh`, `stops_per_veh`, `vehicles_total`, `vehicles_completed`,
`vehicles_unfinished`, `vehicles_undeparted`, `departed_ratio`, `completion_ratio`,
`total_delay_s`, `total_stops`, `mean_time_loss_s`, `mean_depart_delay_s`, plus the
per-signal `junction_delay_s` and their mean `corridor_junction_delay_s`. The cost of a
plan is `sum(weight * metric / baseline_metric)` over the weighted measures
(`control_delay` weights `control_delay_s_per_veh`, `stops` weights `stops_per_veh`):
lower is better and the baseline scores about the sum of the weights.

### How the service and the MCP server use it

The HTTP service's synchronous `POST /v1/city/optimize` (and the asynchronous
`POST /v1/city/optimizations`) build a `CorridorOptimizationConfig` from the request
and call the built-in signal-timing optimizer, `run_corridor_optimization`, directly.
The synchronous response carries the NEMA plan and the KPI comparison (plus
`unserved_movements` when the plan leaves a movement unserved):

```json
{
  "nema_plan": {"schema": "terasim.nema_timing.v1", "corridor_id": "demo_arterial", "...": "..."},
  "kpis": {
    "cost_weights": {"control_delay": 0.7, "stops": 0.3},
    "baseline": {
      "cost": 1.0,
      "metrics": {"control_delay_s_per_veh": 25.98, "stops_per_veh": 0.82, "...": "..."},
      "timing": {"cycle_s": {"J1": 90.0}, "offsets_s": {"J1": 0.0}, "greens_s": {"J1": [48.0, 34.0]}}
    },
    "optimized": {
      "cost": 0.95,
      "metrics": {"control_delay_s_per_veh": 24.34, "stops_per_veh": 0.81, "...": "..."},
      "timing": {"cycle_s": {"J1": 90.0}, "offsets_s": {"J1": 65.0}, "greens_s": {"J1": [48.0, 34.0]}}
    }
  }
}
```

The MCP server (`terasim-agent-skill`) exposes the optimization as tools
(`optimize_intersection`, `optimize_intersection_by_id`, `optimize_corridor`,
`optimize_registered_intersection`) that call this HTTP endpoint, so NVIDIA Metropolis
VSS (Video Search and Summarization) agents reach the same optimizer. In v0.1.0 the
service and the MCP tools always run the built-in optimizer; plug-ins run through the
Python API and the `terasim-plugin` command line. A design for dispatching service
and MCP requests to registered plug-ins is in
[Proposed for a later release](#proposed-for-a-later-release-not-in-v010).

## The interface

### Inputs

A plugin receives one `terasim_plugin_sdk.OperationRequest`:

| Field | Type | Meaning |
|---|---|---|
| `map` | `MapInput` | The road network (below) |
| `traffic` | `TrafficSnapshot` | The TMC counts (below) |
| `output_dir` | path | Every file the plugin writes goes here |
| `corridor` | `CorridorSelection` | `tls_ids=(...)` (explicit signals, in order) or `from_ref`/`to_ref` (TMC intersection ids or `"lat,lon"`); empty selects the only signal of a single-signal map |
| `current_plan` | `CurrentPlan` or `None` | The timing running today (below) |
| `weights` | `KpiWeights` | Weights of the KPI cost, default `{"control_delay": 0.7, "stops": 0.3}` |
| `sim_end` | float | Simulated seconds, default 1800 |
| `seed` | int | Random seed, default 42 |
| `n_workers` | int | Parallel SUMO processes, default 2 |
| `vehicle_behavior` | str | A name from `VEHICLE_BEHAVIORS`, default `sumo_passive` |
| `params` | JSON object | Plugin-specific options; each plugin documents and validates its keys |

**Map** (`MapInput`): `MapInput.from_sumocfg(path)` for a SUMO scenario file, or
`MapInput.from_store(map_id, data_root=...)` for a map package in the map store
(`data_root` defaults to `$TERASIM_CITY_DATA_ROOT`). HD maps arrive through the map
adapter: `terasim_plugin_sdk.toolkit.ingest_map("corridor.xodr", data_root=...)`
(or the service's map upload, or the MCP `upload_map` tool) converts an OpenDRIVE,
Lanelet2 or OpenStreetMap file into a stored package and returns its `map_id`.
`MapInput.from_store()` without a `map_id` selects the stored map that covers every TMC
intersection, HD maps first, then prebuilt scenarios, then OpenStreetMap.

**TMC snapshot** (`TrafficSnapshot.from_file(path)` or
`TrafficSnapshot.from_payload(dict)`), the core's TMC contract:

```json
{
  "corridor_id": "demo_arterial",
  "observation_interval": "1h",
  "timestamp_start": "2026-07-01T08:00:00",
  "intersections": [
    {
      "intersection_id": "i1",
      "lat": 37.335,
      "lon": -121.90339,
      "counts": {
        "East":  {"Left": 60, "Straight": 560, "Right": 90},
        "West":  {"Left": 50, "Straight": 400, "Right": 80},
        "North": {"Left": 45, "Straight": 120, "Right": 70},
        "South": {"Left": 40, "Straight": 110, "Right": 60}
      }
    }
  ]
}
```

`corridor_id`, `observation_interval` (`"10m"`, `"15m"`, `"1h"`, `"60s"`, ...) and a
non-empty `intersections` list are required; each intersection needs
`intersection_id`, `lat`, `lon` and `counts`. Counts are vehicles over the observation
window (scaled to hourly volumes internally); the direction keys map to the core's
eastbound, westbound, northbound and southbound movement codes (`EB`, `WB`, `NB`,
`SB`) and the turn keys to `left`, `through`, `right`. `timestamp_start` selects the
time-of-day pattern of stored NEMA plans when no `plan_time` is given.

**Current plan** (`CurrentPlan`):

| Constructor | Kind | Source |
|---|---|---|
| `CurrentPlan.timing_sheet(path)` | `timing_sheet` | A `terasim.timing_sheet.v1` file: NEMA phase numbers, movements and green/yellow/all-red splits against one corridor cycle, plus offsets |
| `CurrentPlan.nema_plan(path, plan_time=None)` | `nema_plan` | A `terasim.nema_plan.v1` document (rings, phases, patterns, time-of-day schedule per intersection); the SDK stores it in `<output_dir>/inputs/plan_store` |
| `CurrentPlan.plan_store(data_root, plan_time=None)` | `plan_store` | An asset volume whose plan store already holds uploaded NEMA plans |
| `CurrentPlan.scenario_programs()` | `scenario_programs` | The map's own `<tlLogic>` programs |
| `None` | | No current timing known: the reference is a Webster plan computed from the counts, as in the core |

A NEMA plan that does not cover every corridor signal at the selected time is not used;
the run falls back to the Webster reference and says so in the result's `notes`.

**KPI weights** (`KpiWeights({"control_delay": 0.7, "stops": 0.3})`): keys
`control_delay` and `stops`, at least one, each a finite number greater than zero; the
same rules as the service's `cost.weights`.

### Outputs

A plugin returns a `terasim_plugin_sdk.OperationResult` (JSON schema
`terasim.operation_result.v1`):

| Field | Meaning |
|---|---|
| `nema_plan` | The recommended plan as `terasim.nema_timing.v1`, also written to `<output_dir>/nema_plan.json` |
| `kpis` | `{"cost_weights", "baseline": {"cost", "metrics", "timing"}, "optimized": {...}}`, the same shape as the service's `kpis` (above); `baseline` is the current (or reference) plan and `optimized` the recommended one, both simulated on the same scenario; a side without timing leaves `timing` out |
| `corridor_id`, `tls_ids` | The corridor and its signals in corridor order |
| `unserved_movements` | Per signal, vehicle movements the recommended plan never serves (empty when it serves all) |
| `artifacts` | Name to path (relative to `output_dir`) of the files and directories worth keeping |
| `notes` | Sentences a caller should show, such as a fallback or a skipped search |
| `details` | Plugin-specific JSON, for example which plans were compared |
| `plugin`, `plugin_version`, `api_version`, `output_dir` | Who produced it, against which interface version, where |

`result.response()` gives the body the service's `POST /v1/city/optimize` returns:
`{"nema_plan", "kpis"}`, plus `unserved_movements` when there are some.

The NEMA timing document lists, per signal, the stage rows (`nema_phase`, `movements`,
`split_s`, `green_s`, `yellow_s`, `all_red_s`), the offset and the coordination cycle.
It is `handoff_ready` when every green carries NEMA phase numbers: timing sheets and
NEMA plans do; stages derived from counts list `nema_phase: null` for a traffic
engineer to number.

### The plugin protocol

```python
from terasim_plugin_sdk import API_VERSION, OperationRequest, OperationResult, ProgressCallback


class MyOperation:
    name = "my_operation"          # lowercase identifier, also the entry-point name
    version = "1.0.0"              # the plugin's own version
    api_version = API_VERSION      # the interface version it was written against
    description = "One sentence shown by 'terasim-plugin list'"

    def run(self, request: OperationRequest, progress: ProgressCallback | None = None) -> OperationResult:
        ...
```

`terasim_plugin_sdk.TrafficOperationPlugin` is the matching `typing.Protocol`. A plugin
must write only under `request.output_dir`, write its recommended plan to
`<output_dir>/nema_plan.json`, return an `OperationResult`, raise the SDK errors below,
let exceptions of `progress` propagate unchanged, and give the same result for the same
request. One instance serves many runs, so it keeps no per-run state.

## Running a plugin

### Lifecycle

1. **Discovery.** The registry holds the built-in `signal_timing` plugin and, on first
   use, loads every entry point in the `terasim.operation_plugins` group of the
   installed distributions (once per process).
2. **Validation.** Each plugin object is checked when it is registered: name pattern,
   `version`, a compatible `api_version`, a `run(request, progress=None)` method.
3. **Request.** The caller builds an `OperationRequest`; the types validate on
   construction and raise `PluginInputError` for invalid values.
4. **Run.** `terasim_plugin_sdk.run_operation(name_or_plugin, request, progress=...)`
   makes the request's paths absolute, checks that its files exist, creates
   `output_dir`, writes `operation_request.json`, calls `plugin.run(request, progress)`,
   checks the result against the contract, writes `operation_result.json` and returns
   the result. The command line and the conformance kit use this harness.

### Error contract

| Exception (`terasim_plugin_sdk`) | Raised when | `terasim-plugin` exit code |
|---|---|---|
| `PluginInputError` (a `ValueError`) | The request cannot run as given: invalid weights or params, missing map, TMC or plan file, a corridor the map does not contain | 2 |
| `PluginNotFoundError` (a `LookupError`) | No plugin of that name is registered; the message lists the registered ones | 2 |
| `PluginRunError` (a `RuntimeError`) | The request was valid but produced no result: no plan simulated validly, or the recommended plan cannot be written as one NEMA timing; any other exception escaping a plugin is wrapped in it | 1 |
| `ConformanceError` | The result breaks the result contract; `problems` lists how | 1 |
| `PluginRegistrationError` | A plugin object is malformed, declares an incompatible `api_version` or reuses a registered name | 1 |

All derive from `PluginError`. The built-in plugin maps the core's errors onto this
contract: configuration and corridor errors (`ValueError`) become `PluginInputError`,
a run without any valid seed evaluation (`RuntimeError`) becomes `PluginRunError`.

### Progress and cancellation

`progress(done, total, label)` follows the core's callback contract: one call with
`(0, total, None)` before the first simulation, then one after each evaluated plan or
batch with the last plan's id. `done` never decreases and never exceeds `total`; it may
stop short of `total` when a search ends early. To cancel, the caller raises from its
callback: the exception propagates unchanged through the plugin and the harness
(`terasim_plugin_sdk.ProgressRelay` helps a plugin that converts errors tell a
cancellation apart).

### Determinism

The request's `seed` is pinned in the generated scenario's SUMO config and seeds the
built-in search, and every plan is simulated in a fresh SUMO process on the same
scenario. The same request therefore gives the same recommendation and the same KPIs;
only the `generated_at` timestamp of the NEMA document differs. The test suite checks
this for the example plugin, with the counts given once as a file and once as an
in-memory payload.

### Output directory layout

```text
<output_dir>/
  operation_request.json   the request as run (absolute paths)        written by run_operation
  operation_result.json    the OperationResult                         written by run_operation
  nema_plan.json           the recommended plan, terasim.nema_timing.v1
  inputs/                  materialized inputs: tmc.json for a payload snapshot,
                           plan_store/ for a NEMA plan document
  scenario/                the shared scenario: routes.rou.xml, simulation.sumocfg,
                           metadata.json and the demand generator's working files
  trials/trial_NNNN/       one directory per simulated plan: plan.json, plan.tls.xml,
                           simulation.sumocfg, sim_output/ (tripinfo.xml, edgedata.xml,
                           run.log), kpi.json
```

`signal_timing` adds `corridor_optimization_result.json` and `trials.jsonl` (and
`derived_tmc.json` when its counts come from the map's routes). The result's
`artifacts` lists what a run actually wrote.

## Registration

**Entry points.** A distribution publishes a plugin in its `pyproject.toml`:

```toml
[project.entry-points."terasim.operation_plugins"]
webster_fixed_time = "webster_fixed_time:WebsterFixedTimePlugin"
```

The entry-point name must equal the plugin's `name`. The target may be a plugin class
(instantiated without arguments), a zero-argument factory, or an instance. Installing
the distribution is all it takes: `terasim-plugin list` shows the plugin and
`run_operation("webster_fixed_time", request)` runs it.

**Explicit registration.** In-process code registers an object directly:

```python
from terasim_plugin_sdk import PluginRegistry, register

register(MyOperation())                      # the process-wide registry
registry = PluginRegistry()                  # or a registry of your own
registry.register(MyOperation(), replace=True)
```

**Duplicates and broken plugins.** Names are unique. `register()` of a taken name raises
`PluginRegistrationError` unless `replace=True`. An entry point whose name is taken
(including `signal_timing`), whose module does not import, whose name differs from the
plugin's, or whose plugin is malformed or API-incompatible is skipped with a logged
warning and recorded in `registry.load_errors`; the other plugins still load, and
`terasim-plugin list` prints the skipped ones on stderr.

**API compatibility.** A plugin declares the `API_VERSION` it was written against.
The registry accepts it when the MAJOR numbers are equal and the plugin's MINOR is not
newer than the SDK's (`is_compatible_api_version()`).

## The built-in `signal_timing` plugin

`signal_timing` maps a request onto `CorridorOptimizationConfig`, runs
`run_corridor_optimization` and returns the exported `nema_plan.json` with the KPI block
projected exactly as the service projects it (the test suite compares the two). The
current plan becomes `baseline_sheet`, `plan_data_root` + `plan_time` or
`seed_from_scenario_programs`; the weights become the `KpiConfig`.

| `params` key | Meaning |
|---|---|
| `optimizer` | `auto` (default: offsets for two or more signals, green splits for one), `exhaustive` (seed plans only), `bayes_offsets`, `bayes_splits`, `bayes_corridor` (splits, offsets and optionally the cycle) |
| `max_trials`, `patience` | Search budget in simulated plans (default 20) and early stop after that many plans without improvement (default 8) |
| `stage_mode` | Webster seed stages: `two_stage` (default) or `per_approach` |
| `search_cycle`, `cycle_min_s`, `cycle_max_s` | Let the search vary the cycle within the bounds (default off, 45 to 150 s) |
| `flow_source`, `counts_source` | Demand from the counts (`corridor_graph`, `tmc`, default) or from the map's own routes (`scenario_routes`) |

`SignalTimingPlugin().build_config(request)` returns the config it would run, without
running it.

## Building blocks for plugin authors

`terasim_plugin_sdk.toolkit` calls the same core code as the built-in optimizer, so a
third-party module simulates, scores and exports plans the same way:

| Helper | What it does |
|---|---|
| `prepare_scenario(request)` | Resolves the map and the corridor, generates the shared scenario from the counts with the core TMC pipeline (`run_tmc_pipeline`), pins seed and horizon, disables teleporting; returns a `Scenario` (`tmc_json`, `tmc`, `corridor`, `tls_ids`, `sumocfg`, `net_file`, `map`) |
| `baseline_plan(scenario, current_plan)` | The plan to measure against, from any current-plan kind (or the Webster reference), plus notes |
| `evaluate_plans(scenario, plans, progress=...)` | Simulates every plan with the core `BatchEvaluator` (one fresh SUMO process and trial directory per plan, `n_workers` at a time) and scores it with `score_moe` against the first plan; returns `PlanScore`s (`plan`, `valid`, `cost`, `metrics`, `trial_dir`) |
| `best_plan(scores)` | The valid plan with the lowest cost, the earliest on ties |
| `export_nema(scenario, plan)` | Writes `nema_plan.json` with the core NEMA exporter |
| `build_result(plugin, scenario, nema_plan=..., baseline=..., optimized=...)` | Assembles the `OperationResult`: KPI block, artifacts, unserved movements |
| `build_kpis()`, `kpis_from_corridor_result()`, `plan_timing()`, `kpi_metrics()` | The KPI block from two scores, or from a `terasim.corridor_optimization.v1` result; a plan's timing; the weight keys |
| `resolve_map()`, `ingest_map()`, `ingest_nema_plan()` | Map and plan-store access |

Plans are the core's plan objects: `terasim_city.plans.CorridorPlan` (static programs,
for example from `terasim_city.plans.build_webster_plan`) or
`terasim_city.plans.nema_plan.NemaCorridorPlan`. Plugins may use any public core API
next to these helpers.

## Conformance kit

`terasim_plugin_sdk.conformance` checks a plugin the way the harness does, from a
third party's own tests:

```python
from terasim_plugin_sdk.conformance import demo_request, run_conformance


def test_my_operation_conforms(tmp_path):
    request = demo_request("path/to/examples/corridor_demo", tmp_path / "out")
    result = run_conformance(MyOperation(), request)
    assert result.kpis["optimized"]["cost"] <= result.kpis["baseline"]["cost"]
```

`demo_request()` builds a small request on the synthetic three-signal demo corridor
(`examples/corridor_demo`: signals J1, J2, J3, corridor `i1` to `i3`, its 90 s timing
sheet as the current plan, 300 simulated seconds). `run_conformance()` checks the plugin
object, runs it through `run_operation`, and checks:

* the progress reports (first `(0, total, None)`, never backwards, never above `total`);
* the result: schema and API version; plugin name and version; a non-empty corridor;
  `nema_plan` is `terasim.nema_timing.v1` for the same corridor, with exactly one
  intersection per corridor signal, finite non-negative splits and clearances, valid
  NEMA phase numbers and a positive cycle; `kpis` has the request's weights echoed in
  `cost_weights`, finite costs, the weighted measures in both `metrics`, and an
  `optimized` cost not above the `baseline` cost (when nothing beats the current plan,
  the current plan is the recommendation); `artifacts` are inside `output_dir`, exist,
  and include the written `nema_plan.json`, identical to the result's `nema_plan`;
  `unserved_movements` names corridor signals and known movements; the result is JSON
  with finite numbers;
* that `operation_result.json` reads back to the returned result.

`validate_result(result, request)` returns the list of problems (empty when
conformant), `check_result()` raises `ConformanceError`, and `terasim-plugin check
<output_dir>` runs the same check on a result on disk.

## Writing, packaging and publishing an operations module

The example in `examples/plugins/webster_fixed_time` follows these steps; the
[walkthrough](example-plugin.md) explains its code.

**1. Create a distribution** of its own, outside the core packages:

```text
my-operation/
  pyproject.toml
  README.md
  my_operation/__init__.py
  my_operation/plugin.py
  tests/test_conformance.py
```

**2. Implement the plugin class** in `plugin.py`: `name`, `version`,
`api_version = API_VERSION`, `description`, and `run()`. Build `run()` from the toolkit
(`prepare_scenario`, `baseline_plan`, `evaluate_plans`, `best_plan`, `export_nema`,
`build_result`) and the public core API; validate `request.params` and raise
`PluginInputError` for keys or values you do not accept.

**3. Declare the dependencies and the entry point** in `pyproject.toml`:

```toml
[project]
name = "my-operation"
version = "1.0.0"
dependencies = ["terasim-plugin-sdk>=0.1.0,<0.2", "terasim-city>=0.1.0,<0.2"]

[project.entry-points."terasim.operation_plugins"]
my_operation = "my_operation:MyOperation"
```

**4. Install it next to TeraSim Metropolis** and check that the registry finds it:

```bash
pip install -e packages/terasim-plugin-sdk   # from a TeraSim Metropolis checkout
pip install -e path/to/my-operation
terasim-plugin list
```

**5. Run it** on the demo corridor or your own map:

```bash
terasim-plugin run my_operation \
    --map examples/corridor_demo/demo.sumocfg \
    --tmc examples/corridor_demo/demo_tmc.json \
    --plan examples/corridor_demo/baseline_sheet.json \
    --from i1 --to i3 --sim-end 600 --out /tmp/my_operation
```

**6. Test it with the conformance kit** (`tests/test_conformance.py`, as in the previous
section) and run `pytest`.

**7. Build and publish** the distribution like any Python package (`python -m build`,
then upload the wheel and sdist to your package index). Users install it with
`pip install my-operation` into the environment that runs TeraSim Metropolis; nothing in
the core changes.

## Command line

`terasim-plugin` is installed with the SDK (also `python -m terasim_plugin_sdk`):

```bash
terasim-plugin list [--json]
terasim-plugin run NAME --map MAP --tmc TMC --out DIR
                   [--plan PLAN [--plan-time ISO]] [--weights WEIGHTS]
                   [--from REF --to REF | --tls ID[,ID...]]
                   [--sim-end S] [--seed N] [--workers N] [--vehicle-behavior NAME]
                   [--data-root DIR] [--param KEY=VALUE ...] [--json] [--quiet]
terasim-plugin check RESULT [--request REQUEST]
```

`--map` is a `.sumocfg` file, the `map_id` of a stored map, or `auto` (the stored map
covering the corridor); `--plan` is a timing sheet or NEMA plan JSON file (told apart by
its `schema`), a plan-store directory, or `scenario` (the map's own programs);
`--weights` is `control_delay=0.7,stops=0.3` or a JSON object; `--param` values are
read as JSON when they parse (`--param max_trials=10 --param search_cycle=true`).
Progress lines go to stderr unless `--quiet`. Exit status: 0 success, 1 run failed or
result not conformant, 2 invalid request or unknown plugin.

## Proposed for a later release (not in v0.1.0)

Nothing in this section is implemented in v0.1.0. In v0.1.0 the HTTP service and the MCP
server call the built-in signal-timing optimizer directly, as described above.

### Dispatch from the HTTP service and the MCP server

* **Operation field on the optimize request.** `POST /v1/city/optimize` and
  `POST /v1/city/optimizations` accept an optional `"operation"` (a registered plugin
  name, default `signal_timing`). A request without it behaves exactly as today. The
  service builds the `OperationRequest` from what it already resolves: the map from the
  map store or the request's `base_sumocfg`, the counts from the aggregated snapshot
  queue, the current plan from the plan store (`use_stored_plans`) or the corridor's
  timing sheet, the weights from `cost.weights`, the run settings from the deployment;
  it answers with `OperationResult.response()`, the same `{nema_plan, kpis}` body.
* **Operations endpoints.** `GET /v1/city/operations` lists the registered plugins
  (name, version, API version, description) and `POST /v1/city/operations/{name}` runs
  one with plugin-specific `params`, returning the full `OperationResult`.
* **MCP tools.** A `list_operations` tool, and an optional `operation` argument on the
  optimize tools, mapped onto the endpoints above.
* **Status codes.** `PluginInputError` 422, unknown operation 404 (endpoint) or 422
  (request field), `PluginRunError` 409 when no NEMA plan could be exported and 500
  otherwise, `ConformanceError` 500.
* **Runtime.** Plugin runs reserve `n_workers` concurrency slots like optimization runs
  do; the service's progress callback keeps feeding run status and stops a run whose
  lease was lost, which the progress contract already supports.
* **Installation.** Plugins are installed into the service image with `pip` in a
  derived image (`FROM <registry>/terasim-metropolis:0.1.0`); an allow-list setting
  names the plugins the service may dispatch to, since plugins run with the service's
  privileges.

### Compatibility and versioning policy

What v0.1.0 enforces: `API_VERSION = "1.0"`, the MAJOR/MINOR compatibility check at
registration, and `api_version` recorded in every result. The proposed policy for later
releases:

* **Interface version (`API_VERSION`, `MAJOR.MINOR`).** A MINOR increase only adds:
  optional request fields with defaults, optional result fields, new helpers, new
  `params` of the built-in plugin. A MAJOR increase is reserved for removing or renaming
  fields or changing their meaning. A plugin written against `1.x` keeps loading on every
  `1.y` with `y >= x`.
* **Distribution version (`terasim-plugin-sdk`, semantic versioning).** Patch releases
  fix bugs; minor releases may raise the interface MINOR. Until 1.0.0, plugins pin the
  minor series they were tested with (`terasim-plugin-sdk>=0.1,<0.2`); from 1.0.0 on,
  `terasim-plugin-sdk>=1.N,<2`.
* **Deprecation.** A field or helper scheduled for removal stays for at least one minor
  release and emits a `DeprecationWarning` before the MAJOR increase removes it.
* **Discovery.** The operations endpoint reports the service's `API_VERSION`, so a
  caller can tell which interface a deployment offers.
