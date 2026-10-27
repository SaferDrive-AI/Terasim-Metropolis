# Release notes

## v0.1.0

The first release of TeraSim Metropolis: simulation-based traffic signal timing optimization for
NVIDIA Metropolis VSS (Video Search and Summarization). The complete list of changes is in
`CHANGELOG.md` at the root of the repository.

### Deliverables

| Item | Version |
| --- | --- |
| Python packages: `terasim`, `terasim-nde-nade`, `terasim-city`, `terasim-service`, `terasim-agent-skill`, `terasim-tmc-service`, `terasim-plugin-sdk` | see each package's `pyproject.toml` |
| Container image `terasim-metropolis` (`apps/deploy/city/Dockerfile`) | `0.1.0` |
| Neural NDE image definition `apps/deploy/city/Dockerfile.cnde` (derived from the release image; the runtime module and checkpoint are installed separately) | `0.1.0-cnde` |
| Helm chart `terasim-phase1` (Kubernetes and OpenShift `restricted-v2`) | `0.6.0`, app version `0.1.0` |
| Docker Compose stack `apps/deploy/city/phase1/compose.yml` | image `0.1.0` |

### Contents

- **Simulation core (A3)**: the TeraSim engine on SUMO 1.23.1 with `libsumo`, environments,
  agent factories and the YAML configuration system.
- **Behavior models (A1)**: naturalistic driving behavior (NDE/NADE) and the `sumo_passive`
  behavior of the optimization trials; the Neural NDE inference interface (`cnde`), the
  per-request traffic model (`traffic_model="cnde"` simulates every candidate of one run under
  Neural NDE on the GPU, also from the VSS chat through the MCP optimize tools) and the optional
  GPU re-evaluation stage of the optimizer (the runtime module and checkpoint are installed
  separately); a fast mode (`TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY=1`) for 24 candidates in
  about 12 minutes on one GPU; vehicles handed back from the model to SUMO resume SUMO's full
  safety checks.
- **Adversarial environment (A2)**: parameterized vehicle, vulnerable-road-user and static
  adversities with their parameter files in `configs/adversities/`.
- **HD map adapter (A4)**: OpenDRIVE, Lanelet2, OpenStreetMap and SUMO maps into one map store,
  with a projection override for maps without a usable geo-reference.
- **KPIs (B)**: control delay, stops per vehicle and their weighted cost with city-defined
  weights; per-junction delay; simulated signal-performance measures per approach, signal and
  corridor.
- **Signal optimization (C)**: seeds from the city's current plan (uploaded NEMA plan or timing
  sheet) or a Webster plan, simulation-based Bayesian search with corridor coordination (common
  cycle and offsets), NEMA timing output (`terasim.nema_timing.v1`) and a PDF timing report.
- **Services**: the REST service (`/v1/city/*`), the MCP server for the VSS agent with download
  links, and the TMC collector that counts turning movements from VSS behavior objects with the
  NVIDIA reference counting functions.
- **Operating modes**: On-Demand and Scheduled through the same optimization tool, plus an
  optional built-in scheduler.
- **Corridor and cameras**: one camera inventory generates the TMC spec, the corridor
  configuration and the Helm values (`corridorConfig`) of a multi-intersection, multi-camera
  corridor; examples for the demo corridor and an eight-signal corridor with 32 cameras.
- **Plugin SDK**: operation plugins discovered through the `terasim.operation_plugins` entry
  points, with the signal-timing optimization as the built-in plugin `signal_timing` and an
  example plugin.
- **Examples**: the synthetic three-signal demo corridor, On-Demand, Scheduled and incident
  demos, and the end-to-end acceptance script (25 criteria, plus a Neural NDE profile with
  A1-3 and A1-4).
