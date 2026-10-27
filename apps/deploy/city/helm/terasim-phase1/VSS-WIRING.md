# Wiring the VSS agent to the TeraSim service (OpenShift)

This is the **VSS-side** configuration that lets the NVIDIA Metropolis VSS (Video
Search and Summarization) agent call the TeraSim signal-optimization service once
the `terasim-phase1` Helm chart is deployed. It changes **only the VSS agent's
`config.yml`**: no VSS image or code change. Everything below is text you add to
the agent config; the values in `<...>` depend on your environment.

## The three connection points

1. **VSS agent → TeraSim MCP** (the agent calls the TeraSim tools). You add a
   `terasim_mcp` tool group to the agent config, pointing at the in-cluster MCP
   Service.
2. **TeraSim → VSS behavior API** (the TMC collector pulls the detections). This
   is a **value on the TeraSim chart**, not a VSS change: set `tmc.vssBehaviorUrl`
   to the VSS video-analytics-api Service at install, e.g.
   `http://vss-video-analytics-api.<vss-namespace>.svc:8081` (port 8081; not
   `vss-vios-ingress`, which is the VST proxy on 30888).
3. **Download links** (PDF/JSON of each result). Served by the TeraSim chart's own
   OpenShift **Route** (`https://<externalHost>/files/...`). No VSS change.

## Prerequisites

- The `terasim-phase1` chart is deployed (default: release `terasim-phase1` in
  namespace `terasim`). The MCP Service is then reachable in-cluster at:
  ```
  http://terasim-phase1-mcp.<terasim-namespace>.svc:9911/mcp
  ```
  (default `http://terasim-phase1-mcp.terasim.svc:9911/mcp`).

## Edits to the VSS agent `config.yml`

Back up the file first. On the NVIDIA VSS chart the agent config is shipped via
a ConfigMap built from `configs/vss-agent/config.yml`, so make these edits in
that config source, then redeploy / restart the `vss-agent` pod (see "Apply").

**1. Add the tool group.** After the `function_groups.video_analytics_mcp`
block, before `functions:`:

```yaml
  terasim_mcp:
    _type: mcp_client
    server:
      transport: streamable-http
      # In-cluster Service DNS of the TeraSim MCP (adjust namespace/release if changed):
      url: http://terasim-phase1-mcp.terasim.svc:9911/mcp
    tool_call_timeout: 300          # an optimization is synchronous and can take minutes
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

**2. Add the tools to `workflow.tool_names`** (after the last existing entry):

```yaml
  - terasim_mcp.list_intersections
  - terasim_mcp.register_intersection
  - terasim_mcp.optimize_registered_intersection
  - terasim_mcp.optimize_intersection
  - terasim_mcp.optimize_corridor
  - terasim_mcp.upload_signal_plan
```

**3. In `workflow.prompt`:** append item 8 to the `PRIORITY ACTIONS:` list, and
insert the `SIGNAL TIMING OPTIMIZATION (TeraSim):` section before the
`GET PLACES TOOL:` section:

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

**4. Required for the result to reach the chat box.** Append item 4 to the
`RESPONSE FORMAT (CRITICAL):` list at the very end of `workflow.prompt`. STEP 4
sits far above the place where the answer is composed; this closing rule makes a
small planner model keep the block intact:

```yaml
    4. If a tool result begins with a "**Signal timing optimization: ...**" markdown block, your
       final answer IS that block copied EXACTLY and COMPLETELY - from the title line down to the
       last "**Downloads**" URL, every table row and every column unchanged - with nothing added
       before or after it. That block already is the clean, formatted answer rules 1-3 ask for:
       do not restructure it into bullets, do not rename or drop table columns, do not turn the
       URLs into links, never replace any part of it with a prose summary, and never include
       the JSON object that follows the block.
```

**5. Recommended:** raise `workflow.max_iterations` from `15` to `25` (a sibling
of `tool_names:`). A signal-timing conversation spends iterations on
`list_intersections`, the optimization itself and the final answer; 25 leaves
room for a retry.

**6. Planning mode:** if the agent profile sets `workflow.planning_enabled: true`,
set it to `false` (the agent's own default). With planning on, the final answer
is composed by a plan-execution step that summarizes the tool results instead of
returning the markdown block of STEP 4.

## Apply

1. Edit the agent `config.yml` in your VSS chart's config source (ConfigMap).
2. Redeploy / restart the `vss-agent` pod so it reloads the config.
3. On startup the agent logs `Adding tool ... to group` for each `terasim_mcp`
   tool — confirms it reached the MCP Service.

## Verify

In the VSS chat, ask: **"Optimize the intersection of <street A> and <street B>."**
With the demo corridor configuration of the chart, for example "Optimize the
intersection of Main St and 1st Ave" (intersection `i1`). The agent calls
`list_intersections` → `optimize_registered_intersection` and replies with the
plan, a KPI table, the per-phase NEMA splits and a **Downloads** block of links.
Right after a registration the TMC collector injects the latest complete
observation window; when that window holds no detections yet, the counts arrive
with the next window (10 minutes).

## Notes

- The optimization is synchronous and can take a few minutes. The MCP server keeps
  the response stream alive with a heartbeat (a progress and a log notification
  every 2 s), so no VSS-side change is needed for that beyond
  `tool_call_timeout: 300` above.
- **Neural NDE (CNDE) traffic, on request only.** Every optimize tool takes an
  optional `traffic_model`; the agent leaves it unset (SUMO traffic) for every
  ordinary request and sets `"cnde"` only when the user explicitly asks for CNDE,
  Neural NDE, neural traffic or learned / naturalistic driver behavior (the tool
  descriptions and STEP 3 above say so). Every candidate plan of that one
  optimization is then simulated with the Neural NDE learned driver model on the
  GPU, and the answer's facts line reads `Traffic model: CNDE (Neural NDE, learned
  driver behavior) simulated every candidate plan.` Such a call runs far longer
  than 300 s (minutes per candidate plan): raise `tool_call_timeout` to cover it,
  and `TERASIM_CITY_TIMEOUT_S` of the TeraSim MCP server with it. A deployment
  without the Neural NDE runtime answers with an error naming what is missing
  instead of running SUMO.
- Only `/files` (downloads) is exposed externally via the Route; the MCP protocol
  endpoint (`/mcp`) stays in-cluster.
- `optimize_intersection_by_id` (flat-argument variant) exists but is left out of
  `include:` to keep the planner's toolbox small; add it only if the agent has to
  optimize an intersection that is not in the registry.
- Scheduled operation needs no TeraSim-side logic: the VSS agent invokes the same
  optimization tool on its own cadence. The chart also offers an optional built-in
  scheduler (`scheduler.enabled`, hourly by default) that optimizes the configured
  corridors from their recent counts.
