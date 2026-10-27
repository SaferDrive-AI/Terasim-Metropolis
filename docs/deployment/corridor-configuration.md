# Corridor and camera configuration

A deployment serves one corridor: its intersections, the camera on each approach and the
signals TeraSim optimizes. Two files describe it, and both are read at start-up:

| File | Read by | Content |
| --- | --- | --- |
| `Metadata.json` | TMC collector (`TMC_SPEC_FILE`) | the corridor specification: per intersection its location, `Headings` (road segment of the behavior objects → direction of travel), `Movements` (heading pair → turn) and `Assignments` (approach → camera); see [VSS integration](vss-integration.md#corridor-specification-metadatajson) |
| `scheduled_config.yaml` | service (`TERASIM_CITY_SCHEDULED_CONFIG`) | the corridor's map (`base_sumocfg`), signals (`corridor.tls_ids`) and optimization budget; see [Configuration system](../developer-guide/configuration.md#corridor-configuration) |

The Helm chart ships both for the synthetic demo corridor (`demo_arterial`: intersections
`i1`-`i3`, signals `J1`-`J3`, cameras `i1-cam1` … `i3-cam4`). Another corridor, more
intersections or more cameras need neither a new chart nor a new image: generate the two
files from a camera inventory and hand them to the chart.

## Camera inventory

The generator `apps/deploy/city/camera-config/camcfg.py` reads one YAML inventory per
corridor (corridor → intersections → cameras) and writes every file that names a camera or
an intersection:

```yaml
version: 1
corridor: {id: demo_arterial, name: Arterial Ave, city: {id: example, name: Example City}, observation_interval: 10m}
map:
  sumocfg: /data/input/map/demo.sumocfg          # the map as the service sees it
  sumo_net: ../../../../../examples/corridor_demo/demo.net.xml   # a local copy, to bind the signals
road_graph:
  osm: inputs/demo_arterial_road_graph.osm      # the OSM road graph VSS map-matches against
optimization: {sim_end: 1800, max_trials: 24, patience: 16, n_workers: 4}
intersections:
  - id: i1
    name: Arterial Ave & 1st Cross St
    lat: 37.335
    lon: -121.90339
    cameras:
      - {sensor: i1-cam1, approach: South, rtsp_url: "rtsp://i1-cam1.cameras.example:554/stream1"}
      - {sensor: i1-cam2, approach: West, rtsp_url: "rtsp://i1-cam2.cameras.example:554/stream1"}
      - {sensor: i1-cam3, approach: North, rtsp_url: "rtsp://i1-cam3.cameras.example:554/stream1"}
      - {sensor: i1-cam4, approach: East, rtsp_url: "rtsp://i1-cam4.cameras.example:554/stream1"}
```

- `sensor` is the VST stream name, and the camera id in the corridor specification and in the
  VA-API calibration.
- `approach` is the traffic the camera counts, by direction of travel (`South` = southbound
  traffic); `none` for a camera that counts nothing (a leg without inbound traffic).
- Each camera has either `rtsp_url` (a camera) or `test_feed` (a recorded feed of the VSS
  deployment, replayed under the camera's own stream URL).
- `calibration` is `placeholder` (to be calibrated in VSS Studio) or
  `{copy_from: <calibration file>, sensor: <id>}`.
- `map` is either `sumocfg` + `sumo_net` (a map the service already has, such as the baked
  demo map) or `osm_extract`: an OpenStreetMap extract that the service stores at run time
  (`POST /v1/city/maps`), which the generator converts the same way.

The tool's README (`apps/deploy/city/camera-config/README.md`) lists every key.

## Generate

```bash
pip install pyyaml sumolib eclipse-sumo==1.23.1
python3 apps/deploy/city/camera-config/camcfg.py generate \
    apps/deploy/city/camera-config/examples/arterial_32.yaml -o generated/
```

| Generated file | What it is |
| --- | --- |
| `terasim/Metadata.json`, `terasim/scheduled_config.yaml` | the two corridor files |
| `terasim/values-corridor.yaml` | both files as chart values (`corridorConfig`) |
| `terasim/corridor-configmap.yaml` | both files as a ConfigMap you manage |
| `terasim/register-intersections.sh` | registers every intersection with the TMC collector |
| `terasim/register-map.sh` | stores the corridor's OpenStreetMap extract in TeraSim (`osm_extract` only) |
| `vss/vst-streams.json`, `vss/register-vst-streams.sh` | one `{"name", "sensorUrl"}` per camera for VST's `POST /api/v1/rtsp-streams/add`, and the script that posts them |
| `vss/rtsp-fanout.yaml` | recorded test feeds only: one RTSP server that re-serves the recorded feeds under one path per camera |
| `vss/calibration.json`, `vss/calibration-todo.json` | VA-API calibration entries; placeholders of the cameras still to calibrate |
| `report.md`, `report.json` | the signal bound for every intersection, what was derived from the maps, every issue |

`Headings` come from the OSM road graph VSS map-matches against, so the road-segment ids are
the ones the behavior objects carry; only the street network connected to the junction
counts, one-way streets give one direction, legs closed to cars none. Signals are bound the
way the service binds them (the nearest signal within 60 m, refused when ambiguous). Problems
(a signal the map does not cover, a junction the map holds only in part, a camera on an
approach without inbound traffic) are listed in `report.md`; errors make the command exit 1.

## Apply

**Chart values** (recommended): upgrade the release with the generated values file on top of
the site values. The service and TMC pods carry a checksum of the two files, so the upgrade
restarts both.

```bash
generated/terasim/register-map.sh <path to the extract>     # osm_extract inventories, once
helm upgrade --install terasim-phase1 apps/deploy/city/helm/terasim-phase1 -n terasim \
  -f values-site.yaml -f generated/terasim/values-corridor.yaml
generated/terasim/register-intersections.sh
```

| Value | Default | Meaning |
| --- | --- | --- |
| `corridorConfig.metadataJson` | `""` | the corridor specification's text (`--set-file corridorConfig.metadataJson=Metadata.json`); empty = the file shipped in the chart |
| `corridorConfig.scheduledConfigYaml` | `""` | the scheduled configuration's text; empty = the file shipped in the chart |
| `corridorConfig.existingConfigMap` | `""` | name of a ConfigMap you manage (keys `Metadata.json`, `scheduled_config.yaml`), mounted instead of the chart's own |

The chart refuses files the services would refuse at start-up (not JSON or YAML, no corridor
id, an empty intersection list or table, an `Assignments` heading without a `Movements` row, a
corridor without `base_sumocfg`, more than 1 MiB together), and its notes print the corridor,
the number of intersections and cameras, and a warning when the specification's corridor is
not in the scheduled configuration.

**A ConfigMap you manage** (GitOps): apply `generated/terasim/corridor-configmap.yaml`, install
the chart with `--set corridorConfig.existingConfigMap=<its name>`, and restart the service and
TMC Deployments after each change of the ConfigMap (the chart cannot see its content).

**Configuration only**, on an installed release: replace the two keys of the chart's ConfigMap
and restart both readers.

```bash
oc set data configmap/terasim-phase1-config -n terasim \
  --from-file=Metadata.json=generated/terasim/Metadata.json \
  --from-file=scheduled_config.yaml=generated/terasim/scheduled_config.yaml
oc rollout restart -n terasim deploy/terasim-phase1-service deploy/terasim-phase1-tmc
```

A later `helm upgrade` without the values file restores the chart's files; use the values file
or `existingConfigMap` for a lasting change.

In every case the TMC collector counts only registered intersections: run
`register-intersections.sh` (or `POST /v1/tmc/intersections` per intersection, or the MCP tool
`register_intersection`) after the new specification is in place. On the VSS side, register the
streams (`register-vst-streams.sh`, after the CV pipeline reports its active sources) and upload
the calibration.

## Example: 32 cameras on eight signals

`examples/arterial_32.yaml` describes a synthetic load-test corridor, Arterial Blvd from Oak St
to Walnut St, with four cameras per signal replaying four recorded feeds. Its map is an
OpenStreetMap extract (`examples/inputs/arterial_8.osm`) that the service stores at run time
(map id `ed0947a56fee`); every input is synthetic.

| Intersection | Signal | Cameras | Counting approaches |
| --- | --- | --- | --- |
| Oak St | `1001` | cam5-cam8 | NB, EB, SB, WB |
| Elm St | `1002` | cam9-cam12 | NB, EB, WB (pedestrian mall north of the junction) |
| Pine St | `1003` | cam1-cam4 | NB, EB, SB, WB |
| Cedar St | `1004` | cam13-cam16 | NB, EB, WB (one-way northbound) |
| Maple St | `1005` | cam17-cam20 | EB, SB, WB (one-way southbound) |
| Birch St | `1006` | cam21-cam24 | NB, EB, WB (one-way northbound) |
| Ash St | `1007` | cam25-cam28 | EB, SB, WB (one-way southbound) |
| Walnut St | `1008` | cam29-cam32 | EB, SB, WB (plaza south of the junction) |

26 cameras count an approach; six watch legs without inbound traffic (`approach: none`) and
run as VSS streams. The generator reports all eight signals bound, the service binds the same
ones when it stores the extract, and the chart renders the generated values into a ConfigMap
with the eight intersections. `examples/demo_arterial.yaml` describes the demo corridor and
regenerates the two files the chart ships.

## Requirements

Python 3.9 or later with PyYAML and sumolib; `netconvert` (from `eclipse-sumo`) for inventories
whose map is an OpenStreetMap extract. The release environment and the TeraSim image provide
all of them. `camcfg.py generate` only reads its inputs and writes the output directory;
`camcfg.py osm-extract` downloads the corridor's OpenStreetMap extract from Overpass, the way the
service would.
