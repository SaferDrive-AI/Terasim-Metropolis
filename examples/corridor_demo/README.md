# Corridor demo — 3-intersection arterial

A small, fully self-contained demo corridor for the TeraSim corridor signal
optimization pipeline: an E-W arterial with three signalised junctions
(`J1`, `J2`, `J3`, ~300 m apart) and N-S cross streets, geo-referenced
(UTM zone 10, centred on 37.335 N, 121.900 W). Every asset is synthetic.

## Contents

| File | What it is |
|---|---|
| `build_demo_map.py` | Regenerates the network, signal, counts and sheet assets in this directory (idempotent); the three customer-style scenario files below are hand-written and it leaves them alone |
| `demo.nod.xml` / `demo.edg.xml` / `demo.con.xml` | Hand-written netconvert inputs |
| `demo.net.xml` | Generated SUMO net (netconvert `--proj.utm`), default TLS program `0` per junction |
| `demo.tls.xml` | Alternative static program `alt_fixed` (uniform 30 s greens) per junction |
| `demo.rou.xml` | Empty placeholder — real routes come from the TMC pipeline |
| `demo.sumocfg` | Base SUMO config (net + placeholder routes + tls additional) |
| `demo_tmc.json` | 1-hour TMC turning-movement counts for `i1`/`i2`/`i3` (lat/lon exactly on `J1`/`J2`/`J3`) |
| `snapshots/snap_01..06.json` | Six 10-minute TMC snapshots (same corridor, varying demand) |
| `baseline_sheet.json` | Timing sheet (`terasim.timing_sheet.v1`): 90 s cycle, offsets 0/20/40, EW 48+3+1 / NS 34+3+1 |
| `scheduled_config.yaml` | Scheduler corridor config (tls_ids J1/J2/J3, sim_end 600, max_trials 6) |
| `demo_scenario.sumocfg` | Customer-style scenario: `demo.net.xml` + `demo_routes.rou.xml` + `demo.tls.xml`, simulated 0–600 s |
| `demo_routes.rou.xml` | Its demand: ten constant-rate flows (the arterial both ways, left turns off it at `J2`, a cross street at each junction) |
| `placeholder_tmc.json` | Placeholder TMC file for `counts_source=scenario_routes`: only its corridor id and timestamp are read, the counts come from the routes |

## Rebuild the assets

Requires `netconvert`/`sumo` on PATH and `sumolib` + `terasim_city` importable:

```bash
python examples/corridor_demo/build_demo_map.py
```

The script runs its own sanity checks: sumolib net import, TLS program
presence, a 10 s headless SUMO run, and a full `run_tmc_pipeline` call that
must produce at least one flow.

## How every demo sources demand (queue-first)

All demos follow the production contract: TMC pushes periodic 10-minute
snapshots to `POST /v1/city/snapshots` (each payload carries its own
`corridor_id` — that string names the queue), and EVERY trigger mode
(on-demand agent call, scheduled cadence, incident) sources demand by
aggregating the buffered queue over the lookback window — counts are summed
and `observation_interval` becomes the actual covered seconds, so partial
windows proportionally scale up to hourly rates downstream. The per-request
`tmc_json` file path remains supported as a secondary/testing path only.

## Demo 1 — on-demand corridor optimization

Starts the TeraSim service, pushes the six 10-minute snapshots to
`/v1/city/snapshots`, then submits an optimization by
`corridor_id=demo_arterial` over the corridor `i1 → i3` (the service
aggregates the buffered last-hour queue into the demand file), polls
progress, and prints the best offsets plus the NEMA handoff table:

```bash
python examples/corridor_demo/demo_on_demand.py
```

## Demo 2 — scheduled (snapshot-driven) optimization

Starts the service with the scheduler enabled, feeds the six 10-minute
snapshots to `/v1/city/snapshots`, waits for the scheduler tick to aggregate
them and auto-submit a run, then prints the winning plan:

```bash
python examples/corridor_demo/demo_scheduled.py
```

## Demo 3 — incident-triggered (expedited) optimization

Starts the service with the corridor config loaded but the cadence scheduler
OFF, feeds the six snapshots, then POSTs the unified optimization request —
`corridor_id` plus an inline `incident` (one stalled vehicle ~40 m west of
`J2`) — to `/v1/city/optimizations`. The run is expedited via the reserved
emergency concurrency slots (`trigger=incident`); the demo verifies the best
trial's `kpi.json` actually simulated the stall on the mapped arterial edge:

```bash
python examples/corridor_demo/demo_incident.py
```

## Customer-style scenario

`demo_scenario.sumocfg` is a scenario of the kind a customer brings: a net, its own route
file and its own signal programs (`alt_fixed`: 66 s, uniform 30 s greens, offset 0 at every
junction). The optimizer can run on it as it is: demand from its routes
(`flow_source=scenario_routes`), turning counts derived from them
(`counts_source=scenario_routes`), the programs as the baseline
(`seed_from_scenario_programs`); the project documentation (`docs/`) describes these
switches. The service image carries this directory as `/data/input/demo_scenario`, and
`apps/deploy/city/phase1/scenario_e2e.py` registers it and runs such an optimization through
the REST API.

## Using the map directly from Python

```python
from terasim_city.api import run_tmc_pipeline

scenario = run_tmc_pipeline(
    "examples/corridor_demo/demo_tmc.json",
    "examples/corridor_demo/demo.sumocfg",
    "/tmp/corridor_demo_out",
    sim_end=300.0,
)
print(scenario.n_flows, scenario.sumocfg_file)
```
