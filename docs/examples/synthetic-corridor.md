# Synthetic corridor

All examples, tests and the acceptance run use one fully synthetic corridor, in
`examples/corridor_demo`. It needs no camera data and no network access.

```text
             J1_N2         J2_N2         J3_N2
               │             │             │
             J1_N1         J2_N1         J3_N1
               │             │             │
 W2 ── W1 ──── J1 ──────── J2 ──────── J3 ──── E1 ── E2
               │   ~300 m    │   ~300 m    │
             J1_S1         J2_S1         J3_S1
               │             │             │
             J1_S2         J2_S2         J3_S2
```

| | |
| --- | --- |
| Corridor id | `demo_arterial` |
| Signals | `J1`, `J2`, `J3`, about 300 m apart on an east-west arterial with north-south cross streets |
| TMC intersections | `i1` (37.335, -121.90339), `i2` (37.335, -121.9), `i3` (37.335, -121.89661), exactly on the signals |
| Projection | UTM zone 10 (the network is geo-referenced, so coordinates bind to the signals) |

## Files

| File | Content |
| --- | --- |
| `demo.nod.xml`, `demo.edg.xml`, `demo.con.xml` | The netconvert inputs of the network |
| `demo.net.xml` | The SUMO network (default program `0` per signal) |
| `demo.tls.xml` | Alternative static program `alt_fixed` per signal (30 s greens, 66 s cycle) |
| `demo.rou.xml` | Empty route file: the demand comes from the TMC counts |
| `demo.sumocfg` | The map as the service uses it: network, empty routes, signal programs |
| `demo_tmc.json` | One hour of turning-movement counts for `i1`, `i2`, `i3` |
| `snapshots/snap_01.json` … `snap_06.json` | Six 10-minute snapshots of the corridor with varying demand |
| `baseline_sheet.json` | Timing sheet: 90 s cycle, offsets 0 / 20 / 40 s, east-west 48 + 3 + 1 s, north-south 34 + 3 + 1 s |
| `baseline_sheet_uncoordinated.json` | The same plan with every offset at 0 |
| `scheduled_config.yaml` | Corridor configuration entry for `demo_arterial` (signals J1 to J3, the uncoordinated sheet as current plan, 6 trials of 600 s, 2 workers) |
| `demo_scenario.sumocfg`, `demo_routes.rou.xml` | A complete SUMO scenario on the same network, with its own constant-rate flows and the `alt_fixed` programs, simulated 0 to 600 s |
| `placeholder_tmc.json` | Placeholder demand file for runs whose counts come from the scenario's own routes |
| `demo_on_demand.py`, `demo_scheduled.py`, `demo_incident.py` | Demo scripts, see [On-Demand mode](on-demand.md) and [Scheduled mode](scheduled.md) |
| `build_demo_map.py` | Regenerates the network, signal, count and sheet files and checks them |

Regenerate the generated assets (needs `netconvert` and `sumo`, which the SUMO wheels provide):

```bash
python examples/corridor_demo/build_demo_map.py
```

The script checks its output: the network loads in `sumolib`, every signal has its programs, a
10 s SUMO run succeeds and `run_tmc_pipeline` produces flows.

## Where the corridor is used

- **Container image**: `demo.sumocfg`, `demo.net.xml`, `demo.tls.xml` and `demo.rou.xml` are baked
  at `/data/input/map`; the complete scenario at `/data/input/demo_scenario`.
- **Deployment assets**: `apps/deploy/city/phase1/scheduled_config.yaml` and `Metadata.json` (and
  the copies in the Helm chart's `configs/`) configure `demo_arterial` on the baked map and
  describe its twelve cameras for the TMC collector. The compose `stub` profile serves synthetic
  behavior objects for these cameras.
- **Acceptance run** and **tests**: `examples/acceptance/run_acceptance.py` and the fixtures in
  `tests/fixtures/`.

## Optimizing a complete SUMO scenario

A calibrated SUMO scenario (network, route files and its own signal programs) can be optimized as
it is; `demo_scenario.sumocfg` is one. The asynchronous API takes the switches that make the
scenario the source of everything:

```bash
curl -s -X POST http://localhost:8000/v1/city/optimizations -H 'Content-Type: application/json' -d "{
  \"tmc_json\": \"$PWD/examples/corridor_demo/placeholder_tmc.json\",
  \"base_sumocfg\": \"$PWD/examples/corridor_demo/demo_scenario.sumocfg\",
  \"corridor\": {\"tls_ids\": [\"J1\", \"J2\", \"J3\"]},
  \"flow_source\": \"scenario_routes\",
  \"counts_source\": \"scenario_routes\",
  \"seed_from_scenario_programs\": true,
  \"stage_mode\": \"per_approach\",
  \"optimizer\": \"bayes_corridor\",
  \"search_cycle\": true, \"cycle_min_s\": 45, \"cycle_max_s\": 180,
  \"max_trials\": 6, \"patience\": 4, \"n_workers\": 2, \"sim_end\": 600
}"
```

| Switch | Effect |
| --- | --- |
| `flow_source: scenario_routes` | The simulated demand is the scenario's own route files, unchanged |
| `counts_source: scenario_routes` | The turning counts that size the seed plans are derived from those routes (within `[0, sim_end)`); the TMC file is only a placeholder |
| `seed_from_scenario_programs: true` | The scenario's own signal programs are the baseline (unless uploaded NEMA plans cover the corridor) |
| `stage_mode: per_approach` | The Webster seed gives every approach its own protected stage (`two_stage`: one east-west and one north-south stage) |
| `optimizer: bayes_corridor` | Joint search over every signal's splits and offsets |
| `search_cycle`, `cycle_min_s`, `cycle_max_s` | The search also varies the common cycle within the bounds |

The answer is `{"run_id": ..., "status": "submitted"}`; follow the run with
`/v1/city/optimizations/{run_id}/status`, then read `/result` and `/nema`. In the result,
`seed.source` is `sumo_tls` (the scenario's programs), `config.tmc_json` points at the
`derived_tmc.json` the counts were written to, and `search` and `nema_export` report the search
and the export.

The same switches can be set in a corridor configuration entry. The synchronous call replaces
the entry's corridor with its target, so corridors that derive their counts from scenario routes
are optimized through the asynchronous API or the built-in scheduler.

Inside the image, `apps/deploy/city/phase1/scenario_e2e.py` runs exactly this request on the
baked scenario and checks the result; see [Docker Compose](../deployment/docker-compose.md#built-in-checks).
