# terasim-agent-skill

Agent-facing tooling for the synchronous TeraSim signal-optimization flow that
NVIDIA Metropolis VSS (Video Search and Summarization) calls over MCP.

This package exposes structured upload/optimization MCP tools; it does not
parse PDF bytes itself. A deployment that receives signal timing plans as PDF
documents converts them into a `terasim.nema_plan.v1` JSON document first and
passes that document to `upload_signal_plan`.

The MCP layer is stateless. It does not retain intersections from uploaded TMC
snapshots, resolve names, look up Redis data, or rewrite Plan coordinates. The
VSS Agent owns the intersections it has sent and supplies complete
`intersection_id`, `name`, `lat`, and `lon` objects on later calls.
Plans must already contain their final IDs and coordinates.

The public MCP surface contains nine tools. They follow the three skills of the VSS
integration: (1) register an intersection for periodic turning-movement counts (TMC),
(2) provide the map and the current signal plan, (3) optimize the signal timing.

- `list_intersections()` — corridor intersections known to the TMC microservice (skill 1 helper)
- `register_intersection(intersection_id, name, lat, lon, corridor_id=None)` — skill 1
- `upload_map(path, proj=None)` — skill 2, map already on the TeraSim asset volume: `.sumocfg`,
  `.osm` (OpenStreetMap or Lanelet2) or `.xodr` (OpenDRIVE; HD maps are converted to SUMO on upload);
  `proj` is the proj.4 frame of an HD map without a usable geo-reference
- `upload_signal_plan(plan, corridor_id=None)` — skill 2
- `upload_traffic_snapshot(snapshot)` — manual TMC push (the TMC microservice does this periodically)
- `optimize_registered_intersection(intersection_id, cost=None, traffic_model=None)` — skill 3,
  **preferred**: the registry supplies `name` / `lat` / `lon` / `corridor_id`, so an agent only has
  to pass one string
- `optimize_intersection(corridor_id, intersection, cost=None, traffic_model=None)` — skill 3
- `optimize_intersection_by_id(corridor_id, intersection_id, name, lat, lon, cost=None,
  traffic_model=None)` — skill 3, the same call with flat scalar fields instead of a nested
  `intersection` object
- `optimize_corridor(corridor_id, from_intersection, to_intersection, cost=None,
  traffic_model=None)` — skill 3

`traffic_model` (`"sumo"` | `"cnde"`, optional) picks what simulates every candidate plan of
that one optimization. Ordinary requests leave it unset: SUMO traffic, the deployment default.
The agent sets `"cnde"` only when the user explicitly asks for **CNDE**, **Neural NDE**,
**neural traffic** or **learned / naturalistic driver behavior**; the service then simulates
every candidate with the Neural NDE (CNDE) learned driver model, which needs a GPU and takes
minutes per candidate plan instead of seconds (raise `TERASIM_CITY_TIMEOUT_S` below, and the
agent's tool timeout, to cover it). The tool descriptions and the parameter's own description
carry these rules, so the agent sees them in the tool list. A deployment without the Neural NDE
runtime answers HTTP 422 naming what is missing (the tool call fails with that text); it never
runs SUMO instead, and the agent is told not to re-run without CNDE on its own.

`list_intersections` / `register_intersection` / `optimize_registered_intersection` are only
registered when `TERASIM_TMC_URL` points at a running `terasim-tmc-service`.
`optimize_registered_intersection` raises a `RuntimeError` listing the registered ids (and telling
the caller to use `list_intersections` / `register_intersection`) when the id is unknown.

These tools use Pydantic input models, so MCP discovery exposes the required
Snapshot, Plan, endpoint, and cost fields (including separate bounded `lat`
and `lon` numbers). This schema validation is stateless and does not consult
Redis.

For example, `intersection` and each corridor endpoint are complete objects:

```json
{
  "intersection_id": "i1",
  "name": "Arterial Ave & 1st Cross St",
  "lat": 37.335,
  "lon": -121.90339
}
```

The service validates the JSON shape and uses `intersection_id` as the current
authoritative optimization target. It does not compare the supplied object
with Redis-buffered TMC data. The current core does not use the request's
`name` or `lat`/`lon` to select the target; those fields are structured VSS
context and the ID must still exist in the traffic data used by the real
corridor resolver.

The optimization tools synchronously call `POST /v1/city/optimize` and answer with **two text
content blocks**: first a ready-to-show markdown block, then the normalized public response as
JSON (discarding legacy or unknown extra fields):

```
**Signal timing optimization: Arterial Ave & 1st Cross St**

Plan: MIDDAY coordination pattern, cycle 100 s, offset 10 s (unchanged); the 8 NEMA phase splits were re-balanced (table below).
Demand: 512 vehicles simulated from the measured traffic counts. Cycle unchanged at 100 s. Hand-off ready: yes.
Signal timing report (PDF): https://<gateway>/terasim/files/<artifact_id>/timing_report.pdf

**KPIs (current plan -> optimized)**

| metric | current plan | optimized | change |
| --- | --- | --- | --- |
| control delay (s/veh) | 31.40 | 27.90 | -11.1% |
| stops per vehicle | 0.71 | 0.68 | -3.9% |
| per-junction delay, edge-based (s/veh) | 24.30 | 22.10 | -9.1% |
| weighted cost (delay 0.7, stops 0.3) | 1.00 | 0.91 | -8.9% |

**Per-junction delay (edge-based, s/veh)**

| signal | current plan | optimized |
| --- | --- | --- |
| J1 | 24.3 | 22.1 |

**NEMA splits (current -> optimized): Arterial Ave & 1st Cross St**

| NEMA phase | movements | current split (s) | optimized split (s) | change (s) | green (s) |
| --- | --- | --- | --- | --- | --- |
| 1 | EB-left | 12.00 | 13.00 | +1.00 | 8.50 |
| ... | | | | | |

**Downloads**
- Signal timing report (PDF): https://<gateway>/terasim/files/<artifact_id>/timing_report.pdf
- Optimized NEMA plan (JSON): https://<gateway>/terasim/files/<artifact_id>/nema_plan.json
- ...
```

```json
{
  "artifact_id": "i1-20260923T042034Z-7cdcb7",
  "downloads": {"timing_report.pdf": "https://...", "nema_plan.json": "https://..."},
  "nema_plan": {},
  "kpis": {
    "cost_weights": {"control_delay": 0.7, "stops": 0.3},
    "baseline": {
      "cost": 1.0,
      "metrics": {
        "control_delay_s_per_veh": 31.4, "stops_per_veh": 0.712, "vehicles_total": 512,
        "junction_delay_s": {"J1": 24.3},
        "corridor_junction_delay_s": 24.3
      },
      "timing": {"cycle_s": {"J1": 100.0}, "offsets_s": {"J1": 10.0}}
    },
    "optimized": {
      "cost": 0.911,
      "metrics": {
        "control_delay_s_per_veh": 27.9, "stops_per_veh": 0.684, "vehicles_total": 512,
        "junction_delay_s": {"J1": 22.1},
        "corridor_junction_delay_s": 22.1
      },
      "timing": {"cycle_s": {"J1": 100.0}, "offsets_s": {"J1": 10.0}}
    }
  }
}
```

Each side of `kpis` carries the metrics of its plan and its `timing`: per signal the `cycle_s`,
the `offsets_s` and, for a generated (static) plan, the `greens_s` durations (a stored NEMA
pattern has none). The metrics hold two delay measures. `control_delay_s_per_veh` and
`stops_per_veh` average over all demand and feed the cost. `junction_delay_s` (per signal) and
`corridor_junction_delay_s` (their mean) are the edge-based delay from SUMO's own edge data,
the mean over the signal's incoming edges of `timeLoss / left`; they are reported next to the
cost and never part of it; a signal none of whose incoming edges a vehicle left reports `null`.

An agent pastes the first block verbatim instead of re-summarizing the nested JSON (small planner
models drop the splits and the links; raw markdown is far easier to copy than the same block
JSON-escaped inside a field). The `current` columns, the `(unchanged)` / `(was N s)` offset note
and the `**Downloads**` list appear when the server runs with an artifact store
(`TERASIM_ARTIFACT_ROOT`) and the city's plan is in the plan store; a plan that is not one of the
city's patterns (a generated plan, or the scenario's own programs) says so in the plan line — a
coordinated corridor plan across several signals, or a single signal's plan without a coordination
pattern — and shows plain `split (s)` columns. The demand sentence says the vehicles came from the
scenario's own route files when the service reports a `scenario_routes` source (`config`), and from
the measured traffic counts otherwise.
For a CNDE run (the service sends `traffic_model: "cnde"`, which the JSON block keeps) the facts
line adds `Traffic model: CNDE (Neural NDE, learned driver behavior) simulated every candidate
plan.` right after the demand sentence, so the pasted answer tells the user CNDE traffic was used;
a default run's block is unchanged.
The facts line says what happened to the cycle: `Cycle unchanged at 100 s.`, `Cycle 140 s -> 164 s.`,
or a range such as `60–280 s (per signal)` for a side whose signals run different cycles. When the
plan never lets some vehicle movements move (red or yellow in every phase) (the service then sends `unserved_movements`), the
facts line names them, e.g. `Unserved movements: n69 WB-left, WB-through, WB-right.` A result
that carries no junction delay or timing (an older server) renders `n/a` in the extra KPI row and
leaves out the cycle sentence and the per-signal table.
Scripts read the payload with `json.loads` from the last text block, see
`apps/deploy/city/phase1/phase1_e2e.py::tool_result_json`.

An optional cost object has the form
`{"weights": {"control_delay": 0.7, "stops": 0.3}}`. Optimizer selection,
trial counts, run submission, and polling are deliberately absent from the
MCP contract (`traffic_model` above is the one simulation choice the agent
makes). The older asynchronous methods remain available only on the
low-level Python client for compatibility.

The MCP console script requires the optional dependency:
`pip install 'terasim-agent-skill[mcp]'`.

The service base URL is taken from the `TERASIM_CITY_URL` environment variable
(default `http://127.0.0.1:8000`). Synchronous optimization may take several
minutes, so the client timeout defaults to 1800 seconds; set
`TERASIM_CITY_TIMEOUT_S` to a positive number of seconds to override it for the
MCP process.

Transport: `TERASIM_MCP_TRANSPORT` (`stdio` default, `streamable-http` for the VSS NAT
agent, which connects to `http://<host>:<port>/mcp`), `TERASIM_MCP_HOST` (`0.0.0.0`),
`TERASIM_MCP_PORT` (`9911`), `TERASIM_MCP_STATELESS` (`1` for stateless HTTP). The
`mcp` extra is pinned to `mcp>=1.0,<2`.

The four optimization tools run the blocking HTTP call in a thread and, every
`TERASIM_MCP_HEARTBEAT_S` seconds (default `2`, must be positive), send **both** an MCP progress
notification (`notifications/progress`) and an info log notification (`notifications/message`) for
as long as it runs. Both ride the request's own response stream, which keeps it producing bytes.

Why both, and why 2 s: some MCP clients have short read timeouts and never send a progress token.
NVIDIA NAT is one — it builds its `httpx.AsyncClient` with no `timeout` argument
(`nat/plugins/mcp/client/client_base.py`), so httpx's **5 s default read timeout** applies and any
stream silent for 5 s loses its response, however long `tool_call_timeout` is; and it passes no
`progress_callback`, so FastMCP's `report_progress` returns early and emits nothing at all.
`Context.info` needs no token, so it reaches those clients; the 2 s interval leaves margin under a
5 s read timeout. Clients that do pass a progress token
still get progress events; a notification that fails is swallowed and never fails the call.
