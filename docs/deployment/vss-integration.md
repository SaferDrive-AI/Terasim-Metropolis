# VSS integration

TeraSim Metropolis connects to an NVIDIA Metropolis VSS (Video Search and Summarization)
deployment at three points:

| Connection | Direction | Configured where |
| --- | --- | --- |
| Agent tools | VSS agent → `terasim-agent-skill` MCP server (`/mcp`, port 9911) | VSS agent `config.yml` |
| Traffic counts | `terasim-tmc-service` → VSS behavior API (`GET /behavior`, port 8081) | `VSS_BEHAVIOR_URL` (Helm `tmc.vssBehaviorUrl`) |
| Download links | reader's browser → `terasim-agent-skill` `/files` | `TERASIM_MCP_PUBLIC_URL` (Helm: the Route, `global.externalHost`) |

No VSS image or code changes are needed: the agent gains a tool group in its configuration,
and the TMC collector reads the behavior API that VSS already serves.

## Traffic counts from VSS

### Corridor specification (`Metadata.json`)

The TMC collector reads one corridor from `TMC_SPEC_FILE`. The format is the one of the NVIDIA
reference TMC script: per intersection, which camera watches which approach, and how the road
segments of the behavior objects map to headings and turns.

```json
{
  "corridor_id": "demo_arterial",
  "observation_interval": "10m",
  "intersections": [
    {
      "intersection_id": "i1",
      "lat": 37.335,
      "lon": -121.90339,
      "Headings": {
        "w1-j1": "East",  "j1-j2": "East",
        "j2-j1": "West",  "j1-w1": "West",
        "j1s1-j1": "North", "j1-j1n1": "North",
        "j1n1-j1": "South", "j1-j1s1": "South"
      },
      "Movements": {
        "East - East": "Straight",  "East - North": "Left",  "East - South": "Right",
        "West - West": "Straight",  "West - South": "Left",  "West - North": "Right",
        "North - North": "Straight", "North - West": "Left", "North - East": "Right",
        "South - South": "Straight", "South - East": "Left", "South - West": "Right"
      },
      "Assignments": {"East": "i1-cam4", "West": "i1-cam2", "North": "i1-cam3", "South": "i1-cam1"}
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `corridor_id` | The corridor the snapshots are filed under; it must match the corridor the optimizations name |
| `observation_interval` | Window length: an integer and a unit, `s`, `m`, `h` or `d` (for example `10m`) |
| `intersection_id`, `lat`, `lon` | The intersection and its location (WGS84) |
| `Headings` | Road segment id of the behavior objects → heading, the direction of travel (`North`, `South`, `East`, `West`) |
| `Movements` | `"<entry heading> - <exit heading>"` → turn (`Left`, `Straight`, `Right`) |
| `Assignments` | Approach heading → camera (sensor id) that counts it |

The deployment assets ship the specification of the synthetic demo corridor
(`apps/deploy/city/phase1/Metadata.json`, and the same file in the Helm chart's `configs/`).

### How the counting works

1. An intersection becomes active when it is registered: through the MCP tool
   `register_intersection` or `POST /v1/tmc/intersections`. The id must be in the specification
   and the coordinates within 50 m of its location. Registrations persist in
   `TMC_REGISTRY_FILE`.
2. Every observation window, aligned to the clock and taken once it ended `TMC_SETTLE_LAG_S`
   seconds ago (default 180 s, because behavior objects are written when a track ends), the
   collector queries the behavior API for every camera of the registered intersections:
   `GET <VSS_BEHAVIOR_URL>/behavior?sensorId=<camera>&queryString=end:[<start> TO <end>]`.
3. It keeps the objects of that camera whose `object.type` is a counted label, takes the
   heading of the first and of the last road segment of each object's `edges`, and counts the
   turn that `Movements` gives for the pair, under the approach the camera is assigned to. The
   counting functions are the NVIDIA reference functions, vendored in
   `terasim_tmc_service/nv_generate_tmc.py`.
4. It pushes the snapshot (one count table per registered intersection, see
   [TMC snapshot format](../reference/tmc-snapshot-format.md)) to `terasim-service`, which
   buffers it for two hours. The window is remembered, also across restarts, so a window is
   never pushed twice.

A registration also starts one counting pass right away, and `POST /v1/tmc/run-once` runs one
on request. `GET /v1/tmc/health` shows the registered intersections, the counted labels and the
result of the last pass, including the object labels each camera returned.

### Counted labels

`TMC_COUNTED_OBJECT_TYPES` lists the detector labels counted as traffic (comma-separated,
case-insensitive, `-` and `_` are the same). The default, `Vehicle,two_wheeler`, matches the
labels of the NVIDIA Metropolis smart-city detector. A detector that labels vehicles `car` and
`bicycle` needs `TMC_COUNTED_OBJECT_TYPES=car,bicycle` (Helm `tmc.countedObjectTypes`);
otherwise every count is zero. The `object_types` of a pass in `/v1/tmc/health` shows the
labels the cameras actually return.

## VSS agent configuration

The edits below go into the VSS agent's `config.yml` (on the VSS Helm chart, the config source
of the agent's ConfigMap). Back up the file first; after the edits, restart the agent so it
reloads the configuration. The URL depends on the deployment:

| TeraSim deployment | MCP URL |
| --- | --- |
| Helm, release `terasim-phase1` in namespace `terasim` | `http://terasim-phase1-mcp.terasim.svc:9911/mcp` |
| Docker Compose, agent with host networking on the same host | `http://127.0.0.1:9911/mcp` |
| Docker Compose, agent on its own Compose network on the same host (VSS 3.3) | `http://<host private IP>:9911/mcp`, with `terasim-mcp` also published on that address |

**1. Add the tool group**, after the `function_groups.video_analytics_mcp` block and before
`functions:`:

```yaml
  terasim_mcp:
    _type: mcp_client
    server:
      transport: streamable-http
      url: http://terasim-phase1-mcp.terasim.svc:9911/mcp
    tool_call_timeout: 300          # an optimization is synchronous and takes minutes (1800 with Neural NDE traffic)
    include:
    - list_intersections
    - register_intersection
    - upload_map
    - upload_signal_plan
    - upload_traffic_snapshot
    - optimize_registered_intersection
    - optimize_intersection
    - optimize_corridor
```

**2. Add the tools to `workflow.tool_names`**, after the last entry:

```yaml
  - terasim_mcp.list_intersections
  - terasim_mcp.register_intersection
  - terasim_mcp.optimize_registered_intersection
  - terasim_mcp.optimize_intersection
  - terasim_mcp.optimize_corridor
  - terasim_mcp.upload_signal_plan
```

**3. In `workflow.prompt`**, append an item to the `PRIORITY ACTIONS:` list and insert the
section below before the `GET PLACES TOOL:` section:

```yaml
    8. For SIGNAL TIMING OPTIMIZATION requests ("optimize the intersection/corridor of ...",
       "improve the traffic light timing at ...") → follow the SIGNAL TIMING OPTIMIZATION steps

    SIGNAL TIMING OPTIMIZATION (TeraSim):
    Use the terasim_mcp tools; every optimization needs a registered intersection.
    STEP 1: CALL terasim_mcp.list_intersections (no parameters). Match the user's street names
            to an entry. If no entry matches, tell the user which intersections are available
            and stop.
    STEP 2: If the entry shows "registered": false, CALL terasim_mcp.register_intersection with
            intersection_id, name (human readable street names), lat and lon taken from the entry.
            After a fresh registration, traffic counts accumulate for one observation interval
            (10 minutes); say so if the optimization reports no traffic data yet.
    STEP 3: CALL terasim_mcp.optimize_registered_intersection with ONLY the intersection_id from
            STEP 1; the registry supplies the name, coordinates and corridor, so send no other
            parameter. One exception: if the user explicitly asks for CNDE, Neural NDE, neural
            traffic or learned / naturalistic driver behavior, also send traffic_model = "cnde"
            (never otherwise). The call is synchronous and can take a few minutes, with
            traffic_model = "cnde" much longer — do not retry while it is running. If the result
            says CNDE is not available on this deployment, tell the user so and do not re-run
            without it. Fall back to terasim_mcp.optimize_intersection (corridor_id plus the nested
            intersection object with "intersection_id", "name", "lat" and "lon", and the same
            traffic_model rule) only if the id is not registered.
    STEP 4: The tool result BEGINS with a ready-made markdown answer: a bold title line
            "**Signal timing optimization: ...**", a plan sentence, a KPI table, a NEMA splits
            table and a "**Downloads**" list of URLs; a JSON object follows it. Your final
            answer IS that markdown block, copied EXACTLY and COMPLETELY - from the title line
            down to the last "**Downloads**" URL, every table row included - and nothing else:
            no introduction, no interpretation, no closing sentence, no JSON. Never shorten or
            paraphrase it and never ask whether the user wants the phase timings.
    - If the user provides a signal timing plan document already converted to JSON, CALL
      terasim_mcp.upload_signal_plan with it before optimizing.
```

**4. Add the response rule.** Append item 4 to the `RESPONSE FORMAT (CRITICAL):` list at the
very end of `workflow.prompt`. STEP 4 sits far above the place where the answer is composed;
this closing rule makes the planner keep the block intact:

```yaml
    4. If a tool result begins with a "**Signal timing optimization: ...**" markdown block, your
       final answer IS that block copied EXACTLY and COMPLETELY - from the title line down to the
       last "**Downloads**" URL, every table row and every column unchanged - with nothing added
       before or after it. That block already is the clean, formatted answer rules 1-3 ask for:
       do not restructure it into bullets, do not rename or drop table columns, do not turn the
       URLs into links, never replace any part of it with a prose summary, and never include
       the JSON object that follows the block.
```

The same configuration text ships with the Helm chart as
`apps/deploy/city/helm/terasim-phase1/VSS-WIRING.md`.

**5. Recommended:** raise `workflow.max_iterations` from `15` to `25`. A signal-timing
conversation spends iterations on `list_intersections`, the optimization and the answer; 25
leaves room for a retry.

**6. Planning mode.** VSS agent profiles that set `workflow.planning_enabled: true` compose the
final answer in a separate plan-execution step that summarizes the tool results. Set
`workflow.planning_enabled: false` (the agent's own default) so the answer is the tool's
markdown block as written in STEP 4.

On startup the agent logs each `terasim_mcp` tool it added, which confirms that it reached the
MCP server. `optimize_intersection_by_id` (the same optimization with flat arguments) is
available too; add it to `include:` and `tool_names` only if the agent has to optimize
intersections that are not in the registry.

### Long tool calls

An optimization runs for minutes inside one tool call. While it runs, the MCP server sends a
progress notification and a log notification every `TERASIM_MCP_HEARTBEAT_S` seconds (default
2, Helm `mcp.heartbeatS`) on the call's response stream. This keeps the stream active for MCP
clients with short read timeouts, so no client-side setting other than `tool_call_timeout` is
needed.

### Neural NDE traffic on request

When the TeraSim deployment runs the Neural NDE image (`compose.cnde.yml`, see
[Behavior models](../developer-guide/behavior-models.md)), the optimize tools accept
`traffic_model="cnde"`, and STEP 3 above tells the agent to send it only when the user asks for
Neural NDE traffic explicitly. Such a call simulates every candidate plan on the GPU: about 12
minutes for 24 candidates of 600 s in the fast mode (`TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY=1`,
eight at a time) on the reference deployment, about 39 minutes in the default mode. The
synchronous chain then needs matching timeouts:

| Where | Setting | Value (fast mode, 24 candidates) |
| --- | --- | --- |
| VSS agent, `terasim_mcp` tool group | `tool_call_timeout` | `1800` |
| TeraSim MCP server | `TERASIM_CITY_TIMEOUT_S` | `1800` |
| Reverse proxy in front of the VSS chat UI (for example HAProxy `timeout client` / `timeout server`) | idle timeout | longer than the tool timeout, for example `2100s` |

The VSS chat page ends a request after 300 s without data, and the agent sends none while a tool
call runs, so a Neural NDE request goes through the agent API (`POST /generate` on the agent's
port 8000, body `{"messages": [{"role": "user", "content": "<request>"}]}`); other requests
work in both.

The answer's facts line then reads `Traffic model: CNDE (Neural NDE, learned driver behavior)
simulated every candidate plan.`, and the run is listed with `traffic_model: "cnde"`. Requests
that do not ask for it, and the scheduled runs, keep SUMO traffic.

## Verify

In the VSS chat, ask for example: *"Optimize the intersection of <street A> and
<street B>."* The agent calls `list_intersections`, registers the intersection if needed, calls
`optimize_registered_intersection` and answers with the plan sentence, the KPI table, the NEMA
splits and the download links. Right after a registration the TMC collector counts the latest
complete observation window; when that window holds no detections yet, the counts arrive with
the next window (10 minutes by default).

## Download links

Each optimization through MCP writes its files under `TERASIM_ARTIFACT_ROOT` and links them in
the answer:

| File | Content |
| --- | --- |
| `nema_plan.json` | The optimized plan, `terasim.nema_timing.v1` |
| `optimization.json` | The whole result: KPIs, cost weights, targets, both plans |
| `current_plan.json` | The city's current plan from the plan store, when there is one |
| `timing_report.pdf` | The signal timing report (current plan, optimized plan, KPI comparison), when the current plan is in the plan store |

Links read `<TERASIM_MCP_PUBLIC_URL>/files/<artifact_id>/<file>`. With Helm on OpenShift, the
chart's Route publishes only `/files` at `https://<global.externalHost>`; the MCP protocol
endpoint stays inside the cluster. With Docker Compose, put a reverse proxy in front of
`127.0.0.1:9911/files/` and set `TERASIM_MCP_PUBLIC_URL` to its address.

## Timing plans from the city

`upload_signal_plan` (and `POST /v1/city/plans`) take the city's current timing as a
`terasim.nema_plan.v1` document: phases, rings, coordination patterns and the time-of-day
schedule per intersection, with coordinates. Once stored, the plan is the baseline of later
optimizations and the seed of their search, the KPI comparison is against it, and the timing
report shows current and optimized timing side by side. See
[NEMA plan formats](../reference/nema-plan-format.md).
