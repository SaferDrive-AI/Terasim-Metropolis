# MCP tools

!!! note "Generated page"
    This page is generated from the code by `docs/_scripts/gen_reference.py`. Do not edit it by hand; re-run the script after changing the code.

`terasim-agent-skill` is an MCP server (server name `terasim-city-optimization`). With `TERASIM_MCP_TRANSPORT=streamable-http` it serves the MCP endpoint at `http://<host>:<TERASIM_MCP_PORT>/mcp` (port 9911 by default) and, with `TERASIM_ARTIFACT_ROOT` set, the result files at `/files/<artifact_id>/<file>`. Every tool forwards to the `terasim-service` REST API at `TERASIM_CITY_URL`; the MCP layer itself keeps no state. Input arguments are validated against the schemas below before any request is made.

| Tool | Group | Summary |
| --- | --- | --- |
| [`list_intersections`](#tool-list-intersections) | Intersection registry | List the corridor's known intersections and which ones are registered. |
| [`register_intersection`](#tool-register-intersection) | Intersection registry | Activate periodic TMC collection for a spec intersection (Skill 1). |
| [`upload_map`](#tool-upload-map) | Asset and data upload | Register a map file that already sits on the TeraSim asset volume: .sumocfg (prebuilt SUMO), .osm (OpenStreetMap or Lanelet2) or .xodr (OpenDRIVE); HD maps are converted to SUMO. |
| [`upload_signal_plan`](#tool-upload-signal-plan) | Asset and data upload | Upload the complete NEMA plan supplied by VSS without rewriting it. |
| [`upload_traffic_snapshot`](#tool-upload-traffic-snapshot) | Asset and data upload | Upload a canonical TMC snapshot without retaining client-side state. |
| [`optimize_registered_intersection`](#tool-optimize-registered-intersection) | Optimization | Optimize signal timing for an already-registered intersection by id only (the registry supplies name/lat/lon/corridor). |
| [`optimize_intersection`](#tool-optimize-intersection) | Optimization | Synchronously return the NEMA plan and KPIs for one intersection. |
| [`optimize_intersection_by_id`](#tool-optimize-intersection-by-id) | Optimization | Optimize one intersection from flat fields instead of a nested object. |
| [`optimize_corridor`](#tool-optimize-corridor) | Optimization | Synchronously return the NEMA plan and KPIs for a corridor. |

## Intersection registry

Registered only when `TERASIM_TMC_URL` points at a running `terasim-tmc-service`. A registered intersection is counted by the TMC collector every observation window.

### `list_intersections` { #tool-list-intersections }

List the corridor's known intersections and which ones are registered.

**Arguments**: none.

**Result**: the JSON answer of the service call.

### `register_intersection` { #tool-register-intersection }

Activate periodic TMC collection for a spec intersection (Skill 1).

``intersection_id`` must be one of the ids returned by ``list_intersections``; ``lat``/``lon`` must lie within 50 m of that intersection's spec location.

**Arguments**

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `intersection_id` | string | yes |  |  |
| `name` | string | yes |  |  |
| `lat` | number | yes |  |  |
| `lon` | number | yes |  |  |
| `corridor_id` | string \| null | no | `null` |  |

**Result**: the JSON answer of the service call.

## Asset and data upload

Map and timing-plan upload, and a manual TMC snapshot push (the TMC collector pushes snapshots on its own).

### `upload_map` { #tool-upload-map }

Register a map file that already sits on the TeraSim asset volume: .sumocfg (prebuilt SUMO), .osm (OpenStreetMap or Lanelet2) or .xodr (OpenDRIVE); HD maps are converted to SUMO. `proj` is optional: a proj.4 string for the map's own x/y when the file has no usable geo-reference (the error says when it is needed). Returns map_id, duplicate, source and a summary (edges, junctions, traffic_lights, geo_referenced, warnings).

**Arguments**

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `path` | string | yes |  |  |
| `proj` | string \| null | no | `null` |  |

**Result**: the JSON answer of the service call.

### `upload_signal_plan` { #tool-upload-signal-plan }

Upload the complete NEMA plan supplied by VSS without rewriting it.

**Arguments**

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `plan` | [`SignalPlanInput`](#model-signalplaninput) | yes |  |  |
| `corridor_id` | string \| null | no | `null` |  |

**Result**: the JSON answer of the service call.

### `upload_traffic_snapshot` { #tool-upload-traffic-snapshot }

Upload a canonical TMC snapshot without retaining client-side state.

**Arguments**

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `snapshot` | [`TrafficSnapshotInput`](#model-trafficsnapshotinput) | yes |  |  |

**Result**: the JSON answer of the service call.

## Optimization

Synchronous: each call runs the full optimization and answers when the NEMA plan and KPIs are ready. The result is two text content blocks: a ready-to-show Markdown summary (plan, KPI table, NEMA splits, download links when an artifact store is configured), then the JSON payload (`nema_plan`, `kpis`, and `artifact_id` / `downloads` when artifacts were saved).

### `optimize_registered_intersection` { #tool-optimize-registered-intersection }

Optimize signal timing for an already-registered intersection by id only (the registry supplies name/lat/lon/corridor). Prefer this over optimize_intersection. Synchronous; may take a few minutes. The result starts with a ready-to-show markdown block (plan, KPI table, NEMA splits, download links); present that block verbatim. Leave traffic_model unset unless the user explicitly asks for CNDE, Neural NDE, neural traffic or learned / naturalistic driver behavior; then pass "cnde" (needs a GPU, much slower).

**Arguments**

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `intersection_id` | string | yes |  |  |
| `cost` | [`OptimizationCostInput`](#model-optimizationcostinput) \| null | no | `null` |  |
| `traffic_model` | `"sumo"` \| `"cnde"` \| null | no | `null` | Traffic model that simulates every candidate plan. Leave it unset (SUMO traffic, the default) unless the user explicitly asks for CNDE, Neural NDE, neural traffic or learned / naturalistic driver behavior; then pass "cnde". A CNDE optimization needs a GPU and takes much longer (minutes per candidate plan instead of seconds). If the service says CNDE is not available, tell the user; do not re-run without it. |

**Result**: a Markdown text block, then a JSON text block.

### `optimize_intersection` { #tool-optimize-intersection }

Synchronously return the NEMA plan and KPIs for one intersection. The result starts with a ready-to-show markdown block (plan, KPI table, NEMA splits, download links); present that block verbatim. Leave traffic_model unset unless the user explicitly asks for CNDE, Neural NDE, neural traffic or learned / naturalistic driver behavior; then pass "cnde" (needs a GPU, much slower).

**Arguments**

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `corridor_id` | string | yes |  |  |
| `intersection` | [`IntersectionInput`](#model-intersectioninput) | yes |  |  |
| `cost` | [`OptimizationCostInput`](#model-optimizationcostinput) \| null | no | `null` |  |
| `traffic_model` | `"sumo"` \| `"cnde"` \| null | no | `null` | Traffic model that simulates every candidate plan. Leave it unset (SUMO traffic, the default) unless the user explicitly asks for CNDE, Neural NDE, neural traffic or learned / naturalistic driver behavior; then pass "cnde". A CNDE optimization needs a GPU and takes much longer (minutes per candidate plan instead of seconds). If the service says CNDE is not available, tell the user; do not re-run without it. |

**Result**: a Markdown text block, then a JSON text block.

### `optimize_intersection_by_id` { #tool-optimize-intersection-by-id }

Optimize one intersection from flat fields instead of a nested object. The result starts with a ready-to-show markdown block (plan, KPI table, NEMA splits, download links); present that block verbatim. Leave traffic_model unset unless the user explicitly asks for CNDE, Neural NDE, neural traffic or learned / naturalistic driver behavior; then pass "cnde" (needs a GPU, much slower).

**Arguments**

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `corridor_id` | string | yes |  |  |
| `intersection_id` | string | yes |  |  |
| `name` | string | yes |  |  |
| `lat` | number | yes |  |  |
| `lon` | number | yes |  |  |
| `cost` | [`OptimizationCostInput`](#model-optimizationcostinput) \| null | no | `null` |  |
| `traffic_model` | `"sumo"` \| `"cnde"` \| null | no | `null` | Traffic model that simulates every candidate plan. Leave it unset (SUMO traffic, the default) unless the user explicitly asks for CNDE, Neural NDE, neural traffic or learned / naturalistic driver behavior; then pass "cnde". A CNDE optimization needs a GPU and takes much longer (minutes per candidate plan instead of seconds). If the service says CNDE is not available, tell the user; do not re-run without it. |

**Result**: a Markdown text block, then a JSON text block.

### `optimize_corridor` { #tool-optimize-corridor }

Synchronously return the NEMA plan and KPIs for a corridor. The result starts with a ready-to-show markdown block (plan, KPI table, NEMA splits, download links); present that block verbatim. Leave traffic_model unset unless the user explicitly asks for CNDE, Neural NDE, neural traffic or learned / naturalistic driver behavior; then pass "cnde" (needs a GPU, much slower).

**Arguments**

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `corridor_id` | string | yes |  |  |
| `from_intersection` | [`IntersectionInput`](#model-intersectioninput) | yes |  |  |
| `to_intersection` | [`IntersectionInput`](#model-intersectioninput) | yes |  |  |
| `cost` | [`OptimizationCostInput`](#model-optimizationcostinput) \| null | no | `null` |  |
| `traffic_model` | `"sumo"` \| `"cnde"` \| null | no | `null` | Traffic model that simulates every candidate plan. Leave it unset (SUMO traffic, the default) unless the user explicitly asks for CNDE, Neural NDE, neural traffic or learned / naturalistic driver behavior; then pass "cnde". A CNDE optimization needs a GPU and takes much longer (minutes per candidate plan instead of seconds). If the service says CNDE is not available, tell the user; do not re-run without it. |

**Result**: a Markdown text block, then a JSON text block.

## Input models

Nested argument types, as published in the tools' input schemas.

### `IntersectionInput` { #model-intersectioninput }

One complete intersection already resolved by the VSS Agent.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `intersection_id` | string | yes |  | Stable intersection ID selected from TMC data sent by VSS. (min length 1) |
| `name` | string | yes |  | Stable human-readable name used by the VSS Agent. (min length 1) |
| `lat` | number | yes |  | WGS84 latitude as a separate numeric field. (>= -90, <= 90) |
| `lon` | number | yes |  | WGS84 longitude as a separate numeric field. (>= -180, <= 180) |

Unknown fields are rejected.

### `OptimizationCostInput` { #model-optimizationcostinput }

Optional VSS-visible KPI weights; BO controls remain server-side.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `weights` | [`OptimizationWeightsInput`](#model-optimizationweightsinput) | yes |  | Positive KPI weights, such as control_delay and stops. |

Unknown fields are rejected.

### `OptimizationWeightsInput` { #model-optimizationweightsinput }

The only KPI names exposed to VSS; at least one must be supplied.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `control_delay` | number | no | `null` | (> 0) |
| `stops` | number | no | `null` | (> 0) |

Unknown fields are rejected.

### `PlanChangeIntervalsInput` { #model-planchangeintervalsinput }

Optional intersection-level clearance fallback.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `yellow_s` | number | no | `null` | (>= 0) |
| `yellow_src` | `"read"` \| `"inferred"` \| `"inherited"` \| `"default"` | no | `null` |  |
| `all_red_s` | number | no | `null` | (>= 0) |
| `all_red_src` | `"read"` \| `"inferred"` \| `"inherited"` \| `"default"` | no | `null` |  |

Unknown fields are rejected.

### `PlanPatternInput` { #model-planpatterninput }

One time-of-day coordination pattern.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `pattern_id` | string | yes |  | (min length 1) |
| `description` | string | no | `null` |  |
| `cycle_s` | number | yes |  | (> 0) |
| `cycle_src` | `"read"` \| `"inferred"` \| `"inherited"` \| `"default"` | no | `null` |  |
| `offset_s` | number | no | `null` | (>= 0) |
| `offset_src` | `"read"` \| `"inferred"` \| `"inherited"` \| `"default"` | no | `null` |  |
| `coordinated` | boolean | no | `null` |  |
| `splits` | object (string → number) | yes |  |  |

Unknown fields are rejected.

### `PlanPhaseInput` { #model-planphaseinput }

One net-independent NEMA phase extracted from the source Plan.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `phase` | integer | yes |  | (>= 1, <= 8) |
| `movements` | array of `"EB-left"` \| `"EB-through"` \| `"EB-right"` \| `"WB-left"` \| `"WB-through"` \| `"WB-right"` \| `"NB-left"` \| `"NB-through"` \| `"NB-right"` \| `"SB-left"` \| `"SB-through"` \| `"SB-right"` | yes |  | (min items 1) |
| `min_green_s` | number | no | `null` | (> 0) |
| `min_green_src` | `"read"` \| `"inferred"` \| `"inherited"` \| `"default"` | no | `null` |  |
| `yellow_s` | number | no | `null` | (>= 0) |
| `yellow_src` | `"read"` \| `"inferred"` \| `"inherited"` \| `"default"` | no | `null` |  |
| `all_red_s` | number | no | `null` | (>= 0) |
| `all_red_src` | `"read"` \| `"inferred"` \| `"inherited"` \| `"default"` | no | `null` |  |

Unknown fields are rejected.

### `PlanProvenanceInput` { #model-planprovenanceinput }

Audit metadata required by the PDF conversion contract.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `source_files` | array of string | yes |  |  |
| `generated_by` | string | no | `null` |  |
| `generated_at` | string | no | `null` |  |
| `handoff_ready` | boolean | yes |  |  |
| `gaps` | array of string | yes |  |  |

Unknown fields are rejected.

### `PlanRingsInput` { #model-planringsinput }

Dual-ring ordering and the coordination anchor.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `ring1` | array of integer | yes |  |  |
| `ring2` | array of integer | yes |  |  |
| `barrier_phases` | array of integer | no | `null` |  |
| `barrier2_phases` | array of integer | no | `null` |  |
| `coordinated_phases` | array of integer | yes |  | (min items 1) |
| `src` | `"read"` \| `"inferred"` \| `"inherited"` \| `"default"` | no | `null` |  |

Unknown fields are rejected.

### `PlanScheduleRuleInput` { #model-planscheduleruleinput }

One per-intersection time-of-day schedule rule.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `days` | array of `"Mon"` \| `"Tue"` \| `"Wed"` \| `"Thu"` \| `"Fri"` \| `"Sat"` \| `"Sun"` | yes |  | (min items 1) |
| `start` | string | yes |  | (pattern `^([01][0-9]\|2[0-3]):[0-5][0-9]$`) |
| `pattern_id` | string | yes |  | (min length 1) |

Unknown fields are rejected.

### `SignalPlanInput` { #model-signalplaninput }

Top-level ``terasim.nema_plan.v1`` document: a controller timing plan as JSON.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `schema` | `"terasim.nema_plan.v1"` | yes |  | TeraSim NEMA Plan document version. |
| `corridor_id` | string | no | `null` | (min length 1) |
| `intersections` | array of [`SignalPlanIntersectionInput`](#model-signalplanintersectioninput) | yes |  | (min items 1) |
| `provenance` | [`PlanProvenanceInput`](#model-planprovenanceinput) | yes |  |  |

Unknown fields are rejected.

### `SignalPlanIntersectionInput` { #model-signalplanintersectioninput }

One complete intersection from ``terasim.nema_plan.v1``.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `intersection_id` | string | yes |  | Stable intersection ID selected from TMC data sent by VSS. (min length 1) |
| `name` | string | yes |  | Stable human-readable name used by the VSS Agent. (min length 1) |
| `lat` | number | yes |  | WGS84 latitude as a separate numeric field. (>= -90, <= 90) |
| `lon` | number | yes |  | WGS84 longitude as a separate numeric field. (>= -180, <= 180) |
| `controller` | object | no | `null` |  |
| `phases` | array of [`PlanPhaseInput`](#model-planphaseinput) | yes |  | (min items 1) |
| `rings` | [`PlanRingsInput`](#model-planringsinput) | yes |  |  |
| `change_intervals` | [`PlanChangeIntervalsInput`](#model-planchangeintervalsinput) | no | `null` |  |
| `patterns` | array of [`PlanPatternInput`](#model-planpatterninput) | yes |  |  |
| `schedule` | array of [`PlanScheduleRuleInput`](#model-planscheduleruleinput) | yes |  |  |
| `free` | boolean | no | `null` |  |

Unknown fields are rejected.

### `TMCCountsInput` { #model-tmccountsinput }

Allowed external TMC approach-direction labels.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `South` | [`TMCTurnCountsInput`](#model-tmcturncountsinput) | no | `null` |  |
| `West` | [`TMCTurnCountsInput`](#model-tmcturncountsinput) | no | `null` |  |
| `North` | [`TMCTurnCountsInput`](#model-tmcturncountsinput) | no | `null` |  |
| `East` | [`TMCTurnCountsInput`](#model-tmcturncountsinput) | no | `null` |  |

Unknown fields are rejected.

### `TMCIntersectionInput` { #model-tmcintersectioninput }

Intersection identity plus one observation window's turning counts.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `intersection_id` | string | yes |  | Stable intersection ID selected from TMC data sent by VSS. (min length 1) |
| `name` | string | yes |  | Stable human-readable name used by the VSS Agent. (min length 1) |
| `lat` | number | yes |  | WGS84 latitude as a separate numeric field. (>= -90, <= 90) |
| `lon` | number | yes |  | WGS84 longitude as a separate numeric field. (>= -180, <= 180) |
| `counts` | [`TMCCountsInput`](#model-tmccountsinput) | yes |  | Direction to movement to observed vehicle count. |

Additional fields are accepted.

### `TMCTurnCountsInput` { #model-tmcturncountsinput }

Allowed external TMC turn labels for one approach direction.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `Right` | integer | no | `null` | (>= 0) |
| `Straight` | integer | no | `null` | (>= 0) |
| `Left` | integer | no | `null` | (>= 0) |

Unknown fields are rejected.

### `TrafficSnapshotInput` { #model-trafficsnapshotinput }

Canonical 10-minute or 15-minute TMC snapshot.

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `corridor_id` | string | yes |  | (min length 1) |
| `timestamp_start` | string | yes |  | ISO-8601 start time for the observation window. |
| `observation_interval` | string | yes |  | Observation duration, normally '10m' or '15m'. |
| `intersections` | array of [`TMCIntersectionInput`](#model-tmcintersectioninput) | yes |  | (min items 1) |

Additional fields are accepted.
