# HTTP API

!!! note "Generated page"
    This page is generated from the code by `docs/_scripts/gen_reference.py`. Do not edit it by hand; re-run the script after changing the code.

Two HTTP services ship in the image. `terasim-service` (port 8000) serves the city signal-optimization API under `/v1/city/*`; `terasim-tmc-service` (port 8200) is the TMC collector with its intersection registry. The raw OpenAPI documents are [openapi.json](openapi.json) (`terasim-service`) and [openapi-tmc.json](openapi-tmc.json) (`terasim-tmc-service`); a running `terasim-service` also serves its own at `/openapi.json` and an interactive view at `/docs`.

| Service | Default port | OpenAPI title | Endpoints |
| --- | --- | --- | --- |
| `terasim-service` | 8000 | TeraSim Metropolis service 0.1.0 | 31 |
| `terasim-tmc-service` | 8200 | TeraSim Metropolis TMC collector 0.1.0 | 4 |

The city API is mounted when `TERASIM_ENABLE_CITY_API` is true (the container image sets it); the asset endpoints (`/v1/city/maps`, `/v1/city/plans`) additionally need `TERASIM_CITY_DATA_ROOT`. Errors are JSON objects with a `detail` field.

## terasim-service: city API

### Optimization runs

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | [`/v1/city/optimizations`](#op-get-v1-city-optimizations) | Recent optimization runs |
| `POST` | [`/v1/city/optimizations`](#op-post-v1-city-optimizations) | Submit |
| `POST` | [`/v1/city/optimize`](#op-post-v1-city-optimize) | Optimize Direct |
| `GET` | [`/v1/city/optimizations/{run_id}/status`](#op-get-v1-city-optimizations-run-id-status) | Get Status |
| `GET` | [`/v1/city/optimizations/{run_id}/result`](#op-get-v1-city-optimizations-run-id-result) | Get Result |
| `GET` | [`/v1/city/optimizations/{run_id}/nema`](#op-get-v1-city-optimizations-run-id-nema) | Get Nema |
| `GET` | [`/v1/city/optimizations/{run_id}/baseline`](#op-get-v1-city-optimizations-run-id-baseline) | Baseline (current) plan of a run |
| `GET` | [`/v1/city/optimizations/{run_id}/sim_metrics`](#op-get-v1-city-optimizations-run-id-sim-metrics) | Simulated signal-performance measures of a run |
| `GET` | [`/v1/city/optimizations/{run_id}/report.pdf`](#op-get-v1-city-optimizations-run-id-report-pdf) | Timing report (PDF) of a run |

#### `GET /v1/city/optimizations` { #op-get-v1-city-optimizations }

*Recent optimization runs*

Every optimization run, newest first: scheduled, API and synchronous ones.

Runs are kept as long as their job record (seven days by default).

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `corridor_id` | query | string \| null | no |  | Only this corridor's runs. |
| `limit` | query | integer | no | `20` | How many runs, newest first (at most 200). (>= 1) |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: array of [`OptimizationRunSummary`](#schema-optimizationrunsummary) |
| 503 | The job store is unavailable |  |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `POST /v1/city/optimizations` { #op-post-v1-city-optimizations }

*Submit*

Asynchronous submission, used by the built-in scheduler and by scripts. Name the demand with exactly one of `corridor_id` (aggregates the buffered snapshots, as the synchronous call does) or `tmc_json` (a demand file under `TERASIM_CITY_INPUT_ROOT`). `corridor` selects the geometry: `from`/`to` (TMC intersection ids or `"lat,lon"` strings) or explicit `tls_ids`. An inline `incident` (stalled vehicles) is simulated in every trial and the run uses the reserved emergency worker slots.

Answers 202 `{"run_id": ..., "status": "submitted"}`; poll `/status`, then read `/result` and `/nema`. Errors: 400 for an input file that is missing or outside `TERASIM_CITY_INPUT_ROOT`, 409 when no snapshots are buffered for `corridor_id`, 422 for an invalid request, `n_workers` above `TERASIM_CITY_MAX_CONCURRENCY` or no stored map while OpenStreetMap downloads are off, 429 when the worker slots are busy, 503 when the job store is unavailable.

**Request body** (required): `application/json`, [`OptimizationRequest`](#schema-optimizationrequest)

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 202 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `POST /v1/city/optimize` { #op-post-v1-city-optimize }

*Optimize Direct*

Synchronous optimization, the call behind the MCP optimization tools. `type` is `intersection` (with `intersection`) or `corridor` (with `corridor.from` and `corridor.to`); `cost.weights` defaults to `{"control_delay": 0.7, "stops": 0.3}`. The demand is the TMC snapshots buffered for `corridor_id` within the last `TERASIM_CITY_WINDOW_S` seconds, aggregated into one demand file. A scheduled-config entry for `corridor_id`, when present, supplies request defaults (map, timing sheet, trial budget, workers). Without a map from the entry, the service selects a stored map covering the intersections, or downloads and converts an OpenStreetMap extract when `TERASIM_CITY_OSM_DOWNLOAD` allows it.

The request stays open until the run has finished and answers `{nema_plan, kpis}`, plus `unserved_movements` and `config` when they apply. Errors: 400 or 422 for an invalid request or configuration, 409 when no recent traffic data is buffered for the corridor or when the best plan has no NEMA export (the detail gives the reason), 429 when not enough worker slots are free, 503 when the job store is unavailable. The run is recorded with origin `direct` and is listed by `GET /v1/city/optimizations`.

**Request body** (required): `application/json`, [`DirectOptimizationRequest`](#schema-directoptimizationrequest)

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: [`DirectOptimizationResponse`](#schema-directoptimizationresponse) |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /v1/city/optimizations/{run_id}/status` { #op-get-v1-city-optimizations-run-id-status }

*Get Status*

`{run_id, status, progress: {done, total}, best_score, updated_at}`; `status` is `submitted`, `running`, `completed` or `failed`. 404 for an unknown run.

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `run_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /v1/city/optimizations/{run_id}/result` { #op-get-v1-city-optimizations-run-id-result }

*Get Result*

The run's full result document (`corridor_optimization_result.json`, schema `terasim.corridor_optimization.v1`): the resolved corridor, the seed plans, every trial, the baseline and best plans with their measures of effectiveness and timing, the search summary and the NEMA export status. 409 until the run has completed (or when it failed), 410 when the file is no longer available.

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `run_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /v1/city/optimizations/{run_id}/nema` { #op-get-v1-city-optimizations-run-id-nema }

*Get Nema*

The exported NEMA timing (`nema_plan.json`, schema `terasim.nema_timing.v1`, see [NEMA plan formats](nema-plan-format.md)). 409 until the run has completed, or with the reason when the run exported no NEMA plan.

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `run_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /v1/city/optimizations/{run_id}/baseline` { #op-get-v1-city-optimizations-run-id-baseline }

*Baseline (current) plan of a run*

The plan the run's optimization was measured against, as NEMA timing.

409 until the run has completed; 404 with the reason when the baseline cannot be expressed as NEMA timing (or its files are gone).

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `run_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: [`BaselinePlanResponse`](#schema-baselineplanresponse) |
| 404 | Unknown run, or the item cannot be produced (the detail says why) |  |
| 409 | The run has not completed (or failed) |  |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /v1/city/optimizations/{run_id}/sim_metrics` { #op-get-v1-city-optimizations-run-id-sim-metrics }

*Simulated signal-performance measures of a run*

Simulated signal-performance measures of the run's baseline and optimized plans.

Computed in the background after every completed run while TERASIM_CITY_SIM_METRICS is on (two extra SUMO runs per optimization): 202 {"status": "pending"} until then, 404 with the reason when disabled or when the computation failed.

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `run_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | The metrics document | `application/json`: [`SimMetricsDocument`](#schema-simmetricsdocument) |
| 202 | Still being computed | `application/json`: [`SimMetricsPending`](#schema-simmetricspending) |
| 404 | Unknown run, metrics disabled, or the computation failed (the detail says why) |  |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /v1/city/optimizations/{run_id}/report.pdf` { #op-get-v1-city-optimizations-run-id-report-pdf }

*Timing report (PDF) of a run*

The run's signal timing report: current plan, optimized plan and KPI comparison.

Rendered on the first request and kept; 409 until the run has completed; 404 with the reason when it cannot be rendered (no NEMA plan exported, no PDF renderer on this deployment, or a rendering error).

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `run_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | The timing report | `application/pdf`: any |
| 404 | Unknown run, or the item cannot be produced (the detail says why) |  |
| 409 | The run has not completed (or failed) |  |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

### Traffic snapshot ingestion

| Method | Path | Summary |
| --- | --- | --- |
| `POST` | [`/v1/city/snapshots`](#op-post-v1-city-snapshots) | Post Snapshot |
| `GET` | [`/v1/city/snapshots/{corridor_id}`](#op-get-v1-city-snapshots-corridor-id) | Get Snapshots |

#### `POST /v1/city/snapshots` { #op-post-v1-city-snapshots }

*Post Snapshot*

Buffers one TMC snapshot (see [TMC snapshot format](tmc-snapshot-format.md)) under its `corridor_id`. Answers 202 `{corridor_id, buffered}` with the number of snapshots now buffered for the corridor. 422 when a required field is missing or malformed, or when `timestamp_start` lies more than 300 s in the future. Snapshots are kept for a rolling two hours.

**Request body** (required): `application/json`, JSON object

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 202 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /v1/city/snapshots/{corridor_id}` { #op-get-v1-city-snapshots-corridor-id }

*Get Snapshots*

`{corridor_id, count, window_s}`: the number of buffered snapshots and the span between the oldest and the newest `timestamp_start` (0 with fewer than two).

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `corridor_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

### Maps and signal plans

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | [`/v1/city/plans`](#op-get-v1-city-plans) | List Plans |
| `POST` | [`/v1/city/plans`](#op-post-v1-city-plans) | Upload Plan |
| `GET` | [`/v1/city/maps`](#op-get-v1-city-maps) | List Maps |
| `POST` | [`/v1/city/maps`](#op-post-v1-city-maps) | Upload Map |

#### `GET /v1/city/plans` { #op-get-v1-city-plans }

*List Plans*

`{plans: [[corridor_id, intersection_id], ...], count}`: the stored plan keys.

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |

#### `POST /v1/city/plans` { #op-post-v1-city-plans }

*Upload Plan*

Stores a `terasim.nema_plan.v1` document, one record per intersection (see [NEMA plan formats](nema-plan-format.md)); the optional `corridor_id` query parameter labels a document that carries none. A later upload for the same intersection replaces the stored plan. Answers 201 `{stored: [[corridor_id, intersection_id], ...], notes: [...]}`; 400 with the reason for an invalid plan.

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `corridor_id` | query | string \| null | no |  |  |

**Request body** (required): `application/json`, JSON object

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 201 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /v1/city/maps` { #op-get-v1-city-maps }

*List Maps*

`{maps: [map_id, ...], count}`: the stored map ids.

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |

#### `POST /v1/city/maps` { #op-post-v1-city-maps }

*Upload Map*

Body `{"path": "<map file on the service volume>", "proj": "<optional proj.4 string>"}`. Detects the format (OpenStreetMap, Lanelet2, OpenDRIVE or a prebuilt SUMO scenario), converts HD maps to a SUMO network and stores the map package (see [Map formats](map-formats.md)). Answers 201 `{map_id, duplicate, source, summary}`; 400 for an unknown format or an unusable geo-reference, 422 when no loadable network can be built.

**Request body** (required): `application/json`, JSON object

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 201 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

### Scheduled runs

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | [`/v1/city/scheduled/{corridor_id}/latest`](#op-get-v1-city-scheduled-corridor-id-latest) | Get Latest Scheduled Run |

#### `GET /v1/city/scheduled/{corridor_id}/latest` { #op-get-v1-city-scheduled-corridor-id-latest }

*Get Latest Scheduled Run*

run_id of the most recent scheduler-submitted run for this corridor.

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `corridor_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

### Audit log

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | [`/v1/city/audit/events`](#op-get-v1-city-audit-events) | Audit log events, newest first |
| `POST` | [`/v1/city/audit/events`](#op-post-v1-city-audit-events) | Record a console event (feedback, plan view or download, session) |
| `GET` | [`/v1/city/audit/events.csv`](#op-get-v1-city-audit-events-csv) | Audit log events as CSV |
| `GET` | [`/v1/city/audit/summary`](#op-get-v1-city-audit-summary) | Console usage and feedback of the last N days |

#### `GET /v1/city/audit/events` { #op-get-v1-city-audit-events }

*Audit log events, newest first*

Recorded events matching every given filter, newest first.

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `corridor_id` | query | string \| null | no |  |  |
| `run_id` | query | string \| null | no |  |  |
| `intersection_id` | query | string \| null | no |  |  |
| `actor` | query | string \| null | no |  |  |
| `type` | query | `"console_opened"` \| `"plan_viewed"` \| `"plan_downloaded"` \| `"feedback"` \| null | no |  |  |
| `since` | query | string \| null | no |  | Inclusive; ISO-8601 date-time or date (UTC). |
| `until` | query | string \| null | no |  | Exclusive; ISO-8601 date-time or date (UTC). |
| `limit` | query | integer | no | `200` | At most 2000 (larger values return that many). (>= 1) |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Events, newest first | `application/json`: array of [`AuditEvent`](#schema-auditevent) |
| 404 | The audit log is disabled on this deployment |  |
| 422 | Invalid filter |  |

#### `POST /v1/city/audit/events` { #op-post-v1-city-audit-events }

*Record a console event (feedback, plan view or download, session)*

Record one operator event. The service stamps id, time (UTC) and actor.

actor: the authenticating proxy's X-Forwarded-User / X-Forwarded-Email / X-Remote-User (actor_source "proxy"), else the body's display name ("self"), else "anonymous".

**Request body** (required): `application/json`, object

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 201 | Successful Response | `application/json`: [`AuditEvent`](#schema-auditevent) |
| 404 | The audit log is disabled on this deployment |  |
| 413 | Body larger than 8 KiB |  |
| 415 | Body not application/json |  |
| 422 | Invalid event (the detail says why) |  |
| 429 | Too many events per minute (Retry-After) |  |
| 503 | The log cannot be written (the detail says why) |  |

#### `GET /v1/city/audit/events.csv` { #op-get-v1-city-audit-events-csv }

*Audit log events as CSV*

The events of GET /audit/events as a CSV file (UTF-8 with BOM), newest first.

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `corridor_id` | query | string \| null | no |  |  |
| `run_id` | query | string \| null | no |  |  |
| `intersection_id` | query | string \| null | no |  |  |
| `actor` | query | string \| null | no |  |  |
| `type` | query | `"console_opened"` \| `"plan_viewed"` \| `"plan_downloaded"` \| `"feedback"` \| null | no |  |  |
| `since` | query | string \| null | no |  | Inclusive; ISO-8601 date-time or date (UTC). |
| `until` | query | string \| null | no |  | Exclusive; ISO-8601 date-time or date (UTC). |
| `limit` | query | integer | no | `10000` | At most 100000 (larger values return that many). (>= 1) |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | CSV download | `text/csv`: any |
| 404 | The audit log is disabled on this deployment |  |
| 422 | Invalid filter |  |

#### `GET /v1/city/audit/summary` { #op-get-v1-city-audit-summary }

*Console usage and feedback of the last N days*

Per-day counts by event type, users, feedback verdicts and acceptance rate.

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `corridor_id` | query | string \| null | no |  | Only this corridor (default: all). |
| `days` | query | integer | no | `30` | (>= 1, <= 366) |
| `tz` | query | string \| null | no |  | IANA zone of the day buckets (default UTC). |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: [`AuditSummary`](#schema-auditsummary) |
| 404 | The audit log is disabled on this deployment |  |
| 422 | days outside 1-366 |  |

### Service health

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | [`/health`](#op-get-health) | Health Check |

#### `GET /health` { #op-get-health }

*Health Check*

`{"status": "healthy"}`.

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |

## terasim-tmc-service

### Intersection registry and TMC generation

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | [`/v1/tmc/intersections`](#tmc-op-get-v1-tmc-intersections) | List Intersections |
| `POST` | [`/v1/tmc/intersections`](#tmc-op-post-v1-tmc-intersections) | Register Intersection |
| `POST` | [`/v1/tmc/run-once`](#tmc-op-post-v1-tmc-run-once) | Run Once |
| `GET` | [`/v1/tmc/health`](#tmc-op-get-v1-tmc-health) | Health |

#### `GET /v1/tmc/intersections` { #tmc-op-get-v1-tmc-intersections }

*List Intersections*

`{corridor_id, observation_interval, available, registered}`: every intersection of the corridor spec (`TMC_SPEC_FILE`) with its location, cameras (`sensors`) and registration state, and the registration records.

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |

#### `POST /v1/tmc/intersections` { #tmc-op-post-v1-tmc-intersections }

*Register Intersection*

Registers an intersection of the corridor spec for periodic counting. `lat`/`lon` must lie within 50 m of the spec location; `corridor_id`, when given, must match the spec. Answers 201 with the record (`intersection_id`, `name`, `lat`, `lon`, `corridor_id`, `sensors`, `registered_at`); 404 for an id the spec does not contain, 422 for coordinates too far from it or another corridor. A successful registration also starts one counting pass in the background.

**Request body** (required): `application/json`, [`RegisterRequest`](#tmc-schema-registerrequest)

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 201 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#tmc-schema-httpvalidationerror) |

#### `POST /v1/tmc/run-once` { #tmc-op-post-v1-tmc-run-once }

*Run Once*

Runs one counting pass now: pulls the behavior objects of the latest settled observation window for every camera of the registered intersections, counts the turning movements and pushes the snapshot to `terasim-service`. Answers the pass summary: `status` (`injected`; `skipped` when that window was already injected; `idle` without registered intersections), the window, the behavior count and the object labels per camera, and the counts. 502 when the behavior API or the service rejects a request.

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |

#### `GET /v1/tmc/health` { #tmc-op-get-v1-tmc-health }

*Health*

`{status, corridor_id, observation_interval, registered, settle_lag_s, counted_object_types, autostart, last_tick, last_injected}`; `last_injected` is the last tick that injected a window.

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |

## terasim-service: legacy co-simulation endpoints

These endpoints drive single TeraSim co-simulation runs from an external simulator (start, tick, read state, send agent commands). They are part of the engine's service and independent of the city signal-optimization API.

### Co-simulation: simulations

| Method | Path | Summary |
| --- | --- | --- |
| `GET` | [`/simulation_result/{simulation_id}`](#op-get-simulation-result-simulation-id) | Get detailed simulation result data |
| `GET` | [`/av_route/{simulation_id}`](#op-get-av-route-simulation-id) | Get autonomous vehicle route coordinates |
| `POST` | [`/start_simulation`](#op-post-start-simulation) | Start a new TeraSim simulation |
| `GET` | [`/simulation_status/{simulation_id}`](#op-get-simulation-status-simulation-id) | Get real-time simulation status |
| `POST` | [`/simulation_control/{simulation_id}`](#op-post-simulation-control-simulation-id) | Control simulation execution |
| `POST` | [`/simulation_tick/{simulation_id}`](#op-post-simulation-tick-simulation-id) | Single-step simulation execution |
| `GET` | [`/simulation_results/{simulation_id}`](#op-get-simulation-results-simulation-id) | Get comprehensive simulation results |
| `GET` | [`/simulation/{simulation_id}/state`](#op-get-simulation-simulation-id-state) | Get complete simulation state snapshot |

#### `GET /simulation_result/{simulation_id}` { #op-get-simulation-result-simulation-id }

*Get detailed simulation result data*

Retrieve detailed result data from Redis for a completed simulation.

Accesses simulation results stored in Redis during execution. Provides structured access to key simulation outcomes and metrics.

Args: simulation_id: UUID of the target simulation

Returns: dict: Structured simulation result data from Redis storage

Note: This endpoint retrieves results cached in Redis during simulation execution. For comprehensive file-based results, use get_simulation_results instead.

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `simulation_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Simulation result retrieved successfully | `application/json`: any |
| 404 | Simulation not found |  |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /av_route/{simulation_id}` { #op-get-av-route-simulation-id }

*Get autonomous vehicle route coordinates*

Retrieve the autonomous vehicle's planned route in geographic coordinates.

Returns the complete AV route as latitude/longitude coordinate pairs. Essential for understanding the AV's intended path and analyzing scenario interactions along the route.

Args: simulation_id: UUID of simulation containing the AV route

Returns: list: Array of [latitude, longitude] coordinate pairs defining the AV route

Example: Returns route like: [[40.7128, -74.0060], [40.7140, -74.0070], ...]

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `simulation_id` | path | string | yes |  | Simulation UUID to get AV route from |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `POST /start_simulation` { #op-post-start-simulation }

*Start a new TeraSim simulation*

Start a new TeraSim simulation with advanced scenario generation capabilities.

This endpoint initializes a complete TeraSim simulation instance with support for: - Multi-agent autonomous vehicle scenarios - Real-time simulation control and monitoring - Corner case and adversarial scenario testing - Redis-based state management for distributed control

Args: config: SimulationConfig containing the YAML configuration file path and execution settings

Returns: dict: Response containing unique simulation_id for tracking and control

Example: Start a highway merge scenario: { "config_file": "/path/to/highway_merge.yaml", "auto_run": true }

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `enable_viz` | query | boolean | no | `false` |  |
| `viz_port` | query | integer | no | `8050` |  |
| `viz_update_freq` | query | integer | no | `5` |  |

**Request body** (required): `application/json`, [`SimulationConfig`](#schema-simulationconfig)

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /simulation_status/{simulation_id}` { #op-get-simulation-status-simulation-id }

*Get real-time simulation status*

Retrieve real-time status information for an active TeraSim simulation.

Provides comprehensive status monitoring including execution state, progress tracking, and health information. Uses Redis backend for distributed state management.

Args: simulation_id: UUID string identifier for the target simulation instance

Returns: SimulationStatus: Detailed status object with current execution state

Status values: - "started": Simulation initialized but not yet running - "running": Active execution in progress - "paused": Temporarily suspended, can be resumed - "completed": Successfully finished execution - "failed": Encountered error during execution

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `simulation_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful response | `application/json`: [`SimulationStatus`](#schema-simulationstatus) |
| 404 | Simulation not found |  |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `POST /simulation_control/{simulation_id}` { #op-post-simulation-control-simulation-id }

*Control simulation execution*

Send real-time control commands to manage simulation execution.

Provides precise control over simulation lifecycle with immediate effect. Commands are processed through Redis messaging for distributed control.

Args: simulation_id: UUID of the target simulation instance command: Control operation to execute

Available commands: - pause: Temporarily suspend simulation execution - resume: Continue paused simulation from current state - stop: Terminate simulation and clean up resources

Returns: dict: Confirmation message with command execution status

Example: Pause a running simulation: { "command": "pause" }

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `simulation_id` | path | string | yes |  |  |

**Request body** (required): `application/json`, [`SimulationCommand`](#schema-simulationcommand)

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Command sent successfully | `application/json`: any |
| 404 | Simulation not found |  |
| 500 | Failed to send command |  |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `POST /simulation_tick/{simulation_id}` { #op-post-simulation-tick-simulation-id }

*Single-step simulation execution*

Advance simulation by exactly one time step for precise control.

Enables step-by-step debugging and detailed scenario analysis. Only functions when simulation was started with auto_run=false. Essential for detailed behavioral analysis and corner case investigation.

Args: simulation_id: UUID of the target simulation instance

Returns: dict: Confirmation that single step command was executed

Note: This command requires the simulation to be started with auto_run=false. Use this for detailed step-by-step analysis of vehicle behaviors.

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `simulation_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Tick command sent successfully | `application/json`: any |
| 404 | Simulation not found |  |
| 500 | Failed to send tick command |  |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /simulation_results/{simulation_id}` { #op-get-simulation-results-simulation-id }

*Get comprehensive simulation results*

Retrieve comprehensive results and analytics from completed simulation.

Provides access to complete simulation output including vehicle trajectories, collision data, performance metrics, and generated scenario files. Results include both quantitative metrics and qualitative behavioral analysis.

Args: simulation_id: UUID of the simulation to retrieve results for

Returns: dict: Complete simulation results including: - Execution status and completion state - Vehicle trajectory data (FCD files) - Collision detection results - Performance metrics and KPIs - Generated OpenSCENARIO files - Output directory paths for detailed files

Result states: - running: Simulation still executing, partial results available - completed: Full results available for analysis - failed: Error occurred, diagnostic information provided

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `simulation_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Simulation results retrieved successfully | `application/json`: any |
| 404 | Simulation not found |  |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `GET /simulation/{simulation_id}/state` { #op-get-simulation-simulation-id-state }

*Get complete simulation state snapshot*

Retrieve comprehensive real-time state information for active simulation.

Provides complete snapshot of current simulation state including: vehicle positions, speeds, accelerations, and behavioral states. Essential for real-time monitoring and analysis.

Args: simulation_id: UUID of the target simulation

Returns: dict: Complete simulation state including: - All vehicle states (position, velocity, acceleration) - Traffic light states - Environmental conditions - Simulation time and step information - Agent behavioral states

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `simulation_id` | path | string | yes |  |  |

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

### Co-simulation: agents

| Method | Path | Summary |
| --- | --- | --- |
| `POST` | [`/simulation/{simulation_id}/agent_command`](#op-post-simulation-simulation-id-agent-command) | Send real-time command to control specific agent |
| `POST` | [`/simulation/{simulation_id}/agent_commands_batch`](#op-post-simulation-simulation-id-agent-commands-batch) | Send coordinated batch commands to multiple agents |

#### `POST /simulation/{simulation_id}/agent_command` { #op-post-simulation-simulation-id-agent-command }

*Send real-time command to control specific agent*

Send real-time control commands to specific agents during simulation.

Enables precise control over individual vehicle or pedestrian behavior for testing specific scenarios and edge cases. Commands are queued and executed in the next simulation step.

Args: simulation_id: UUID of the simulation containing the target agent command: AgentCommand containing agent ID and control parameters

Returns: dict: Confirmation that command was queued for execution

Command types: - Lane change commands - Speed control (acceleration/deceleration) - Emergency braking - Route modifications - Behavioral parameter changes

Example: Force emergency braking for vehicle "car_1": { "agent_id": "car_1", "command_type": "emergency_brake", "parameters": {"deceleration": 8.0} }

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `simulation_id` | path | string | yes |  |  |

**Request body** (required): `application/json`, [`AgentCommand`](#schema-agentcommand)

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

#### `POST /simulation/{simulation_id}/agent_commands_batch` { #op-post-simulation-simulation-id-agent-commands-batch }

*Send coordinated batch commands to multiple agents*

Execute coordinated control commands across multiple agents simultaneously.

Enables complex scenario orchestration by controlling multiple vehicles and pedestrians in coordination. Commands are executed in the order provided for precise timing control.

Args: simulation_id: UUID of simulation containing the target agents command_batch: Collection of AgentCommand objects for batch execution

Returns: dict: Confirmation with count of commands successfully queued

Use cases: - Coordinated lane changes for convoy behavior - Simultaneous emergency responses - Traffic pattern manipulation - Multi-agent corner case generation

Example: Coordinate emergency braking across multiple vehicles: { "commands": [ {"agent_id": "car_1", "command_type": "emergency_brake"}, {"agent_id": "car_2", "command_type": "lane_change", "target_lane": 2} ] }

**Parameters**

| Parameter | In | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `simulation_id` | path | string | yes |  |  |

**Request body** (required): `application/json`, [`AgentCommandBatch`](#schema-agentcommandbatch)

**Responses**

| Status | Description | Body |
| --- | --- | --- |
| 200 | Successful Response | `application/json`: any |
| 422 | Validation Error | `application/json`: [`HTTPValidationError`](#schema-httpvalidationerror) |

## Schemas

### terasim-service: city API

#### `OptimizationRunSummary` { #schema-optimizationrunsummary }

One optimization run of the listing.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `run_id` | string | yes |  |  |
| `corridor_id` | string \| null | no |  |  |
| `origin` | `"scheduled"` \| `"api"` \| `"direct"` | yes |  | scheduled: the cadence scheduler; api: POST /v1/city/optimizations; direct: the synchronous POST /v1/city/optimize. |
| `status` | `"queued"` \| `"running"` \| `"completed"` \| `"failed"` \| `"lost"` | yes |  | lost: queued or running, but its worker stopped without recording an outcome. |
| `submitted_at` | string \| null | no |  | ISO-8601 UTC. |
| `completed_at` | string \| null | no |  | ISO-8601 UTC; null until the run completed or failed. |
| `intersection_ids` | array of string | yes |  | The run's intersections: those of its resolved corridor once completed, else those the request named. |
| `traffic_model` | `"sumo"` \| `"cnde"` \| null | no |  | The traffic model every candidate plan was simulated with: sumo (the deployment's default driver model) or cnde (the CNDE learned driver behavior, requested per run). Null for runs recorded before it was kept. |
| `kpis` | [`RunKpiSummary`](#schema-runkpisummary) \| null | no |  | Null until completed. |
| `nema_exported` | boolean | yes |  | Whether /nema serves a plan. |
| `has_report` | boolean | yes |  | Whether report.pdf can be served (rendered or renderable). |
| `sim_metrics` | `"ready"` \| `"pending"` \| `"unavailable"` | yes |  | State of the run's simulated signal metrics (/sim_metrics). |

Unknown fields are rejected.

#### `RunKpiSummary` { #schema-runkpisummary }

Baseline versus optimized (the exported plan) KPIs of a completed run.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `baseline` | [`RunKpis`](#schema-runkpis) | yes |  |  |
| `optimized` | [`RunKpis`](#schema-runkpis) | yes |  |  |

Unknown fields are rejected.

#### `RunKpis` { #schema-runkpis }

One side of a run's KPI summary (from the run's result, like the synchronous answer).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `cost` | number \| null | no |  | Weighted cost; the baseline is 1.0. |
| `control_delay_s_per_veh` | number \| null | no |  |  |
| `stops_per_veh` | number \| null | no |  |  |
| `vehicles_total` | integer \| null | no |  |  |

Unknown fields are rejected.

#### `HTTPValidationError` { #schema-httpvalidationerror }

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `detail` | array of [`ValidationError`](#schema-validationerror) | no |  |  |

#### `ValidationError` { #schema-validationerror }

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `loc` | array of string \| integer | yes |  |  |
| `msg` | string | yes |  |  |
| `type` | string | yes |  |  |
| `input` | any | no |  |  |
| `ctx` | object | no |  |  |

#### `OptimizationRequest` { #schema-optimizationrequest }

One corridor-optimization request. Unknown fields are rejected.

Demand comes from EXACTLY one of ``corridor_id`` (primary: server-side aggregation of buffered snapshots) or ``tmc_json`` (secondary/testing: an explicit file, ``base_sumocfg`` required).

``corridor_id`` is the snapshot QUEUE key — it must match the ``corridor_id`` field carried by the TMC payloads pushed to POST /v1/city/snapshots. A scheduled-config entry for the id is optional: when present it supplies defaults (explicit request fields win, and ``base_sumocfg`` may be omitted). When it is absent, the worker selects a covering cached map or downloads and persists an OSM extract.

``corridor`` names the GEOMETRY (from/to intersections or tls_ids) and may be combined with either demand source; when given alongside ``corridor_id`` it wins over any config entry's corridor.

An optional inline ``incident`` expedites the run via the reserved emergency concurrency headroom.

``sim_end=None`` means "use the server default" (1800 s, applied by the worker); when given it must be a realistic horizon (30..7200 s).

``stage_mode`` / ``search_cycle`` / ``cycle_min_s`` / ``cycle_max_s`` / ``flow_source`` / ``counts_source`` / ``seed_from_scenario_programs`` go straight to the optimization config, which validates them against each other (e.g. min < max). ``None`` means "server default", so a scheduled-config entry can supply them. A corridor_id request with ``counts_source="scenario_routes"`` needs no buffered snapshots, but then ``base_sumocfg`` and the corridor's ``tls_ids`` are required.

``traffic_model`` (``None`` = ``"sumo"``, the deployment's configured driver model) may be ``"cnde"``: every candidate of this run is then simulated with the CNDE learned driver behavior, much slower and GPU only, and ``n_workers`` is capped at the deployment's CNDE worker limit (422 where the CNDE runtime is not installed).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `tmc_json` | string \| null | no |  |  |
| `corridor_id` | string \| null | no |  |  |
| `base_sumocfg` | string \| null | no |  |  |
| `corridor` | [`CorridorRef`](#schema-corridorref) \| null | no |  |  |
| `baseline_sheet` | string \| null | no |  |  |
| `incident` | [`IncidentPayload`](#schema-incidentpayload) \| null | no |  |  |
| `optimizer` | `"auto"` \| `"exhaustive"` \| `"bayes_offsets"` \| `"bayes_splits"` \| `"bayes_corridor"` | no | `"auto"` |  |
| `max_trials` | integer | no | `20` | (>= 1, <= 200) |
| `patience` | integer | no | `8` |  |
| `kpi_weights` | object (string → number) \| null | no |  |  |
| `use_stored_plans` | boolean | no | `true` |  |
| `plan_time` | string \| null | no |  |  |
| `sim_end` | number \| null | no |  | (>= 30, <= 7200) |
| `n_workers` | integer | no | `2` | (>= 1, <= 8) |
| `seed` | integer | no | `42` | (>= 0) |
| `stage_mode` | `"two_stage"` \| `"per_approach"` \| null | no |  |  |
| `search_cycle` | boolean \| null | no |  |  |
| `cycle_min_s` | number \| null | no |  | (>= 20, <= 600) |
| `cycle_max_s` | number \| null | no |  | (>= 20, <= 600) |
| `flow_source` | `"corridor_graph"` \| `"scenario_routes"` \| null | no |  |  |
| `counts_source` | `"tmc"` \| `"scenario_routes"` \| null | no |  |  |
| `seed_from_scenario_programs` | boolean \| null | no |  |  |
| `traffic_model` | `"sumo"` \| `"cnde"` \| null | no |  |  |

Unknown fields are rejected.

#### `CorridorRef` { #schema-corridorref }

Corridor selection: explicit TLS ids, or a from/to intersection pair.

``from``/``to`` are the wire names (Python keywords, hence the aliases); the field names ``from_ref``/``to_ref`` are also accepted for direct (in-process) callers.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `from` | string \| null | no |  |  |
| `to` | string \| null | no |  |  |
| `tls_ids` | array of string | no |  |  |

Unknown fields are rejected.

#### `IncidentPayload` { #schema-incidentpayload }

Inline incident report: at least one stalled vehicle.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `stalled_vehicles` | array of [`StallItem`](#schema-stallitem) | yes |  | (min items 1) |

Unknown fields are rejected.

#### `StallItem` { #schema-stallitem }

One stalled-vehicle geometry as reported by the video analytics.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `latitude` | number | yes |  | (>= -90, <= 90) |
| `longitude` | number | yes |  | (>= -180, <= 180) |

Unknown fields are rejected.

#### `DirectOptimizationRequest` { #schema-directoptimizationrequest }

Minimal synchronous optimization contract.

Natural-language interpretation and name-to-coordinate resolution happen in the caller.  This model intentionally does not expose internal BO controls such as optimizer choice or trial budget. ``traffic_model`` picks what every candidate plan is simulated with: ``"sumo"`` (the default, the deployment's configured driver model) or ``"cnde"`` (the CNDE learned driver behavior: much slower, GPU only, 422 where it is not installed).

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `type` | `"intersection"` \| `"corridor"` | yes |  |  |
| `corridor_id` | string | yes |  | (min length 1) |
| `intersection` | [`GeoIntersectionRef`](#schema-geointersectionref) \| null | no |  |  |
| `corridor` | [`GeoCorridorRef`](#schema-geocorridorref) \| null | no |  |  |
| `cost` | [`OptimizationCost`](#schema-optimizationcost) | no |  |  |
| `traffic_model` | `"sumo"` \| `"cnde"` \| null | no |  |  |

Unknown fields are rejected.

#### `GeoIntersectionRef` { #schema-geointersectionref }

A TMC intersection the caller resolved; the service performs no NLP/geocoding.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `intersection_id` | string | yes |  | (min length 1) |
| `name` | string \| null | no |  |  |
| `lat` | number | yes |  | (>= -90, <= 90) |
| `lon` | number | yes |  | (>= -180, <= 180) |

Unknown fields are rejected.

#### `GeoCorridorRef` { #schema-geocorridorref }

Two explicit, separately-addressable corridor endpoints.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `from` | [`GeoIntersectionRef`](#schema-geointersectionref) | yes |  |  |
| `to` | [`GeoIntersectionRef`](#schema-geointersectionref) | yes |  |  |

Unknown fields are rejected.

#### `OptimizationCost` { #schema-optimizationcost }

Configurable weighted cost exposed to synchronous callers.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `weights` | object (string → number) | no |  |  |

Unknown fields are rejected.

#### `DirectOptimizationResponse` { #schema-directoptimizationresponse }

Complete synchronous response; no run id or internal path is exposed.

``unserved_movements`` (``{tls_id: [movement, ...]}``) names the vehicle movements the returned plan never lets move; it is present only when there are some. ``config`` (``{"flow_source", "counts_source"}``) says where the simulated demand came from; it is present only when either is the scenario's own routes. ``traffic_model`` (``"cnde"``) says every candidate was simulated with the CNDE learned driver behavior; it is present only for such a run. A default run answers exactly as before.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `nema_plan` | object | yes |  |  |
| `kpis` | [`DirectKpiResponse`](#schema-directkpiresponse) | yes |  |  |
| `unserved_movements` | object (string → array of string) \| null | no |  |  |
| `config` | object (string → string) \| null | no |  |  |
| `traffic_model` | `"sumo"` \| `"cnde"` \| null | no |  |  |

Unknown fields are rejected.

#### `DirectKpiResponse` { #schema-directkpiresponse }

Stable KPI response contract of the synchronous optimization.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `cost_weights` | object (string → number) | yes |  |  |
| `baseline` | [`DirectCostResult`](#schema-directcostresult) | yes |  |  |
| `optimized` | [`DirectCostResult`](#schema-directcostresult) | yes |  |  |

Unknown fields are rejected.

#### `DirectCostResult` { #schema-directcostresult }

One side of the baseline-versus-optimized KPI comparison.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `cost` | number | yes |  |  |
| `metrics` | object | yes |  |  |
| `timing` | object \| null | no |  |  |

Unknown fields are rejected.

#### `BaselinePlanResponse` { #schema-baselineplanresponse }

The plan a run measured its optimization against, as NEMA timing.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `run_id` | string | yes |  |  |
| `plan_id` | string | yes |  | The baseline's plan id in the run's trial ledger. |
| `source` | string | yes |  | Where the baseline came from: stored_nema (the uploaded signal plans), baseline_sheet (a timing sheet), webster_init (timings computed from the demand) or sumo_tls (the scenario's own signal programs). |
| `nema_plan` | object | yes |  | The baseline in the terasim.nema_timing.v1 shape of /nema: the stored plan as the run used it, or the export of the baseline static plan (stage rows with nema_phase null unless the plan numbers its phases). |

Unknown fields are rejected.

#### `SimMetricsDocument` { #schema-simmetricsdocument }

Simulated signal-performance measures of a run (schema corridor_ui.sim_metrics.v1).

The run's baseline and optimized trials re-simulated with virtual lane-area detectors: per approach, per signal and per corridor arrivals on green / red, occupancy, split failures, queues, delay, stops, phase utilization and degree of saturation. Simulated estimates of the plans under the run's demand, not field measurements.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `schema` | string | yes |  | corridor_ui.sim_metrics.v1 |
| `corridor_id` | string \| null | no |  |  |
| `run_id` | string | yes |  |  |
| `generated_at` | string | yes |  |  |
| `method` | object | yes |  | How the measures were computed. |
| `corridor_order` | array of string | yes |  | Signals in corridor order. |
| `mainline` | array of string | yes |  | Mainline directions (NB/SB or EB/WB). |
| `plans` | object | yes |  | "optimized" and "baseline": corridor, intersections, phase0_starts_s. |

Unknown fields are rejected.

#### `SimMetricsPending` { #schema-simmetricspending }

The simulated signal metrics of the run are still being computed.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `status` | string | no | `"pending"` | Always "pending". |

Unknown fields are rejected.

#### `AuditEvent` { #schema-auditevent }

One recorded event.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | string | yes |  | 32 hex characters. |
| `at` | string | yes |  | Service time, ISO-8601 UTC with milliseconds. |
| `type` | `"console_opened"` \| `"plan_viewed"` \| `"plan_downloaded"` \| `"feedback"` | yes |  |  |
| `corridor_id` | string | yes |  |  |
| `run_id` | string \| null | no |  |  |
| `intersection_id` | string \| null | no |  |  |
| `plan_id` | string \| null | no |  |  |
| `actor` | string | yes |  | Who: proxy identity, display name, or "anonymous". |
| `actor_source` | `"proxy"` \| `"self"` \| `"anonymous"` | yes |  |  |
| `detail` | object | yes |  |  |

#### `AuditSummary` { #schema-auditsummary }

Usage of the last N days.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `corridor_id` | string \| null | no |  |  |
| `days` | integer | yes |  |  |
| `timezone` | string | yes |  | Zone of the day buckets (UTC when the requested one is unknown). |
| `since` | string | yes |  | Start of the first day, ISO-8601 UTC. |
| `until` | string | yes |  | Now, ISO-8601 UTC. |
| `totals` | object (string → integer) | yes |  | Events per type in the window, and total. |
| `per_day` | array of object | yes |  | One entry per local day, oldest first. |
| `downloads_by_format` | object (string → integer) | yes |  |  |
| `feedback` | object | yes |  | events (all feedback events), accepted / rejected / needs_changes and acceptance_rate over each user's latest verdict per plan (null without verdicts). |
| `unique_actors` | integer | yes |  | Distinct actors other than anonymous. |
| `anonymous_events` | integer | yes |  |  |
| `actors` | array of object | yes |  | Up to 20 actors, most events first. |
| `last_activity` | object \| null | no |  | The newest event of the corridor at any time. |

### terasim-tmc-service

#### `RegisterRequest` { #tmc-schema-registerrequest }

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `intersection_id` | string | yes |  | (min length 1) |
| `name` | string | yes |  | (min length 1) |
| `lat` | number | yes |  | (>= -90, <= 90) |
| `lon` | number | yes |  | (>= -180, <= 180) |
| `corridor_id` | string \| null | no |  |  |

#### `HTTPValidationError` { #tmc-schema-httpvalidationerror }

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `detail` | array of [`ValidationError`](#tmc-schema-validationerror) | no |  |  |

#### `ValidationError` { #tmc-schema-validationerror }

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `loc` | array of string \| integer | yes |  |  |
| `msg` | string | yes |  |  |
| `type` | string | yes |  |  |
| `input` | any | no |  |  |
| `ctx` | object | no |  |  |

### terasim-service: legacy co-simulation endpoints

#### `SimulationConfig` { #schema-simulationconfig }

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `config_file` | string | yes |  | Path to the simulation configuration file |
| `auto_run` | boolean | no | `false` | Whether to automatically run the simulation or wait for manual control |

#### `SimulationStatus` { #schema-simulationstatus }

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `id` | string | yes |  | Unique identifier for the simulation |
| `status` | string | yes |  | Current status of the simulation |
| `progress` | number | no | `0.0` | Progress of the simulation as a percentage |

#### `SimulationCommand` { #schema-simulationcommand }

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `command` | string | yes |  | Control command for the simulation (e.g., 'pause', 'resume', 'stop') |

#### `AgentCommand` { #schema-agentcommand }

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `agent_id` | string | no | `""` |  |
| `agent_type` | string | no | `""` |  |
| `command_type` | string | no | `""` |  |
| `data` | object | no | `{}` |  |

#### `AgentCommandBatch` { #schema-agentcommandbatch }

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `commands` | array of [`AgentCommand`](#schema-agentcommand) | yes |  | List of agent commands to execute |
