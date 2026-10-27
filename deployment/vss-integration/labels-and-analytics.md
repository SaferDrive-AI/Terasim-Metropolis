# Detector labels, behavior analytics and the TMC collector

The TMC collector counts a VSS behavior object when its `object.type` is a counted label and it
carries road-network `edges` (map matching of VSS behavior analytics). Three settings must name
the same classes (behavior analytics compares case-sensitively):

| Setting | Where | Smart-city labels | Lowercase labels |
| --- | --- | --- | --- |
| Detector label file (line N = class id N) | VSS real-time computer vision | `two_wheeler`, `Vehicle`, `Person` | `bicycle`, `car`, `person` |
| `mapMatchingClasses` | VSS behavior analytics, `app` section | `["Vehicle","Person","two_wheeler"]` | `["car","person","bicycle"]` |
| `TMC_COUNTED_OBJECT_TYPES` | TeraSim TMC collector | `Vehicle,two_wheeler` (default) | `car,bicycle` |

Behavior analytics map-matches against an OpenStreetMap graph that must cover the cameras
(`coordinateReferenceSystem.roadNetwork.graph` of its configuration; for the reference corridor:
`vss/behavior-analytics-graph.json` and `vss/osm-cache/` of the San José reference corridor
package). Calibration and road network:

```bash
curl -X POST <va-api>/config/upload-file/calibration  -F configFiles=@calibration.json
curl -X POST <va-api>/config/upload-file/road-network -F configFiles=@road-network.json
```

The VST stream name of a camera is its sensor id in the calibration and its camera id in the
corridor spec (`Assignments` of `Metadata.json`). For many streams, the real-time computer vision
needs as many sources (`NUM_SENSORS`). Check: `GET /v1/tmc/health` of the collector lists the
counted labels and, per camera, the labels returned and the objects with `edges`.
