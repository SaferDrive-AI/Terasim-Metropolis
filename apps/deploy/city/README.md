# TeraSim Metropolis — deployment guide

TeraSim Metropolis optimizes the signal timing of corridors that NVIDIA Metropolis VSS
(Video Search and Summarization) observes. VSS detects and tracks vehicles; the TeraSim
TMC collector turns its behavior objects into turning-movement counts (TMC); the TeraSim
service simulates the corridor in SUMO, searches cycle, splits and offsets, and returns a
NEMA timing plan with a before/after KPI comparison. The VSS agent calls it through MCP.

This directory holds everything needed to run it: the image definition (`Dockerfile`,
build notes in `BUILD.md`), a single-host Compose stack (`phase1/`), the
OpenShift-ready Helm chart (`helm/terasim-phase1/`, with `VSS-WIRING.md` for the VSS
agent configuration) and the self-tests.

## 1. The image

`terasim-metropolis:0.1.0` (from a registry: `<registry>/terasim-metropolis:0.1.0`),
linux/amd64. One image, three commands:

| Command | Port | Role |
| --- | --- | --- |
| `terasim-service` (default) | 8000 | REST API `/v1/city/*`; SUMO simulation and Bayesian search; starts an embedded Redis unless `TERASIM_REDIS_HOST` names another host |
| `terasim-agent-skill` | 9911 | MCP server (streamable HTTP at `/mcp`) and the `/files` downloads of each result |
| `terasim-tmc-service` | 8200 | TMC collector: VSS behavior objects → turning-movement counts → snapshots for the service |

Read-only content baked into the image:

| Path | Content |
| --- | --- |
| `/data/input/map` | the synthetic demo corridor's SUMO map: `demo.sumocfg`, `demo.net.xml`, `demo.tls.xml`, `demo.rou.xml` (an E-W arterial with the signals `J1`, `J2`, `J3` about 300 m apart; the demand comes from the TMC snapshots) |
| `/data/input/demo_scenario` | the same corridor as a complete SUMO scenario (its own routes and signal programs) with a placeholder TMC file |
| `/licenses` | `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md` |
| `/app/TeraSim/apps/deploy/city/phase1` | the self-tests `phase1_e2e.py` and `scenario_e2e.py` and the VSS behavior stub `stub_behavior_server.py` |

Writable paths: `/data/output` (map store, plan store and run directories),
`/data/artifacts` (MCP downloads), `/data/tmc` (TMC registry) and `$HOME`
(`/home/terasim`). The image runs as uid 1001 / gid 0 and works under any uid with gid 0
(OpenShift `restricted-v2`); all ports are above 1024.

## 2. Single container: the REST API

```bash
docker run -d -p 8000:8000 --restart unless-stopped --name terasim terasim-metropolis:0.1.0
```

The container starts Redis, then the service on `http://localhost:8000` (logs:
`docker logs -f terasim`). Optimize the demo corridor:

```bash
# 1) Register the baked demo map.
curl -s -X POST http://localhost:8000/v1/city/maps -H "Content-Type: application/json" \
  -d '{"path": "/data/input/map/demo.sumocfg"}'

# 2) Send a 10-minute TMC snapshot (counts by direction of travel and turn) for every
#    signal of the corridor.
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
curl -s -X POST http://localhost:8000/v1/city/snapshots -H "Content-Type: application/json" --data @- <<EOF
{"corridor_id": "demo_arterial", "observation_interval": "10m", "timestamp_start": "$NOW",
 "intersections": [
  {"intersection_id": "i1", "name": "Arterial Ave & 1st Cross St", "lat": 37.335, "lon": -121.90339,
   "counts": {"East": {"Left": 10, "Straight": 93, "Right": 15}, "West": {"Left": 8, "Straight": 67, "Right": 13},
              "North": {"Left": 8, "Straight": 20, "Right": 12}, "South": {"Left": 7, "Straight": 18, "Right": 10}}},
  {"intersection_id": "i2", "name": "Arterial Ave & 2nd Cross St", "lat": 37.335, "lon": -121.9,
   "counts": {"East": {"Left": 12, "Straight": 98, "Right": 13}, "West": {"Left": 9, "Straight": 72, "Right": 12},
              "North": {"Left": 8, "Straight": 23, "Right": 13}, "South": {"Left": 8, "Straight": 22, "Right": 11}}},
  {"intersection_id": "i3", "name": "Arterial Ave & 3rd Cross St", "lat": 37.335, "lon": -121.89661,
   "counts": {"East": {"Left": 9, "Straight": 87, "Right": 17}, "West": {"Left": 10, "Straight": 63, "Right": 15},
              "North": {"Left": 7, "Straight": 17, "Right": 10}, "South": {"Left": 8, "Straight": 25, "Right": 12}}}]}
EOF
# -> {"corridor_id":"demo_arterial","buffered":1}

# 3) Optimize the corridor from i1 to i3. The request blocks until the plan is ready.
curl -s -X POST http://localhost:8000/v1/city/optimize -H "Content-Type: application/json" \
  -d '{"type": "corridor", "corridor_id": "demo_arterial",
       "corridor": {"from": {"intersection_id": "i1", "name": "Arterial Ave & 1st Cross St", "lat": 37.335, "lon": -121.90339},
                    "to":   {"intersection_id": "i3", "name": "Arterial Ave & 3rd Cross St", "lat": 37.335, "lon": -121.89661}},
       "cost": {"weights": {"control_delay": 0.7, "stops": 0.3}}}'
```

The answer is the NEMA timing plan and the KPI comparison:

```json
{
  "nema_plan": {"schema": "terasim.nema_timing.v1", "intersections": []},
  "kpis": {
    "cost_weights": {"control_delay": 0.7, "stops": 0.3},
    "baseline": {"cost": 1.0, "metrics": {}, "timing": {}},
    "optimized": {"cost": 0.92, "metrics": {}, "timing": {}}
  }
}
```

A single intersection is `{"type": "intersection", "corridor_id": ..., "intersection": {...}}`.
`bash apps/deploy/city/smoke.sh` (with `IMAGE=` naming the image) runs these three steps
in a throwaway container and checks the answer.

## 3. One host: Docker Compose

`phase1/compose.yml` runs the three services from the same image, published on loopback
(`127.0.0.1:8100` service, `127.0.0.1:9911` MCP, `127.0.0.1:8200` TMC collector), with the
corridor configuration and TMC spec of the demo corridor mounted at `/config`:

```bash
docker compose -f apps/deploy/city/phase1/compose.yml build
# offline: the "stub" profile adds vss-behavior-stub, a synthetic VSS behavior API for the
# demo corridor's twelve camera sensors (the TMC collector's default VSS_BEHAVIOR_URL)
docker compose -f apps/deploy/city/phase1/compose.yml --profile stub up -d
# with a VSS deployment instead:
VSS_BEHAVIOR_URL=http://<vss-host>:8081 docker compose -f apps/deploy/city/phase1/compose.yml up -d
```

End-to-end check of the whole chain (register intersection `i1` → one TMC window → MCP
`upload_map` → MCP `optimize_intersection`; it prints the KPIs and `=== PASS ===`):

```bash
docker compose -f apps/deploy/city/phase1/compose.yml run --rm --no-deps terasim-mcp \
  python apps/deploy/city/phase1/phase1_e2e.py \
  --service http://terasim-service:8000 --tmc http://terasim-tmc:8200 --mcp http://terasim-mcp:9911/mcp
```

`--with-plan <file>` uploads a `terasim.nema_plan.v1` plan first. A `run-once` inside an
observation window that was already injected answers `skipped`, which the check accepts.

Scenario self-test (a complete SUMO scenario through the asynchronous API: demand and
turning counts from the scenario's own routes, its own signal programs as the baseline,
per-approach stages and a joint cycle/offset search; it ends with `=== SCENARIO PASS ===`):

```bash
docker compose -f apps/deploy/city/phase1/compose.yml run --rm --no-deps terasim-mcp \
  python apps/deploy/city/phase1/scenario_e2e.py --service http://terasim-service:8000
```

The scenario registered by this self-test stays in the map store and covers the same area as
the demo map, so run it on a stack whose requests name their map (the demo corridor's
configuration does) or on a throwaway stack (`docker compose down -v` afterwards).

From a checkout with the packages installed, `phase1/optimize_with_plan.py` uploads a plan
and optimizes over MCP in one go, and `phase1/fetch_async_run.py` waits for an
asynchronous run and saves its NEMA plan and KPIs.

## 4. Kubernetes and OpenShift: Helm

The chart `helm/terasim-phase1` (version 0.6.0, application version 0.1.0) deploys the
three services, a password-protected Redis, PVCs, NetworkPolicies and an OpenShift Route
for the downloads, under the default `restricted-v2` SCC:

```bash
helm upgrade --install terasim-phase1 apps/deploy/city/helm/terasim-phase1 -n terasim --create-namespace \
  --set image.repository=<registry>/terasim-metropolis --set image.tag=0.1.0 \
  --set global.externalHost=terasim.apps.<cluster-domain> \
  --set tmc.vssBehaviorUrl=http://vss-video-analytics-api.<vss-namespace>.svc:8081
```

Values, security model, Redis authentication and NetworkPolicies: `helm/terasim-phase1/README.md`.
VSS agent configuration (tool group, prompt steps): `helm/terasim-phase1/VSS-WIRING.md`.

## 5. Connecting VSS

- **VSS agent → MCP.** The agent's `mcp_client` tool group connects to
  `http://<mcp-host>:9911/mcp` (streamable HTTP). The tools are listed in
  `packages/terasim-agent-skill/README.md`; the preferred call is
  `optimize_registered_intersection(intersection_id)`. Each optimize result starts with a
  ready-to-show markdown block (plan, KPI table, NEMA splits, download links), followed by
  the JSON payload.
- **TMC collector → VSS behavior API.** `VSS_BEHAVIOR_URL` is the VSS video-analytics API
  (port 8081). `TMC_SPEC_FILE` is the corridor spec (`Metadata.json`, the format of
  NVIDIA's `generate_tmc.py`): per intersection its location, `Headings` (edge →
  direction of travel), `Movements` (heading pair → turn) and `Assignments` (approach →
  camera sensor). `register_intersection` activates an intersection of the spec; every
  observation interval (10 minutes, after a settle lag) the collector counts the
  registered intersections and posts the snapshot to the service.
- **Downloads.** With `TERASIM_ARTIFACT_ROOT` set, every optimize result is also written as
  `timing_report.pdf` (signal-controller timing-report style, current vs optimized plan),
  `nema_plan.json`, `current_plan.json` (when the plan store has the intersection's plan)
  and `optimization.json`, served at `GET /files/<artifact_id>/<file>`.
  `TERASIM_MCP_PUBLIC_URL` is the address readers open: a reverse proxy (or the chart's
  Route) that forwards `<public>/files/` to the MCP server's `/files/`.

## 6. Operating modes

- **On demand:** the VSS agent invokes the optimization tool when a user asks.
- **Scheduled:** the VSS agent invokes the same tool on its own cadence; no separate logic
  is involved.
- **Built-in scheduler (optional):** with `TERASIM_CITY_SCHEDULER_ENABLED=true` the service
  optimizes every corridor of its corridor configuration (`TERASIM_CITY_SCHEDULED_CONFIG`)
  once per `TERASIM_CITY_CADENCE_S` (default 3600 s) from the snapshots of the last
  `TERASIM_CITY_WINDOW_S` seconds. Off by default. `GET /v1/city/scheduled/{corridor_id}/latest`
  names the latest scheduled run.

## 7. Corridor configuration

`TERASIM_CITY_SCHEDULED_CONFIG` names a YAML file with one entry per corridor, keyed by the
`corridor_id` the TMC snapshots carry. The synchronous optimize call uses the entry's keys
as defaults; the built-in scheduler optimizes every entry. The shipped file
(`phase1/scheduled_config.yaml`, also `helm/terasim-phase1/configs/scheduled_config.yaml`):

```yaml
corridors:
  demo_arterial:
    base_sumocfg: /data/input/map/demo.sumocfg   # the map to simulate
    corridor:
      tls_ids: [J1, J2, J3]                      # or from/to intersection ids
    sim_end: 1800                                # seconds simulated per trial
    max_trials: 24                               # search budget
    patience: 16                                 # trials without improvement before stopping
    n_workers: 4                                 # SUMO processes (worker slots) per run
```

Further keys: `baseline_sheet`, `stage_mode` (`two_stage` / `per_approach`),
`search_cycle` with `cycle_min_s` / `cycle_max_s`, `flow_source` and `counts_source`
(`scenario_routes` for a complete scenario), `seed_from_scenario_programs`, `optimizer`,
and, for the scheduler only, `rerun_unchanged`.

**Another corridor, more intersections or cameras.** The corridor configuration and the TMC
spec (`Metadata.json`) are generated together from one camera inventory by
`camera-config/camcfg.py` (corridor → intersections → cameras), which also writes the Helm
values (`corridorConfig`), a ConfigMap, the intersection and map registration scripts, the VST
stream registration and the VA-API calibration entries. With Compose, mount the two generated
files at `/config` instead of the shipped ones; with Helm, pass the generated
`values-corridor.yaml`. See `camera-config/README.md`, including a 32-camera example on eight
signals.

## 8. Maps

- The demo corridor's map is baked at `/data/input/map/demo.sumocfg`.
- `POST /v1/city/maps` (MCP `upload_map`) registers a map file on the asset volume:
  `.sumocfg` (prebuilt SUMO), `.osm` (OpenStreetMap or Lanelet2) or `.xodr` (OpenDRIVE);
  HD maps are converted to SUMO. `proj` (a proj.4 string) geo-references a map whose own
  coordinates carry no usable reference. Maps are kept in the map store under
  `TERASIM_CITY_DATA_ROOT` (`GET /v1/city/maps` lists them).
- A request without an explicit map uses the stored map that covers its intersections;
  when none does, the service downloads an OpenStreetMap extract from Overpass, converts
  it and stores it for reuse. `TERASIM_CITY_OSM_DOWNLOAD=0` switches downloads off (a
  request no stored map covers then gets 422 naming the setting); set it where the maps
  ship as files.
- Persist `/data/output` to keep the map store, the plan store and the run history across
  restarts.

## 9. Signal plans

`POST /v1/city/plans` (MCP `upload_signal_plan`) stores a `terasim.nema_plan.v1` document:
per intersection its NEMA phases, rings, coordination patterns and time-of-day schedule.
Plans are kept per `(corridor_id, intersection_id)`, the latest upload wins, and an upload
within 30 m of a stored intersection replaces it. At run time a plan within 60 m of a map
junction is that junction's current plan: the run optimizes the pattern the schedule
selects for the local time of day (the timezone follows from the corridor's coordinates)
and measures the plan as read as its baseline. Without a stored plan the baseline is the
corridor's timing sheet (`baseline_sheet`) or, by default, a Webster plan computed from the
observed counts; `seed_from_scenario_programs` makes a complete scenario's own signal
programs the baseline. `GET /v1/city/plans` lists the stored plans.

## 10. Environment variables

terasim-service:

| Variable | Default | Meaning |
| --- | --- | --- |
| `TERASIM_CITY_INPUT_ROOT` / `TERASIM_CITY_OUTPUT_ROOT` / `TERASIM_CITY_DATA_ROOT` | `/data/input` / `/data/output` / `/data/output/store` (image) | read-only inputs, run directories, map and plan store |
| `TERASIM_CITY_SCHEDULED_CONFIG` | unset | corridor configuration file (section 7) |
| `TERASIM_CITY_SCHEDULER_ENABLED` / `TERASIM_CITY_CADENCE_S` | off / `3600` | built-in scheduler (section 6) |
| `TERASIM_CITY_WINDOW_S` | `3600` | seconds of buffered snapshots an optimization aggregates |
| `TERASIM_CITY_MAX_CONCURRENCY` | `3` (Compose and chart: `8`) | worker slots all runs together may hold (section 12) |
| `TERASIM_CITY_EMERGENCY_SLOTS` | `2` | extra slots for incident runs |
| `TERASIM_CITY_LEASE_TTL` | `120` | seconds a run keeps its slots without a worker heartbeat |
| `TERASIM_CITY_OSM_DOWNLOAD` | `1` | OpenStreetMap downloads (section 8) |
| `TERASIM_CITY_OVERPASS_URL` / `TERASIM_CITY_OVERPASS_TIMEOUT_S` / `TERASIM_CITY_MAP_MARGIN_M` | `https://overpass-api.de/api/interpreter` / `60` / `250` | Overpass endpoint, request timeout, margin in metres around the requested intersections |
| `TERASIM_CITY_SIM_METRICS` / `TERASIM_CITY_SIM_METRICS_TIMEOUT_S` / `TERASIM_CITY_SIM_METRICS_SLOT_WAIT_S` | `1` / `900` / `3600` | simulated signal-performance measures of every completed run (section 11) |
| `TERASIM_CITY_AUDIT_ENABLED` | `1` | audit log endpoints (section 11) |
| `TERASIM_REDIS_HOST` / `PORT` / `DB` / `PASSWORD` | `127.0.0.1` / `6379` / `0` / unset | Redis; a local host starts the embedded Redis, any other host is used as an external Redis |

terasim-agent-skill (MCP server):

| Variable | Default | Meaning |
| --- | --- | --- |
| `TERASIM_CITY_URL` / `TERASIM_CITY_TIMEOUT_S` | `http://127.0.0.1:8000` / `1800` | the service and the timeout of a synchronous optimization |
| `TERASIM_TMC_URL` | unset | the TMC collector; enables `list_intersections`, `register_intersection` and `optimize_registered_intersection` |
| `TERASIM_MCP_TRANSPORT` / `TERASIM_MCP_HOST` / `TERASIM_MCP_PORT` / `TERASIM_MCP_STATELESS` | `stdio` / `0.0.0.0` / `9911` / `0` | transport (`streamable-http` for VSS), bind address, stateless HTTP mode |
| `TERASIM_MCP_HEARTBEAT_S` | `2` | seconds between the progress and log notifications that keep a long call's stream alive |
| `TERASIM_ARTIFACT_ROOT` / `TERASIM_MCP_PUBLIC_URL` | unset / `http://127.0.0.1:<port>` | downloads (section 5) |
| `TERASIM_CITY_DATA_ROOT` | unset | the service's store, mounted read-only: the PDF report prints the current plan |

terasim-tmc-service (TMC collector):

| Variable | Default | Meaning |
| --- | --- | --- |
| `TMC_SPEC_FILE` | required | corridor spec (`Metadata.json`) |
| `VSS_BEHAVIOR_URL` | required | VSS behavior API, e.g. `http://<vss-host>:8081` |
| `TERASIM_CITY_URL` | `http://127.0.0.1:8000` | the service the snapshots go to |
| `TMC_REGISTRY_FILE` | `./tmc_registry.json` | registered intersections (persisted) |
| `TMC_SETTLE_LAG_S` | `180` | seconds a window must be over before it is counted |
| `TMC_COUNTED_OBJECT_TYPES` | `Vehicle,two_wheeler` | detector labels counted as traffic (case-insensitive; a detector labelling `car` / `bicycle` needs `car,bicycle`) |
| `TMC_AUTOSTART` / `TMC_HOST` / `TMC_PORT` | `1` / `0.0.0.0` / `8200` | periodic loop, bind address |

## 11. Run history, reports, simulated metrics and audit log

Every optimization is kept under `TERASIM_CITY_OUTPUT_ROOT/<run_id>/` and recorded in the
job store (seven days), whatever started it: the built-in scheduler,
`POST /v1/city/optimizations` (asynchronous; also accepts an inline `incident` with
stalled vehicles, which expedites the run) or the synchronous `POST /v1/city/optimize`
(listed with origin `direct`).

| Endpoint | Answer |
| --- | --- |
| `GET /v1/city/optimizations?corridor_id=&limit=` | the runs, newest first (`limit` default 20, at most 200): origin (`scheduled` / `api` / `direct`), status (`queued` / `running` / `completed` / `failed` / `lost`), times, intersections, KPI summary, and which of the NEMA plan, the report and the simulated metrics can be served |
| `GET /v1/city/optimizations/{run_id}/status`, `/result`, `/nema` | progress, the full result, the NEMA timing plan |
| `GET /v1/city/optimizations/{run_id}/baseline` | the plan the run was measured against, in the shape of `/nema`; 409 until completed |
| `GET /v1/city/optimizations/{run_id}/sim_metrics` | simulated signal-performance measures: 200 with the document, 202 while they are computed, 404 with the reason when disabled or failed |
| `GET /v1/city/optimizations/{run_id}/report.pdf` | the signal timing report (current plan, optimized plan, KPI comparison); 409 until completed |

The simulated metrics re-run the baseline and the exported plan with virtual detectors
on every signalized approach after a run completes (two extra SUMO runs holding one
worker slot).

The audit log records operator feedback on plans (`accepted` / `rejected` /
`needs_changes` with an optional comment), plan views, downloads and client sessions:
`POST /v1/city/audit/events`, `GET /v1/city/audit/events` (filters, newest first),
`GET /v1/city/audit/events.csv`, `GET /v1/city/audit/summary`. The actor is the user an
authenticating proxy names (`X-Forwarded-User`, `X-Forwarded-Email` or `X-Remote-User`)
or the name the client sends. Events are appended to
`TERASIM_CITY_OUTPUT_ROOT/audit/events-YYYY-MM.jsonl`.

## 12. Worker slots and disk

Every run starts `n_workers` SUMO processes (one CPU core each) and holds that many worker
slots until it finishes; all runs together hold at most `TERASIM_CITY_MAX_CONCURRENCY`.
A request or corridor entry whose `n_workers` exceeds it is rejected with 422; a request
that fits while other runs hold the slots gets 429 and can be retried; the scheduler
retries on its next pass. Size the CPU to the slots expected in use at once.

Each trial keeps its SUMO outputs in the run directory: a run on the demo corridor takes a
few MB, a large network simulated for 1800 s about 2–3 MB per trial. Run directories are
kept; delete old `/data/output/<run_id>/` and `/data/output/scheduled/<corridor>/`
directories when they are no longer needed.

## 13. Optional Neural NDE (GPU): per-request traffic model and refine pass

Every candidate plan is evaluated with SUMO's built-in driver model
(`vehicle_behavior="sumo_passive"`). A GPU deployment with the Neural NDE runtime and its
checkpoint (`cnde` is the Neural NDE inference interface; the runtime module and checkpoint are
installed separately, see the documentation page "Behavior models" and `Dockerfile.cnde`) adds
two optional uses of the Neural NDE (`cnde`) background-vehicle behavior.

**Per request: `traffic_model`.** Both `POST /v1/city/optimize` and
`POST /v1/city/optimizations`, and the MCP optimize tools, take an optional `traffic_model`:
`"sumo"` (the default) keeps the deployment's behavior; `"cnde"` simulates every candidate of
that one run with Neural NDE, for the configured `max_trials` (at most
`TERASIM_CITY_CNDE_MAX_TRIALS` when that is set, so an entry shared with SUMO runs keeps their
budget). Such a run lowers its `n_workers`, and the worker slots it reserves, to
`TERASIM_CITY_CNDE_MAX_WORKERS` (each Neural NDE trial process holds the checkpoint plus a CUDA
context, about 1.5 GB growing to about 6 GB over a long run), skips a refine pass under `cnde`,
and is recorded, listed and answered with `traffic_model: "cnde"`. A deployment without the
runtime or its checkpoint answers 422 naming what is missing, before any slot is reserved; it
never runs SUMO instead. So does a request whose map is not a stored map package holding
`cnde_map.pkl` and `cnde_id_mapping.pkl` (the CNDE map artifacts) where this deployment cannot
generate them: ship the two files with the map package and point the corridor's
`base_sumocfg` at the package's base sumocfg. Requests without the field and scheduled runs are
unchanged. A Neural NDE run takes minutes per candidate plan: raise the MCP server's
`TERASIM_CITY_TIMEOUT_S` and the VSS agent's `tool_call_timeout` to cover it.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TERASIM_CITY_CNDE_MAX_WORKERS` | `3` | most Neural NDE trials of one `traffic_model="cnde"` run on the GPU at once |
| `TERASIM_CITY_CNDE_MAX_TRIALS` | unset | most Bayesian trials of a `traffic_model="cnde"` run (unset: the request's `max_trials`) |
| `TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY` | unset (off) | `1`: fast mode, each inference predicts only the vehicles the model may control |

**Refine pass.** The deployment can also add a refine pass that re-evaluates the best
candidates with Neural NDE before the plan is exported. The pass is configured by the
deployment only (callers cannot request it):

| Variable | Default | Meaning |
| --- | --- | --- |
| `TERASIM_CITY_VEHICLE_BEHAVIOR` | `sumo_passive` | behavior of every evaluation (`sumo_passive` or `cnde`) |
| `TERASIM_CITY_REFINE_TOP_K` | `0` (off) | number of best candidates re-evaluated |
| `TERASIM_CITY_REFINE_BEHAVIOR` | unset | behavior of the refine pass (`cnde`) |
| `TERASIM_CITY_REFINE_MAX_COLLISIONS` | unset | optional collision cap for a refined candidate |
| `TERASIM_CITY_REFINE_N_WORKERS` | `1` | refine candidates evaluated in parallel (one GPU context each) |

All five are validated when a request is submitted, on the synchronous and the
asynchronous path, before any worker slot is reserved: a value out of range is a 422. The two
`TERASIM_CITY_CNDE_*` limits are validated the same way when a `traffic_model="cnde"` request is
submitted. Every Neural NDE trial process starts with
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` unless the deployment sets its own value.

## 14. Troubleshooting

| What you see | Meaning / fix |
| --- | --- |
| `409 no recent traffic data buffered` | send a snapshot for the same `corridor_id` first (`TERASIM_CITY_WINDOW_S` back at most) |
| `422 ... every snapshot must cover the same intersections` | the buffered snapshots of a corridor list different intersections; send every snapshot with the same set |
| `429 not enough capacity` | all worker slots are taken; retry shortly |
| `422 n_workers=... exceeds this deployment's maximum concurrency` | lower `n_workers` or raise `TERASIM_CITY_MAX_CONCURRENCY` |
| `503 job store unavailable` | Redis unreachable; check `TERASIM_REDIS_*` |
| `422 ... no shipped or registered map covers this area ... (TERASIM_CITY_OSM_DOWNLOAD=0)` | register a map (`POST /v1/city/maps`), name a `base_sumocfg`, or allow downloads |
| TMC counts are all zero | the detector's labels differ from `TMC_COUNTED_OBJECT_TYPES`; `/v1/tmc/health` shows the counted set, each tick reports the labels it fetched |
