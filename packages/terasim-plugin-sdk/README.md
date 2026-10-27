# terasim-plugin-sdk

The Plugin Interface / SDK of TeraSim Metropolis. Traffic Operation plug-ins are
installable Python modules that turn a road network, a turning-movement-count (TMC)
snapshot, the current signal timing and KPI weights into a recommended NEMA timing
plan with a before/after KPI comparison, without changes to the core.

The package provides:

* the typed request and result (`OperationRequest`, `MapInput`, `TrafficSnapshot`,
  `CurrentPlan`, `KpiWeights`, `CorridorSelection`, `OperationResult`);
* the plugin protocol (`TrafficOperationPlugin`) and the interface version
  (`API_VERSION = "1.0"`);
* the registry: the built-in plugin, entry points in the `terasim.operation_plugins`
  group, explicit `register()`;
* `run_operation()`, the harness that validates a request, runs a plugin, checks its
  result and writes `operation_request.json` / `operation_result.json`;
* `terasim_plugin_sdk.toolkit`: scenario generation, plan evaluation, scoring and NEMA
  export on top of the `terasim-city` pipeline;
* the built-in `signal_timing` plugin (the core corridor signal-timing optimizer);
* the conformance kit (`terasim_plugin_sdk.conformance`) for plugin test suites;
* the `terasim-plugin` command line (`list`, `run`, `check`).

## Install

From a TeraSim Metropolis checkout, with the core packages installed:

```bash
pip install -e packages/terasim-plugin-sdk
terasim-plugin list
```

## Run a plugin

```bash
terasim-plugin run signal_timing \
    --map examples/corridor_demo/demo.sumocfg \
    --tmc examples/corridor_demo/demo_tmc.json \
    --plan examples/corridor_demo/baseline_sheet.json \
    --from i1 --to i3 --sim-end 600 --param max_trials=6 \
    --out /tmp/signal_timing_demo
```

or from Python:

```python
from terasim_plugin_sdk import (
    CorridorSelection, CurrentPlan, KpiWeights, MapInput, OperationRequest,
    TrafficSnapshot, run_operation,
)

request = OperationRequest(
    map=MapInput.from_sumocfg("examples/corridor_demo/demo.sumocfg"),
    traffic=TrafficSnapshot.from_file("examples/corridor_demo/demo_tmc.json"),
    corridor=CorridorSelection.between("i1", "i3"),
    current_plan=CurrentPlan.timing_sheet("examples/corridor_demo/baseline_sheet.json"),
    weights=KpiWeights({"control_delay": 0.7, "stops": 0.3}),
    sim_end=600,
    params={"max_trials": 6},
    output_dir="/tmp/signal_timing_demo",
)

if __name__ == "__main__":  # evaluations run in a spawn-based process pool
    result = run_operation("signal_timing", request)
    print(result.kpis["baseline"]["cost"], result.kpis["optimized"]["cost"])
```

## Documentation

* `docs/plugins/index.md` -- the interface, the lifecycle, the error contract,
  registration, the conformance kit, and how to write and publish a plugin.
* `docs/plugins/example-plugin.md` -- a walkthrough of the example plugin in
  `examples/plugins/webster_fixed_time`.

## Tests

```bash
python -m pytest tests/test_plugin_sdk
```

The end-to-end tests simulate the synthetic demo corridor and need the `sumo` binary
and `libsumo` (both come with the core's `eclipse-sumo` dependency).
