# Scheduled mode

Instead of an operator's request, the caller invokes the **same optimization tool** as in
[On-Demand mode](on-demand.md) on its own cadence; every invocation optimizes on the counts
buffered at that moment. The optimization has no scheduling logic of its own.

```text
every N minutes:
   caller ── optimize_corridor (MCP) or POST /v1/city/optimize ──►
   ◄── NEMA plan + KPIs on the latest observation windows
```

## The caller drives the cadence

Any scheduler can be the caller: a VSS workflow, a cron job, an orchestration pipeline. A minimal
Python caller that invokes the MCP tool twice, one minute apart:

```python
import asyncio
import json
import time
from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "http://localhost:9911/mcp"
CADENCE_S = 60
ARGS = {
    "corridor_id": "demo_arterial",
    "from_intersection": {"intersection_id": "i1", "name": "Arterial Ave & 1st Cross St",
                          "lat": 37.335, "lon": -121.90339},
    "to_intersection": {"intersection_id": "i3", "name": "Arterial Ave & 3rd Cross St",
                        "lat": 37.335, "lon": -121.89661},
}


async def optimize() -> dict:
    async with streamablehttp_client(MCP_URL, sse_read_timeout=1800) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "optimize_corridor", ARGS, read_timeout_seconds=timedelta(seconds=1800))
            return json.loads(result.content[-1].text)


for invocation in range(2):
    started = time.monotonic()
    payload = asyncio.run(optimize())
    kpis = payload["kpis"]
    print(invocation, kpis["baseline"]["cost"], "->", kpis["optimized"]["cost"])
    time.sleep(max(0.0, CADENCE_S - (time.monotonic() - started)))
```

In production the TMC collector keeps the snapshot buffer current between invocations, so each
call optimizes the snapshots of the last `TERASIM_CITY_WINDOW_S` seconds (one hour by default).
The acceptance run checks this mode with `--scheduled-invocations` and `--cadence-s` (see
[Acceptance test](acceptance.md)).

## Built-in scheduler (optional)

`terasim-service` can also run the cadence itself. Off by default; turn it on with:

| Setting | Default | Helm |
| --- | --- | --- |
| `TERASIM_CITY_SCHEDULER_ENABLED` | off | `scheduler.enabled` |
| `TERASIM_CITY_CADENCE_S` | `3600` | `scheduler.cadenceSeconds` |
| `TERASIM_CITY_SCHEDULED_CONFIG` | required when on | the chart's `configs/scheduled_config.yaml` |
| `TERASIM_CITY_WINDOW_S` | `3600` | `service.windowS` |

Every cadence, for each corridor of the [corridor configuration](../developer-guide/configuration.md#corridor-configuration):

- the snapshots of the last `TERASIM_CITY_WINDOW_S` seconds are aggregated into one demand
  file (under `<output root>/scheduled/<corridor_id>/`) and an optimization run is submitted
  with the entry's settings; a corridor without recent snapshots is skipped until data arrives;
- an entry whose counts come from its scenario's routes (`counts_source: scenario_routes`) runs
  when its inputs changed (the entry, its scenario files and its timing sheet), or on every pass
  with `rerun_unchanged: true`;
- a pass never overlaps the previous one; a submission refused for capacity (429) or
  configuration (422) is logged with the reason and retried at the next pass.

Scheduled runs are recorded like any other run (origin `scheduled`):

```bash
curl -s http://localhost:8000/v1/city/scheduled/demo_arterial/latest   # {"corridor_id": ..., "run_id": ...}
curl -s 'http://localhost:8000/v1/city/optimizations?corridor_id=demo_arterial'
curl -s http://localhost:8000/v1/city/optimizations/<run_id>/nema
```

Walkthrough: [Quickstart, step 7](../getting-started/quickstart.md#7-optional-the-built-in-scheduler).

## The demo script

`examples/corridor_demo/demo_scheduled.py` starts a local `terasim-service` with the built-in
scheduler on (30 s cadence, two-hour window, the demo corridor configuration), pushes the six
snapshots, waits for the scheduler to submit a run and prints the winning plan and its NEMA
table. It needs Redis on `localhost:6379` and port 8000 free.

```bash
python examples/corridor_demo/demo_scheduled.py --cadence-s 30
```
