# Installation

Same code in two forms: a container image for deployments, Python packages for development.

## Requirements

| Component | Version | Notes |
| --- | --- | --- |
| Operating system | Linux x86-64 | The container image is `linux/amd64`. |
| Python | 3.10 to 3.12 | The image uses Python 3.11; the Neural NDE image, Python 3.10. |
| SUMO | 1.23.1 | Installed with the Python packages as the `eclipse-sumo`, `libsumo`, `traci` and `sumolib` wheels; no system package is needed. |
| Redis | server | Snapshot buffer and job store of `terasim-service`. Included in the image; for a local setup install `redis-server` (for example `sudo apt-get install redis-server`). |
| C/C++ compiler | gcc / g++ | Builds the Cython extensions of `terasim-nde-nade` (source installs only). |
| Docker | Engine with the Compose plugin | For the image and the Compose stack. |
| Kubernetes / OpenShift | Helm 3; OpenShift 4.19 or later | For the Helm chart. |

## Option 1: container image

The release image is `terasim-metropolis:0.2.0`. Either:

- pull it from the GitHub Container Registry as `ghcr.io/saferdrive-ai/terasim-metropolis:0.2.0`
  (see [Get the image](../deployment/docker-compose.md#get-the-image));
- load it from an image archive: `docker load -i <archive>`;
- or build it from a checkout, from the repository root (the build context must be the root so
  `packages/` and `examples/` resolve):

```bash
docker build -f apps/deploy/city/Dockerfile -t terasim-metropolis:0.2.0 .
```

On Apple Silicon, add `--platform linux/amd64` to build the x86-64 image. The build installs the
packages, compiles the Cython extensions and verifies the runtime as the non-root user (imports,
the `libsumo` backend, the `sumo` binary and its libraries, Redis, the writable data directories,
the read-only baked inputs); a broken runtime fails the build.

The image contains the release packages and their dependencies, SUMO 1.23.1 and `redis-server`,
plus:

| Path | Content |
| --- | --- |
| `/data/input/map` | Synthetic demo corridor map: `demo.sumocfg`, `demo.net.xml`, `demo.tls.xml`, `demo.rou.xml` |
| `/data/input/demo_scenario` | Demo scenario |
| `/data/output` | Writable: service runs and stores |
| `/data/artifacts` | Writable: MCP downloads |
| `/data/tmc` | Writable: TMC registry |

The image runs as uid 1001 in group 0 and works under any uid in group 0 (OpenShift
`restricted-v2`). It exposes ports 8000, 9911 and 8200 and starts `terasim-service` by default;
pass `terasim-agent-skill` or `terasim-tmc-service` as the command to start the other two. See
[Docker Compose](../deployment/docker-compose.md) and
[Helm on OpenShift](../deployment/helm-openshift.md).

## Option 2: Python packages from source

Clone the repository, create a virtual environment and install the packages in editable mode. The
constraints file pins the version ranges that span several packages (FastAPI, Starlette, MCP,
httpx).

```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools wheel

C=apps/deploy/city/constraints.txt
pip install -c $C -e packages/terasim
pip install -c $C -e packages/terasim-nde-nade
(cd packages/terasim-nde-nade && python setup.py build_ext --inplace)
pip install -c $C -e packages/terasim-city
pip install -c $C -e packages/terasim-service
pip install -c $C -e 'packages/terasim-agent-skill[mcp,report]'
pip install -c $C -e packages/terasim-tmc-service
pip install -c $C -e packages/terasim-plugin-sdk
```

Extras of `terasim-agent-skill`: `mcp` installs the MCP server runtime, `report` the PDF renderer
of the timing report.

The SUMO wheels put `sumo`, `netconvert` and the other SUMO tools on the virtual environment's
`PATH`. For tools that look for `SUMO_HOME`:

```bash
export SUMO_HOME="$(python -c 'import sumo; print(sumo.SUMO_HOME)')"
```

### Check the installation

```bash
python -c "import terasim, terasim_nde_nade, terasim_city, terasim_service, terasim_agent_skill, terasim_tmc_service; print('packages ok')"
python -c "from terasim.overlay import has_libsumo; print('libsumo backend:', has_libsumo)"
python -c "import terasim_nde_nade.utils.collision.collision_check_cy; print('cython extensions ok')"
sumo --version | head -1
redis-server --version
```

The console scripts `terasim-service`, `terasim-agent-skill` and `terasim-tmc-service` are now on
the `PATH`.

## Next steps

- [Quickstart](quickstart.md): optimize the synthetic corridor in both operating modes.
- [Acceptance test](../examples/acceptance.md): every acceptance criterion in one command.
- [Repository layout](../developer-guide/repository-layout.md) and
  [Packages](../developer-guide/packages.md): a tour of the code.
