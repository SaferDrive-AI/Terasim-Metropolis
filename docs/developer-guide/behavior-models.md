# Behavior models (NADE and Neural NDE)

Module A1 of the simulation core provides the driving-behavior models of the background
traffic. Two families are available:

| Model | What it is | Where |
| --- | --- | --- |
| **NDE / NADE** | Naturalistic driving behavior (NDE) and its naturalistic-and-adversarial variant (NADE) with importance sampling; drives the background vehicles and vulnerable road users of the adversarial environment | `terasim-nde-nade` (see [Adversarial environment](adversarial-environment.md)) |
| **Neural NDE** | A learned behavior model: a neural network predicts the trajectories of the background vehicles from their recent motion, the lane map and the signal states, and drives them in closed loop with the simulation | inference interface in `terasim-city`; runtime module and model checkpoint installed separately (see below) |

The signal-timing optimization evaluates every candidate plan with SUMO's built-in driver
model (`sumo_passive`), which needs no GPU. A deployment with a GPU and the Neural NDE runtime
offers two more uses of the Neural NDE model:

- **per request:** an optimization request (or an MCP optimize tool call) with
  `traffic_model="cnde"` simulates every candidate plan of that one run under Neural NDE;
- **per deployment:** a re-evaluation stage simulates the current plan and the best candidates
  under Neural NDE before a plan is exported.

## What this repository contains

| Part | In this repository | Module |
| --- | --- | --- |
| NDE / NADE behavior models, vehicle factories, adversity library | yes | `terasim_nde_nade` |
| Behavior registry: `sumo_passive` (default) and `cnde` | yes | `terasim_city.factories` |
| Neural NDE inference interface: model loading, per-trial simulator, vehicle factory, runtime check | yes | `terasim_city.factories.cnde_behavior` |
| Neural NDE evaluation environment for a signalized corridor | yes | `terasim_city.envs.cnde_corridor_env` |
| Map artifacts of the Neural NDE model: generation, caching in a stored map, readiness check | yes | `terasim_city.maps.cnde_artifacts` |
| Per-request traffic model (`traffic_model`) on the HTTP API and the MCP optimize tools | yes | `terasim-service`, `terasim-agent-skill` |
| Re-evaluation stage of the optimizer and its deployment settings | yes | `terasim_city.orchestration.corridor_optimization`, `terasim-service` |
| Neural NDE image (`Dockerfile.cnde`) and its Compose override (`compose.cnde.yml`) | yes | `apps/deploy/city` |
| Acceptance criteria A1-1 to A1-4 and the Neural NDE profile of the acceptance check | yes | `examples/acceptance` |
| Neural NDE runtime module (`csim`, `cutils`, the `cnde` model package, the planner trees its rule controller imports) and the model checkpoint | **no** | installed separately |

## Per-request traffic model (`traffic_model`)

Both optimization endpoints (`POST /v1/city/optimize`, `POST /v1/city/optimizations`) and the
four MCP optimize tools (`optimize_registered_intersection`, `optimize_intersection`,
`optimize_intersection_by_id`, `optimize_corridor`) take an optional `traffic_model`:

| Value | Every candidate plan of the run is simulated with |
| --- | --- |
| unset or `"sumo"` | the deployment's behavior (`TERASIM_CITY_VEHICLE_BEHAVIOR`, default SUMO's driver model); the run answers exactly as without the field |
| `"cnde"` | the Neural NDE model on the GPU |

A `traffic_model="cnde"` run:

- runs the seed plans and every Bayesian trial under `cnde`, at most
  `TERASIM_CITY_CNDE_MAX_WORKERS` of them at once (the request's `n_workers` and the worker
  slots the run reserves are lowered to it) and at most `TERASIM_CITY_CNDE_MAX_TRIALS` trials
  when that is set (a corridor entry's budget then stays as it is for SUMO runs);
- skips a re-evaluation stage under `cnde` (the plans were already scored under it);
- is recorded, listed and answered with `traffic_model: "cnde"`: the run listing
  (`GET /v1/city/optimizations`), the result JSON, the synchronous response, and in the MCP
  answer both the JSON block and the facts line of the markdown block
  (`Traffic model: CNDE (Neural NDE, learned driver behavior) simulated every candidate plan.`);
- is refused with HTTP 422 before any slot is reserved when this deployment lacks the runtime
  module or the checkpoint, or when the run's map is not a stored map package that holds its
  Neural NDE map artifacts (or can generate them); the detail names what is missing. A
  refused request never runs SUMO instead.

Scheduled runs never set it. The VSS agent sets it only when a user explicitly asks for Neural
NDE traffic (STEP 3 of the agent prompt, see [VSS integration](../deployment/vss-integration.md)).
A Neural NDE run takes minutes per candidate plan instead of seconds: the timeouts of the
synchronous chain (`TERASIM_CITY_TIMEOUT_S` of the MCP server, the agent's
`tool_call_timeout`, any proxy in front of the chat) have to cover it.

## Inference interface

The interface keeps `terasim-city` free of GPU dependencies: `torch` and the runtime module
are imported only inside the functions that use them, so importing the registry never loads a
GPU runtime.

| Function / class | Contract |
| --- | --- |
| `terasim_city.factories.cnde_behavior.get_cnde_model()` | Loads the checkpoint once per process (`csim.simulator.terasim_cnde_simulator.load_cnde_model()`) and returns the model on the GPU; every trial of the process reuses it |
| `terasim_city.factories.cnde_behavior.build_trial_nnde_simulator(map_dir)` | Builds a fresh per-trial simulator (`csim.simulator.terasim_cnde_simulator.build_nnde_simulator(map_pkl, desc, model=...)`) for a stored map; never reused across trials |
| `terasim_city.factories.cnde_behavior.missing_cnde_runtime()` | What this process lacks to run `cnde` (`torch`, the `cnde` model package, the `csim` simulator package, the checkpoint next to it); empty when nothing is missing. Imports nothing heavy |
| `terasim_city.factories.cnde_behavior.CndeVehicleFactory` | Background-vehicle factory: vehicles keep a one-second motion history and accept the model's trajectory commands |
| `terasim_city.envs.cnde_corridor_env.CndeCorridorSignalEnv` | Corridor evaluation environment: applies the candidate signal plan and merges the model's trajectory commands into the vehicles' default commands every step; vulnerable road users stay under SUMO control |
| `terasim_city.maps.cnde_artifacts.ensure_cnde_artifacts(map_dir)` | Converts a stored map's `net.net.xml` to the model's map format (`cnde_map.pkl`, `cnde_id_mapping.pkl`) with the runtime's converter (`tools.dataset.x2cnde`) on first use, and caches the files in the map package |
| `terasim_city.maps.cnde_artifacts.missing_cnde_map_artifacts(base_sumocfg)` | Why a `cnde` run on that map would get no map artifacts; empty when the package holds them or this process can generate them |
| `terasim_city.factories.resolve_vehicle_factory("cnde")` | Resolves the behavior by name in the evaluation process |

The runtime module provides `csim`, `cutils`, the `cnde` model package and `tools.dataset`.
Its root directory, its `network/cnde` directory and the two planner trees it carries
(`nuplan-devkit`, `tuplan-garage`) go on `PYTHONPATH`, and the checkpoint sits in the runtime's
`assets/` directory as `nn_weights.pth`. A CUDA GPU is required; one process holds the
checkpoint (about 1 GB) plus a CUDA context, about 1.5 GB of GPU memory at the start, growing
to about 6 GB over a long run.

Evaluations under the Neural NDE model need a map that lives in the map store as a package
with its own network (maps uploaded as OpenStreetMap, OpenDRIVE or Lanelet2 files, see
[Map formats](../reference/map-formats.md)): the model's map artifacts sit next to it. The
converter needs TensorFlow and the Waymo Open Dataset protocol buffers besides the runtime; an
environment without them (such as the Neural NDE image) uses map packages that already hold
the two files, and a corridor's `base_sumocfg` points at such a package's base sumocfg.

## Neural NDE image

`apps/deploy/city/Dockerfile.cnde` derives an image from the release image: PyTorch with CUDA,
the runtime's dependencies, and the runtime module from a second build context named
`neural-nde`. The checkpoint is mounted at run time at
`/app/TeraSim/packages/cnde/assets/nn_weights.pth`, never baked in.

```bash
docker build -f apps/deploy/city/Dockerfile -t terasim-metropolis:0.1.0 .
docker build -f apps/deploy/city/Dockerfile.cnde \
  --build-context neural-nde=<path of the runtime module> -t terasim-metropolis:0.1.0-cnde .
```

`apps/deploy/city/phase1/compose.cnde.yml`, layered on the Compose stack, runs
`terasim-service` from that image on one GPU with the checkpoint and a map package mounted
read-only and sets the two limits below; the MCP server's `TERASIM_CITY_TIMEOUT_S` goes to
1800 s (see [Docker Compose](../deployment/docker-compose.md)).

## Re-evaluation stage

After the simulation-based search, the optimizer can re-evaluate the current plan and the K
best distinct candidates under another behavior, and export the best of them by the same KPI
cost (control delay and stops, normalized against the current plan under the same behavior).

```text
search (sumo_passive, n_workers in parallel)  ->  K best candidates
re-evaluation (cnde): current plan + K candidates  ->  best re-evaluated plan  ->  NEMA export
```

- Every re-evaluated plan is simulated for the same horizon as the search (`sim_end`).
- The current plan is always re-evaluated, so the costs of the stage compare plans under one
  behavior.
- `refine_max_collisions` optionally excludes candidates with more collisions from the
  selection.
- One process evaluates the plans one after another by default (one model copy, one CUDA
  context); `refine_n_workers` runs several in parallel, each with its own copy. Every trial
  seeds its random generators and iterates in a fixed order, so a plan evaluates to the same
  result in either mode.
- The result of a run (`GET /v1/city/optimizations/{run_id}/result`) carries a `refine` block
  (`behavior`, `top_k`, `candidates_evaluated`, `baseline` and `evaluations` with each plan's
  score and measures, `winner_plan_id`, `invalid` with the reasons) and `export_source`:
  `"refine"` when the exported plan comes from the stage, `"coarse"` when the stage produced no
  usable result and the search winner is exported. The KPIs of the answer belong to the
  exported plan.
- Without the runtime module, the stage reports why it could not evaluate (service log and
  `refine.invalid`) and the run completes with the search winner (`export_source: "coarse"`).

## Configuration

These are deployment settings of `terasim-service`, validated when a request is submitted, on
the synchronous and on the asynchronous path (the two `TERASIM_CITY_CNDE_*` limits when a
`traffic_model="cnde"` request is submitted).

| Variable | Default | Meaning |
| --- | --- | --- |
| `TERASIM_CITY_CNDE_MAX_WORKERS` | `3` | Neural NDE trials of one `traffic_model="cnde"` run on the GPU at once |
| `TERASIM_CITY_CNDE_MAX_TRIALS` | unset | Most Bayesian trials of a `traffic_model="cnde"` run (unset: the request's `max_trials`) |
| `TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY` | unset (off) | Fast mode: each inference predicts only the vehicles the model may control, not every vehicle with a complete history; with two OpenMP / MKL / OpenBLAS threads per trial, eight trials share one GPU |
| `TERASIM_CITY_CNDE_DIAG_LOGS` | unset (off) | `1` writes the per-step diagnostic logs of every Neural NDE trial (trajectories, headings, audit; about 70 MB per 600 s trial) |
| `TERASIM_CITY_REFINE_TOP_K` | `0` (off) | Number of best candidates re-evaluated |
| `TERASIM_CITY_REFINE_BEHAVIOR` | unset | Behavior of the re-evaluation: `cnde` |
| `TERASIM_CITY_REFINE_MAX_COLLISIONS` | unset | Optional collision cap of a re-evaluated candidate |
| `TERASIM_CITY_REFINE_N_WORKERS` | `1` | Re-evaluation processes in parallel (one GPU context each) |
| `TERASIM_CITY_VEHICLE_BEHAVIOR` | `sumo_passive` | Behavior of every evaluation, including the search (`cnde` makes every search of the deployment run on the GPU) |
| `TERASIM_CITY_CNDE_RELAX_QUEUE_FILTER` | unset (off) | Relaxes the stop-line and low-speed filters, so the model also drives vehicles approaching the stop line or moving slowly |

Every Neural NDE trial process starts with `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
unless the deployment sets its own value. In Python, the same settings are fields of
`CorridorOptimizationConfig` (`vehicle_behavior`, `cnde_max_workers`, `refine_top_k`,
`refine_vehicle_behavior`, `refine_max_collisions`, `refine_n_workers`), see
[Python API](python-api.md).

Example, a service with the runtime installed in `/opt/neural-nde`:

```bash
export PYTHONPATH=/opt/neural-nde/network/cnde:/opt/neural-nde:/opt/neural-nde/nuplan-devkit:/opt/neural-nde/tuplan-garage
export TERASIM_CITY_CNDE_MAX_WORKERS=3          # per-request traffic_model="cnde"
export TERASIM_CITY_REFINE_TOP_K=2 TERASIM_CITY_REFINE_BEHAVIOR=cnde   # optional re-evaluation
terasim-service
```

## Validation

| Criterion | What it checks |
| --- | --- |
| A1-1 | NADE drives the background traffic of the demo corridor; `cnde` is registered |
| A1-2 | With the deployment settings above, an optimization on a stored map runs the re-evaluation stage with behavior `cnde` and returns a NEMA plan and KPIs (without the runtime module: the search winner, `export_source: "coarse"`) |
| A1-3 | With the runtime module and checkpoint installed: the current plan and the K candidates are re-evaluated under Neural NDE on the GPU, and the exported plan comes from that stage (`export_source: "refine"`) |
| A1-4 | With the runtime module and checkpoint installed: an MCP optimize tool call with `traffic_model="cnde"` simulates every candidate under Neural NDE within the deployment's limits and is answered and recorded as a Neural NDE run; the same call without the field runs under SUMO with the corridor's own budget |

A1-1 and A1-2 run in the standard acceptance check; the unit tests cover the per-request
option without a GPU (`tests/test_service/test_city_traffic_model.py`). A1-3 and A1-4 run in
the Neural NDE profile, on a machine with a CUDA GPU and the runtime installed:

```bash
python examples/acceptance/run_acceptance.py --neural-nde --out /tmp/terasim-acceptance-nnde
```

The profile also checks the environment (runtime importable, CUDA device, checkpoint present)
and service health; see [Acceptance test](../examples/acceptance.md).
