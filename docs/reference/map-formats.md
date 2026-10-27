# Map formats

A corridor's map can come in four formats. The map adapter (`terasim_city.maps.MapStore`,
behind `POST /v1/city/maps` and the MCP tool `upload_map`) recognizes the format from the file
suffix and the XML root element, converts HD maps to a SUMO network once, and stores the result
as a map package under `$TERASIM_CITY_DATA_ROOT/maps/<map_id>/`.

| Format | File | Recognized by | Conversion | Geo-reference | Traffic lights |
| --- | --- | --- | --- | --- | --- |
| OpenStreetMap | `.osm` | `<osm>` root without `type=lanelet` relations | `netconvert --osm-files` with the SUMO `osmBuild` flags (`--geometry.remove --junctions.join --tls.guess-signals --tls.discard-simple --ramps.guess --tls.default-type static --output.street-names`) | node lat/lon | OpenStreetMap signal tags |
| SUMO scenario | `.sumocfg` | `<configuration>` or `<sumoConfiguration>` root | none: the scenario stays where it is, only its manifest is stored | the net's own projection | the net's programs and its additional files |
| OpenDRIVE | `.xodr` | `<OpenDRIVE>` root | `netconvert --opendrive-files --junctions.join --tls.default-type static --output.street-names` | the header's `<geoReference>`, or the `proj` override | the OpenDRIVE signals netconvert recognizes as traffic lights; without any, `--tls.guess` (reported as `tls_source: "guessed"`) |
| Lanelet2 | `.osm` | at least one `type=lanelet` relation | built-in converter: lanelets to SUMO plain XML, then netconvert | node lat/lon; else `local_x`/`local_y` through `map_projector_info.yaml` next to the file (MGRS, LocalCartesianUTM, LocalCartesian) or the `proj` override | `traffic_light` regulatory elements |

When several stored maps cover a corridor, the richer source wins: OpenDRIVE, then Lanelet2,
then a SUMO scenario, then OpenStreetMap. A corridor must lie entirely inside one map.

## Uploading a map

The file must already be readable by the service, typically on its data volume; the request
carries the path, not the file:

```bash
curl -s -X POST http://localhost:8000/v1/city/maps -H 'Content-Type: application/json' \
  -d '{"path": "/data/input/maps/corridor.xodr"}'
```

```python
# MCP (terasim-agent-skill): upload_map(path, proj=None)
# In process:
from terasim_city.maps import MapStore
MapStore("/data/output/store").ingest("/data/input/maps/corridor.xodr", proj=None)
```

The answer (HTTP 201) is `{"map_id", "duplicate", "source", "summary"}`:

- `map_id` is a 12-character content hash. Uploading the same content again answers
  `"duplicate": true` without converting again. For a SUMO scenario the hash covers the files the
  configuration references; for HD maps it includes the `proj` override, so a different `proj` is
  a different map.
- `source` is `osm`, `prebuilt`, `opendrive` or `lanelet2`.
- `summary` describes the network: `edges`, `lanes`, `junctions`, `intersections` (junctions
  joining three or more roads), `signalized_junctions`, `traffic_lights`, `dead_ends`,
  `drivable_edges`, `components`, `largest_component_share` and
  `largest_strong_component_share` (the share of drivable edges in the largest weakly and
  strongly connected piece), `geo_referenced`, `proj` and `warnings` (no signal, a fragmented
  network, guessed signals, a replaced geo-reference, ...). Converted maps add the conversion
  time, netconvert's warning counts and `tls_source`; Lanelet2 maps add a `lanelet2` block
  (lanelets used and skipped, junction clusters, driving side, coordinate source), and their
  package carries `lanelet2_mapping.json` (SUMO lanes and junctions to lanelet ids).

Errors: 400 for an unknown format or an unusable geo-reference (the message says how to fix
it), 422 when no network SUMO can load can be built.

`GET /v1/city/maps` lists the stored map ids.

## Projection override (`proj`)

Matching coordinates (TMC intersections, targets, plans) to the map needs a geo-referenced
network. Many HD maps are not: OpenDRIVE files without `<geoReference>`, files that declare UTM
while their x/y are local metres (rejected, as the map would land far from its real place), and
Lanelet2 maps with local coordinates only. `proj` is a proj.4 definition of the map's own planar
coordinates (OpenDRIVE x/y, Lanelet2 `local_x`/`local_y`):

| Map coordinates | `proj` |
| --- | --- |
| UTM metres | `+proj=utm +zone=<zone> +datum=WGS84 +units=m +no_defs` |
| Local metres around a known origin | `+proj=tmerc +lat_0=<LAT> +lon_0=<LON> +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs` |
| UTM metres minus an origin `(E0, N0)`; `<CM>` is the central longitude of the UTM zone | `+proj=tmerc +lat_0=0 +lon_0=<CM> +k=0.9996 +x_0=<500000-E0> +y_0=<-N0> +datum=WGS84 +units=m +no_defs` |

For OpenDRIVE the override is written into the `<geoReference>` of a temporary copy of the file
before conversion. `proj` is rejected for OpenStreetMap files and SUMO scenarios, which carry
their own reference.

## Conversion details

**OpenDRIVE.** A fragmented result (less than 80 % of the edges in one connected piece) is
converted again with `--junctions.join-same` and kept when that reconnects it. netconvert reads
lane-level `<speed>` records; roads with only a road-level speed get netconvert's default lane
speed. The converted network must load in SUMO; a file whose topology netconvert cannot rebuild
is rejected with the reason.

**Lanelet2.** Every drivable lanelet (`road`, `highway`, `bus_lane`, `play_street`,
`emergency_lane`) becomes a lane following its centerline; crosswalks, walkways, shoulders and
bicycle lanes are skipped. Lanelets inside an intersection are collapsed into one SUMO junction
whose internal lanes netconvert builds; lanelets running side by side between the same
junctions form one multi-lane edge. The driving side is inferred from shared lane boundaries.
Elevation, pedestrian and bicycle lanelets, `right_of_way` and `traffic_sign` regulatory
elements and lane-change restrictions from line types are not used.

**Signal timings.** The programs of converted maps are netconvert's static defaults. The
optimizer replaces them: with the city's uploaded plan (`POST /v1/city/plans`), a timing sheet,
or a Webster plan computed from the counts.

## Map selection during an optimization

An optimization uses, in this order: the map named by the request or its corridor
configuration entry (`base_sumocfg`), else the stored map covering every TMC intersection of the
demand (best source first), else, when `TERASIM_CITY_OSM_DOWNLOAD` allows it, an OpenStreetMap
extract around the intersections (250 m margin, `TERASIM_CITY_MAP_MARGIN_M`) downloaded from
the Overpass endpoint (`TERASIM_CITY_OVERPASS_URL`), converted and stored for later runs. With
downloads off, a request that no stored map covers answers 422.

## Map package

```text
$TERASIM_CITY_DATA_ROOT/maps/<map_id>/
├── map_package.json            the manifest
├── *.net.xml, base.sumocfg     converted network and its
│                               base configuration
└── source.osm | source.xodr    archived source file
```

The manifest (schema `terasim.map_package.v1`) names the network, the base configuration and
the signal files, and records `geo_referenced`, `source` and the conversion summary
(`metadata.conversion`). It stores paths relative to the package, so a package directory can be
moved as a unit. For a SUMO scenario only the manifest is stored and the scenario's files stay
in place.
