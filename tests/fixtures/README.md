# Test fixtures

Synthetic data shared by the test suites. Every id, name, count, timing value
and coordinate here is invented; map-bound data sits on the demo corridor in
`examples/corridor_demo` (signals `J1`/`J2`/`J3`, TMC intersections
`i1`/`i2`/`i3` at (37.335, -121.90339) / (37.335, -121.9) / (37.335, -121.89661)).

| File | Contents |
|---|---|
| `demo_corridor.py` | Demo-corridor paths and ids; builders for a sumocfg whose signal programs cover only some signals (`write_partial_program_sumocfg`) and for the demo network rebuilt with sidewalks and pedestrian crossings (`build_crossing_net`, needs `netconvert`) |
| `tmc_templates.py` | TMC payloads and timing sheets: the demo corridor's own counts, snapshots and baseline sheets as Python literals, a two-signal hourly payload and a production-shaped ten-minute payload; `write_tmc` / `write_snapshots` write them to files |
| `nema_plan_files.py` + `nema_plans/` | `terasim.nema_plan.v1` documents: an 8-phase dual-ring intersection with protected lefts and full per-phase timing (`main_1st`), a 4-phase intersection with permissive lefts (`four_phase`), and the coordinated demo-corridor plan with AM / midday / PM patterns, Free evenings and weekday / weekend schedules (`demo_arterial`) |
| `signal_plans.py` | The demo corridor's `alt_fixed` program as a `SignalPlan` per signal (`demo_alt_fixed_plan`) |
| `tmc_service/` | Fixtures of the TMC collector tests |

Accessors return fresh copies, so tests may mutate what they get.
