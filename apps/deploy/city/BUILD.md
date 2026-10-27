# Building the TeraSim Metropolis image

The image `terasim-metropolis:0.1.0` is built from `apps/deploy/city/Dockerfile`. How to run
it: `README.md`.

## Build

Run from the **repository root** (the build context must hold `packages/`, `examples/` and the
license files):

```bash
docker build -f apps/deploy/city/Dockerfile -t terasim-metropolis:0.1.0 .
```

The image targets linux/amd64; on an arm64 machine add `--platform linux/amd64`.

The Dockerfile has two stages:

- **builder** — the C/C++ toolchain; installs the six first-party packages (`terasim`,
  `terasim-nde-nade`, `terasim-city`, `terasim-service`, `terasim-agent-skill` with its `mcp`
  and `report` extras, `terasim-tmc-service`) and their dependencies with the version bounds
  of `constraints.txt`, and builds the `terasim-nde-nade` Cython extensions.
- **runtime** — the same base image digest, updated, with only the shared libraries SUMO
  loads at run time plus `redis-server`; no compilers and no pip. It runs as uid 1001 / gid 0
  and works under any uid with gid 0 (OpenShift `restricted-v2`).

The last build step checks the runtime as the non-root user and fails the build when any
check fails: the imports, the libsumo backend, the Cython extensions, the SUMO binary and its
shared libraries, Redis, the writable data paths, the read-only inputs, the baked demo map
loading in SUMO, the license texts and the absence of pip.

## What the image bakes

Read-only, root-owned:

- `/data/input/map` — the synthetic demo corridor's map, four files from
  `examples/corridor_demo`: `demo.sumocfg`, `demo.net.xml`, `demo.tls.xml`, `demo.rou.xml`;
- `/data/input/demo_scenario` — the demo scenario, five files from `examples/corridor_demo`:
  `demo_scenario.sumocfg`, `demo.net.xml`, `demo_routes.rou.xml`, `demo.tls.xml`,
  `placeholder_tmc.json`;
- `/licenses` — `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md`, `LICENSES/EPL-2.0.txt`.

The scripts, README and snapshots of `examples/corridor_demo` are development material and
are not copied. Next to the packages, under `/app/TeraSim/apps/deploy/city/phase1`, the image
carries the self-tests `phase1_e2e.py` and `scenario_e2e.py` and the VSS behavior stub
`stub_behavior_server.py`.

`tests/test_tmc_service/test_deploy_assets.py` pins this layout and keeps the deployment
files consistent with each other: the corridor spec (`phase1/Metadata.json`,
`helm/terasim-phase1/configs/Metadata.json` and the test fixture are byte-identical), the
corridor configuration (names the spec's corridor on the baked map), the image tag (Compose,
the chart's `values.yaml` and its `appVersion` agree) and the smoke test (registers the
baked map, counts every intersection of the spec).

## Derived images

An image that adds packages on top switches back with `USER 0`, restores pip with
`python -m ensurepip --upgrade` before its install steps, and ends with `USER 1001`.

`Dockerfile.cnde` is such an image: the optional Neural NDE (CNDE) GPU runtime on top of the
release image. It installs PyTorch 2.11.0 (CUDA 12.8 wheels), `torch-cluster` and the runtime's
other dependencies, and copies the Neural NDE runtime module from a second build context
named `neural-nde` (the module is not part of this repository):

```bash
docker build -f apps/deploy/city/Dockerfile.cnde \
  --build-context neural-nde=<path of the Neural NDE runtime module> \
  -t terasim-metropolis:0.1.0-cnde .
```

The model checkpoint is mounted at run time at
`/app/TeraSim/packages/cnde/assets/nn_weights.pth`; the build fails when the runtime module
holds a `*.pth` file. The last build step checks, besides the imports, that the service's own
runtime check (`missing_cnde_runtime()`) reports only the checkpoint as missing and that libsumo
is still the active backend. Run it with `phase1/compose.cnde.yml` (one GPU, checkpoint and map
package mounts).

## Publish or hand over

Tag and push to the registry the deployment pulls from:

```bash
docker tag terasim-metropolis:0.1.0 <registry>/terasim-metropolis:0.1.0
docker push <registry>/terasim-metropolis:0.1.0
```

or save it as an archive and load it on the target host:

```bash
docker save terasim-metropolis:0.1.0 | gzip > terasim-metropolis-0.1.0.tar.gz
docker load -i terasim-metropolis-0.1.0.tar.gz
```

## Checks

```bash
IMAGE=terasim-metropolis:0.1.0 bash apps/deploy/city/smoke.sh     # single container, REST API
docker compose -f apps/deploy/city/phase1/compose.yml --profile stub up -d
docker compose -f apps/deploy/city/phase1/compose.yml run --rm --no-deps terasim-mcp \
  python apps/deploy/city/phase1/phase1_e2e.py \
  --service http://terasim-service:8000 --tmc http://terasim-tmc:8200 --mcp http://terasim-mcp:9911/mcp
docker compose -f apps/deploy/city/phase1/compose.yml run --rm --no-deps terasim-mcp \
  python apps/deploy/city/phase1/scenario_e2e.py --service http://terasim-service:8000
helm lint --strict apps/deploy/city/helm/terasim-phase1 \
  --set tmc.vssBehaviorUrl=http://vss-video-analytics-api.vss.svc:8081 \
  --set global.externalHost=terasim.apps.example.test
```
