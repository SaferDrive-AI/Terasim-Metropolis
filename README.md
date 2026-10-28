# TeraSim Metropolis

[![CI](https://github.com/SaferDrive-AI/Terasim-Metropolis/actions/workflows/ci.yml/badge.svg)](https://github.com/SaferDrive-AI/Terasim-Metropolis/actions/workflows/ci.yml)
[![Docs](https://github.com/SaferDrive-AI/Terasim-Metropolis/actions/workflows/docs.yml/badge.svg)](https://saferdrive-ai.github.io/Terasim-Metropolis/)
[![Images](https://github.com/SaferDrive-AI/Terasim-Metropolis/actions/workflows/images.yml/badge.svg)](https://github.com/SaferDrive-AI/Terasim-Metropolis/pkgs/container/terasim-metropolis)

TeraSim Metropolis optimizes the signal timing of city corridors in simulation. From a corridor
map, turning-movement counts and the city's current timing plan, it produces an optimized NEMA
timing plan with its KPIs (control delay, stops per vehicle) and a PDF timing report, when the
NVIDIA Metropolis VSS agent asks for it through MCP tools (On-Demand) or on a cadence (Scheduled).
It collects the counts from VSS every 10 minutes.

v0.2.0 · Apache-2.0 · [documentation](https://saferdrive-ai.github.io/Terasim-Metropolis/) ·
image `ghcr.io/saferdrive-ai/terasim-metropolis:0.2.0` · [release notes](docs/release-notes.md) ·
[changelog](CHANGELOG.md)

## Reproduce

| Result | Guide | Needs |
| --- | --- | --- |
| Both operating modes on a synthetic corridor, KPI and NEMA outputs checked | [Quick start](#quick-start) | Linux, Python 3.10 to 3.12 or Docker |
| The full pipeline on recorded video: VSS counts four cameras of a San José intersection, TeraSim optimizes its timing with SUMO or Neural NDE traffic | [deployment/brev](deployment/brev/README.md) | NVIDIA Brev instance with 2 × RTX PRO 6000, NGC API key; the sample videos and data: email haotian@inchor.ai |
| Neural NDE traffic on the public MTR network | [Open Neural NDE](docs/developer-guide/open-neural-nde.md) | NVIDIA GPU, an MTR checkpoint (research use) |

## Quick start

Docker, with an offline stand-in for the VSS behavior API
([Docker Compose guide](https://saferdrive-ai.github.io/Terasim-Metropolis/deployment/docker-compose/)):

```bash
git clone https://github.com/SaferDrive-AI/Terasim-Metropolis.git && cd Terasim-Metropolis
docker pull ghcr.io/saferdrive-ai/terasim-metropolis:0.2.0
docker tag ghcr.io/saferdrive-ai/terasim-metropolis:0.2.0 terasim-metropolis:0.2.0
docker compose -f apps/deploy/city/phase1/compose.yml --profile stub up -d
curl -s http://127.0.0.1:8100/health
```

From source (Linux x86_64, Python 3.10 to 3.12, a C compiler, `redis-server`; SUMO 1.23.1 is
installed from PyPI), then the end-to-end acceptance check on synthetic data:

```bash
python3.10 -m venv .venv && . .venv/bin/activate
pip install ./packages/terasim ./packages/terasim-nde-nade ./packages/terasim-city \
    ./packages/terasim-service "./packages/terasim-agent-skill[mcp,report]" \
    ./packages/terasim-tmc-service ./packages/terasim-plugin-sdk
python examples/acceptance/run_acceptance.py --out /tmp/terasim-acceptance
```

Deployment: Docker Compose (`apps/deploy/city/phase1/compose.yml`), Helm for Kubernetes and
OpenShift (`apps/deploy/city/helm/terasim-phase1`), and a generator that turns one camera
inventory into the TMC spec, the corridor configuration and the Helm values
(`apps/deploy/city/camera-config/camcfg.py`).

## Components

| Module | Package |
| --- | --- |
| A1 Behavior models: naturalistic (NDE), naturalistic and adversarial (NADE), Neural NDE traffic per request (`traffic_model="cnde"`); open runtime on the public MTR network in `packages/open-nnde` | `terasim-nde-nade`, `terasim-city` |
| A2 Adversarial environment: red-light running, cut-ins, rear-end, VRU crossings, stalled objects, construction zones | `terasim-nde-nade`, `configs/adversities/` |
| A3 Simulation core: SUMO kernel, Python API, configuration system | `terasim` |
| A4 HD map adapter: OpenDRIVE, Lanelet2, OpenStreetMap and SUMO maps to geo-referenced SUMO networks | `terasim-city` |
| B KPIs: control delay, stops per vehicle, weighted score, simulated signal metrics | `terasim-city` |
| C Signal timing optimization: candidates seeded from the current plan, simulation-based search, corridor coordination, NEMA output, PDF report | `terasim-city` |
| Interfaces: HTTP API, MCP server for the VSS agent, TMC collector of VSS behavior data | `terasim-service`, `terasim-agent-skill`, `terasim-tmc-service` |
| Plugin SDK for third-party traffic-operation modules | `terasim-plugin-sdk`, `examples/plugins/` |

## License

Apache License 2.0 (LICENSE); attribution in NOTICE, third-party licenses in
THIRD_PARTY_NOTICES.md. Built on TeraSim (https://github.com/mcity/TeraSim), originally developed
at Mcity, University of Michigan. Contributing: CONTRIBUTING.md; security issues: SECURITY.md.
