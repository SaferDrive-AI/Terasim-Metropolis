# On-Demand mode

In On-Demand mode an operator asks for an optimization and gets the result in the same
conversation:

```text
operator
   │ "optimize the corridor from 1st to 3rd Cross St"
   ▼
VSS agent
   │ MCP tool call: optimize_corridor
   ▼
terasim-agent-skill
   │ POST /v1/city/optimize (synchronous)
   ▼
terasim-service
   │ aggregate the buffered counts, simulate the baseline and
   │ the candidates, export the best plan: {nema_plan, kpis}
   ▼
terasim-agent-skill
   │ ready-to-show summary + JSON payload + download links
   ▼
VSS agent ──► operator
```

The examples below run against a local service started as in the
[Quickstart](../getting-started/quickstart.md) (steps 1 to 3: service running, demo map
registered, snapshots buffered).

## Upload the current timing plan

Without a stored plan, the corridor's timing sheet (or a Webster plan computed from the counts)
is the baseline. With the city's current plan uploaded, that plan is the baseline and the seed
of the search, and the result compares against it. A four-phase, 90 s coordinated plan for the
three demo intersections:

```python
import json
import urllib.request

BASE = "http://localhost:8000"
INTERSECTIONS = [  # id, name, lat, lon, offset (s)
    ("i1", "Arterial Ave & 1st Cross St", 37.335, -121.90339, 0),
    ("i2", "Arterial Ave & 2nd Cross St", 37.335, -121.9, 20),
    ("i3", "Arterial Ave & 3rd Cross St", 37.335, -121.89661, 40),
]


def phase(number, direction, min_green):
    return {"phase": number,
            "movements": [f"{direction}-through", f"{direction}-right", f"{direction}-left"],
            "min_green_s": min_green, "yellow_s": 3.0, "all_red_s": 1.0}


plan = {
    "schema": "terasim.nema_plan.v1",
    "corridor_id": "demo_arterial",
    "intersections": [
        {
            "intersection_id": iid, "name": name, "lat": lat, "lon": lon,
            "phases": [phase(2, "WB", 10), phase(4, "NB", 8),
                       phase(6, "EB", 10), phase(8, "SB", 8)],
            "rings": {"ring1": [2, 4], "ring2": [6, 8], "coordinated_phases": [2, 6]},
            "patterns": [{"pattern_id": "p90", "description": "Coordinated 90 s plan",
                          "cycle_s": 90, "offset_s": offset,
                          "splits": {"2": 52, "4": 38, "6": 52, "8": 38}}],
            "schedule": [{"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                          "start": "00:00", "pattern_id": "p90"}],
        }
        for iid, name, lat, lon, offset in INTERSECTIONS
    ],
    "provenance": {"source_files": [], "handoff_ready": True, "gaps": []},
}

request = urllib.request.Request(
    f"{BASE}/v1/city/plans",
    data=json.dumps(plan).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request) as reply:
    print(reply.status, reply.read().decode())
```

```text
201 {"stored": [["demo_arterial", "i1"], ["demo_arterial", "i2"],
                ["demo_arterial", "i3"]],
     "notes": ["demo_arterial/i1: new", "demo_arterial/i2: new",
               "demo_arterial/i3: new"]}
```

Through MCP the same document goes to `upload_signal_plan(plan)`. The format is described in
[NEMA plan formats](../reference/nema-plan-format.md).

## Optimize

The synchronous REST call, as in the Quickstart:

```bash
curl -s -X POST http://localhost:8000/v1/city/optimize -H 'Content-Type: application/json' -d '{
  "type": "corridor",
  "corridor_id": "demo_arterial",
  "corridor": {
    "from": {"intersection_id": "i1", "name": "Arterial Ave & 1st Cross St", "lat": 37.335, "lon": -121.90339},
    "to":   {"intersection_id": "i3", "name": "Arterial Ave & 3rd Cross St", "lat": 37.335, "lon": -121.89661}
  }
}'
```

With the stored plans covering every signal, the run's baseline is the city's pattern `p90`
(offsets 0, 20, 40 s) and the search moves the offsets of `J2` and `J3` over the stored splits.
The exported plan keeps the pattern's identity:

```json
{"tls_id": "J2", "pattern_id": "p90", "description": "Coordinated 90 s plan",
 "valid_windows": [{"days": ["Mon", "...", "Sun"], "start": "00:00", "end": "00:00"}],
 "offset_s": 65.0, "cycle_s": 90.0,
 "phases": [{"nema_phase": 2, "movements": ["WB-through", "WB-right", "WB-left"],
             "split_s": 52.0, "green_s": 48.0, "yellow_s": 3.0, "all_red_s": 1.0, "notes": ""}, "..."]}
```

Through MCP, `optimize_corridor` takes the same two endpoints (`from_intersection`,
`to_intersection`); `optimize_intersection` takes one; `optimize_registered_intersection` takes
only the id of an intersection registered with the TMC collector. The first text block of the
answer is ready to show (abridged here):

```markdown
**Signal timing optimization: Arterial Ave & 1st Cross St / Arterial Ave & 3rd Cross St**

Plan: P90 coordination pattern, cycle 90 s, offset 0 s.
Plan: P90 coordination pattern, cycle 90 s, offset 65 s.
Plan: P90 coordination pattern, cycle 90 s, offset 53 s.
Demand: 237 vehicles simulated from the measured traffic counts. Cycle unchanged at 90 s. Hand-off ready: yes.
Signal timing report (PDF): http://127.0.0.1:9911/files/i1+i3-20261021T004855Z-c02319/timing_report.pdf

**KPIs (baseline -> optimized)**

| metric | baseline | optimized | change |
| --- | --- | --- | --- |
| control delay (s/veh) | 26.16 | 23.07 | -11.8% |
| stops per vehicle | 0.87 | 0.75 | -14.0% |
| per-junction delay, edge-based (s/veh) | 18.86 | 16.73 | -11.3% |
| weighted cost (delay 0.7, stops 0.3) | 1.00 | 0.88 | -12.5% |

**NEMA splits (optimized): Arterial Ave & 1st Cross St**

| NEMA phase | movements | split (s) | green (s) |
| --- | --- | --- | --- |
| 2 | WB-through, WB-right, WB-left | 52.00 | 48.00 |
| ... | | | |

**Downloads**
- Signal timing report (PDF): http://127.0.0.1:9911/files/i1+i3-20261021T004855Z-c02319/timing_report.pdf
- Optimized NEMA plan (JSON): http://127.0.0.1:9911/files/i1+i3-20261021T004855Z-c02319/nema_plan.json
- Current plan as read (JSON): http://127.0.0.1:9911/files/i1+i3-20261021T004855Z-c02319/current_plan.json
- Full optimization result (JSON): http://127.0.0.1:9911/files/i1+i3-20261021T004855Z-c02319/optimization.json
```

The numbers come from a short demo run (a small trial budget and 300 s of simulated traffic)
and differ from run to run with the budget and the demand. The second text block is the JSON
payload (`nema_plan`, `kpis`, `artifact_id`, `downloads`).

## Weights chosen by the city

The cost weights are part of the request (`cost.weights` on the REST call and the MCP tools):

```json
"cost": {"weights": {"control_delay": 0.5, "stops": 0.5}}
```

The answer echoes them in `kpis.cost_weights`, and the baseline's cost is their sum. See
[KPIs](../reference/kpis.md#weighted-cost).

## The demo script

`examples/corridor_demo/demo_on_demand.py` runs the same exchange against the asynchronous API,
which exposes the search options. It starts a local `terasim-service` (port 8000, Redis on
`localhost:6379`), pushes the six snapshots, submits one optimization of `demo_arterial` from
`i1` to `i3` with the uncoordinated timing sheet as the current plan and the offset search
(`bayes_offsets`), follows its progress and prints the ranking, the best offsets and the NEMA
table:

```bash
python examples/corridor_demo/demo_on_demand.py --max-trials 12 --sim-end 600
```

`examples/corridor_demo/demo_incident.py` adds an inline incident to the request: a stalled
vehicle west of `J2`, simulated in every trial. The run uses the reserved emergency worker slots
and the script checks that the trials simulated the stall.
