# Acceptance test

`examples/acceptance/run_acceptance.py` runs the acceptance chain end to end, the way a VSS agent
drives it, through the public interfaces only (MCP tools, the HTTP API, the TMC collector), and
reports one pass/fail line per acceptance criterion with the measured evidence:

```text
input data
 → bring-up: map and timing-plan upload, OpenDRIVE conversion
 → TMC collector pulls VSS behavior objects, injects snapshots
 → On-Demand: one optimization tool call
 → Scheduled: the same tool on a fixed cadence (and the
   built-in scheduler)
 → KPIs, signal measures, NEMA timing plan, PDF timing report
 → an eight-signal corridor with 32 cameras from one camera inventory
 → a short adversarial-environment run on the demo corridor
 → an optimization with the Neural NDE re-evaluation stage switched on
```

Every input is synthetic: the [demo corridor](synthetic-corridor.md), a generated TMC
specification, generated VSS behavior objects (served by a small local stand-in for the VSS
behavior API), a generated NEMA timing plan, and the synthetic 32-camera corridor of the
[camera inventory examples](../deployment/corridor-configuration.md).

## Requirements

- Python 3.10 to 3.12 with the release packages installed (`terasim`, `terasim-nde-nade`,
  `terasim-city`, `terasim-service`, `terasim-agent-skill[mcp,report]`,
  `terasim-tmc-service`); see [Installation](../getting-started/installation.md).
- SUMO 1.23.1, from the `eclipse-sumo` wheel installed with the packages.
- `redis-server` on `PATH` (local mode).

## Local mode

Starts `redis-server`, `terasim-service`, the MCP server, the TMC collector and the behavior
stand-in on free localhost ports from the installed packages, runs every criterion and stops
everything again:

```bash
python examples/acceptance/run_acceptance.py --out /tmp/terasim-acceptance
```

With the contract settings (1,800 s simulated per candidate) the run takes a while. For a quicker
check, lower the horizon and the budget, for example `--sim-end 600 --max-trials 6`.

## Remote mode

Checks an existing deployment that runs the behavior stand-in, for example the
[Docker Compose](../deployment/docker-compose.md) stack with the `stub` profile, on the map baked
into the image:

```bash
python examples/acceptance/run_acceptance.py --remote \
  --service http://127.0.0.1:8100 --mcp http://127.0.0.1:9911/mcp --tmc http://127.0.0.1:8200
```

Remote mode registers and optimizes one intersection (`i2`) with the deployment's own TMC
specification and behavior source (a corridor needs all of its intersections registered before
the collector's first observation window). Reported as skipped in remote mode:

- `C3-1`, `C3-2`: corridor coordination;
- `A4-2`, `A4-3`, `A4-4`, `B-3`: need files on the service's asset volume;
- `A1-2`: the re-evaluation stage, a setting of the service.

`SCH-2` runs when the deployment has its built-in scheduler on.

## Neural NDE profile

Needs a CUDA GPU and the Neural NDE runtime module and checkpoint (both installed separately, see
[Behavior models](../developer-guide/behavior-models.md)).

```bash
python examples/acceptance/run_acceptance.py --neural-nde --out /tmp/terasim-acceptance-nnde
```

`--neural-nde` checks the model itself:

1. environment (runtime importable, CUDA device, checkpoint present) and service health;
2. `A1-2`, `A1-3`: one optimization of the demo corridor in which the current plan and the two
   best candidates are re-evaluated under Neural NDE;
3. `A1-4`: an `optimize_corridor` tool call with `traffic_model="cnde"`, in which every candidate
   runs under Neural NDE, followed by the same call without it.

The corridor's map in this profile is an OpenStreetMap version of the demo corridor, written at
run time and stored through the map upload tool, so the model's map artifacts are generated in
the map store. For `A1-4` the stored package, with its artifacts, is placed under the input root
the way a deployment mounts one, and the corridor's entry points at it with a budget of six
trials and four workers. The deployment limits `TERASIM_CITY_CNDE_MAX_TRIALS` and
`TERASIM_CITY_CNDE_MAX_WORKERS` cut the Neural NDE run down to `--cnde-max-trials` and
`--cnde-max-workers`; the SUMO run keeps the entry's budget.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--out DIR` | `acceptance-out-<timestamp>` | Output directory (must be new or empty) |
| `--sim-end S` | `1800` | Simulated seconds per candidate plan |
| `--max-trials N` | `24` | Candidate plans per optimization (the deployment default) |
| `--n-workers N` | `4` | Parallel SUMO processes per run |
| `--scheduled-invocations N` | `2` | Invocations of the Scheduled mode |
| `--cadence-s S` | `60` | Cadence of the caller-driven Scheduled mode |
| `--builtin-scheduler`, `--no-builtin-scheduler` | on | Also check the optional built-in scheduler (local mode) |
| `--builtin-cadence-s S` | `60` | Cadence of the built-in scheduler check |
| `--tool-timeout-s S` | `1800` | Timeout of one MCP tool call |
| `--sim-metrics-timeout-s S` | `600` | How long to wait for the simulated signal measures |
| `--remote` | off | Remote mode |
| `--service`, `--mcp`, `--tmc` | `http://127.0.0.1:8100`, `http://127.0.0.1:9911/mcp`, `http://127.0.0.1:8200` | Remote mode: the deployment's URLs |
| `--map-path` | `/data/input/map/demo.sumocfg` | Remote mode: the map file on the service's volume |
| `--neural-nde` | off | Neural NDE profile (local mode; needs the runtime module, its checkpoint and a CUDA GPU) |
| `--refine-top-k N` | `2` | Candidates re-evaluated by the Neural NDE stage (`A1-2`, `A1-3`) |
| `--nnde-sim-end S` | `600` with `--neural-nde`, else `300` | Simulated seconds per plan of the `A1-2` / `A1-3` run |
| `--nnde-max-trials N` | `6` with `--neural-nde`, else `4` | Candidate plans of the search in the `A1-2` / `A1-3` run |
| `--cnde-sim-end S` | `300` | Simulated seconds per candidate of the `traffic_model="cnde"` call (`A1-4`) |
| `--cnde-max-trials N` | `2` | `TERASIM_CITY_CNDE_MAX_TRIALS` of the `A1-4` deployment |
| `--cnde-max-workers N` | `2` | `TERASIM_CITY_CNDE_MAX_WORKERS` of the `A1-4` deployment |
| `--cnde-fast` | off | Neural NDE fast mode for `A1-2` to `A1-4` (`TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY=1`, two math-library threads per trial) |

## Criteria

| ID | Component | Criterion |
| --- | --- | --- |
| `ENV-1` | Runtime | Python, release packages, SUMO 1.23.1 and Redis are available |
| `A3-1` | A3 Simulation core | Service, MCP server and TMC collector start and report healthy; MCP exposes the 9 tools |
| `A4-1` | A4 Map adapter | Map upload skill registers a SUMO map from the asset volume (3 signals found) |
| `A4-2` | A4 Map adapter | OpenDRIVE map is converted on upload: geo-referenced, 3 signalized junctions, fully connected |
| `A4-3` | A4 Map adapter | OpenDRIVE without geo-reference is refused with guidance and accepted with a proj override |
| `A4-4` | A4 Map adapter | The converted HD map feeds the simulation core: an optimization runs on it and returns NEMA + KPIs |
| `C1-1` | C1 Baseline | Timing-plan upload skill stores the corridor's current NEMA plan for every intersection |
| `TMC-1` | Traffic state | TMC collector pulls VSS behavior objects for registered intersections and injects snapshots |
| `OD-1` | On-Demand mode | One optimization tool call returns a NEMA plan, KPIs, a ready-to-show summary and download links |
| `B1-1` | B1 Control delay | Control delay (s/veh) reported for the current and the optimized plan |
| `B2-1` | B2 Stops | Stops per vehicle reported for the current and the optimized plan |
| `B-3` | B KPI weights | Weighted cost = sum(w * metric / baseline metric); weights echoed; city-defined weights honored |
| `B-4` | B KPI menu | Simulated signal metrics (per-approach measures) are produced for a completed run |
| `C1-2` | C1 Baseline | The uploaded current plan is the baseline and the seed of the candidate search |
| `C2-1` | C2 Optimization | Candidates simulated for the configured horizon (1,800 s), scored on the KPIs, best plan selected |
| `C3-1` | C3 Coordination | The exported plan coordinates every corridor intersection (common cycle, per-signal offsets) |
| `C3-2` | C3 Coordination | An eight-signal corridor with 32 cameras, defined by one camera inventory: the generated TMC spec is accepted and one optimization coordinates all eight signals |
| `C4-1` | C4 NEMA output | NEMA timing plan (terasim.nema_timing.v1) and PDF timing report are served for the run |
| `SCH-1` | Scheduled mode | The same optimization tool invoked on a fixed cadence: every invocation returns NEMA + KPIs on the latest data |
| `SCH-2` | Scheduled mode | Optional built-in scheduler: a scheduler-triggered run completes with NEMA + KPIs |
| `A2-1` | A2 Adversarial environment | Parameterized intersection adversities inject safety-critical events on the corridor |
| `A2-2` | A2 Adversarial environment | Adversities are selected by configuration: only the configured types fire, none configured none fire |
| `A2-3` | A2 Adversarial environment | Every adversity parameter file in `configs/adversities` resolves to an adversity class |
| `A1-1` | A1 Behavior models | NADE naturalistic behavior drives the background traffic; the Neural NDE behavior slot is registered |
| `A1-2` | A1 Behavior models | Neural NDE inference interface: deployment settings switch on the re-evaluation stage (behavior `cnde`); an optimization on a stored map runs it and returns NEMA + KPIs (without the runtime module: the search winner, `export_source: "coarse"`) |
| `A1-3` | A1 Behavior models | Neural NDE profile only: with the runtime and checkpoint installed, the current plan and the top candidates are re-evaluated under Neural NDE on the GPU and the exported plan comes from that stage (`export_source: "refine"`) |
| `A1-4` | A1 Behavior models | Neural NDE profile only: an optimize tool call with `traffic_model="cnde"` simulates every candidate plan under Neural NDE on the GPU, within the deployment's limits, and is answered and recorded as a Neural NDE run; the same call without it keeps SUMO traffic |

## Results

The script prints one `[PASS]`, `[FAIL]` or `[SKIP]` line per criterion and writes into `--out`:

| File | Content |
| --- | --- |
| `acceptance_report.md` | The result, the mode and the simulated horizon, then a table of every criterion with its status and evidence |
| `acceptance_report.json` | Schema `terasim.acceptance_report.v1`: start and end time, duration, counts per status, the overall `result` and, per criterion, its status, evidence, detail and duration |

The exit code is 0 only when no criterion failed.
