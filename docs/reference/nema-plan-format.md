# NEMA plan formats

Two JSON documents carry signal timing:

| Schema | Direction | Used by |
| --- | --- | --- |
| `terasim.nema_plan.v1` | in: the city's current timing | `POST /v1/city/plans`, MCP `upload_signal_plan`, `PlanStore` |
| `terasim.nema_timing.v1` | out: the optimized (or baseline) timing | the `nema_plan` of an optimization answer, `GET /v1/city/optimizations/{run_id}/nema` and `/baseline`, `nema_plan.json` downloads |

## Input: `terasim.nema_plan.v1`

A complete controller description per intersection: NEMA phases and the movements they serve,
the dual-ring structure, the coordination patterns and the time-of-day schedule. Example for one
intersection of the synthetic demo corridor (a 90 s, four-phase coordinated pattern):

```json
{
  "schema": "terasim.nema_plan.v1",
  "corridor_id": "demo_arterial",
  "intersections": [
    {
      "intersection_id": "i2",
      "name": "Arterial Ave & 2nd Cross St",
      "lat": 37.335,
      "lon": -121.9,
      "phases": [
        {"phase": 2, "movements": ["WB-through", "WB-right", "WB-left"], "min_green_s": 10, "yellow_s": 3.0, "all_red_s": 1.0},
        {"phase": 4, "movements": ["NB-through", "NB-right", "NB-left"], "min_green_s": 8, "yellow_s": 3.0, "all_red_s": 1.0},
        {"phase": 6, "movements": ["EB-through", "EB-right", "EB-left"], "min_green_s": 10, "yellow_s": 3.0, "all_red_s": 1.0},
        {"phase": 8, "movements": ["SB-through", "SB-right", "SB-left"], "min_green_s": 8, "yellow_s": 3.0, "all_red_s": 1.0}
      ],
      "rings": {"ring1": [2, 4], "ring2": [6, 8], "coordinated_phases": [2, 6]},
      "patterns": [
        {"pattern_id": "p90", "description": "Coordinated 90 s plan", "cycle_s": 90, "offset_s": 20,
         "splits": {"2": 52, "4": 38, "6": 52, "8": 38}}
      ],
      "schedule": [
        {"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "start": "00:00", "pattern_id": "p90"}
      ]
    }
  ],
  "provenance": {"source_files": [], "handoff_ready": true, "gaps": []}
}
```

### Document

| Field | Required | Meaning |
| --- | --- | --- |
| `schema` | yes | `"terasim.nema_plan.v1"` |
| `corridor_id` | no | Label of the corridor. If absent, the `corridor_id` of the upload request applies, else `"unassigned"`. Plans are found by location, so the label does not have to match the TMC corridor |
| `intersections` | yes | One or more intersections |
| `provenance` | yes (MCP tool) | Audit information kept with the plan: `source_files` (list), `handoff_ready` (bool), `gaps` (list of strings); optional `generated_by`, `generated_at` |

### Intersection

| Field | Required | Meaning |
| --- | --- | --- |
| `intersection_id`, `name` | yes | Identity and display name |
| `lat`, `lon` | yes | WGS84 location; the optimizer binds the plan to the map signal within 60 m |
| `phases` | yes | NEMA phases: `phase` (1 to 8), `movements` (see below), optional `min_green_s`, `yellow_s`, `all_red_s` |
| `rings` | yes | `ring1` and `ring2`: phase order per ring (0 pads an empty slot); `coordinated_phases`: the coordinated phases, the reference of the offset; optional `barrier_phases`, `barrier2_phases` |
| `patterns` | yes | Coordination patterns: `pattern_id`, `cycle_s` (> 0), `splits` (phase number as a string → split in seconds), optional `offset_s`, `description`, `coordinated` |
| `schedule` | yes | Time-of-day rules: `days` (`Mon` to `Sun`), `start` (`HH:MM`), `pattern_id` (a pattern of this intersection, or `free`) |
| `change_intervals` | no | Intersection-level `yellow_s` / `all_red_s` fallback |
| `controller` | no | Free-form controller information |
| `free` | no | The intersection runs free (actuated) |

Movements are named by approach and turn: `EB-left`, `EB-through`, `EB-right`, and the same for
`WB`, `NB` and `SB`. Timing fields may carry a source tag (`min_green_src`, `yellow_src`,
`all_red_src`, `cycle_src`, `offset_src`, `src`): `read`, `inferred`, `inherited` or `default`.
The MCP tool validates the document against this model before uploading it; unknown fields are
rejected.

### Validation at upload

A plan that could not be simulated is rejected with HTTP 400 and the reason:

- every phase listed in a ring is defined in `phases` and has a split in every pattern;
- every split covers its phase's minimum green plus yellow plus all-red (defaults 5 s, 3 s
  and 2 s when absent);
- every schedule rule names a defined pattern or `free`;
- `lat`, `lon`, the rings and the coordinated phases are present.

### Storage and lookup

Each intersection is stored on its own, keyed by `(corridor_id, intersection_id)`. A new upload
for the same key, or for a stored intersection within 30 m, replaces the stored plan; the
replaced version is archived. During an optimization, the plan of each corridor signal is found
by the location of the map junction (within 60 m). When the stored plans cover every signal of
the corridor, they are the baseline: for each intersection the pattern scheduled at the time of
the traffic data (`plan_time`, by default the snapshot time, in the corridor's local time) is
selected. A `free` period gets a pattern timed from the measured demand on the plan's own
dual-ring structure.

In the simulation, a pattern runs as the exact fixed-time expansion of its dual-ring diagram:
green = split − yellow − all-red, a left turn sharing a stage with the opposing through
movement yields, and every movement that exists at the junction must be served by some phase.

## Output: `terasim.nema_timing.v1`

The timing of the exported plan, per signal, ready for a traffic engineer:

```json
{
  "schema": "terasim.nema_timing.v1",
  "generated_at": "2026-10-21T00:48:41.167661+00:00",
  "corridor_id": "demo_arterial",
  "handoff_ready": true,
  "coordination": {"cycle_length_s": 90.0, "reference_intersection": "J1"},
  "intersections": [
    {
      "tls_id": "J2",
      "pattern_id": "p90",
      "description": "Coordinated 90 s plan",
      "valid_windows": [{"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "start": "00:00", "end": "00:00"}],
      "offset_s": 65.0,
      "cycle_s": 90.0,
      "phases": [
        {"nema_phase": 2, "movements": ["WB-through", "WB-right", "WB-left"],
         "split_s": 52.0, "green_s": 48.0, "yellow_s": 3.0, "all_red_s": 1.0, "notes": ""},
        {"nema_phase": 4, "movements": ["NB-through", "NB-right", "NB-left"],
         "split_s": 38.0, "green_s": 34.0, "yellow_s": 3.0, "all_red_s": 1.0, "notes": ""}
      ]
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `corridor_id` | The corridor of the run |
| `handoff_ready` | `true` when every row carries a NEMA phase number |
| `coordination.cycle_length_s` | The corridor cycle |
| `coordination.reference_intersection` | The signal the offsets refer to (the first of the corridor) |
| `intersections[].tls_id` | The signal id in the map; the run's result (`resolved_corridor`) maps TMC intersection ids to signal ids |
| `intersections[].offset_s` | Offset of the signal, in seconds |
| `intersections[].description`, `valid_windows` | Which pattern this is and when it is scheduled to run |
| `intersections[].pattern_id`, `cycle_s` | Present when the plan comes from a stored NEMA pattern |
| `intersections[].phases[]` | One row per NEMA phase: `nema_phase`, `movements`, `split_s`, `green_s`, `yellow_s`, `all_red_s`, `notes` |

The rows depend on where the exported plan comes from:

- **A stored NEMA pattern** (the city's uploaded plan, optimized): one row per phase of the
  pattern, with the stored phase numbers; `valid_windows` are the times the schedule runs this
  pattern; `handoff_ready` is `true`.
- **A stage-based plan** (timing sheet, Webster plan, or the search over them): the program is
  read as a loop of stages; each green stage is one row per NEMA phase number it carries, and
  phases sharing one green interval are noted as a concurrent stage. The yellow and all-red time
  after a green belongs to its row, so the splits of a signal add up to its cycle. A stage
  without phase numbers gives a row with `"nema_phase": null`, and `handoff_ready` is then
  `false`. `valid_windows` is the whole week.

When the exported plan has no common cycle (for example a scenario's own programs with a
different cycle at every signal), no NEMA timing is written: the run's result says why in
`nema_export`, and `GET /nema` answers 409 with that reason.
