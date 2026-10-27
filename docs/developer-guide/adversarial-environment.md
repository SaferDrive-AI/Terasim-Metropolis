# Adversarial environment

`terasim-nde-nade` provides the behavior models (module A1) and the adversarial driving
environment (module A2) of the simulation core:

- **NDE**, the naturalistic driving environment: background vehicles and vulnerable road users
  that drive like real traffic.
- **NADE**, the naturalistic and adversarial driving environment: NDE plus adversities,
  safety-critical maneuvers (a cut-in, running a red light, jaywalking, ...) that are injected
  with a controlled probability. Importance sampling keeps the results statistically
  meaningful: every episode carries the likelihood ratio of the choices made, so event rates
  measured under the boosted adversities can be re-weighted to naturalistic rates.
- **NADEWithAV**, NADE around an ego vehicle under test.

The methods follow the published NDE and NADE work ("Learning naturalistic driving environment
with statistical realism", *Nature Communications* 14, 2037 (2023); "Intelligent driving
intelligence test for autonomous vehicles with naturalistic and adversarial environment",
*Nature Communications* 12, 748 (2021)).

## Adversity parameter files

Each adversity is a small YAML file under `configs/adversities/`. The file names the class to
instantiate (`_target_`, created with Hydra) and its parameters; the environment receives the
merged entries as `adversity_cfg` (see [Configuration system](configuration.md)).

```yaml
# configs/adversities/vehicle/intersection_tfl.yaml
adversity_cfg:
  vehicle:
    intersection_tfl:
      _target_: terasim_nde_nade.adversity.vehicles.trafficrule_adversity.TrafficRuleAdversity
      _convert_: 'all'
      location: 'intersection'
      ego_type: 'vehicle'
      probability: 0.010141902545438387
      predicted_collision_type: "intersection_tfl"
```

### Maneuver adversities

Vehicle and vulnerable-road-user adversities share four parameters:

| Parameter | Meaning |
| --- | --- |
| `location` | Where the adversity can trigger: `intersection`, `highway`, `roundabout` (vehicles) or `crosswalk` (vulnerable road users). The location of an agent is derived from the map. |
| `ego_type` | The kind of agent that performs it: `vehicle` or `vulnerable_road_user`. |
| `probability` | The naturalistic probability of the maneuver when its trigger condition holds, per decision step. `0` disables the adversity. |
| `predicted_collision_type` | The label recorded with the event; it also selects the importance-sampling magnitude (see below). |

The library as shipped:

| File | Class | Location | Probability | Collision type |
| --- | --- | --- | --- | --- |
| `vehicle/intersection_tfl.yaml` | `TrafficRuleAdversity` | intersection | 1.01e-2 | `intersection_tfl` |
| `vehicle/intersection_rearend.yaml` | `LeaderAdversity` | intersection | 1.74e-4 | `intersection_rearend` |
| `vehicle/intersection_cutin.yaml` | `LanechangeAdversity` | intersection | 1e-7 | `intersection_cutin` |
| `vehicle/intersection_headon.yaml` | `HeadonAdversity` | intersection | 1.07e-8 | `intersection_headon` |
| `vehicle/highway_cutin.yaml` | `LanechangeAdversity` | highway | 3.20e-5 | `highway_cutin` |
| `vehicle/highway_cutin_abort.yaml` | `LanechangeAbortAdversity` | highway | 1e-5 | `highway_cutin` |
| `vehicle/highway_merge.yaml` | `MergeAdversity` | highway | 1e-5 | `highway_cutin` |
| `vehicle/highway_rearend.yaml` | `LeaderAdversity` | highway | 1e-4 | `highway_rearend` |
| `vehicle/highway_rearend_accel.yaml` | `LeaderAdversity` | highway | 1e-4 | `highway_rearend` |
| `vehicle/highway_rearend_decel.yaml` | `FollowerAdversity` | highway | 1e-5 | `highway_rearend` |
| `vehicle/roundabout_cutin.yaml` | `LanechangeAdversity` | roundabout | 2.31e-5 | `roundabout_cutin` |
| `vehicle/roundabout_fail_to_yield.yaml` | `TrafficRuleAdversity` | roundabout | 5.03e-4 | `roundabout_fail_to_yield` |
| `vehicle/roundabout_rearend.yaml` | `LeaderAdversity` | roundabout | 1.84e-10 | `roundabout_rearend` |
| `vru/jaywalking.yaml` | `JaywalkingAdversity` | crosswalk | 0.1 | `intersection_jaywalking` |
| `vru/crossingstop.yaml` | `StopCrossingAdversity` | crosswalk | 0.1 | `intersection_stopcrossing` |
| `vru/red_light_running.yaml` | `RunningRedLightAdversity` | crosswalk | 0.1 | `intersection_runningredlight` |

### Static adversities

Static adversities place objects in the network: a stalled vehicle, a collision scene, a
construction zone (`configs/adversities/static/`). Their parameters:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `lane_id` | `""` | Lane where the object is placed (`placement_mode: lane_position`) |
| `lane_position` | `-1` | Position along the lane, in metres |
| `start_time`, `end_time` | `0`, `-1` | Active interval in simulation seconds; `-1` keeps it to the end |
| `object_type` | `""` | SUMO vehicle type of the object (a list for a collision scene) |
| `other_settings` | `None` | Adversity-specific settings, for example `duration` or `collision_type: side_swipe` |
| `placement_mode` | `lane_position` | Or `xy_angle` (`x`, `y`, `angle` in network coordinates) or `latlon_degree` (`lat`, `lon`, `degree`) |

The signal-optimization pipeline uses the stalled-object adversity to simulate incidents: an
optimization request with an inline `incident` places the reported stalled vehicles in every
trial.

## Tuning the adversarial environment

| Setting | Where | Effect |
| --- | --- | --- |
| `probability` of an adversity | its YAML file | Naturalistic probability of the maneuver; `0` turns it off |
| `adversity_sampling_probability` | `environment.parameters` | When greater than 0, the probability with which a triggered adversity is actually sampled. `0` uses importance-sampling magnitudes instead |
| `IS_MAGNITUDE_INTERSECTION`, `IS_MAGNITUDE_HIGHWAY`, `IS_MAGNITUDE_ROUNDABOUT` | environment variables | Magnitude by which the naturalistic probability is boosted, chosen by the `predicted_collision_type` (default 20, times 40 for maneuvers the other road user cannot avoid; the sampled probability is capped at 0.05) |
| `AVOID_COLLISION_IS_PROB` | environment variable | Sampling probability of the other road user's avoidance maneuver after an adversarial maneuver (default 0.2) |
| `warmup_time_lb`, `warmup_time_ub`, `run_time` | `environment.parameters` | Warm-up drawn from `[lb, ub)` seconds, then `run_time` seconds of evaluation |
| `MOBIL_lc_flag`, `stochastic_acc_flag`, `drive_rule` | `environment.parameters` | Lane-change model, stochastic acceleration, `righthand` or `lefthand` traffic |

Every adversarial or normal choice multiplies the episode's importance-sampling weight by the
ratio of its naturalistic probability to its sampled probability. A run stops at the first
collision.

## Example: adversities on the demo corridor

The intersection adversities on the synthetic demo scenario, with a fixed sampling probability
so that events occur in a short run:

```python
from pathlib import Path

import yaml

from terasim_service.utils.base import create_environment, create_simulator

ADVERSITIES = Path("configs/adversities")
vehicle_adversities = {}
for path in sorted((ADVERSITIES / "vehicle").glob("intersection_*.yaml")):
    vehicle_adversities.update(yaml.safe_load(path.read_text())["adversity_cfg"]["vehicle"])

config = {
    "input": {
        "sumo_net_file": "examples/corridor_demo/demo.net.xml",
        "sumo_config_file": "examples/corridor_demo/demo_scenario.sumocfg",
    },
    "simulator": {
        "module": "terasim.simulator",
        "class": "Simulator",
        "parameters": {"num_tries": 10, "gui_flag": False, "realtime_flag": False,
                       "sumo_seed": 42, "sumo_output_file_types": ["collision", "tripinfo"]},
    },
    "environment": {
        "module": "terasim_nde_nade.envs",
        "class": "NADE",
        "parameters": {
            "log_flag": True, "warmup_time_lb": 60, "warmup_time_ub": 61, "run_time": 300,
            "MOBIL_lc_flag": True, "stochastic_acc_flag": False, "drive_rule": "righthand",
            "adversity_sampling_probability": 0.05,
            "adversity_cfg": {"vehicle": vehicle_adversities},
        },
    },
}

out = Path("/tmp/terasim/nade")
env = create_environment(config, out)
sim = create_simulator(config, out)
sim.bind_env(env)
sim.run()
print("importance-sampling weight:", env.importance_sampling_weight)
```

Run it from the repository root. The output directory receives the SUMO outputs
(`collision.xml`, `tripinfo.xml`, `run.log`), `monitor.json` (the episode record: finish
reason, the vehicles involved, warm-up and run time, the importance-sampling weight) and
`conflict_info.jsonl` (the conflicts evaluated at every step). Setting
`adversity_sampling_probability` to 0 and every `probability` to 0 gives a purely naturalistic
run.

## Behavior models in the signal-optimization pipeline

The signal-optimization trials need reproducible, rule-abiding background traffic, so they use
the `sumo_passive` behavior by default: SUMO's own car-following and lane-change models with
SUMO's safety checks intact (`terasim_city.factories.PassiveVehicleFactory`). The behavior is
selected by name through the registry in `terasim_city.factories`:

```python
from terasim_city.factories import VEHICLE_BEHAVIORS

print(sorted(VEHICLE_BEHAVIORS))           # ['cnde', 'sumo_passive']
```

The `cnde` entry is the inference interface of the Neural NDE model, whose runtime module and
checkpoint are installed separately (see [Behavior models](behavior-models.md)). A request asks for it with `traffic_model="cnde"`;
a deployment selects it with `TERASIM_CITY_VEHICLE_BEHAVIOR=cnde`, or uses it to re-evaluate
only the best candidates (`TERASIM_CITY_REFINE_TOP_K`, `TERASIM_CITY_REFINE_BEHAVIOR=cnde`); see the
[Configuration reference](../deployment/configuration.md#neural-nde-runtime-optional).
