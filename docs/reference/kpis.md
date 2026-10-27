# KPIs

Every candidate plan is simulated on the same scenario (same demand, same random seed) for
`sim_end` seconds, 1,800 s by default. Its SUMO outputs give the measures of effectiveness
below; two of them make up the cost that ranks the candidates. After the run, the baseline and
the exported plan are simulated once more with virtual detectors for the signal-performance
measures.

Code: `terasim_city.evaluation.moe` (measures, cost), `terasim_city.evaluation.junction_delay`
(per-junction delay), `terasim_city.evaluation.sim_metrics` (signal-performance measures).

## Measures of effectiveness

Computed from SUMO's `tripinfo.xml`, written with unfinished and undeparted vehicles included,
so the means cover all demand: vehicles that completed their trip, vehicles still in the network
at the end, and vehicles that could not enter.

| Key | Definition |
| --- | --- |
| `control_delay_s_per_veh` | Mean over all vehicles of `timeLoss + departDelay` (seconds): time lost against free-flow travel plus time waiting to enter the network |
| `stops_per_veh` | Mean over all vehicles of SUMO's `waitingCount`; a vehicle that never entered counts at least one stop |
| `vehicles_total`, `vehicles_completed`, `vehicles_unfinished`, `vehicles_undeparted` | Vehicle counts by outcome |
| `departed_ratio`, `completion_ratio` | Share of vehicles that entered the network, and that completed their trip |
| `total_delay_s`, `total_stops` | Sums behind the two means |
| `mean_time_loss_s`, `mean_depart_delay_s` | The two parts of the control delay |
| `junction_delay_s` | Per signal: the mean over its incoming edges of `timeLoss / left` from SUMO edge data (seconds per vehicle that left the edge); `null` when no vehicle left any incoming edge |
| `corridor_junction_delay_s` | Mean of `junction_delay_s` over the signals that have a value |

The per-junction delay is the edge-based measure many agencies use. It is reported next to the
cost and never part of it: it counts only vehicles that left an approach, while the control
delay also counts vehicles still queued or held back.

## Weighted cost

The cost of a plan is a weighted sum of its measures relative to the baseline plan:

```text
cost(plan) = Σ_k  w_k · metric_k(plan) / max(metric_k(baseline), 1e-6)
```

| Weight key | Metric |
| --- | --- |
| `control_delay` | `control_delay_s_per_veh` |
| `stops` | `stops_per_veh` |

- Lower is better. The baseline's cost is the sum of the weights: 1.0 with the default weights
  `{"control_delay": 0.7, "stops": 0.3}`.
- Weights must be positive numbers; unknown keys are rejected. A city can set its own weights per
  request (`cost.weights` of `POST /v1/city/optimize` and of the MCP optimization tools,
  `kpi_weights` of `POST /v1/city/optimizations`).
- The winner is the evaluated plan with the lowest cost, the baseline included, so the
  exported plan never has a higher cost than the baseline.
- A candidate whose simulation fails gets an infinite cost and is never selected.

The answer of an optimization reports both sides:

```json
"kpis": {
  "cost_weights": {"control_delay": 0.7, "stops": 0.3},
  "baseline":  {"cost": 1.0,  "metrics": {"control_delay_s_per_veh": 23.81, "stops_per_veh": 0.86, "...": "..."},
                "timing": {"cycle_s": {"J1": 90.0}, "offsets_s": {"J1": 0.0}, "greens_s": {"J1": [48.0, 34.0]}}},
  "optimized": {"cost": 0.94, "metrics": {"...": "..."}, "timing": {"...": "..."}}
}
```

`timing` gives, per signal, the cycle, the offset and, for plans built from stages (timing sheet,
Webster or the search over them), the green times of the stages.

## Simulated signal-performance measures

After every completed run the service simulates the baseline and the exported plan again, with
virtual detectors on every signalized approach, and serves the result at
`GET /v1/city/optimizations/{run_id}/sim_metrics` (document schema
`corridor_ui.sim_metrics.v1`). The values are simulated estimates of each plan under the run's
demand.

| Measure | Key | Definition |
| --- | --- | --- |
| Volume | `volume_veh_h` | Vehicles crossing the stop line per hour |
| Demand | `demand_veh_h` | Vehicles entering the approach region per hour |
| Arrivals on green / red | `aog_pct`, `aor_pct` | Share of primary-movement arrivals at the stop line during green or red, by the signal state at the projected free-flow arrival time (yellow counts as red) |
| Green occupancy ratio | `gor_pct` | Share of green time with a vehicle in the stop-bar zone (last 12 m) |
| Red occupancy ratio | `ror5_pct` | The same over the first 5 s of red |
| Split failures | `split_failures`, `split_failures_per_hr` | Cycles with both occupancy ratios above 80 % |
| Queue length | `queue_veh`, `queue_censored` | Largest number of jammed vehicles in the approach region per cycle, averaged over cycles; censored when the queue reaches the region's upstream end |
| Approach delay and stops | `control_delay_s`, `stops_per_veh` | Time loss and stops inside the approach region of vehicles that crossed the stop line |
| Phase utilization | `phase_util_pct` | Green needed (until a 3 s gap after the 5 s minimum green) over green scheduled |
| Degree of saturation | `dos` | Primary-movement demand over capacity (saturation flow × lanes × green / cycle); the saturation flow is measured in split-failure cycles when there are at least two, otherwise 1,800 veh/h/lane |
| Green, cycle | `green_s`, `cycle_s` | Mean green of the primary movement and mean cycle |

The approach region is every controlled lane extended upstream along straight connections to
150 m, never past the upstream signal. The document holds:

- `plans.baseline` and `plans.optimized`, each with `corridor` (mainline arrivals on green and on
  red, the worst degree of saturation and its signal `dos_max_tls`, the end-to-end mainline
  travel time per direction `travel_time_s`) and `intersections` (per signal the measures above,
  volume-weighted, the worst queue, and `approaches` per direction `NB`, `SB`, `EB`, `WB`);
- `corridor_order` and `mainline`: the signals in corridor order and the mainline directions;
- `method`: the warm-up excluded from the measures (300 s, at most half the horizon), the
  horizon, the sample interval and the detector parameters.

`TERASIM_CITY_SIM_METRICS=0` turns the computation off; see the
[Configuration reference](../deployment/configuration.md#measures-and-audit-log).
