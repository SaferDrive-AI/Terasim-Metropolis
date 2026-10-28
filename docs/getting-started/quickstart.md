# Quickstart

Optimize the synthetic demo corridor in `examples/corridor_demo`: an east-west arterial with
three signals, `J1`, `J2` and `J3`, about 300 m apart, corridor id `demo_arterial`
([Synthetic corridor](../examples/synthetic-corridor.md)). Everything runs locally from a
[source installation](installation.md#option-2-python-packages-from-source), with no camera
and no network access.

| Intersection | Signal | Latitude | Longitude |
| --- | --- | --- | --- |
| `i1` | `J1` | 37.335 | -121.90339 |
| `i2` | `J2` | 37.335 | -121.9 |
| `i3` | `J3` | 37.335 | -121.89661 |

## One command: the acceptance run

The acceptance script starts Redis, the three services and a stand-in for the VSS behavior API on
free local ports, then exercises the whole chain on synthetic data:

```bash
python examples/acceptance/run_acceptance.py --out /tmp/terasim-acceptance
```

It writes `acceptance_report.md` and `acceptance_report.json` into `--out` and exits with 0 when
no criterion fails. Options and criteria: [Acceptance test](../examples/acceptance.md).

The steps below do the same by hand against the REST API.

## 1. Start Redis and the service

Run from the repository root (the demo configuration uses paths relative to it):

```bash
redis-server --daemonize yes --save "" --appendonly no   # skip if a local Redis already runs

export TERASIM_ENABLE_CITY_API=true
export TERASIM_CITY_OUTPUT_ROOT=/tmp/terasim/output
export TERASIM_CITY_DATA_ROOT=/tmp/terasim/store
export TERASIM_CITY_SCHEDULED_CONFIG=examples/corridor_demo/scheduled_config.yaml
export TERASIM_CITY_OSM_DOWNLOAD=0      # maps come from files only
terasim-service                         # serves http://localhost:8000
```

`examples/corridor_demo/scheduled_config.yaml` declares the corridor `demo_arterial`: its map
(`demo.sumocfg`), its signals `J1`, `J2`, `J3`, its current timing sheet (an uncoordinated 90 s
plan) and a small trial budget (`max_trials: 6`, `sim_end: 600`) so that the demo finishes in a
few minutes. Requests for this corridor take these values as defaults.

Run the next steps in a second terminal, also in the repository root. The interactive API is at
`http://localhost:8000/docs`.

## 2. Register the map

The demo map is a prebuilt SUMO scenario:

```bash
curl -s -X POST http://localhost:8000/v1/city/maps \
  -H 'Content-Type: application/json' \
  -d "{\"path\": \"$PWD/examples/corridor_demo/demo.sumocfg\"}"
```

```json
{
  "map_id": "<12 hex characters>",
  "duplicate": false,
  "source": "prebuilt",
  "summary": {"format": "prebuilt", "edges": 36, "junctions": 19, "intersections": 3,
              "traffic_lights": 3, "geo_referenced": true, "warnings": [], "...": "..."}
}
```

The map id is a content hash: registering the same files again answers `"duplicate": true`.

## 3. Send traffic counts

Replay six recorded 10-minute snapshots of the demo corridor, re-stamped into the last hour (the
TMC collector normally pushes one snapshot per observation window):

```bash
python - <<'EOF'
import datetime as dt, json, pathlib, urllib.request

BASE = "http://localhost:8000"
paths = sorted(pathlib.Path("examples/corridor_demo/snapshots").glob("snap_*.json"))
now = dt.datetime.now(dt.timezone.utc)
for index, path in enumerate(paths):
    snapshot = json.loads(path.read_text())
    # Replay the six 10-minute windows so that the newest one starts now.
    start = now - dt.timedelta(minutes=10 * (len(paths) - 1 - index))
    snapshot["timestamp_start"] = start.isoformat(timespec="seconds")
    request = urllib.request.Request(
        f"{BASE}/v1/city/snapshots",
        data=json.dumps(snapshot).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request) as reply:
        print(path.name, reply.status, reply.read().decode())
EOF
```

Each push answers `202` with the number of snapshots buffered for the corridor;
`{"corridor_id":"demo_arterial","buffered":6}` after the last one. Format:
[TMC snapshot format](../reference/tmc-snapshot-format.md).

## 4. On-Demand: optimize the corridor

The call the MCP optimization tools make for the VSS agent; it blocks until the optimization has
finished:

```bash
curl -s -X POST http://localhost:8000/v1/city/optimize \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "corridor",
    "corridor_id": "demo_arterial",
    "corridor": {
      "from": {"intersection_id": "i1", "name": "Arterial Ave & 1st Cross St", "lat": 37.335, "lon": -121.90339},
      "to":   {"intersection_id": "i3", "name": "Arterial Ave & 3rd Cross St", "lat": 37.335, "lon": -121.89661}
    },
    "cost": {"weights": {"control_delay": 0.7, "stops": 0.3}}
  }' > optimize.json
```

Answer (numbers shortened):

```json
{
  "nema_plan": {
    "schema": "terasim.nema_timing.v1",
    "corridor_id": "demo_arterial",
    "handoff_ready": true,
    "coordination": {"cycle_length_s": 90.0, "reference_intersection": "J1"},
    "intersections": [
      {"tls_id": "J1", "offset_s": 0.0, "description": "Optimized plan",
       "phases": [{"nema_phase": 2, "movements": ["EB-left", "EB-through", "..."],
                   "split_s": 52.0, "green_s": 48.0, "yellow_s": 3.0, "all_red_s": 1.0}, "..."]},
      "..."
    ]
  },
  "kpis": {
    "cost_weights": {"control_delay": 0.7, "stops": 0.3},
    "baseline":  {"cost": 1.0,  "metrics": {"control_delay_s_per_veh": 23.8, "stops_per_veh": 0.86, "...": "..."},
                  "timing": {"cycle_s": {"J1": 90.0, "...": "..."}, "offsets_s": {"J1": 0.0, "J2": 0.0, "J3": 0.0}}},
    "optimized": {"cost": 0.94, "metrics": {"control_delay_s_per_veh": 23.1, "stops_per_veh": 0.75, "...": "..."},
                  "timing": {"cycle_s": {"J1": 90.0, "...": "..."}, "offsets_s": {"J1": 0.0, "J2": 65.0, "J3": 53.0}}}
  }
}
```

The baseline is the corridor's current plan (here the timing sheet), so its cost is the sum of
the weights, 1.0. The optimizer coordinated the three signals: it kept the common 90 s cycle and
the splits and searched the offsets of `J2` and `J3`. Your numbers will differ with the trial
budget. Definitions: [KPIs](../reference/kpis.md).

## 5. Look at the run

Every optimization, the synchronous ones included, is recorded as a run:

```bash
curl -s 'http://localhost:8000/v1/city/optimizations?corridor_id=demo_arterial'
```

Each entry carries the `run_id`, its origin (`direct` for the synchronous call), status and KPI
summary. With the run id:

```bash
RUN=<run_id>
curl -s http://localhost:8000/v1/city/optimizations/$RUN/result        # full result document
curl -s http://localhost:8000/v1/city/optimizations/$RUN/baseline      # the current plan as NEMA timing
curl -s http://localhost:8000/v1/city/optimizations/$RUN/sim_metrics   # simulated signal measures (202 while computed)
curl -s -o report.pdf http://localhost:8000/v1/city/optimizations/$RUN/report.pdf
```

## 6. Scheduled: the same call on a cadence

The caller invokes the same optimization on its own cadence; each call uses the snapshots
buffered at that time. Two invocations one minute apart:

```bash
for i in 1 2; do
  curl -s -X POST http://localhost:8000/v1/city/optimize \
    -H 'Content-Type: application/json' \
    -d '{"type": "intersection", "corridor_id": "demo_arterial",
         "intersection": {"intersection_id": "i2", "name": "Arterial Ave & 2nd Cross St",
                          "lat": 37.335, "lon": -121.9}}' | python -m json.tool | head -20
  sleep 60
done
```

A single intersection is optimized on its green splits (the corridor call above coordinates
offsets). In production the TMC collector keeps the buffer fresh between invocations.

## 7. Optional: the built-in scheduler

Stop `terasim-service` (Ctrl+C) and start it again with the scheduler on:

```bash
export TERASIM_CITY_SCHEDULER_ENABLED=true
export TERASIM_CITY_CADENCE_S=60
terasim-service
```

Replay the snapshots (step 3) if they are older than an hour, wait for the next tick, then ask
for the corridor's latest scheduled run:

```bash
curl -s http://localhost:8000/v1/city/scheduled/demo_arterial/latest
# {"corridor_id":"demo_arterial","run_id":"3733195b..."}
curl -s http://localhost:8000/v1/city/optimizations/<run_id>/status
curl -s http://localhost:8000/v1/city/optimizations/<run_id>/nema
```

The scheduler optimizes every corridor of `TERASIM_CITY_SCHEDULED_CONFIG` once per cadence when
its window holds recent counts. Turn it off again (`TERASIM_CITY_SCHEDULER_ENABLED=false`, the
default) before continuing.

## 8. Through MCP

Start the MCP server (the VSS agent's way to the same optimization) next to the service:

```bash
export TERASIM_CITY_URL=http://localhost:8000
export TERASIM_MCP_TRANSPORT=streamable-http
export TERASIM_MCP_PORT=9911
export TERASIM_ARTIFACT_ROOT=/tmp/terasim/artifacts   # enables the download links
export TERASIM_CITY_DATA_ROOT=/tmp/terasim/store
terasim-agent-skill                                    # MCP endpoint http://localhost:9911/mcp
```

Call a tool with the MCP Python client (installed with `terasim-agent-skill[mcp]`):

```python
import asyncio
import json
from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ARGS = {
    "corridor_id": "demo_arterial",
    "from_intersection": {"intersection_id": "i1", "name": "Arterial Ave & 1st Cross St",
                          "lat": 37.335, "lon": -121.90339},
    "to_intersection": {"intersection_id": "i3", "name": "Arterial Ave & 3rd Cross St",
                        "lat": 37.335, "lon": -121.89661},
}


async def main() -> None:
    async with streamablehttp_client("http://localhost:9911/mcp", sse_read_timeout=1800) as (
        read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "optimize_corridor", ARGS, read_timeout_seconds=timedelta(seconds=1800))
            print(result.content[0].text)               # ready-to-show Markdown summary
            payload = json.loads(result.content[-1].text)
            print(payload["downloads"])                 # file name -> download URL


asyncio.run(main())
```

The first text block is the summary a chat answer shows (plan, KPI table, NEMA splits, download
links); the last one is the JSON payload. The downloads always include the optimized NEMA plan
(`nema_plan.json`) and the full result (`optimization.json`); when the city's current plan is in
the plan store (step [Upload the current timing plan](../examples/on-demand.md#upload-the-current-timing-plan)),
also `current_plan.json` and the timing report `timing_report.pdf`. See
[MCP tools](../reference/mcp-tools.md).

## Next steps

- [On-Demand mode](../examples/on-demand.md) and [Scheduled mode](../examples/scheduled.md) in
  more depth, including the scripts in `examples/corridor_demo`.
- [Docker Compose](../deployment/docker-compose.md) to run the three services from the image.
- [VSS integration](../deployment/vss-integration.md) to connect a VSS agent.
