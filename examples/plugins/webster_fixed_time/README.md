# webster_fixed_time: an example Traffic Operation plug-in

A minimal third-party operations module for TeraSim Metropolis, built on the
Plugin Interface (`terasim-plugin-sdk`). It derives a fixed-time plan from the
turning-movement counts with Webster's method, simulates it next to the current
plan on the same scenario, and recommends whichever has the lower KPI cost, as a
NEMA timing plan with the KPI comparison.

It uses only public APIs (`terasim_plugin_sdk`, `terasim_city.plans.build_webster_plan`)
and is discovered through its entry point; the core is not modified.

| File | What it is |
|---|---|
| `webster_fixed_time/plugin.py` | The plugin class (`name`, `version`, `api_version`, `run`) |
| `pyproject.toml` | Package metadata and the `terasim.operation_plugins` entry point |
| `tests/test_conformance.py` | The SDK conformance kit run against the synthetic demo corridor |

## Install

From a TeraSim Metropolis checkout, with the core packages installed:

```bash
pip install -e packages/terasim-plugin-sdk
pip install -e examples/plugins/webster_fixed_time
terasim-plugin list        # shows signal_timing (built in) and webster_fixed_time
```

## Run

```bash
terasim-plugin run webster_fixed_time \
    --map examples/corridor_demo/demo.sumocfg \
    --tmc examples/corridor_demo/demo_tmc.json \
    --plan examples/corridor_demo/baseline_sheet.json \
    --from i1 --to i3 --sim-end 600 \
    --out /tmp/webster_demo
```

The output directory holds `nema_plan.json` (the recommended plan,
`terasim.nema_timing.v1`), `operation_result.json` (the full result with the KPI
comparison), the generated `scenario/` and one `trials/trial_NNNN/` directory per
simulated plan.

Options (`--param KEY=VALUE`): `stage_mode` (`two_stage` or `per_approach`),
`cycle_min_s`, `cycle_max_s`.

## Test

```bash
pip install -e 'examples/plugins/webster_fixed_time[test]'
pytest examples/plugins/webster_fixed_time
```

The test needs the `sumo` binary (installed with the core's `eclipse-sumo`
dependency) and finds the demo corridor of the checkout; elsewhere, set
`TERASIM_CORRIDOR_DEMO` to an `examples/corridor_demo` directory.

The walkthrough in `docs/plugins/example-plugin.md` explains the code step by step.
