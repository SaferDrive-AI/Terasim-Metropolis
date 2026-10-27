# Camera configuration generator (camcfg.py)

One YAML inventory per corridor (corridor → intersections → cameras) is the single source
for every place a camera or an intersection is named. `camcfg.py` derives the tables the
TeraSim services and NVIDIA Metropolis VSS need from it and writes them as files, so adding
an intersection or a camera is one more entry in the inventory and a re-run.

Requirements: Python 3.9+, PyYAML and sumolib (`pip install pyyaml sumolib`). An inventory
whose map is an OpenStreetMap extract also needs `netconvert` (`pip install
eclipse-sumo==1.23.1`). The release environment and the TeraSim image have all three.

## 1. Inventory

```yaml
version: 1
corridor: {id: demo_arterial, name: Arterial Ave, city: {id: example, name: Example City}, observation_interval: 10m}
map:
  sumocfg: /data/input/map/demo.sumocfg          # the map as the service sees it (scheduled_config.yaml)
  sumo_net: ../../../../../examples/corridor_demo/demo.net.xml   # a local copy, to bind the signals
  # or, for a map TeraSim stores at run time:  osm_extract: inputs/arterial_8.osm
road_graph:
  osm: inputs/demo_arterial_road_graph.osm      # the OSM road graph VSS map-matches against
  headings_radius_m: 125
optimization: {sim_end: 1800, max_trials: 24, patience: 16, n_workers: 4}
intersections:
  - id: i1
    name: Arterial Ave & 1st Cross St
    lat: 37.335
    lon: -121.90339
    cameras:
      - sensor: i1-cam1     # VST stream name = camera id everywhere (TMC spec, calibration)
        approach: South     # the traffic it counts, by direction of travel; none = counts nothing
        rtsp_url: "rtsp://i1-cam1.cameras.example:554/stream1"   # a live camera, or
        # test_feed: cam1   # replay a recorded feed of the VSS deployment instead
        calibration: placeholder                                   # or {copy_from: <file>, sensor: <id>}
```

| Section | Keys |
| --- | --- |
| `corridor` | `id` (the corridor id of the TMC spec and of the scheduled configuration), `name`, `city` (`id`, `name`; the calibration's `city` place), `observation_interval` (TMC window, e.g. `10m`) |
| `map` | `sumocfg` (service-side path of the map; default: the image's demo map), `sumo_net` (local copy of that map's network, read to bind the signals), or `osm_extract` (an OpenStreetMap extract: TeraSim stores it with `POST /v1/city/maps` and the generator converts it the same way), optional `store_root` |
| `road_graph` | `osm` (OSM XML, `.osm.gz` or an Overpass JSON response: the graph VSS behavior analytics map-matches against), `headings_radius_m` (how far along each street the Headings table reaches, default 125 m) |
| `optimization` | the corridor's request defaults in `scheduled_config.yaml` (default: 1800 s simulated per plan, 24 plans, patience 16, 4 workers) |
| `vss` | `namespace` (where the VSS recorded-feed pods run, default `vss`), `test_feed_url`, `fanout_name`, `fanout_image`, `calibration_city` |
| `terasim` | `namespace` (default `terasim`), `release` (default `terasim-phase1`), optional `fullname` (the chart's `fullnameOverride`): the Deployment names in the generated scripts |
| `intersections[]` | `id`, `name`, `lat`, `lon`, optional `streets` (the two street names when the junction has more), `tls_id` (force a signal), `headings_radius_m`, and `cameras[]`: `sensor`, `approach` (`North`/`East`/`South`/`West`, `NB`/`EB`/`SB`/`WB`, or `none`), exactly one of `rtsp_url` and `test_feed`, `calibration`, `note` |

`camera_defaults` (a mapping) is merged into every camera entry.

## 2. Generate

```bash
python3 camcfg.py generate examples/demo_arterial.yaml -o out/demo_arterial
python3 camcfg.py generate examples/arterial_32.yaml -o out/arterial_32
```

Anything that does not fit (a signal outside the map, a camera on an approach without
inbound traffic, an ambiguous signal, a junction the map holds only in part) is listed in
`report.md`; errors make the command exit 1 (`--strict`: warnings too), an unreadable
inventory exits 2. `python3 camcfg.py osm-extract INVENTORY -o corridor.osm` downloads the
OpenStreetMap extract TeraSim itself would fetch for the corridor (the intersections plus
250 m).

| Generated file | What it is | Applied with |
| --- | --- | --- |
| `terasim/Metadata.json` | TMC spec: per intersection the road segment → direction-of-travel table (`Headings`, derived from the road graph), the turn table (`Movements`), approach → camera (`Assignments`) | chart value `corridorConfig.metadataJson` |
| `terasim/scheduled_config.yaml` | the corridor's map, signals (bound like the service binds them: nearest signal within 60 m, refused when ambiguous) and optimization budget | chart value `corridorConfig.scheduledConfigYaml` |
| `terasim/values-corridor.yaml` | both files as chart values | `helm upgrade -f values-corridor.yaml` |
| `terasim/corridor-configmap.yaml` | both files as a ConfigMap you manage | `oc apply`, then `corridorConfig.existingConfigMap` |
| `terasim/register-intersections.sh` | registers every intersection with the TMC collector | run once |
| `terasim/register-map.sh` | (`osm_extract` only) stores the corridor's map in TeraSim | run once, before the upgrade |
| `vss/vst-streams.json`, `vss/register-vst-streams.sh` | one `{"name", "sensorUrl"}` per camera, the body of VST's `POST /api/v1/rtsp-streams/add`, and the script that posts them | run once |
| `vss/rtsp-fanout.yaml` | (recorded test feeds only) one RTSP server that re-serves the recorded feeds under one path per camera, so every camera is its own stream URL in VST | `oc apply` in the VSS namespace |
| `vss/calibration.json` | VA-API calibration upload for the cameras with a calibration (`copy_from`) | VA-API `config/upload-file/calibration` |
| `vss/calibration-todo.json` | placeholder entries of the cameras still to calibrate (VSS Studio) | do not upload |
| `report.md`, `report.json` | what was derived from the maps, the signal of every intersection, every issue | read first |

How the tables are derived:

* **Headings** come from the OSM road graph VSS map-matches against: behaviors carry their
  path as OSM node pairs `<u>-<v>`; every edge of the intersection's streets whose nearer end
  lies within `headings_radius_m` of the junction gets the compass direction of travel along
  it. Only the street network connected to the junction counts (a same-named piece of street
  elsewhere does not), one-way streets give one direction, legs closed to cars none. Without
  a road graph the table is approximated from the SUMO map and the report says so.
* **Movements** are the turn table over the approaches and exits found, plus a row set for
  every heading a camera is assigned to (the TMC collector reads one for each).
* **Assignments** are the cameras' `approach` values.

## 3. Apply

```bash
G=out/arterial_32
# TeraSim (chart terasim-phase1 0.6.0, namespace terasim, release terasim-phase1)
$G/terasim/register-map.sh examples/inputs/arterial_8.osm            # once: stores the map
helm upgrade --install terasim-phase1 apps/deploy/city/helm/terasim-phase1 -n terasim \
  -f values-site.yaml -f $G/terasim/values-corridor.yaml                # service and tmc restart
$G/terasim/register-intersections.sh
# VSS
oc apply -n vss -f $G/vss/rtsp-fanout.yaml                             # recorded test feeds only
curl -sk -X POST https://<vss-route-host>/video-analytics-api/config/upload-file/calibration \
  -F configFiles=@$G/vss/calibration.json
H=https://<vss-route-host> $G/vss/register-vst-streams.sh              # after the CV pipeline logs "Active sources"
```

The scripts default to the chart's names for namespace `terasim` and release
`terasim-phase1`; set `TNS`, `TMC` / `SVC` (or the inventory's `terasim` section) for other
names, and `KUBECTL=kubectl` on plain Kubernetes.

Two other ways to apply the two TeraSim files, both without a new chart or image:

* **A ConfigMap you manage** (GitOps): `oc apply -n terasim -f $G/terasim/corridor-configmap.yaml`,
  install the chart with `--set corridorConfig.existingConfigMap=<its name>`, and restart the
  service and tmc Deployments after each change of the ConfigMap.
* **Configuration only, on an installed chart**: replace the two keys of the chart's own
  ConfigMap (`<release>-config` in the default naming) and restart both readers:

  ```bash
  oc set data configmap/terasim-phase1-config -n terasim \
    --from-file=Metadata.json=$G/terasim/Metadata.json \
    --from-file=scheduled_config.yaml=$G/terasim/scheduled_config.yaml
  oc rollout restart -n terasim deploy/terasim-phase1-service deploy/terasim-phase1-tmc
  ```

  A later `helm upgrade` without the values file puts the chart's files back; keep the values
  file (or `existingConfigMap`) for a lasting change.

In every case, register the intersections afterwards: the TMC collector counts only
registered intersections.

## 4. Examples

Every example input is synthetic, written by `examples/inputs/build_synthetic_inputs.py`
(deterministic; run it again to regenerate).

**`examples/demo_arterial.yaml`**, the demo corridor whose map the image bakes: three signals
(`J1`-`J3`), four live cameras each (`i1-cam1` … `i3-cam4`, placeholder stream URLs). It
regenerates the corridor files the chart ships (`configs/Metadata.json`,
`configs/scheduled_config.yaml`); only the road-segment ids of `Headings` differ, because
they are the node pairs of the synthetic road graph.

**`examples/arterial_32.yaml`**, a 32-camera load-test corridor: the eight signals of a
synthetic arterial (Arterial Blvd, Oak St to Walnut St), four cameras per signal replaying
four recorded feeds (`test_feed`) and copying the calibration of the reference cameras
`cam1`-`cam4` at Pine St. Its map is an OpenStreetMap extract (`inputs/arterial_8.osm`, map
id `ed0947a56fee`) that TeraSim stores at run time:

| Intersection | Signal | Cameras | Counting approaches |
| --- | --- | --- | --- |
| Oak St | `1001` | cam5-cam8 | NB, EB, SB, WB |
| Elm St | `1002` | cam9-cam12 | NB, EB, WB (the north leg is a pedestrian mall) |
| Pine St | `1003` | cam1-cam4 | NB, EB, SB, WB |
| Cedar St | `1004` | cam13-cam16 | NB, EB, WB (one-way northbound) |
| Maple St | `1005` | cam17-cam20 | EB, SB, WB (one-way southbound) |
| Birch St | `1006` | cam21-cam24 | NB, EB, WB (one-way northbound) |
| Ash St | `1007` | cam25-cam28 | EB, SB, WB (one-way southbound) |
| Walnut St | `1008` | cam29-cam32 | EB, SB, WB (a plaza south of the junction) |

26 cameras count an approach; the other six (cam9, cam13, cam19, cam21, cam27, cam31) watch
legs without inbound traffic and run as VSS streams with `approach: none`. A same-named
street piece south of the Walnut St plaza is not connected to the junction and is left out
of its Headings table. The generated `report.md` lists the signal bound for every
intersection; the service binds the same ones when it stores the extract.

With copied calibrations every stream runs, but the detections of a copied camera are placed
where the original camera's image points are (the report says so per camera): give each
camera its own calibration from VSS Studio for real counts at every intersection.

## 5. Tests

`tests/test_tmc_service/test_camera_config.py` (generator) and
`tests/test_tmc_service/test_chart_corridor_config.py` (the chart's `corridorConfig`).
