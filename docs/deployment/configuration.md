# Configuration reference

Every service reads its settings from environment variables. The tables give each variable's
default in the code, the value the container image or the deployment assets set, and the Helm
value that renders it. Boolean switches accept `1`/`true`/`yes` (on) and, where noted,
`0`/`false`/`no`/`off`.

## Container image

The image sets these defaults (`apps/deploy/city/Dockerfile`):

| Variable | Image value |
| --- | --- |
| `TERASIM_ENABLE_CITY_API` | `true` |
| `TERASIM_CITY_INPUT_ROOT` | `/data/input` (read-only baked inputs) |
| `TERASIM_CITY_OUTPUT_ROOT` | `/data/output` |
| `TERASIM_CITY_DATA_ROOT` | `/data/output/store` |
| `TERASIM_REDIS_HOST`, `TERASIM_REDIS_PORT` | `127.0.0.1`, `6379` |
| `SUMO_HOME` | the SUMO wheel's directory |
| `HOME` | the home directory of the image's `terasim` user (uid 1001) |

The entrypoint starts an embedded Redis (no persistence, loopback only) when
`TERASIM_REDIS_HOST` is `localhost`, `127.0.0.1`, `::1` or empty (any other host: an external
Redis, no embedded one), then runs the command (`terasim-service` by default).

## terasim-service

### City API and storage

| Variable | Default | Helm | Meaning |
| --- | --- | --- | --- |
| `TERASIM_ENABLE_CITY_API` | off | always `true` | Mounts the city API (`/v1/city/*`) |
| `TERASIM_CITY_OUTPUT_ROOT` | required with the city API | `/data/output` (PVC) | Run directories, audit log |
| `TERASIM_CITY_DATA_ROOT` | unset | `/data/output/store` | Map store and plan store; enables `/v1/city/maps`, `/v1/city/plans` and the automatic map selection |
| `TERASIM_CITY_INPUT_ROOT` | unset | `service.inputRoot` (`/data/input`) | When set, file paths in asynchronous requests (`tmc_json`, `base_sumocfg`, `baseline_sheet`) and the files a `base_sumocfg` references must lie under it |
| `TERASIM_CITY_SCHEDULED_CONFIG` | unset | `/config/scheduled_config.yaml` (ConfigMap) | [Corridor configuration](../developer-guide/configuration.md#corridor-configuration) |
| `TERASIM_CITY_WINDOW_S` | `3600` | `service.windowS` | Seconds of buffered snapshots one optimization aggregates |

### Scheduling and capacity

| Variable | Default | Helm | Meaning |
| --- | --- | --- | --- |
| `TERASIM_CITY_SCHEDULER_ENABLED` | off | `scheduler.enabled` | Built-in scheduler: optimizes every corridor of the corridor configuration once per cadence when its window holds counts |
| `TERASIM_CITY_CADENCE_S` | `3600` | `scheduler.cadenceSeconds` | Seconds between two scheduler passes |
| `TERASIM_CITY_MAX_CONCURRENCY` | `3` | `service.maxConcurrency` (`8`) | Worker slots all runs together may hold; a run holds `n_workers` slots. Requests above it answer 422, requests while the slots are busy 429 |
| `TERASIM_CITY_EMERGENCY_SLOTS` | `2` | | Extra slots only incident runs (inline `incident`) may use |
| `TERASIM_CITY_LEASE_TTL` | `120` | | Seconds a run's slot lease lives without a heartbeat (heartbeat every 30 s) |

### Maps

| Variable | Default | Helm | Meaning |
| --- | --- | --- | --- |
| `TERASIM_CITY_OSM_DOWNLOAD` | `1` | `service.osmDownload.enabled` | `0`/`false`/`no`/`off`: never download OpenStreetMap data; a request no stored map covers answers 422 |
| `TERASIM_CITY_OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | | Overpass endpoint of the downloads |
| `TERASIM_CITY_OVERPASS_TIMEOUT_S` | `60` | | Download timeout |
| `TERASIM_CITY_MAP_MARGIN_M` | `250` | | Margin around the intersections of a downloaded extract, in metres |

### Measures and audit log

| Variable | Default | Helm | Meaning |
| --- | --- | --- | --- |
| `TERASIM_CITY_SIM_METRICS` | `1` | `simMetrics.enabled` | Simulated signal-performance measures after every completed run (two extra SUMO runs); `0` turns them off |
| `TERASIM_CITY_SIM_METRICS_TIMEOUT_S` | `900` | | Wall time the two runs may take |
| `TERASIM_CITY_SIM_METRICS_SLOT_WAIT_S` | `3600` | | How long the computation waits for a free worker slot |
| `TERASIM_CITY_AUDIT_ENABLED` | `1` | `audit.enabled` | Audit log endpoints `/v1/city/audit/*`, stored under `<output root>/audit/` |

### Redis

| Variable | Default | Helm | Meaning |
| --- | --- | --- | --- |
| `TERASIM_REDIS_HOST` | `localhost` | the chart's Redis Service | Redis host |
| `TERASIM_REDIS_PORT` | `6379` | `6379` | Redis port |
| `TERASIM_REDIS_DB` | `0` | | Redis database |
| `TERASIM_REDIS_PASSWORD` | unset | from the Redis Secret | Redis password |

### Neural NDE runtime (optional)

Behavior of the background vehicles in the candidate evaluations. A request can only ask for
Neural NDE traffic for its own run (`traffic_model="cnde"`), within the two `TERASIM_CITY_CNDE_*`
limits. Invalid values are rejected when a request is submitted (422).

| Variable | Default | Meaning |
| --- | --- | --- |
| `TERASIM_CITY_CNDE_MAX_WORKERS` | `3` | Neural NDE trials of one `traffic_model="cnde"` run on the GPU at once; the run's `n_workers` and its reserved worker slots are lowered to it |
| `TERASIM_CITY_CNDE_MAX_TRIALS` | unset | Most Bayesian trials of a `traffic_model="cnde"` run; unset, the request's (or corridor entry's) `max_trials` |
| `TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY` | unset (off) | `1`: fast mode, each inference predicts only the vehicles the model may control |
| `TERASIM_CITY_CNDE_DIAG_LOGS` | unset (off) | `1`: per-step diagnostic logs of every Neural NDE trial |
| `TERASIM_CITY_VEHICLE_BEHAVIOR` | `sumo_passive` | Behavior of every evaluation: `sumo_passive` or `cnde` (requires the Neural NDE runtime module and checkpoint, installed separately; see [Behavior models](../developer-guide/behavior-models.md)) |
| `TERASIM_CITY_REFINE_TOP_K` | `0` | After the search, re-evaluate the baseline and the K best distinct candidates under `TERASIM_CITY_REFINE_BEHAVIOR`; the best of them is exported. `0` disables the stage |
| `TERASIM_CITY_REFINE_BEHAVIOR` | unset | Behavior of the re-evaluation, typically `cnde` |
| `TERASIM_CITY_REFINE_MAX_COLLISIONS` | unset | Excludes re-evaluated candidates with more collisions from the selection |
| `TERASIM_CITY_REFINE_N_WORKERS` | `1` | Parallel processes of the re-evaluation (each loads its own copy of the runtime) |
| `TERASIM_CITY_INLINE_EVAL` | off | Evaluate candidates one after another in the worker process instead of a process pool |
| `TERASIM_CITY_CNDE_RELAX_QUEUE_FILTER` | off | Experiment switch of the Neural NDE corridor environment |

## terasim-agent-skill (MCP server)

| Variable | Default | Helm | Meaning |
| --- | --- | --- | --- |
| `TERASIM_CITY_URL` | `http://127.0.0.1:8000` | the service's in-cluster URL | Base URL of `terasim-service` |
| `TERASIM_CITY_TIMEOUT_S` | `1800` | | Timeout of a service call (positive number of seconds); 1800 covers a `traffic_model="cnde"` request in the Neural NDE fast mode (`compose.cnde.yml`), 3600 the default mode |
| `TERASIM_TMC_URL` | unset | the TMC Service's in-cluster URL | URL of `terasim-tmc-service`; when set, the registry tools (`list_intersections`, `register_intersection`, `optimize_registered_intersection`) are offered |
| `TERASIM_MCP_TRANSPORT` | `stdio` | `streamable-http` | `stdio`, `sse` or `streamable-http` |
| `TERASIM_MCP_HOST` | `0.0.0.0` | `0.0.0.0` | Bind address (HTTP transports) |
| `TERASIM_MCP_PORT` | `9911` | `mcp.port` | Port (HTTP transports); the MCP endpoint is `/mcp` |
| `TERASIM_MCP_STATELESS` | `0` | | `1`: stateless streamable HTTP |
| `TERASIM_MCP_HEARTBEAT_S` | `2` | `mcp.heartbeatS` | Seconds between progress and log notifications during an optimization call |
| `TERASIM_ARTIFACT_ROOT` | unset | `/data/artifacts` (PVC) | Saves each optimization's files and serves them at `/files/<artifact_id>/<file>`; unset: no files, no links |
| `TERASIM_MCP_PUBLIC_URL` | `http://127.0.0.1:<port>` | `mcp.publicUrl`, else from the Route | Base address of the download links |
| `TERASIM_CITY_DATA_ROOT` | unset | `/data/output/store` (read-only) | The service's plan store, read for the current plan of the timing report |

## terasim-tmc-service (TMC collector)

| Variable | Default | Helm | Meaning |
| --- | --- | --- | --- |
| `TMC_SPEC_FILE` | required | `/config/Metadata.json` (ConfigMap) | [Corridor specification](vss-integration.md#corridor-specification-metadatajson) |
| `VSS_BEHAVIOR_URL` | required | `tmc.vssBehaviorUrl` | VSS behavior API base URL (`GET /behavior`) |
| `TERASIM_CITY_URL` | `http://127.0.0.1:8000` | the service's in-cluster URL | Where snapshots are pushed |
| `TMC_REGISTRY_FILE` | `./tmc_registry.json` | `/data/tmc/tmc_registry.json` (PVC) | Registered intersections; the last counted window is kept next to it |
| `TMC_SETTLE_LAG_S` | `180` | `tmc.settleLagS` | Seconds a window must have ended before it is counted |
| `TMC_COUNTED_OBJECT_TYPES` | `Vehicle,two_wheeler` | `tmc.countedObjectTypes` | Detector labels counted as traffic |
| `TMC_AUTOSTART` | `1` | | `1`: count every window automatically; otherwise only on `POST /v1/tmc/run-once` |
| `TMC_HOST` | `0.0.0.0` | `0.0.0.0` | Bind address |
| `TMC_PORT` | `8200` | `tmc.port` | Port |

## Simulation engine

| Variable | Default | Meaning |
| --- | --- | --- |
| `USE_LIBSUMO` | libsumo | `0` makes the engine use TraCI instead of libsumo |
| `ENABLE_PROFILING` | `False` | `true` or `1` profiles decorated engine functions (needs `line_profiler`) |
| `IS_MAGNITUDE_INTERSECTION`, `IS_MAGNITUDE_HIGHWAY`, `IS_MAGNITUDE_ROUNDABOUT` | `20` | Importance-sampling magnitude of the adversarial environment (see [Adversarial environment](../developer-guide/adversarial-environment.md#tuning-the-adversarial-environment)) |
| `AVOID_COLLISION_IS_PROB` | `0.2` | Sampling probability of avoidance maneuvers in the adversarial environment |
