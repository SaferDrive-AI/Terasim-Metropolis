# Release notes

## v0.1.0

The first release of TeraSim Metropolis: simulation-based traffic signal timing optimization for
NVIDIA Metropolis VSS (Video Search and Summarization). The complete list of changes and the
package versions are in `CHANGELOG.md` at the root of the repository; the documentation is at
https://saferdrive-ai.github.io/Terasim-Metropolis/.

### Highlights

- **Signal timing optimization** for a corridor: candidate plans seeded from the city's current
  plan (NEMA plan or timing sheet) or Webster's method, simulation-based Bayesian search on
  SUMO 1.23.1, corridor coordination, NEMA timing output and a PDF timing report.
- **KPIs**: control delay and stops per vehicle under city-defined weights, and simulated signal
  performance measures per approach, signal and corridor.
- **VSS integration**: MCP tools for the VSS agent and a collector that turns VSS behavior data
  into turning-movement counts every 10 minutes; On-Demand and Scheduled operating modes.
- **HD map adapter** for OpenDRIVE, Lanelet2, OpenStreetMap and SUMO maps.
- **Neural NDE traffic** on a GPU, per request (`traffic_model="cnde"`).
- **Deployment and extension**: container image, Docker Compose stack, Helm chart for
  Kubernetes and OpenShift, Plugin SDK, and an end-to-end acceptance check on synthetic data.

### Validated with

- NVIDIA Metropolis VSS 3.2.1 and 3.3.0;
- NVIDIA Brev, 2 × NVIDIA RTX PRO 6000 Blackwell Server Edition: the reference deployment in
  `deployment/brev/README.md`.

### Container image

`ghcr.io/saferdrive-ai/terasim-metropolis:0.1.0`, also tagged `latest` (linux/amd64, Python 3.11):

```bash
docker pull ghcr.io/saferdrive-ai/terasim-metropolis:0.1.0
```

### Known limitations

- The Neural NDE runtime module and its model checkpoint are not part of this release and are
  provided separately; only Neural NDE traffic needs them.
- The VSS chat page ends a request after 300 s without data, and the agent sends none while a
  tool call runs: a Neural NDE request (about 12 minutes) goes through the VSS agent API
  (`POST /generate`).
