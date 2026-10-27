# Timing sheet format

A timing sheet (`terasim.timing_sheet.v1`) describes a corridor's current fixed-time plan the way
a timing engineer writes it: per intersection, the stages in order with their NEMA phase numbers,
the movements they serve and their green, yellow and all-red times, under one corridor cycle.
It is the simpler alternative to a full [`terasim.nema_plan.v1`](nema-plan-format.md) document
and serves as the baseline of an optimization through `baseline_sheet` (a corridor
configuration entry, the asynchronous request, or `CorridorOptimizationConfig`).

```json
{
  "schema": "terasim.timing_sheet.v1",
  "corridor_id": "demo_arterial",
  "cycle_s": 90,
  "intersections": [
    {
      "intersection_id": "i1",
      "tls_id": "J1",
      "offset_s": 0,
      "phases": [
        {"nema_phases": [2, 6],
         "movements": ["EB-left", "EB-through", "EB-right", "WB-left", "WB-through", "WB-right"],
         "green_s": 48, "yellow_s": 3, "all_red_s": 1},
        {"nema_phases": [4, 8],
         "movements": ["NB-left", "NB-through", "NB-right", "SB-left", "SB-through", "SB-right"],
         "green_s": 34, "yellow_s": 3, "all_red_s": 1}
      ]
    }
  ]
}
```

`examples/corridor_demo/baseline_sheet.json` is a complete sheet for the demo corridor (offsets
0, 20 and 40 s); `baseline_sheet_uncoordinated.json` is the same plan with all offsets at 0.

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `schema` | yes | `"terasim.timing_sheet.v1"` |
| `corridor_id` | no | Label of the corridor |
| `cycle_s` | yes | Corridor cycle in seconds (> 0) |
| `intersections[].intersection_id` | yes | TMC intersection id |
| `intersections[].tls_id` | no | Signal id in the map; when absent, the intersection is matched to the corridor's signal through its TMC intersection id |
| `intersections[].offset_s` | no | Offset in seconds (default 0) |
| `intersections[].phases[]` | yes | The stages in running order |
| `phases[].movements` | yes | Movements served by the stage: `EB-left`, `EB-through`, `EB-right`, and the same for `WB`, `NB`, `SB` |
| `phases[].nema_phases` | no | NEMA phase numbers of the stage; the NEMA export uses them |
| `phases[].green_s` | yes | Green time (s, >= 0) |
| `phases[].yellow_s`, `all_red_s` | no | Clearance times (s, default 0) |

## Rules

- The stage durations of every intersection (green + yellow + all-red, summed over its stages)
  must add up to `cycle_s` within 0.5 s; a sheet that does not close its cycle is rejected.
- A left turn served in the same stage as the opposing through movement yields to it.
- A sheet may describe more signals than a request covers: an optimization of one intersection
  of the corridor, or of a part of it, uses the entries of its signals. A signal of the request
  that the sheet does not describe is an error (HTTP 422 on the synchronous call).
- The movements are mapped to the signal's lanes from the map geometry.
