# End-to-end acceptance check

`run_acceptance.py` runs the Phase 1 acceptance chain on synthetic data, through the public
interfaces only (MCP tools, HTTP API, TMC collector), and writes a report with one row per
criterion:

| ID | What is checked |
| --- | --- |
| ENV-1 | Python, release packages, SUMO 1.23.1 and Redis are available |
| A3-1 | Service, MCP server and TMC collector start; the MCP server exposes its 9 tools |
| A4-1 | Map upload tool registers a SUMO map (3 signals) |
| A4-2 | OpenDRIVE map converted on upload: geo-referenced, 3 signalized junctions, fully connected |
| A4-3 | OpenDRIVE without geo-reference refused with guidance, accepted with a `proj` override |
| A4-4 | An optimization runs on the converted HD map and returns NEMA + KPIs |
| C1-1 | Timing-plan upload tool stores the corridor's current NEMA plan |
| TMC-1 | TMC collector pulls VSS behavior objects for registered intersections and injects snapshots |
| OD-1 | On-Demand mode: one optimization tool call returns NEMA plan, KPIs, summary and download links |
| B1-1, B2-1 | Control delay and stops per vehicle for the current and the optimized plan |
| B-3 | Weighted cost formula, echoed weights, city-defined weights honored |
| B-4 | Simulated signal metrics are produced for a completed run |
| C1-2 | The uploaded current plan is the baseline and the seed of the search |
| C2-1 | Candidates simulated for the configured horizon (default 1,800 s) and the best plan selected |
| C3-1 | The exported plan coordinates every corridor signal (common cycle, offsets) |
| C3-2 | An eight-signal corridor with 32 cameras from one camera inventory (`apps/deploy/city/camera-config/examples/arterial_32.yaml`): the generated TMC spec is accepted and one optimization coordinates all eight signals |
| C4-1 | NEMA timing plan and PDF timing report are served for the run |
| SCH-1 | Scheduled mode: the same tool invoked on a fixed cadence returns NEMA + KPIs on the latest data |
| SCH-2 | Optional built-in scheduler completes a scheduled run |
| A2-1, A2-2, A2-3 | Adversarial environment injects intersection adversities; only the configured adversity types fire; every adversity parameter file resolves to its class |
| A1-1 | NADE behavior drives the background traffic; the Neural NDE behavior slot is registered |
| A1-2 | Deployment settings switch on the Neural NDE re-evaluation stage; an optimization on a stored map runs it and returns NEMA + KPIs |
| A1-3 | `--neural-nde` profile only: with the Neural NDE runtime and checkpoint installed, the re-evaluation runs on the GPU and the exported plan comes from it |

## Inputs (all synthetic)

- Map: the demo corridor in `examples/corridor_demo` (three signals on a generated arterial),
  plus an OpenDRIVE export of it generated with `netconvert` at run time.
- Traffic state: a TMC spec for the three intersections and VSS behavior objects served by a
  local stand-in for the VSS behavior API, with demand taken from the demo corridor's
  snapshots (observation window 1 minute so the run stays short).
- Current timing plan: a coordinated 90 s, four-phase NEMA plan for the three signals.
- A synthetic eight-signal corridor with 32 cameras (camera inventory, OpenStreetMap extract
  and road graph under `apps/deploy/city/camera-config/examples/`).

## Run locally

Requirements: Python 3.10-3.12 with the release packages installed, SUMO 1.23.1 (installed
with the packages) and `redis-server` on `PATH`.

```bash
python examples/acceptance/run_acceptance.py --out /tmp/terasim-acceptance
```

Useful options: `--sim-end` (simulated seconds per candidate, default 1800),
`--max-trials` (default 24), `--n-workers` (default 4), `--scheduled-invocations`
(default 2), `--cadence-s` (default 60), `--no-builtin-scheduler`.

Neural NDE profile, on a machine with a CUDA GPU and the Neural NDE runtime module and
checkpoint installed (not part of this release):

```bash
python examples/acceptance/run_acceptance.py --neural-nde --out /tmp/terasim-acceptance-nnde
```

A full run with the default settings takes about seven minutes on a 16-core workstation. The report is written to
`<out>/acceptance_report.md` and `<out>/acceptance_report.json`; service logs and the
generated inputs stay under `<out>/work/`. The exit code is 0 only when every criterion
passed.

## Run against a deployment

Start the Docker Compose stack with its offline VSS behavior stub (see
`apps/deploy/city/phase1/compose.yml`), then:

```bash
python examples/acceptance/run_acceptance.py --remote \
    --service http://127.0.0.1:8100 --mcp http://127.0.0.1:9911/mcp --tmc http://127.0.0.1:8200
```

Remote mode uses the map built into the image (`/data/input/map/demo.sumocfg`) and the
deployment's own TMC spec and VSS behavior source. It registers and optimizes one intersection
(`i2`): a corridor needs all of its intersections registered before the collector's first
observation window, which local mode does by preparing the collector's registry. Corridor
coordination (C3-1, C3-2), the checks that need files on the service's asset volume (A4-2,
A4-3, A4-4, B-3) and the re-evaluation stage, a setting of the service (A1-2), are therefore
reported as skipped in remote mode; run local mode for them.
