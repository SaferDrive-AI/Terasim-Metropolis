# TeraSim Metropolis

TeraSim Metropolis turns the turning-movement counts that NVIDIA Metropolis VSS (Video Search
and Summarization) derives from intersection cameras into better traffic signal timing.

This site documents release **v0.1.0**: the Python packages, the container image
`terasim-metropolis:0.1.0`, the Helm chart `terasim-phase1` (chart version 0.6.0) and the
synthetic examples that exercise the whole chain.

## What it does

```text
VSS cameras
   │  turning-movement counts, every observation window
   ▼
simulated corridor: the map, driven by the observed demand
   │  candidate timing plans, seeded from the current plan
   ▼
best plan ──► NEMA timing plan + KPI comparison + timing report (PDF)
```

- **Input**: turning-movement counts (TMC) per intersection, approach and turn, produced every
  observation window from VSS behavior objects; the corridor map (OpenDRIVE, Lanelet2,
  OpenStreetMap or a SUMO scenario); optionally the city's current timing plan.
- **Processing**: candidate plans seeded from the city's current plan (or a Webster plan computed
  from the counts), refined by a simulation-based Bayesian search over offsets along a corridor,
  green splits at a single intersection and optionally the cycle length. Each candidate is
  simulated in SUMO (1,800 s by default) and scored on control delay and stops.
- **Output**: a coordinated NEMA timing plan (`terasim.nema_timing.v1`), the KPIs of the current
  and the optimized plan, simulated signal-performance measures and a timing report PDF.

## Contract modules

| Module | Scope | Package |
| --- | --- | --- |
| **A1** Behavior models | Naturalistic driving behavior (NDE/NADE) for background traffic; the Neural NDE inference interface (`vehicle_behavior="cnde"`), Neural NDE traffic for every candidate of one request (`traffic_model="cnde"`) and an optional GPU re-evaluation of the best candidate plans (runtime module and checkpoint installed separately) | `terasim-nde-nade`, `terasim-city` |
| **A2** Adversarial driving environment | Parameterized adversities (cut-ins, red-light running, jaywalking, stalled objects, ...) with their parameter files in `configs/adversities/` | `terasim-nde-nade` |
| **A3** Simulation core | `Simulator`, environments, Python API and the configuration system | `terasim` |
| **A4** HD map adapter | OpenDRIVE, Lanelet2, OpenStreetMap and SUMO maps into one map store, with a projection override | `terasim-city` |
| **B** KPIs | Control delay, stops per vehicle, weighted cost; simulated signal-performance measures | `terasim-city` |
| **C** Signal optimization plug-in | Seeds from the current plan or Webster, simulation-based Bayesian search, corridor coordination, NEMA output and PDF report | `terasim-city`, `terasim-plugin-sdk` |

## How it fits NVIDIA Metropolis VSS

One container image runs next to a VSS deployment with three commands:

| Command | Port | Role |
| --- | --- | --- |
| `terasim-agent-skill` | 9911 | MCP server (`/mcp`) whose tools the VSS agent calls: register an intersection, upload a map or a timing plan, optimize an intersection or a corridor. Result files (PDF report, NEMA plan) at `/files` |
| `terasim-tmc-service` | 8200 | TMC collector: every observation window, pulls the VSS behavior objects of the registered intersections' cameras, counts the turning movements with the NVIDIA reference counting functions and pushes the snapshot to the service |
| `terasim-service` | 8000 | REST service (`/v1/city/*`): buffers the snapshots, stores maps and plans, runs the optimization. Redis (snapshot buffer, job records) runs inside the image by default |

Both operating modes call the same optimization tool: **On-Demand**, when the VSS agent calls it
on an operator's request, and **Scheduled**, when the caller invokes it on its own cadence. An
optional built-in scheduler can also run it periodically. See [Architecture](architecture.md).

## Quick links

<div class="grid cards" markdown>

- **Try it**

    [Installation](getting-started/installation.md) and the
    [Quickstart](getting-started/quickstart.md) on the synthetic three-signal corridor, in both
    operating modes.

- **Deploy it**

    [Docker Compose](deployment/docker-compose.md),
    [Helm on OpenShift](deployment/helm-openshift.md) and the
    [VSS integration](deployment/vss-integration.md) steps.

- **Integrate with it**

    The [HTTP API](reference/http-api.md), the [MCP tools](reference/mcp-tools.md) and the data
    formats: [TMC snapshots](reference/tmc-snapshot-format.md),
    [NEMA plans](reference/nema-plan-format.md), [maps](reference/map-formats.md).

- **Extend it**

    The [Python API](developer-guide/python-api.md), the
    [configuration system](developer-guide/configuration.md) and the
    [Plugin SDK](plugins/index.md).

</div>

## Verify a deployment

The acceptance script runs the full chain on synthetic data and writes a pass/fail report per
acceptance criterion ([Acceptance test](examples/acceptance.md)):

```bash
python examples/acceptance/run_acceptance.py --out /tmp/terasim-acceptance
```
