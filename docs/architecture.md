# Architecture

TeraSim Metropolis is a signal-optimization service built on the TeraSim traffic simulation
engine. It runs next to an NVIDIA Metropolis VSS (Video Search and Summarization) deployment.

## Components

One container image (`terasim-metropolis:0.1.0`) serves three commands; Redis runs inside the
image by default or as a separate deployment.

| Component | Command | Port | Role |
| --- | --- | --- | --- |
| Optimization service | `terasim-service` (default command) | 8000 | REST API `/v1/city/*`: snapshot buffer, map and plan stores, optimization runs, KPIs, NEMA export, timing report |
| MCP server | `terasim-agent-skill` | 9911 | MCP endpoint `/mcp` (streamable HTTP) with the agent tools; result downloads at `/files/<artifact_id>/<file>` |
| TMC collector | `terasim-tmc-service` | 8200 | Intersection registry; every observation window pulls VSS behavior objects, counts turning movements and pushes a TMC snapshot |
| Redis | `redis-server` | 6379 | Snapshot buffer and job store of `terasim-service` (only the service holds a Redis client) |

The MCP server and the TMC collector reach the service only through its REST API. The service
holds the traffic data, maps, plans and runs (in Redis and on its data volume); the TMC collector
keeps its intersection registry, and the MCP server its download files, on their own volumes.

## Data flow

Traffic data, continuously:

```text
VSS cameras
   │
   ▼
VSS behavior API (GET /behavior)
   │  pulled for the cameras of every registered intersection,
   │  once per observation window (default 10 min)
   ▼
terasim-tmc-service :8200
   │  turning movements counted per approach and turn
   │  POST /v1/city/snapshots
   ▼
terasim-service :8000 ──► Redis snapshot buffer (rolling two hours)
```

An optimization, on request:

```text
operator ──► VSS agent
                │  MCP tool call: optimize_*
                ▼
       terasim-agent-skill :9911  (/mcp)
                │  POST /v1/city/optimize  (synchronous)
                ▼
       terasim-service :8000
                │  aggregate the buffered window ─► map ─► corridor
                │  ─► demand ─► seed plans ─► Bayesian search (SUMO)
                ▼
       NEMA timing (terasim.nema_timing.v1) + KPIs
                │  answer: Markdown summary + JSON payload
                │  files: /files/<artifact_id>/... (PDF, JSON)
                ▼
       VSS agent ──► operator
```

## One optimization run

Every optimization, whatever started it, runs this pipeline inside `terasim-service` and keeps its
files under `TERASIM_CITY_OUTPUT_ROOT/<run_id>/`.

| Step | What happens | Code |
| --- | --- | --- |
| 1. Demand | The snapshots buffered for the `corridor_id` within `TERASIM_CITY_WINDOW_S` are summed into one TMC file; the observed window length is kept, so partial windows scale to hourly volumes. | `terasim_service.city.aggregate` |
| 2. Map | A map from the scheduled-config entry, else the stored map whose bounding box covers every intersection (HD maps first), else an OpenStreetMap extract downloaded and converted once (when allowed). | `terasim_city.maps`, `terasim_service.city.map_acquisition` |
| 3. Corridor | Every coordinate (TMC intersections, target, stored plans) is bound independently to a signalized junction of the map; the corridor is the shortest drivable path between the endpoints and every signal along it. | `terasim_city.binding`, `terasim_city.corridor` |
| 4. Scenario | Routes are generated from the turning counts (OD peel-off along the corridor) and assembled with the map into one SUMO scenario shared by all trials. | `terasim_city.flow`, `terasim_city.api.run_tmc_pipeline` |
| 5. Seeds | The baseline plan: the city's uploaded NEMA plan when it covers every signal, else the corridor's timing sheet, else a Webster plan computed from the counts (optionally the scenario's own signal programs first). | `terasim_city.plans`, `terasim_city.orchestration` |
| 6. Search | The seeds and the Bayesian candidates (Optuna TPE) are simulated in parallel SUMO processes for `sim_end` seconds (1,800 s by default) and scored on the weighted KPI cost. The winner is the best of all evaluations, baseline included. | `terasim_city.optimize`, `terasim_city.evaluation` |
| 7. Output | The winner is exported as NEMA timing; the response carries the KPIs of the baseline and of the winner. | `terasim_city.nema` |
| 8. Measures | In the background, the baseline and the exported plan are re-simulated with virtual detectors to produce the simulated signal-performance measures; the timing report PDF is rendered on first request. | `terasim_city.evaluation.sim_metrics`, `terasim_service.city.run_report` |

## The skills

The VSS agent uses two skills, each a set of MCP tools backed by REST endpoints (see
[MCP tools](reference/mcp-tools.md)), plus helper tools:

| Skill | Tools | Behavior |
| --- | --- | --- |
| 1. Map and timing-plan upload | `upload_map` | Registers a map file from the shared asset volume: OpenDRIVE, Lanelet2, OpenStreetMap or a SUMO scenario (HD maps are converted on upload) |
| | `upload_signal_plan` | Stores the city's current timing plan as a `terasim.nema_plan.v1` document; later optimizations use it as the baseline and as the seed of the search |
| 2. Optimization | `optimize_registered_intersection`, `optimize_intersection`, `optimize_intersection_by_id`, `optimize_corridor` | One synchronous optimization; answers with a ready-to-show summary (plan, KPI table, NEMA splits, download links) followed by the JSON payload |
| Helpers | `list_intersections`, `register_intersection` | Manage the TMC collection |
| | `upload_traffic_snapshot` | Pushes a snapshot by hand |

## Interfaces

| Interface | For | Entry points |
| --- | --- | --- |
| **Standard** | Integrations such as the VSS agent, operators, scripts | MCP tools at `/mcp` (port 9911) and the REST API at `/v1/city/*` (port 8000); see [HTTP API](reference/http-api.md) and [MCP tools](reference/mcp-tools.md) |
| **Internal** | Developers embedding the pipeline in Python | `terasim.simulator.Simulator`, `terasim_city.run_tmc_pipeline`, `run_corridor_optimization`, `MapStore`, `PlanStore`; see [Python API](developer-guide/python-api.md) |
| **Plugin** | Third parties adding operations | The Plugin SDK (`terasim_plugin_sdk`), entry-point group `terasim.operation_plugins`; see [Plugin SDK](plugins/index.md) |

## Operating modes

- **[On-Demand](examples/on-demand.md).** An operator asks the VSS agent to optimize an
  intersection or a corridor; the agent calls an optimization tool and shows the result.
- **[Scheduled](examples/scheduled.md).** The caller invokes the same optimization tool on its own
  cadence (for example a workflow that runs every hour). The tool has no separate scheduling
  logic: each call optimizes on the latest buffered data.
- **Built-in scheduler (optional, off by default).** With `TERASIM_CITY_SCHEDULER_ENABLED`, the
  service itself optimizes every corridor of its scheduled configuration once per
  `TERASIM_CITY_CADENCE_S` (default 3600 s) when it has recent counts. In Helm:
  `scheduler.enabled` / `scheduler.cadenceSeconds`.

## State and storage

| Store | Location | Keyed by | Behavior |
| --- | --- | --- | --- |
| Snapshot buffer | Redis | `corridor_id` | Rolling two hours of TMC snapshots per corridor |
| Job store | Redis | `run_id` | One record per run (origin, status, corridor, intersections, KPI summary); kept seven days; worker-slot leases |
| Map store | `$TERASIM_CITY_DATA_ROOT/maps/<map_id>/` | content hash (12 hex) | Immutable map packages: converted net, base sumocfg, archived source, `map_package.json` |
| Plan store | `$TERASIM_CITY_DATA_ROOT/plans/<corridor_id>/<intersection_id>/` | `(corridor_id, intersection_id)`, else location within 30 m | Last upload wins; the replaced version is archived |
| Run directories | `$TERASIM_CITY_OUTPUT_ROOT/<run_id>/` | `run_id` | Inputs, scenario, every trial, result, NEMA export, measures, report |
| Artifacts | `$TERASIM_ARTIFACT_ROOT/<artifact_id>/` (MCP server) | `artifact_id` | Files linked from a tool answer: `timing_report.pdf`, `nema_plan.json`, `current_plan.json`, `optimization.json` |

A run directory holds:

```text
<run_id>/
├── inputs/aggregated_tmc.json   demand of the run
├── scenario/                    matched.json, routes.rou.xml,
│                                simulation.sumocfg, ...
├── trials/trial_NNNN/           one per evaluated plan: plan,
│                                SUMO outputs, kpi.json
├── trials.jsonl                 one line per trial
├── corridor_optimization_result.json
│                                the full result
├── nema_plan.json               the exported plan
├── baseline_nema.json           the baseline, same shape
├── sim_metrics.json             signal-performance measures
└── timing_report.pdf            rendered on first request
```

Run directories are not removed automatically; size the output volume for the history you want
to keep.

## Spatial binding

TMC counts, uploaded plans and optimization targets never have to agree on ids: two records
describe the same intersection when their coordinates bind to the same signalized junction of the
map. The same rules apply everywhere:

- a coordinate binds to signals within **60 m** (`terasim_city.binding.BINDING_RADIUS_M`);
- a match is accepted only when the runner-up is at least **1.5 times** farther than the
  winner; otherwise the request is rejected with the candidates listed;
- a joined traffic light spanning several junctions counts as one candidate.

Uploaded plans also use a 30 m radius to recognize a re-upload of the same intersection.

## Time of day

Signal schedules are local wall-clock tables. The corridor's time zone is derived from its own
coordinates (`terasim_city.geo_timezone`), so a stored plan's time-of-day pattern is selected for
the local time of the observed traffic; no time-zone setting is needed.
