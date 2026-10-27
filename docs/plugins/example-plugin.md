# Example plugin: `webster_fixed_time`

`examples/plugins/webster_fixed_time` is a complete third-party operations module in its
own Python distribution. It derives a fixed-time plan from the turning-movement counts
with Webster's method, simulates it next to the current plan on the same scenario, and
recommends whichever has the lower KPI cost, as NEMA timing with the KPI comparison.
It uses only the public Plugin Interface (`terasim_plugin_sdk`) and the public core API
(`terasim_city`), and TeraSim Metropolis finds it through its entry point. The
[Plugin Interface page](index.md) describes the contract it implements.

## Files

| File | What it is |
|---|---|
| `webster_fixed_time/plugin.py` | The plugin class: identity attributes and `run()` |
| `webster_fixed_time/__init__.py` | Exports `WebsterFixedTimePlugin` and `PARAMS` |
| `pyproject.toml` | Distribution metadata, dependencies and the `terasim.operation_plugins` entry point |
| `tests/test_conformance.py` | The SDK conformance kit run against the synthetic demo corridor |
| `README.md` | Install, run and test commands |

## The code

### Identity

```python
class WebsterFixedTimePlugin:
    name = "webster_fixed_time"      # registry name = entry-point name
    version = "0.1.0"
    api_version = API_VERSION        # the interface version it was written against ("1.0")
    description = "Webster fixed-time plan from the TMC counts, recommended when it beats the current plan"
```

### `run`, step by step

```python
def run(self, request, progress=None):
    options = self._options(request.params)

    # 1. One scenario for every candidate: the map plus demand built from the counts.
    scenario = prepare_scenario(request)

    # 2. The candidates: the current plan (the baseline) and the Webster plan.
    current, notes = baseline_plan(scenario, request.current_plan)
    try:
        webster = build_webster_plan(scenario.tmc_json, scenario.corridor, scenario.net_file, **options)
    except ValueError as exc:
        raise PluginInputError(f"cannot derive a Webster plan: {exc}") from exc

    # 3. Simulate both; the lower KPI cost wins (the baseline keeps a tie).
    scores = evaluate_plans(scenario, [current, webster], progress=progress)
    winner = best_plan(scores)
    if winner is scores[0]:
        notes += ("the Webster plan does not improve on the baseline, which stays the recommendation",)

    # 4. Hand off the winner as NEMA timing, with the KPI comparison.
    nema_plan = export_nema(scenario, winner.plan)
    return build_result(self, scenario, nema_plan=nema_plan, baseline=scores[0],
                        optimized=winner, notes=notes, evaluated=scores,
                        details={"webster": dict(webster.provenance.params)})
```

1. `prepare_scenario` resolves the map and the corridor (here `i1` to `i3`: signals J1,
   J2, J3), generates the demand from the counts with the core TMC pipeline into
   `<output_dir>/scenario/`, and pins the request's seed and horizon. Every candidate is
   simulated on this one scenario, so the comparison is like for like.
2. `baseline_plan` turns the request's current plan into a core plan object: a timing
   sheet, a NEMA plan document, a plan store, the map's own programs, or, when no
   current plan is given, the Webster reference. The candidate comes from the core's
   `build_webster_plan`: Webster's optimum cycle `C0 = (1.5 L + 5) / (1 - Y)` from the
   hourly flow ratios, one corridor cycle (the longest intersection cycle), green splits
   in proportion to the stage flow ratios, offsets 0.
3. `evaluate_plans` simulates both plans in parallel SUMO processes with the core
   `BatchEvaluator` (one `trials/trial_NNNN/` directory each) and scores them with the
   core `score_moe` against the first plan, under the request's KPI weights. It reports
   progress before the first simulation and after each batch; a progress exception
   propagates unchanged. `best_plan` keeps the lower cost, the baseline on a tie.
4. `export_nema` writes the winner to `<output_dir>/nema_plan.json` with the core
   NEMA exporter, and `build_result` assembles the `OperationResult`: the KPI block in
   the service's shape, the artifacts, the movements the plan never serves, and which
   plans were compared (`details["evaluations"]`).

### Options

`request.params` accepts `stage_mode` (`two_stage`: an east-west and a north-south
stage, the default; `per_approach`: one stage per approach), `cycle_min_s` and
`cycle_max_s` (cycle bounds, default 45 s and 150 s). `_options` rejects any other key,
and non-numeric bounds, with `PluginInputError`, so a typo fails before anything is
simulated.

### Registration

```toml
[project]
name = "terasim-webster-fixed-time"
version = "0.1.0"
dependencies = ["terasim-plugin-sdk>=0.1.0,<0.2", "terasim-city>=0.1.0,<0.2"]

[project.entry-points."terasim.operation_plugins"]
webster_fixed_time = "webster_fixed_time:WebsterFixedTimePlugin"
```

Installing the distribution registers the plugin; no file of the core changes.

## Install

From a TeraSim Metropolis checkout, with the core packages installed:

```bash
pip install -e packages/terasim-plugin-sdk
pip install -e examples/plugins/webster_fixed_time
terasim-plugin list
```

```text
NAME                VERSION  API  SOURCE                                          DESCRIPTION
signal_timing       0.1.0    1.0  builtin (terasim-plugin-sdk)                    Corridor signal-timing optimization (offsets, green splits, cycle) with the core optimizer
webster_fixed_time  0.1.0    1.0  entry_point (terasim-webster-fixed-time 0.1.0)  Webster fixed-time plan from the TMC counts, recommended when it beats the current plan
```

## Run

On the synthetic demo corridor, with its 90 s timing sheet as the current plan:

```bash
terasim-plugin run webster_fixed_time \
    --map examples/corridor_demo/demo.sumocfg \
    --tmc examples/corridor_demo/demo_tmc.json \
    --plan examples/corridor_demo/baseline_sheet.json \
    --from i1 --to i3 --sim-end 600 \
    --out /tmp/webster_demo
```

```text
progress 0/2
progress 2/2 (webster_init)
webster_fixed_time 0.1.0 on corridor demo_arterial (J1, J2, J3)
  baseline  cost 1.0000  control_delay_s_per_veh 29.023  stops_per_veh 0.939
  optimized cost 0.8104  control_delay_s_per_veh 20.900  stops_per_veh 0.959
  NEMA plan: /tmp/webster_demo/nema_plan.json (handoff ready: no)
  result:    /tmp/webster_demo/operation_result.json
```

On this demand the 45 s Webster plan lowers the weighted cost by about 19 % against the
90 s sheet, mostly through control delay (29.0 to 20.9 s per vehicle). The recommended
plan's stages come from the counts and carry no NEMA phase numbers, so the export lists
`nema_phase: null` per stage and `handoff_ready: false`: a traffic engineer numbers the
phases before deployment. When the timing sheet stays the recommendation, the export
keeps the sheet's phase numbers and is handoff-ready.

The output directory:

```text
/tmp/webster_demo/
  operation_request.json   the request as run
  operation_result.json    the result: KPIs, notes, details, artifacts
  nema_plan.json           the recommended plan (terasim.nema_timing.v1)
  scenario/                the generated demand and SUMO config
  trials/trial_0000/       the timing sheet's simulation
  trials/trial_0001/       the Webster plan's simulation
```

`terasim-plugin check /tmp/webster_demo` re-checks the result against the contract.

Other inputs work the same way: `--plan scenario` uses the map's own signal programs,
`--plan plan.json` a NEMA plan document (`--plan-time` picks its time-of-day pattern),
no `--plan` measures against the Webster reference itself (with the default options the
two plans coincide and the baseline stays), `--weights control_delay=1` scores delay
only, and `--param stage_mode=per_approach` gives every approach its own stage.

## Use it from Python

```python
from terasim_plugin_sdk import run_operation
from terasim_plugin_sdk.conformance import demo_request

if __name__ == "__main__":  # evaluations run in a spawn-based process pool
    request = demo_request("examples/corridor_demo", "/tmp/webster_demo", sim_end=600)
    result = run_operation("webster_fixed_time", request)
    print(result.kpis["optimized"]["cost"], result.details["recommended_plan_id"])
    print(result.response())   # the {nema_plan, kpis} body of the service's optimize endpoint
```

## Test

The example's own test runs the SDK conformance kit, the way a third party tests a
plugin:

```bash
pip install -e 'examples/plugins/webster_fixed_time[test]'
pytest examples/plugins/webster_fixed_time
```

The SDK's suite runs this test too, together with the example end to end (file and
in-memory counts give identical results), its parameter checks, cancellation, and the
command line:

```bash
python -m pytest tests/test_plugin_sdk
```

Both need the `sumo` binary and `libsumo`, which come with the core's `eclipse-sumo`
dependency.

## Make it your own

1. Copy the directory, rename the package and set `name`, `version` and `description`.
2. Replace step 2 with your module's candidates: any `terasim_city.plans.CorridorPlan`
   (static programs) or `NemaCorridorPlan` (dual-ring NEMA timing); several candidates
   are fine, `evaluate_plans` runs them `n_workers` at a time.
3. Keep steps 1, 3 and 4, document your `params` and reject unknown keys.
4. Update the entry point in `pyproject.toml`, install, and run the conformance test.
