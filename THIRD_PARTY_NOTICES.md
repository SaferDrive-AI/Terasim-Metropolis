# Third-party notices

TeraSim Metropolis is licensed under the Apache License, Version 2.0 (LICENSE, NOTICE).
It uses the third-party components listed below. Each component remains under its own
license; this file summarizes them for redistribution. License texts ship with each
component (the `licenses/` folder or `*.dist-info` directory of every Python package, and
`/usr/share/doc/<package>/copyright` for the operating-system packages of the container
image).

Versions: section 2 lists the versions a fresh install of v0.1.0 resolved on 2026-10-20
(Python 3.10, Linux x86_64). Unpinned dependencies can resolve to newer compatible versions
at build time, so this list is regenerated for every released image.

## 1. Third-party code included in this repository

| Path | Origin | License |
| --- | --- | --- |
| `packages/terasim`, `packages/terasim-nde-nade`, parts of `packages/terasim-service` | TeraSim project (https://github.com/mcity/TeraSim), Mcity, University of Michigan | Apache-2.0 |
| `packages/terasim-tmc-service/terasim_tmc_service/nv_generate_tmc.py` | Reference turning-movement counting script provided by NVIDIA Corporation for the NVIDIA Metropolis VSS integration | As provided by NVIDIA Corporation for this integration |
| `packages/terasim-nde-nade/terasim_nde_nade/utils/sumo_lane_locator.py` | Ported from Eclipse SUMO 1.23.1 (https://eclipse.dev/sumo), German Aerospace Center (DLR) and others | EPL-2.0 OR GPL-2.0-or-later, `LICENSES/EPL-2.0.txt` |

## 2. Python packages installed with the release

Runtime dependencies of `terasim`, `terasim-nde-nade`, `terasim-city`, `terasim-service`,
`terasim-agent-skill[mcp,report]` and `terasim-tmc-service` (transitive closure, development
tools excluded). They are installed from PyPI and are not part of this repository's source.

| Package | Version | License | Note |
| --- | --- | --- | --- |
| addict | 2.4.0 | MIT |  |
| alembic | 1.20.0 | MIT |  |
| annotated-doc | 0.0.5 | MIT |  |
| annotated-types | 0.8.0 | MIT |  |
| antlr4-python3-runtime | 4.9.3 | BSD |  |
| anyio | 4.15.1 | MIT |  |
| async-timeout | 5.0.1 | Apache-2.0 |  |
| attrs | 26.1.0 | MIT |  |
| bidict | 0.23.1 | MPL-2.0 | file-level copyleft (MPL-2.0): unmodified |
| blinker | 1.9.0 | MIT |  |
| certifi | 2026.10.19 | MPL-2.0 | file-level copyleft (MPL-2.0): unmodified |
| cffi | 2.1.1 | MIT-0 |  |
| charset-normalizer | 3.5.2 | MIT |  |
| click | 8.5.0 | BSD-3-Clause |  |
| colorlog | 6.12.0 | MIT |  |
| comm | 0.2.3 | BSD |  |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |  |
| Cython | 3.3.0 | Apache-2.0 |  |
| dash | 4.4.1 | MIT |  |
| eclipse-sumo | 1.23.1 | EPL-2.0 | see section 3 |
| exceptiongroup | 1.3.1 | MIT |  |
| fastapi | 0.156.4 | MIT |  |
| Flask | 3.1.3 | BSD-3-Clause |  |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |  |
| h11 | 0.16.0 | MIT |  |
| httpcore | 1.0.9 | BSD-3-Clause |  |
| httpx | 0.27.2 | BSD-3-Clause |  |
| httpx-sse | 0.4.3 | MIT |  |
| hydra-core | 1.3.7 | MIT |  |
| idna | 3.20 | BSD-3-Clause |  |
| importlib_metadata | 9.0.1 | Apache-2.0 |  |
| itsdangerous | 2.2.0 | BSD |  |
| janus | 2.0.0 | Apache-2.0 |  |
| Jinja2 | 3.1.6 | BSD |  |
| jsonschema | 4.26.0 | MIT |  |
| jsonschema-specifications | 2025.9.1 | MIT |  |
| libsumo | 1.23.1 | EPL-2.0 | see section 3 |
| loguru | 0.7.3 | MIT |  |
| lxml | 6.1.3 | BSD-3-Clause |  |
| Mako | 1.4.3 | MIT |  |
| MarkupSafe | 3.0.4 | BSD-3-Clause |  |
| mcp | 1.30.0 | MIT |  |
| narwhals | 2.27.1 | MIT |  |
| nest-asyncio | 1.6.0 | BSD |  |
| numpy | 1.26.4 | BSD |  |
| omegaconf | 2.3.1 | BSD |  |
| opentelemetry-api | 1.45.1 | Apache-2.0 |  |
| optuna | 5.0.0 | MIT |  |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |  |
| pillow | 12.3.0 | MIT-CMU |  |
| plotly | 7.1.0 | MIT |  |
| pycparser | 3.11 | BSD-3-Clause |  |
| pydantic | 2.14.1 | MIT |  |
| pydantic_core | 2.50.1 | MIT |  |
| pydantic-settings | 2.15.0 | MIT |  |
| PyJWT | 2.15.1 | MIT |  |
| pyproj | 3.7.1 | MIT |  |
| python-dotenv | 1.2.4 | BSD-3-Clause |  |
| python-multipart | 0.0.32 | Apache-2.0 |  |
| PyYAML | 6.0.3 | MIT |  |
| redis | 8.1.0 | MIT |  |
| referencing | 0.37.0 | MIT |  |
| reportlab | 5.0.1 | BSD |  |
| requests | 2.34.2 | Apache-2.0 |  |
| retrying | 1.4.2 | Apache-2.0 |  |
| rpds-py | 0.30.0 | MIT |  |
| rtree | 1.4.1 | MIT |  |
| scipy | 1.15.3 | BSD |  |
| setuptools | 84.2.1 | MIT |  |
| shapely | 2.1.2 | BSD |  |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |  |
| SQLAlchemy | 2.0.55 | MIT |  |
| sse-starlette | 3.5.0 | BSD-3-Clause |  |
| starlette | 1.7.0 | BSD-3-Clause |  |
| sumolib | 1.23.1 | EPL-2.0 | see section 3 |
| toml | 0.10.2 | MIT |  |
| tomli | 2.5.0 | MIT |  |
| tqdm | 4.70.1 | MPL-2.0 AND MIT | file-level copyleft (MPL-2.0): unmodified |
| traci | 1.23.1 | EPL-2.0 | see section 3 |
| typing_extensions | 4.16.0 | PSF-2.0 |  |
| typing-inspection | 0.4.4 | MIT |  |
| tzfpy | 2.1.1 | MIT | bundled time-zone boundary data, see section 4 |
| urllib3 | 2.8.0 | MIT |  |
| utm | 0.9.0 | MIT |  |
| uvicorn | 0.54.0 | BSD-3-Clause |  |
| Werkzeug | 3.1.9 | BSD-3-Clause |  |
| zipp | 4.1.1 | MIT |  |
No package in this list is distributed under a strong copyleft license (GPL, LGPL or AGPL).
The MPL-2.0 components are redistributed unmodified.

## 3. Eclipse SUMO

`eclipse-sumo`, `libsumo`, `sumolib` and `traci` 1.23.1 are Eclipse SUMO
(https://eclipse.dev/sumo), made available under the Eclipse Public License 2.0 with the
GNU General Public License, version 2 or later, as a secondary license
(SPDX: `EPL-2.0 OR GPL-2.0-or-later`). They are redistributed unmodified, in the binary
form published on PyPI. The corresponding source code is available at
https://github.com/eclipse-sumo/sumo (tag `v1_23_1`). The license, notice and author files
ship inside each wheel (`eclipse_sumo-1.23.1.dist-info/licenses/`).

The SUMO wheels bundle native libraries (`eclipse_sumo.libs/` and `libsumo.libs/`), among
them the FOX toolkit, Xerces-C, PROJ, libcurl, OpenSSL, NSS, Kerberos, ICU, libtiff,
libjpeg, libpng, FreeType, fontconfig, gl2ps, GLU and X11 client libraries. SUMO's
`NOTICE.md` lists the third-party content of the SUMO distribution. A redistributed
container image includes these libraries under their own licenses.

## 4. Data

| Data | Where | License |
| --- | --- | --- |
| Time-zone boundaries (tzf-dist, derived from timezone-boundary-builder and OpenStreetMap) | bundled inside the `tzfpy` package; used to derive a corridor's local time from its coordinates | ODbL-1.0; attribution: (c) OpenStreetMap contributors |
| OpenStreetMap extracts | downloaded at run time from an Overpass endpoint when a request names no stored map and `TERASIM_CITY_OSM_DOWNLOAD` is on (the default) | ODbL-1.0; maps derived from them carry the attribution "(c) OpenStreetMap contributors" |
| Demo corridor (`examples/corridor_demo`) and acceptance-test inputs | generated, synthetic | Apache-2.0 (this project) |

## 5. Container image (`terasim-metropolis`)

The image built from `apps/deploy/city/Dockerfile` contains, besides the packages of
sections 2 and 3:

| Component | Version (2026-10-20 build) | License |
| --- | --- | --- |
| Base image `python:3.10-slim-bookworm` (Debian 12) | Debian 12, about 150 packages | various free-software licenses (including GPL-2.0 and LGPL for system libraries); sources at https://sources.debian.org; per-package texts in `/usr/share/doc/*/copyright` |
| CPython | 3.10 | PSF-2.0 |
| Redis server (Debian package `redis-server`) | 7.0.15 | BSD-3-Clause |
| Runtime libraries for SUMO (Debian packages libx11-6, libxext6, libxrender1, libgl1, libexpat1) | Debian 12 | MIT / X11-style licenses |

The image carries this file, LICENSE, NOTICE and `LICENSES/` under `/licenses/`. When
redistributing the image (for example through the NVIDIA NGC catalog or GHCR), keep them with
it and keep the source-code pointers of section 3 available.

## 6. Neural NDE image (`terasim-metropolis:0.1.0-cnde`, optional)

The image built from `apps/deploy/city/Dockerfile.cnde` contains, besides the release image of
section 5, the following packages (versions of the 2026-10-23 build):

| Component | Version | License |
| --- | --- | --- |
| PyTorch (`torch`) | 2.11.0+cu128 | BSD-3-Clause |
| NVIDIA CUDA libraries installed with PyTorch (`nvidia-*-cu12`: CUDA runtime, cuBLAS, cuDNN, cuFFT, cuFile, cuRAND, cuSOLVER, cuSPARSE, cuSPARSELt, NCCL, NVSHMEM, nvJitLink, NVRTC, CUPTI; `cuda-bindings`, `cuda-toolkit`) | CUDA 12.8 | NVIDIA software license agreements; `nvidia-nvtx-cu12` and `cuda-pathfinder`: Apache-2.0 |
| Triton | 3.6.0 | MIT |
| torch-cluster | 1.6.3 | MIT |
| torchdiffeq | 0.2.5 | MIT |
| python-igraph (`igraph`) | 1.0.0 | GPL-2.0-or-later |
| CasADi | 3.7.2 | LGPL-3.0-or-later |
| EasyDict | 1.13 | LGPL-3.0 |
| python-control | 0.10.2 | BSD-3-Clause |
| scikit-learn, scikit-base, joblib, threadpoolctl, dill, psutil, contourpy, kiwisolver, fsspec, networkx, mpmath, sympy, pykalman, nvidia-ml-py | see the image | BSD-3-Clause or BSD-style |
| matplotlib | 3.10.8 | Matplotlib license (PSF-based) |
| OpenCV (`opencv-python-headless`) | 4.11.0.86 | Apache-2.0 |
| fonttools, filelock, pyparsing, pytz, six, texttable, cycler | see the image | MIT or BSD-style |
| python-dateutil, tzdata | see the image | Apache-2.0 / BSD-3-Clause |

It pins `hydra-core` 1.3.2, `omegaconf` 2.3.0 and `plotly` 6.6.0 (licenses as in section 2).
It also holds the Neural NDE runtime module (`/app/TeraSim/packages/cnde`), which is not part
of this repository and not covered by its Apache-2.0 license; the third-party source trees
inside it (nuplan-devkit, tuplan-garage: Apache-2.0; compiled `torch_cluster` extensions: MIT)
carry their license files. License texts of the Python packages: their `*.dist-info`
directories in the image.

## 7. Components not included in this release

- The Neural NDE runtime module and its model checkpoint: not part of this repository. The
  inference interface in `terasim-city` (`vehicle_behavior="cnde"`, `traffic_model="cnde"`)
  loads them when they are installed (docs/developer-guide/behavior-models.md).
- The optional operator console and its camera video relay: not part of this repository
  or of the `terasim-metropolis` image.
