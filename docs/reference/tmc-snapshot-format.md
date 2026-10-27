# TMC snapshot format

A TMC (turning movement count) snapshot holds, for one observation window, the number of
vehicles per intersection, approach and turn. The TMC collector produces one per window from the
VSS behavior objects; `POST /v1/city/snapshots` and the MCP tool `upload_traffic_snapshot`
accept them directly. The same format is the demand file of the Python API
(`run_tmc_pipeline`, `generate_flow`) and of `tmc_json` requests.

```json
{
  "corridor_id": "demo_arterial",
  "observation_interval": "10m",
  "timestamp_start": "2026-07-01T15:00:00Z",
  "intersections": [
    {
      "intersection_id": "i1",
      "name": "Arterial Ave & 1st Cross St",
      "lat": 37.335,
      "lon": -121.90339,
      "counts": {
        "East":  {"Left": 8, "Straight": 79, "Right": 13},
        "West":  {"Left": 7, "Straight": 57, "Right": 12},
        "North": {"Left": 6, "Straight": 17, "Right": 10},
        "South": {"Left": 6, "Straight": 16, "Right": 9}
      }
    }
  ]
}
```

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `corridor_id` | yes | The corridor the snapshot belongs to; optimizations of this corridor aggregate its snapshots |
| `timestamp_start` | yes | ISO-8601 start of the window; send it with a time zone, preferably UTC (`Z`). A time without zone is read as UTC. It may lie at most 300 s in the future |
| `observation_interval` | yes | Window length: a number and a unit, `h`, `m`/`min` or `s` (also `hour(s)`, `minute(s)`, `sec`/`seconds`): `10m`, `15min`, `600s`, `1h` |
| `intersections` | yes | One entry per intersection, ids unique within the snapshot |
| `intersections[].intersection_id` | yes | Intersection id |
| `intersections[].name` | MCP tool: yes | Display name |
| `intersections[].lat`, `lon` | yes | WGS84 location of the intersection; the optimizer binds it to the map signal within 60 m |
| `intersections[].counts` | yes | Approach → turn → vehicles (may be empty) |

Approaches are named by the **direction of travel**: `East` counts eastbound vehicles, `West`
westbound, `North` northbound, `South` southbound. Turns are `Left`, `Straight` and `Right`.
Counts are non-negative integers; an absent approach or turn counts zero.

## Buffering and aggregation

The service buffers snapshots per `corridor_id` for a rolling two hours; an identical
retransmission is stored once. An optimization aggregates the snapshots whose
`timestamp_start` lies within the last `TERASIM_CITY_WINDOW_S` seconds (one hour by default):

- the snapshots must describe the same intersections with the same coordinates, and their
  windows must not overlap;
- the counts are summed per intersection, approach and turn;
- the observation interval of the result is the covered time, so the demand rate is
  `count × 3600 / covered seconds` vehicles per hour, whatever the window length.

`POST /v1/city/snapshots` answers 202 with the number of buffered snapshots of the corridor
and 422 with the reason for an invalid snapshot. An optimization of a corridor without recent
snapshots answers 409.

## The collector's snapshots

The TMC collector fills the fields from its corridor specification (`corridor_id`,
`observation_interval`, the intersection locations) and the registration (`name`), and counts
only registered intersections. See [VSS integration](../deployment/vss-integration.md).
