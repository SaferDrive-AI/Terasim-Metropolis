# TeraSim Metropolis: the city signal-optimization service with an embedded
# Redis in one generic image. The only data baked in are the synthetic demo
# corridor (its SUMO map and a demo scenario, see below).
# `docker run -d -p 8000:8000 <image>`, then POST TMC snapshots, then
# POST /v1/city/optimize for a NEMA plan and KPIs. Maps come from (a) the
# automatic Overpass download + cache, (b) POST /v1/city/maps for files on the
# asset volume, or (c) a pre-populated store volume. Mount persistent storage
# at /data/output to keep cached maps, uploaded plans and the run history across
# restarts. The same image serves three commands (see phase1/compose.yml and
# helm/terasim-phase1): terasim-service (default), terasim-agent-skill (MCP
# server), terasim-tmc-service (TMC collector).
#
# Build from the repository ROOT (the context must hold packages/ and examples/):
#   docker build -f apps/deploy/city/Dockerfile -t terasim-metropolis:0.1.0 .
# Cross-build an amd64 server image from an arm64 machine: add --platform linux/amd64
#
# Two stages:
#   builder — compilers + Cython; installs the six first-party packages
#             (editable, sources under /app/TeraSim) and their dependencies into
#             /usr/local and builds the terasim-nde-nade Cython extensions.
#   runtime — the same base digest, `apt-get upgrade`d, with only the shared
#             libraries SUMO/libsumo load at run time plus redis-server. No
#             compilers, no -dev headers, no pip. Runs as uid 1001 / gid 0 and
#             works under ANY uid with gid 0 (OpenShift restricted-v2): every
#             path the services write is group-0 writable, ports are > 1024,
#             the embedded Redis needs no privileges.
#
# Derived images (for example one that adds the optional Neural NDE GPU module)
# that need apt/pip must switch back with `USER 0` and restore pip with
# `python -m ensurepip --upgrade` before their install steps, then end with
# `USER 1001`.
#
# Python 3.11: Pillow, which reportlab (the PDF renderer of the timing report)
# requires, fixes its CRITICAL advisories CVE-2026-96639, CVE-2026-112393,
# CVE-2026-112398, CVE-2026-112403 and CVE-2026-112900 only in 13.0.0, and Pillow
# 13 needs Python 3.11 or newer.
# Base pinned by digest for reproducible rebuilds; refresh with
#   docker pull python:3.11-slim-bookworm && docker image inspect --format '{{index .RepoDigests 0}}' python:3.11-slim-bookworm
# PYTHON_VERSION names the base image's Python (the build checks it). The Neural NDE
# image (Dockerfile.cnde) derives from the Python 3.10 variant of this image, built with
#   --build-arg PYTHON_VERSION=3.10 \
#   --build-arg BASE_DIGEST=sha256:2559be987fd64d61badbdafd303ea58a9ccab36d6c3c08bce219e762177d2eca
ARG PYTHON_VERSION=3.11
ARG BASE_DIGEST=sha256:90d117a7f1ef6535dc75831e1a9184725757fede0d4d424d7977fb5429d50610
ARG BASE_IMAGE=python:${PYTHON_VERSION}-slim-bookworm@${BASE_DIGEST}

# ---------------------------------------------------------------------------
# Stage 1: builder
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS builder
ARG PYTHON_VERSION

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# The paths of the runtime stage contain PYTHON_VERSION: stop here when it does not
# match the base image.
RUN v="$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])')" \
    && if [ "$v" != "$PYTHON_VERSION" ]; then echo "ERROR: the base image runs Python $v, PYTHON_VERSION is $PYTHON_VERSION"; exit 1; fi

# Only the C/C++ toolchain: every third-party native dependency (numpy, scipy,
# shapely/GEOS, pyproj/PROJ, rtree/libspatialindex, lxml/libxml2, pillow, SUMO)
# ships as a manylinux wheel with its libraries bundled, so no -dev packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/TeraSim
# The four pipeline packages plus the MCP server and the TMC collector.
COPY packages/terasim              packages/terasim
COPY packages/terasim-nde-nade     packages/terasim-nde-nade
COPY packages/terasim-city         packages/terasim-city
COPY packages/terasim-service      packages/terasim-service
COPY packages/terasim-agent-skill  packages/terasim-agent-skill
COPY packages/terasim-tmc-service  packages/terasim-tmc-service
# The end-to-end checks and the offline stand-in for the VSS behavior API
# (standard library only; compose profile "stub").
COPY apps/deploy/city/phase1/phase1_e2e.py apps/deploy/city/phase1/phase1_e2e.py
COPY apps/deploy/city/phase1/scenario_e2e.py apps/deploy/city/phase1/scenario_e2e.py
COPY apps/deploy/city/phase1/stub_behavior_server.py apps/deploy/city/phase1/stub_behavior_server.py

# Version bounds that span several packages (starlette >= 1.3.1 for the 2025/2026
# CVEs, fastapi >= 0.141 to match, mcp < 2, httpx < 0.28); see the file itself.
COPY apps/deploy/city/constraints.txt apps/deploy/city/constraints.txt

RUN pip install -c apps/deploy/city/constraints.txt --upgrade pip setuptools wheel \
    && pip install -c apps/deploy/city/constraints.txt -e packages/terasim \
    && pip install -c apps/deploy/city/constraints.txt -e packages/terasim-nde-nade \
    && pip install -c apps/deploy/city/constraints.txt -e packages/terasim-city \
    && pip install -c apps/deploy/city/constraints.txt -e packages/terasim-service \
    && pip install -c apps/deploy/city/constraints.txt -e 'packages/terasim-agent-skill[mcp,report]' \
    && pip install -c apps/deploy/city/constraints.txt -e packages/terasim-tmc-service \
    && (cd packages/terasim-nde-nade && python setup.py build_ext --inplace && rm -rf build) \
    && find /app/TeraSim -name '__pycache__' -type d -prune -exec rm -rf {} +

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime
ARG PYTHON_VERSION
ARG BASE_DIGEST

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Security updates for the base, then only what runs: SUMO's X11/GL/expat
# runtime libs (linked even headless) and the embedded Redis (redis-tools for
# the entrypoint's readiness ping). No build-essential, no -dev, no perl beyond
# perl-base (Essential).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        libx11-6 libxext6 libxrender1 libgl1 libexpat1 \
        redis-server redis-tools \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# The built Python environment + the editable sources it points at (same
# absolute paths as in the builder, same interpreter build). The base image's
# own site-packages (pip/setuptools/wheel) is removed first so the copy does
# not leave a second, older dist-info next to the builder's versions.
RUN rm -rf /usr/local/lib/python${PYTHON_VERSION}/site-packages
COPY --from=builder /usr/local/lib/python${PYTHON_VERSION}/site-packages /usr/local/lib/python${PYTHON_VERSION}/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app/TeraSim /app/TeraSim
COPY apps/deploy/city/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Bake the synthetic demo corridor's SUMO map (examples/corridor_demo: an E-W
# arterial with three signals, J1 to J3, about 300 m apart) so the deployment is
# self-contained: Kubernetes/OpenShift has no host bind mount and a site without
# Internet access cannot download a map. The service reads it read-only from
# /data/input/map: demo.sumocfg names the other three files by relative path,
# demo.rou.xml is an empty placeholder (the demand comes from the TMC snapshots).
COPY examples/corridor_demo/demo.sumocfg /data/input/map/demo.sumocfg
COPY examples/corridor_demo/demo.net.xml /data/input/map/demo.net.xml
COPY examples/corridor_demo/demo.tls.xml /data/input/map/demo.tls.xml
COPY examples/corridor_demo/demo.rou.xml /data/input/map/demo.rou.xml

# The demo scenario rides along the same way: five files from
# examples/corridor_demo, the same 3-signal corridor entered through
# demo_scenario.sumocfg (its net, its own routes and signal programs) plus a
# placeholder TMC file. It lets phase1/scenario_e2e.py register a complete SUMO
# scenario and optimize it on any deployment. Only these five files: the
# directory's scripts, README and snapshots are development material and stay
# out of the image.
COPY examples/corridor_demo/demo_scenario.sumocfg /data/input/demo_scenario/demo_scenario.sumocfg
COPY examples/corridor_demo/demo.net.xml /data/input/demo_scenario/demo.net.xml
COPY examples/corridor_demo/demo_routes.rou.xml /data/input/demo_scenario/demo_routes.rou.xml
COPY examples/corridor_demo/demo.tls.xml /data/input/demo_scenario/demo.tls.xml
COPY examples/corridor_demo/placeholder_tmc.json /data/input/demo_scenario/placeholder_tmc.json

# The license texts of the release and the notices of its third-party components;
# LICENSES/ holds the license texts NOTICE names for individual files.
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /licenses/
COPY LICENSES /licenses/LICENSES

# Inputs and license texts are only read: root-owned and not writable by group or
# others, so the runtime user (uid 1001, group 0) cannot modify them.
RUN chmod -R go-w /data/input /licenses

# Non-root, arbitrary-UID layout (OpenShift restricted-v2 guidelines): a named
# uid 1001 in group 0 for plain Docker; under an SCC-assigned uid the process
# still has gid 0, so the writable trees are owned by group 0, group-writable
# and setgid. /data/input stays root-owned read-only (inputs), the writable
# trees are /data/output (service store), /data/artifacts (MCP downloads),
# /data/tmc (TMC registry) and $HOME. pip and wheel are removed from the
# runtime image (no package manager at run time; their vendored copies were the
# only remaining Python advisories). Byte-code is precompiled so a read-only
# root filesystem costs nothing at start.
RUN useradd --uid 1001 --gid 0 --home-dir /home/terasim --create-home \
        --shell /usr/sbin/nologin terasim \
    && mkdir -p /data/input /data/output/store /data/artifacts /data/tmc \
    && chown -R 1001:0 /data/output /data/artifacts /data/tmc /home/terasim \
    && chmod -R g+rwX /data/output /data/artifacts /data/tmc /home/terasim \
    && find /data/output /data/artifacts /data/tmc /home/terasim -type d -exec chmod g+s {} + \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh \
    && python -m pip uninstall -y wheel pip \
    && python -m compileall -q -j 0 /app/TeraSim/packages \
    && (python -m compileall -q -j 0 -x '/site-packages/sumo/' /usr/local/lib/python${PYTHON_VERSION}/site-packages \
        || echo 'WARN: some third-party files did not byte-compile (Python 2 tool scripts); continuing')

# No timezone configuration exists: signal schedules are LOCAL wall-clock
# tables and the corridor's timezone is derived from its own coordinates
# (terasim_city.geo_timezone) — a knob would only add a way to silently run
# another city's clock.
ENV HOME=/home/terasim \
    SUMO_HOME=/usr/local/lib/python${PYTHON_VERSION}/site-packages/sumo \
    TERASIM_ENABLE_CITY_API=true \
    TERASIM_CITY_INPUT_ROOT=/data/input \
    TERASIM_CITY_OUTPUT_ROOT=/data/output \
    TERASIM_CITY_DATA_ROOT=/data/output/store \
    TERASIM_REDIS_HOST=127.0.0.1 \
    TERASIM_REDIS_PORT=6379

WORKDIR /app/TeraSim
USER 1001:0

# Fail the build if the runtime is broken — checked AS THE NON-ROOT USER:
# imports, libsumo is the active backend (TeraSim's default; a traci-only
# import wouldn't prove it), sumo runs, no missing libs, redis present, the
# writable trees really are writable, the baked inputs are not (and the baked
# demo map loads in SUMO), the license texts are present and read-only, and pip
# is gone.
RUN python -c "import libsumo, traci, sumolib, terasim_city, terasim_service, terasim_agent_skill, terasim_tmc_service; from mcp.server.fastmcp import FastMCP; import terasim_agent_skill.timing_report; import starlette, fastapi; print('imports ok: starlette', starlette.__version__, 'fastapi', fastapi.__version__)" \
 && python -c "from terasim.overlay import has_libsumo; assert has_libsumo, 'libsumo backend not active'; print('libsumo backend active')" \
 && python -c "import terasim_nde_nade.utils.collision.collision_check_cy, terasim_nde_nade.utils.geometry.geometry_utils_cy, terasim_nde_nade.utils.trajectory.trajectory_utils_cy; print('cython extensions ok')" \
 && sumo --version \
 && if ldd "$SUMO_HOME/bin/sumo" | grep -q 'not found'; then echo 'ERROR: SUMO binary has missing libs:'; ldd "$SUMO_HOME/bin/sumo" | grep 'not found'; exit 1; fi \
 && redis-server --version \
 && test "$(id -u)" != "0" && test -w /data/output && test -w /data/artifacts && test -w /data/tmc && test -w "$HOME" \
 && test -r /data/input/map/demo.sumocfg && test ! -w /data/input/map && test ! -w /data/input/map/demo.net.xml \
 && sumo -c /data/input/map/demo.sumocfg --end 10 --no-step-log true \
 && test ! -w /data/input/demo_scenario && test ! -w /data/input/demo_scenario/demo_scenario.sumocfg \
 && test -f /licenses/LICENSE && test -f /licenses/NOTICE && test -f /licenses/THIRD_PARTY_NOTICES.md \
 && test -f /licenses/LICENSES/EPL-2.0.txt \
 && test ! -w /licenses && test ! -w /licenses/LICENSE \
 && if python -m pip --version >/dev/null 2>&1; then echo 'ERROR: pip still present'; exit 1; fi \
 && if ls /usr/local/lib/python${PYTHON_VERSION}/site-packages | grep -qiE '^(pip|wheel)-[0-9].*\.dist-info$'; then echo 'ERROR: pip/wheel dist-info still present'; exit 1; fi \
 && python -c "from importlib.metadata import distributions; import collections; c = collections.Counter(d.metadata['Name'].lower() for d in distributions()); dup = [n for n, k in c.items() if k > 1]; assert not dup, f'duplicate distributions: {dup}'; print(len(c), 'distributions, no duplicates')" \
 && echo 'runtime verified: non-root, imports + libsumo backend active + cython + sumo binary + no missing libs + redis + writable data trees + read-only inputs + demo map loads + license texts + no pip'

LABEL org.opencontainers.image.title="terasim-metropolis" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="TeraSim Metropolis signal-optimization service (terasim-service / terasim-agent-skill MCP server / terasim-tmc-service TMC collector) with embedded Redis; non-root, multi-stage" \
      org.opencontainers.image.base.name="docker.io/library/python:${PYTHON_VERSION}-slim-bookworm" \
      org.opencontainers.image.base.digest="${BASE_DIGEST}"

EXPOSE 8000 9911 8200
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
