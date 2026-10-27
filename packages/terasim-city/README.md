# terasim-city

City traffic data integration for TeraSim.

This package turns measured city traffic data into SUMO-runnable scenarios:
`terasim-city` consumes observed data (currently TMC turning movement counts)
and produces deterministic, reproducible route files, on which it optimizes
corridor signal timing.

## Quickstart

```bash
# Install
pip install -e packages/terasim-city
```

```python
# One-shot end-to-end (recommended)
from terasim_city import run_tmc_pipeline
scenario = run_tmc_pipeline(
    "examples/corridor_demo/demo_tmc.json",
    "examples/corridor_demo/demo.sumocfg",
    "/tmp/tmc_scenario",
)

# Or with the map store (map auto-discovered from the flow file):
from terasim_city import generate_flow
result = generate_flow("aggregated_tmc.json", "/tmp/out", data_root="/data/store")
```

## Architecture

The pipeline is layered:

- **Layer 1 — Core components (single responsibility)**
  - `CorridorGraphFlowSource` — flow-count JSON → SUMO `rou.xml` (OD peel-off);
    the default, `flow_source="corridor_graph"`
  - `ScenarioRoutesFlowSource` — the base scenario's own route files → one `rou.xml`,
    the demand untouched (`flow_source="scenario_routes"`, see below)
  - `ScenarioAssembler` — combine `rou.xml` with a base sumocfg to produce a
    runnable scenario directory
- **Layer 2 — Python API**: `run_tmc_pipeline()` glues a flow source and the
  assembler together (`flow_source=` picks the source);
  `generate_flow()` adds automatic map lookup from the map store

## Customer scenarios

A customer who already has a calibrated SUMO scenario (net, route files, signal
programs) needs no route synthesis from counts. `flow_source="scenario_routes"`
copies the base sumocfg's route files as they are; the TMC file is still parsed (its
corridor id goes into `metadata.json`), but its counts are not used:

```python
scenario = run_tmc_pipeline(
    "examples/corridor_demo/placeholder_tmc.json",
    "examples/corridor_demo/demo_scenario.sumocfg",
    "/tmp/scenario_out",
    sim_end=600.0,
    flow_source="scenario_routes",
)
```

The optimizer's matching switches (`flow_source`, `counts_source`,
`seed_from_scenario_programs`, `stage_mode`, `search_cycle`, `cycle_min_s`,
`cycle_max_s`, and the `bayes_corridor` optimizer) are described in the
project documentation (`docs/`).

## Supported map formats and upload

A corridor's map is any of four kinds of file. The map store
(`terasim_city.maps.MapStore`, behind `POST /v1/city/maps` and the MCP tool
`upload_map`) detects the kind from suffix + content, converts HD maps to a SUMO
net once, and keeps the result as a map package under
`$TERASIM_CITY_DATA_ROOT/maps/<map_id>/` (net, `base.sumocfg`, archived source,
`map_package.json`). When several stored maps cover a corridor, HD maps win:
OpenDRIVE > Lanelet2 > prebuilt SUMO > OSM.

| Kind | File | Recognized by | Conversion | Geo-reference | Traffic lights |
|---|---|---|---|---|---|
| OpenStreetMap | `.osm` | `<osm>` root, no `type=lanelet` relation | `netconvert --osm-files` (osmBuild-style flags) | node lat/lon (always) | OSM `highway=traffic_signals` |
| Prebuilt SUMO scenario | `.sumocfg` | `<configuration>` / `<sumoConfiguration>` root | none (stays in place; only the manifest is stored) | the net's own `projParameter` | the net and its additional files |
| OpenDRIVE | `.xodr` | `<OpenDRIVE>` root | `netconvert --opendrive-files --junctions.join` | header `<geoReference>` (proj.4), or `proj` | OpenDRIVE signals netconvert recognizes (e.g. type 1000001); none → `--tls.guess`, reported as `tls_source: "guessed"` |
| Lanelet2 | `.osm` | at least one `type=lanelet` relation | built-in converter (`maps/lanelet2.py`) → SUMO plain XML → netconvert | node lat/lon → UTM; else `local_x`/`local_y` via `map_projector_info.yaml` next to the file (MGRS, LocalCartesianUTM, LocalCartesian) or `proj` | `traffic_light` regulatory elements |

### Uploading

The file must already be on the service's asset volume (the client never sends
file bytes):

```bash
curl -X POST http://terasim-service:8000/v1/city/maps -H 'Content-Type: application/json' \
  -d '{"path": "/data/input/maps/demo/corridor.xodr",
       "proj": "+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs"}'
```

```python
# MCP (terasim-agent-skill): upload_map(path, proj=None)
client.upload_map("/data/input/maps/demo/lanelet2_map.osm")
# In-process
MapStore("/data/output/store").ingest("/data/input/maps/corridor.xodr", proj=None)
```

The 201 answer is `{"map_id", "duplicate", "source", "summary"}`. `summary`
(also stored as `metadata.conversion` in the manifest) reports `edges`, `lanes`,
`junctions`, `intersections` (junctions joining three or more roads),
`traffic_lights`, `tls_source`, `geo_referenced`, `proj`,
`largest_component_share` / `largest_strong_component_share` (share of
car-usable edges in the largest weakly / strongly connected piece),
`conversion_seconds`, netconvert's warning counts and a `warnings` list (no
signal, fragmented network, guessed signals, replaced geo-reference, ...).
Lanelet2 adds a `lanelet2` block (lanelets used/skipped, junction clusters,
driving side and its evidence, coordinate source) and the package carries
`lanelet2_mapping.json` (SUMO edge lanes and junctions → lanelet ids).
Errors: 400 for an unknown format or an unusable geo-reference (the message says
how to fix it), 422 when netconvert/SUMO cannot build a loadable net.

### Projection override (`proj`)

Lat/lon matching only works on a geo-referenced net, and many HD maps are not:
OpenDRIVE files without `<geoReference>`, files that declare plain UTM while
their x/y are local metres (rejected: the map would land thousands of km away),
Lanelet2 maps without lat/lon. `proj` is a proj.4 definition of the map's OWN
planar coordinates (OpenDRIVE x/y; Lanelet2 `local_x`/`local_y`):

- true UTM metres: `+proj=utm +zone=10 +datum=WGS84 +units=m +no_defs`
- local metres around a known origin: `+proj=tmerc +lat_0=<LAT> +lon_0=<LON> +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs`
- UTM minus an origin `(E0, N0)` (`<CM>` is the zone's central longitude, e.g. -123 for zone 10):
  `+proj=tmerc +lat_0=0 +lon_0=<CM> +k=0.9996 +x_0=<500000-E0> +y_0=<-N0> +datum=WGS84 +units=m +no_defs`

For OpenDRIVE the override is written into a temporary copy's `<geoReference>`;
it is never passed as `netconvert --proj`, which would read the x/y as lon/lat.
A different `proj` is a different conversion and therefore a different `map_id`.
`proj` is rejected for OSM and prebuilt maps (they carry their own reference).

### Known limitations (basic converters)

- Lanelet2: each lanelet's centerline becomes a lane; junction interiors are
  collapsed into SUMO junctions whose internal lanes netconvert draws (not the
  lanelet geometry); complex areas (bus rotaries, plazas) become large junctions.
  Not used: elevation, crosswalk/pedestrian/bicycle lanelets, `right_of_way` /
  `traffic_sign` regulatory elements (netconvert derives priorities), lane-change
  rules from line types. Signal programs are netconvert's static defaults.
  Driving side is inferred from shared bounds; a map without opposite-direction
  neighbours is assumed right-hand.
- OpenDRIVE: the result is as good as the file's road/lane links. netconvert
  1.23 reads lane-level `<speed>` but not the road-level `<type><speed>`
  (lanes then get the 50 km/h type default); `--junctions.join-same` is retried
  automatically on a fragmented result; a file whose topology netconvert cannot
  rebuild (e.g. no `<junction>` elements, degenerate geometry) is rejected.
- Signal timings of converted maps are placeholders for the optimizer; supply
  real plans through `POST /v1/city/plans`.

## Calling the optimizer from a script — two hard rules

```python
from terasim_city.orchestration.corridor_optimization import (
    CorridorOptimizationConfig,
    run_corridor_optimization,
)

def main():
    cfg = CorridorOptimizationConfig(
        tmc_json="counts.json",
        base_sumocfg="scenario.sumocfg",
        output_dir="out",
        tls_ids=("J1", "J2", "J3"),  # or from_ref="i1", to_ref="i3"
    )
    run_corridor_optimization(cfg)

if __name__ == "__main__":  # REQUIRED — see rule 1
    main()
```

1. **Always call it behind `if __name__ == "__main__":`.** Trials run in a
   spawn-based process pool; spawn re-imports your script, so an unguarded
   top-level call makes every worker relaunch the whole optimization — the
   run hangs with no error and no progress.
2. **On a map with more than one traffic light, identify the corridor** —
   either explicit `tls_ids` or a `from_ref`/`to_ref` pair (TMC intersection
   ids or `"lat,lon"` strings). With neither, a multi-TLS map raises
   `ValueError` instead of guessing which lights you meant.

## Demo corridor

`examples/corridor_demo` is a fully synthetic three-signal arterial (signals
`J1`/`J2`/`J3`, TMC intersections `i1`/`i2`/`i3`) with hourly counts,
10-minute snapshots, a timing sheet and a customer-style scenario. Its README
walks through on-demand, scheduled and incident-triggered optimization runs,
and the integration tests under `tests/test_city` run the pipeline end to end
on it.
