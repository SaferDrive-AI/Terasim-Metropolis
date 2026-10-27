# Docker Compose

`apps/deploy/city/phase1/compose.yml` runs TeraSim Metropolis on one host: the three services
from one image, and optionally a stand-in for the VSS behavior API so the stack runs without
cameras.

| Container | Command | Published on | Role |
| --- | --- | --- | --- |
| `terasim-service` | default (`terasim-service`) | `127.0.0.1:8100` → 8000 | REST API `/v1/city/*`, embedded Redis |
| `terasim-mcp` | `terasim-agent-skill` | `127.0.0.1:9911` | MCP endpoint `/mcp`, downloads at `/files` |
| `terasim-tmc` | `terasim-tmc-service` | `127.0.0.1:8200` | Intersection registry and TMC collector |
| `vss-behavior-stub` | `stub_behavior_server.py` | not published | Profile `stub` only: synthetic `GET /behavior` for the demo corridor's cameras |

All services publish on the loopback interface. A VSS agent container that uses host networking
on the same machine reaches the MCP server at `http://127.0.0.1:9911/mcp`; one on its own Compose
network (VSS 3.3) reaches it at `http://<host private IP>:9911/mcp` once an override file also
publishes `terasim-mcp` there (`ports: ["<host private IP>:9911:9911"]`). From another
machine, use an SSH tunnel (`ssh -L 9911:127.0.0.1:9911 <host>`) or a reverse proxy.

Run every command below from the repository root.

## Get the image

Either pull the published image from the GitHub Container Registry and tag it with the name the
compose file uses (`<owner>` is a placeholder until the public repository is set up):

```bash
docker pull ghcr.io/<owner>/terasim-metropolis:0.1.0
docker tag ghcr.io/<owner>/terasim-metropolis:0.1.0 terasim-metropolis:0.1.0
```

or build it from the checkout:

```bash
docker compose -f apps/deploy/city/phase1/compose.yml build
```

The Neural NDE image, its runtime module and the model checkpoint are not published with the
release image; they are obtained separately (see
[Neural NDE traffic](#neural-nde-traffic-on-a-gpu-host-optional)).

## Start offline, with the behavior stub

```bash
docker compose -f apps/deploy/city/phase1/compose.yml --profile stub up -d
docker compose -f apps/deploy/city/phase1/compose.yml ps
```

The TMC collector's `VSS_BEHAVIOR_URL` defaults to the stub. The stack then holds everything
the synthetic demo corridor needs:

- the map baked into the image at `/data/input/map/demo.sumocfg`;
- the corridor configuration `phase1/scheduled_config.yaml` (mounted at
  `/config/scheduled_config.yaml`): corridor `demo_arterial`, signals `J1`, `J2`, `J3`, 1,800 s
  per trial, at most 24 trials, 4 workers;
- the TMC corridor spec `phase1/Metadata.json` (mounted at `/config/Metadata.json`):
  intersections `i1`, `i2`, `i3`, four cameras each.

Check that the services answer:

```bash
curl -s http://127.0.0.1:8100/health          # {"status":"healthy"}
curl -s http://127.0.0.1:8200/v1/tmc/health   # corridor, registered intersections, last tick
```

## Walk through the chain

Register an intersection with the TMC collector and count one observation window right away
(the collector otherwise counts at every window boundary):

```bash
curl -s -X POST http://127.0.0.1:8200/v1/tmc/intersections -H 'Content-Type: application/json' \
  -d '{"intersection_id": "i1", "name": "Arterial Ave & 1st Cross St", "lat": 37.335, "lon": -121.90339}'
curl -s -X POST http://127.0.0.1:8200/v1/tmc/run-once
```

`run-once` answers the window it counted, the behavior objects per camera and the counts it
pushed to the service (`"status": "injected"`; `"skipped"` when the counting pass that follows
a registration already pushed that window). Then optimize, for example with the synchronous
REST call, which answers after the run, a few minutes with the corridor's settings:

```bash
curl -s -X POST http://127.0.0.1:8100/v1/city/optimize -H 'Content-Type: application/json' \
  -d '{"type": "intersection", "corridor_id": "demo_arterial",
       "intersection": {"intersection_id": "i1", "name": "Arterial Ave & 1st Cross St",
                        "lat": 37.335, "lon": -121.90339}}'
```

or through MCP at `http://127.0.0.1:9911/mcp` with `optimize_registered_intersection`
(`{"intersection_id": "i1"}`), as the VSS agent does. See [MCP tools](../reference/mcp-tools.md).

## Built-in checks

Two scripts ship inside the image. The stack check walks the chain over the public interfaces
(health, registration, one counting pass, map registration over MCP, an optimization over MCP):

```bash
docker compose -f apps/deploy/city/phase1/compose.yml run --rm --no-deps terasim-mcp \
  python apps/deploy/city/phase1/phase1_e2e.py \
  --service http://terasim-service:8000 --tmc http://terasim-tmc:8200 --mcp http://terasim-mcp:9911/mcp
```

The scenario check optimizes the demo scenario baked at `/data/input/demo_scenario` with its own
routes and signal programs:

```bash
docker compose -f apps/deploy/city/phase1/compose.yml run --rm --no-deps terasim-mcp \
  python apps/deploy/city/phase1/scenario_e2e.py --service http://terasim-service:8000
```

Both print a final pass line and exit with 0 on success. For the full acceptance criteria, run
the acceptance script from the host against the stack (it needs the release packages installed
on the host):

```bash
python examples/acceptance/run_acceptance.py --remote \
  --service http://127.0.0.1:8100 --mcp http://127.0.0.1:9911/mcp --tmc http://127.0.0.1:8200
```

See [Acceptance test](../examples/acceptance.md).

## Connect a VSS deployment

Without the `stub` profile, point the TMC collector at the VSS behavior API (VSS
video-analytics API, port 8081):

```bash
VSS_BEHAVIOR_URL=http://<vss-host>:8081 docker compose -f apps/deploy/city/phase1/compose.yml up -d
```

A VSS running on the same host is reachable as `http://host.docker.internal:8081`. Replace
`phase1/Metadata.json` with the corridor specification of your cameras and
`phase1/scheduled_config.yaml` with your corridor's map and settings; see
[VSS integration](vss-integration.md).

## Download links

Every optimization through MCP saves its result files (NEMA plan JSON, full result, and with the
city's current plan in the plan store the timing report PDF) on the `terasim-artifacts` volume
and links them in the answer as `<TERASIM_MCP_PUBLIC_URL>/files/<artifact_id>/<file>`. The
default `http://127.0.0.1:9911` works on the host itself; for readers on other machines put a
reverse proxy in front of `/files/` only and set the public address:

```bash
TERASIM_MCP_PUBLIC_URL=https://<public-host>/terasim \
  docker compose -f apps/deploy/city/phase1/compose.yml up -d --no-deps terasim-mcp
```

The proxy forwards `<public>/files/` to `127.0.0.1:9911/files/`; the MCP endpoint `/mcp` stays
private.

## Settings

The compose file reads these variables from the shell (or an `.env` file next to it):

| Variable | Default | Meaning |
| --- | --- | --- |
| `VSS_BEHAVIOR_URL` | `http://vss-behavior-stub:8081` | VSS behavior API the TMC collector pulls |
| `TMC_SETTLE_LAG_S` | `180` | Seconds a window must have ended before it is counted |
| `TMC_COUNTED_OBJECT_TYPES` | empty (`Vehicle,two_wheeler`) | Detector labels counted as traffic |
| `TERASIM_MCP_PUBLIC_URL` | `http://127.0.0.1:9911` | Base address of the download links |
| `TERASIM_CITY_OSM_DOWNLOAD` | `1` | `0`: never download OpenStreetMap data; requests must be covered by a stored map |
| `TERASIM_CITY_SCHEDULER_ENABLED` | `false` | Built-in scheduler on or off |
| `TERASIM_CITY_CADENCE_S` | `3600` | Seconds between two scheduler passes |

The file also fixes `TERASIM_CITY_WINDOW_S=3600` and `TERASIM_CITY_MAX_CONCURRENCY=8`. Every
other setting is listed in the [Configuration reference](configuration.md).

## Data and Redis

| Volume | Mounted at | Holds |
| --- | --- | --- |
| `terasim-output` | `/data/output` (service, read-write; MCP, read-only) | Run directories, the map store and the plan store (`/data/output/store`), the audit log |
| `terasim-artifacts` | `/data/artifacts` (MCP) | Files linked from optimization answers |
| `tmc-data` | `/data/tmc` (TMC collector) | Intersection registry and the last counted window |

The service starts Redis inside its container (snapshot buffer and job store, kept in memory).
The MCP server and the TMC collector use the service's REST API only; they set
`TERASIM_REDIS_HOST` so that their containers do not start an embedded Redis. To use an
external Redis, set `TERASIM_REDIS_HOST` (and `TERASIM_REDIS_PORT`, `TERASIM_REDIS_DB`,
`TERASIM_REDIS_PASSWORD`) on `terasim-service`; the embedded one is then skipped.

Run directories are not removed automatically; size the `terasim-output` volume for the run
history you want to keep.

## Neural NDE traffic on a GPU host (optional)

`apps/deploy/city/phase1/compose.cnde.yml` adds per-request Neural NDE traffic
(`traffic_model="cnde"`, see [Behavior models](../developer-guide/behavior-models.md)) to the
stack. It runs `terasim-service` from the Neural NDE image (`Dockerfile.cnde`) on one GPU, mounts
the model checkpoint and a map package with its Neural NDE map artifacts read-only, and raises
the MCP server's `TERASIM_CITY_TIMEOUT_S` to 1800 s. Requests without `traffic_model` and the
scheduled runs keep SUMO traffic.

```bash
# next to the compose files: cnde_assets/nn_weights.pth and a map package in cnde_map/
docker compose -f apps/deploy/city/phase1/compose.yml -f apps/deploy/city/phase1/compose.cnde.yml up -d
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `TERASIM_CNDE_IMAGE` | `terasim-metropolis:0.1.0-cnde` | The Neural NDE image |
| `CNDE_CHECKPOINT` | `./cnde_assets/nn_weights.pth` | The checkpoint on the host, mounted at `/app/TeraSim/packages/cnde/assets/nn_weights.pth` |
| `CNDE_MAP_PACKAGE` | `./cnde_map` | A map package with `cnde_map.pkl` and `cnde_id_mapping.pkl`, mounted at `/data/input/cnde_map` |
| `CNDE_GPU` | `0` | GPU device id |
| `TERASIM_CITY_CNDE_MAX_WORKERS` | `3` | Neural NDE trials of one run on the GPU at once |
| `TERASIM_CITY_CNDE_MAX_TRIALS` | empty | Most Bayesian trials of a Neural NDE run (empty: the request's budget) |
| `TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY` | `0` | `1`: fast mode, the model predicts only the vehicles it may control |
| `TERASIM_CITY_TIMEOUT_S` | `1800` | The MCP server's timeout of one synchronous optimization |

The corridor's entry in `scheduled_config.yaml` points `base_sumocfg` at the mounted package
(`/data/input/cnde_map/base.sumocfg`). Back to SUMO traffic only:
`docker compose -f apps/deploy/city/phase1/compose.yml up -d`.

## Single container

The image alone runs the service with its embedded Redis:

```bash
docker run -d -p 8000:8000 --name terasim <registry>/terasim-metropolis:0.1.0
```

`apps/deploy/city/smoke.sh` runs a complete check against it: it registers the baked demo map,
posts a snapshot, optimizes the demo corridor and verifies the NEMA plan and KPIs
(`IMAGE=<registry>/terasim-metropolis:0.1.0 bash apps/deploy/city/smoke.sh`).

## Stop

```bash
docker compose -f apps/deploy/city/phase1/compose.yml --profile stub down      # keeps the volumes
docker compose -f apps/deploy/city/phase1/compose.yml --profile stub down -v   # removes them too
```
