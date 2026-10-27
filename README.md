# TeraSim Metropolis

[![CI](https://github.com/SaferDrive-AI/Terasim-Metropolis/actions/workflows/ci.yml/badge.svg)](https://github.com/SaferDrive-AI/Terasim-Metropolis/actions/workflows/ci.yml)
[![Docs](https://github.com/SaferDrive-AI/Terasim-Metropolis/actions/workflows/docs.yml/badge.svg)](https://saferdrive-ai.github.io/Terasim-Metropolis/)
[![Images](https://github.com/SaferDrive-AI/Terasim-Metropolis/actions/workflows/images.yml/badge.svg)](https://github.com/SaferDrive-AI/Terasim-Metropolis/pkgs/container/terasim-metropolis)

TeraSim Metropolis is an open-source traffic simulation and traffic-operations platform for
city corridors. It turns a corridor map, live turning-movement counts and the city's current
signal timing into an optimized, NEMA-format signal timing plan, scored on standard
traffic-engineering measures of effectiveness. It integrates with NVIDIA Metropolis VSS
(Video Search and Summarization): the VSS agent calls TeraSim through MCP tools, and TeraSim
pulls traffic-state data from VSS on its own schedule.

Release: **v0.1.0** (Phase 1, open-source MVP). License: Apache-2.0.

- Documentation: https://saferdrive-ai.github.io/Terasim-Metropolis/
- Container image: `ghcr.io/saferdrive-ai/terasim-metropolis:0.1.0`
- Release notes: [docs/release-notes.md](docs/release-notes.md); all changes: [CHANGELOG.md](CHANGELOG.md)

## What is inside

| Module | What it does | Package(s) |
| --- | --- | --- |
| A1 Behavior models | Naturalistic driving behavior (NDE) and the naturalistic-and-adversarial environment (NADE); the Neural NDE inference interface, Neural NDE traffic per request (`traffic_model="cnde"`) and an optional GPU re-evaluation stage (runtime module and checkpoint installed separately; image definition `apps/deploy/city/Dockerfile.cnde`) | `terasim-nde-nade`, `terasim-city` (`vehicle_behavior`) |
| A2 Adversarial driving environment | Parameterized injection of safety-critical behaviors (red-light running, cut-ins, rear-end, VRU crossings, stalled objects, construction zones) | `terasim-nde-nade`, `configs/adversities/` |
| A3 Simulation core | SUMO-based simulation kernel with a Python API and a configuration system | `terasim` |
| A4 HD map adapter | Ingests OpenDRIVE, Lanelet2, OpenStreetMap and SUMO maps, converts them to geo-referenced SUMO networks | `terasim-city` (`terasim_city.maps`) |
| B KPI evaluation | Control delay and stops per vehicle, normalized and combined under city-defined weights; simulated signal metrics | `terasim-city` (`terasim_city.evaluation`) |
| C Signal timing optimization plug-in | Candidate plans seeded from the city's current plan, simulation-based search (1,800 s per candidate by default), corridor coordination, NEMA output and PDF timing report | `terasim-city` |
| Interfaces | HTTP API, MCP server for the VSS agent, TMC collector that pulls VSS behavior data | `terasim-service`, `terasim-agent-skill`, `terasim-tmc-service` |
| Plugin SDK | Interface for third-party traffic-operation modules | `terasim-plugin-sdk`, `examples/plugins/` |

Two operating modes share the same components: **On-Demand** (an operator asks the VSS agent,
the agent calls the optimization tool) and **Scheduled** (the caller invokes the same tool on
its own cadence; an optional built-in scheduler is available for deployments without one).
Each answer is a NEMA timing plan plus the measured KPIs, packaged for offline application
through the city's ATCMS.

## Quick start

```bash
git clone https://github.com/SaferDrive-AI/Terasim-Metropolis.git && cd Terasim-Metropolis
```

With Docker, run the published image with the offline behavior stub
([Docker Compose guide](https://saferdrive-ai.github.io/Terasim-Metropolis/deployment/docker-compose/)):

```bash
docker pull ghcr.io/saferdrive-ai/terasim-metropolis:0.1.0
docker tag ghcr.io/saferdrive-ai/terasim-metropolis:0.1.0 terasim-metropolis:0.1.0
docker compose -f apps/deploy/city/phase1/compose.yml --profile stub up -d
curl -s http://127.0.0.1:8100/health
```

From source. Requirements: Linux x86_64, Python 3.10-3.12, a C compiler (for the NADE
extensions) and `redis-server` for the service. SUMO 1.23.1 is installed from PyPI with the
packages.

```bash
python3.10 -m venv .venv && . .venv/bin/activate
pip install ./packages/terasim ./packages/terasim-nde-nade ./packages/terasim-city \
    ./packages/terasim-service "./packages/terasim-agent-skill[mcp,report]" \
    ./packages/terasim-tmc-service ./packages/terasim-plugin-sdk
```

Run the end-to-end acceptance check on synthetic data (starts every service locally,
exercises both operating modes and checks the KPI and NEMA outputs):

```bash
python examples/acceptance/run_acceptance.py --out /tmp/terasim-acceptance
```

Deploy:

- Docker Compose: `apps/deploy/city/phase1/compose.yml`
- Kubernetes / OpenShift (Helm): `apps/deploy/city/helm/terasim-phase1`
- A corridor's intersections and cameras: one camera inventory generates the TMC spec, the
  corridor configuration and the Helm values (`apps/deploy/city/camera-config/camcfg.py`)

## Documentation

The documentation site, https://saferdrive-ai.github.io/Terasim-Metropolis/, is built from `docs/` with MkDocs
(`mkdocs.yml`) on every change to `main`: overview and architecture, developer guide,
deployment guides, HTTP API and MCP tool reference, KPI definitions, supported map formats,
the Plugin SDK and runnable examples. To preview it locally:

```bash
pip install -r docs/requirements.txt && mkdocs serve
```

## Repository layout

```
packages/
  terasim/               simulation core (A3)
  terasim-nde-nade/      behavior models and adversarial environment (A1, A2)
  terasim-city/          corridor pipeline: maps (A4), KPIs (B), optimization and NEMA output (C)
  terasim-service/       HTTP API
  terasim-agent-skill/   MCP server and client for the VSS agent
  terasim-tmc-service/   TMC collector (VSS behavior data -> traffic snapshots)
  terasim-plugin-sdk/    Plugin Interface / SDK for traffic-operation modules
apps/deploy/city/        container image, Docker Compose, Helm chart, camera-config generator
configs/                 simulation and adversity parameter files
examples/                synthetic demo corridor, acceptance check, example plug-in
docs/                    documentation site sources
tests/                   test suites
```

## Versioning and releases

Releases follow semantic versioning and are tagged `vMAJOR.MINOR.PATCH`; see CHANGELOG.md.
For each tag, `.github/workflows/images.yml` publishes the container image
`ghcr.io/saferdrive-ai/terasim-metropolis:<version>` (also `latest`) and the GitHub release, with the notes in
`docs/release-notes.md`.

## License

Apache License 2.0 (LICENSE). See NOTICE for attribution and THIRD_PARTY_NOTICES.md for the
licenses of third-party components. The simulation engine builds on TeraSim
(https://github.com/mcity/TeraSim), originally developed at Mcity, University of Michigan.

Contributions are welcome; see CONTRIBUTING.md. Report security issues as described in
SECURITY.md.
