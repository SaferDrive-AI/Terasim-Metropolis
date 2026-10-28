# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Open Neural NDE runtime on the public MTR network (`packages/open-nnde`), for research: a
  drop-in for the Neural NDE runtime module (`csim`, `cnde`, `tools.dataset`) with conversion of
  the SUMO network to MTR's map polylines, vehicle histories, MTR prediction and pure PyTorch
  versions of MTR's two CUDA operators. `apps/deploy/city/Dockerfile.open-nnde` builds it on the
  release image from public sources; model weights are not included
  (docs/developer-guide/open-neural-nde.md).
- `deployment/brev/configure-vss.py`: TeraSim's edits of a VSS 3.3.0 checkout (cameras, detector
  labels, behavior analytics, Logstash, HAProxy, VSS agent tools and prompt) in one idempotent
  command.

### Changed

- The reference deployment on NVIDIA Brev (`deployment/brev/README.md`) runs from public inputs
  and the sample data (available on request) with VSS 3.3.0, in two modes: SUMO, and MTR (Neural
  NDE traffic on the open runtime).
- README and the documentation pages on installing, running and deploying shortened to
  prerequisites, steps, settings and expected results; the README leads with how to reproduce.

## [0.1.0] - 2026-10-27

First public release of TeraSim Metropolis: the Phase 1 open-source MVP.

### Added

- Simulation core (`terasim` 0.2.0): SUMO 1.23.1 kernel with libsumo, Python API
  (`Simulator`, environments, vehicle factories) and the configuration system.
- Behavior models and adversarial driving environment (`terasim-nde-nade` 0.2.0): NDE
  naturalistic behavior, NADE adversity injection with parameter files under
  `configs/adversities/`.
- Corridor pipeline (`terasim-city` 0.1.0):
  - HD map adapter: OpenDRIVE (`.xodr`) and Lanelet2 (`.osm`) maps are converted to
    geo-referenced SUMO networks on upload, alongside OpenStreetMap and prebuilt SUMO maps;
    optional `proj` override for maps whose coordinates are local; conversion summary
    (edges, junctions, traffic lights, connectivity, warnings).
  - Flow generation from turning-movement counts (TMC).
  - KPI evaluation: control delay and stops per vehicle, normalized against the baseline
    and combined under city-defined weights (default 0.7 / 0.3); simulated signal metrics
    per approach.
  - Signal timing optimization: candidates seeded from the city's current plan (uploaded
    NEMA plan or timing sheet) or from Webster's method, simulation-based Bayesian search,
    corridor coordination (common cycle, offsets), NEMA timing export
    (`terasim.nema_timing.v1`) and PDF timing report.
- HTTP API (`terasim-service` 0.2.0): `/v1/city/*` endpoints for snapshots, maps, plans,
  synchronous and asynchronous optimization, run history, baseline, simulated metrics,
  timing report and audit log; optional built-in scheduler.
- MCP server for the VSS agent (`terasim-agent-skill` 0.1.0): map and timing-plan upload,
  traffic snapshot upload, intersection and corridor optimization tools, intersection
  registry tools, download links for the timing report and the NEMA plan.
- TMC collector (`terasim-tmc-service` 0.1.0): intersection registry, periodic pull of VSS
  behavior objects, turning-movement counting, snapshot injection; configurable counted
  detector labels.
- Plugin Interface / SDK (`terasim-plugin-sdk` 0.1.0) with an example third-party plug-in
  (`examples/plugins/webster_fixed_time`).
- Deployment: container image definition (`apps/deploy/city/Dockerfile`, image
  `terasim-metropolis:0.1.0`, Python 3.11), Docker Compose stack with an offline VSS behavior
  stub, Helm chart `terasim-phase1` 0.6.0 for Kubernetes and OpenShift (restricted-v2, Routes,
  NetworkPolicies, Redis authentication, corridor files from values or an operator-managed
  ConfigMap through `corridorConfig`).
- Image publishing: for a version tag, `.github/workflows/images.yml` builds the image, stops on
  a CRITICAL vulnerability that has a fix, and pushes
  `ghcr.io/saferdrive-ai/terasim-metropolis:<version>` and `latest` to the GitHub Container Registry, then
  publishes the GitHub release with the notes in `docs/release-notes.md`.
- Corridor and camera configuration (`apps/deploy/city/camera-config/camcfg.py`): one camera
  inventory (corridor, intersections, cameras) generates the TMC spec, the corridor
  configuration, the Helm values or ConfigMap, the registration scripts, the VSS stream list and
  calibration entries; synthetic examples for the demo corridor and an eight-signal corridor
  with 32 cameras.
- Neural NDE inference interface (`vehicle_behavior="cnde"`), the per-request traffic model
  (`traffic_model`: `"sumo"` or `"cnde"` on both optimization endpoints and the four MCP
  optimize tools; a `"cnde"` run simulates every candidate under Neural NDE within the
  deployment limits `TERASIM_CITY_CNDE_MAX_WORKERS` / `TERASIM_CITY_CNDE_MAX_TRIALS` and is
  recorded and answered as such) and the optional GPU re-evaluation stage of the optimizer,
  configured by deployment settings; the Neural NDE image definition
  (`apps/deploy/city/Dockerfile.cnde`, on the Python 3.10 variant of the release image) and its
  Compose override (`compose.cnde.yml`). The runtime module and its checkpoint are installed
  separately. Neural NDE trials run about 1.4 times faster than the runtime's original code
  with identical results; the fast mode
  (`TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY=1`) predicts only the vehicles the model may control;
  per-step diagnostic logs are written on request (`TERASIM_CITY_CNDE_DIAG_LOGS=1`); vehicles
  the model hands back to SUMO resume SUMO's full safety checks (red lights, right of way,
  speed limits).
- Documentation site (MkDocs) with deployment guides, API and MCP reference, KPI definitions,
  map formats and runnable examples on a synthetic corridor.
- End-to-end acceptance check on synthetic data (`examples/acceptance/run_acceptance.py`,
  25 criteria, plus a Neural NDE profile with criteria A1-3 and A1-4 for GPU machines with the
  runtime installed).

### Package versions in this release

| Package | Version |
| --- | --- |
| terasim | 0.2.0 |
| terasim-nde-nade | 0.2.0 |
| terasim-city | 0.1.0 |
| terasim-service | 0.2.0 |
| terasim-agent-skill | 0.1.0 |
| terasim-tmc-service | 0.1.0 |
| terasim-plugin-sdk | 0.1.0 |
| Helm chart terasim-phase1 | 0.6.0 (appVersion 0.1.0) |

[0.1.0]: https://github.com/SaferDrive-AI/Terasim-Metropolis/releases/tag/v0.1.0
