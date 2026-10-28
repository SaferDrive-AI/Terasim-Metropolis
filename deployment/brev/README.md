# TeraSim Metropolis with NVIDIA Metropolis VSS on NVIDIA Brev

On one GPU instance, VSS 3.3.0 counts the traffic of Market St and W Santa Clara St, San José,
in four recorded camera videos, and TeraSim optimizes the intersection's signal timing when the
VSS agent is asked and every 10 minutes.

| Mode | Traffic in the simulations | Additional inputs |
| --- | --- | --- |
| SUMO | SUMO's driver model | none |
| MTR | SUMO's driver model; on request, Neural NDE traffic on the public MTR network (`traffic_model="cnde"`) | step 2: the open Neural NDE image and an MTR checkpoint (research use only) |

The four camera videos (GTC 2026 San José recordings) and the corridor package
`sj-reference-corridor-0.2.0` are available by email: haotian@inchor.ai.

## 1. Prerequisites

| Item | Value |
| --- | --- |
| Instance | NVIDIA Brev, AWS `g7e.12xlarge`: 2 × RTX PRO 6000 Blackwell (96 GB), 48 vCPU, 500 GiB memory, 200 GB free disk |
| GPU use | GPU 1: VSS language and vision-language models (71 GB); GPU 0: VSS computer vision (5 GB) and, in MTR mode, the Neural NDE trials (18 GB) |
| Brev secure link | Secure Links, "+ HTTP port", port `7777`: the VSS page and the download links |
| Software | NVIDIA driver 595.58.03 or newer, Docker with the NVIDIA Container Toolkit, Docker Compose v2, git, git-lfs, python3; validated on Ubuntu 22.04, Docker 29.8.2, Compose 5.6.0 |
| Keys | NGC API key with access to the VSS 3.3 containers and models (NVIDIA Developer Program); Google Maps API key (VSS map view) |

## 2. Steps

Variables used by the steps (set them in every new shell):

```bash
REPO=~/Terasim-Metropolis SJ=~/sj-reference-corridor-0.2.0 DEPLOY=~/terasim VSS=~/video-search-and-summarization
IP=$(ip route get 1.1.1.1 | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
PUBLIC=<host name of the Brev secure link for port 7777, for example 7777-abcd1234.gobrev.dev>
```

### Step 1. TeraSim and the sample data

```bash
cd ~ && git clone https://github.com/SaferDrive-AI/Terasim-Metropolis.git
docker pull ghcr.io/saferdrive-ai/terasim-metropolis:0.1.0
docker tag ghcr.io/saferdrive-ai/terasim-metropolis:0.1.0 terasim-metropolis:0.1.0
unzip -q sj-reference-corridor-0.2.0.zip
for n in 1 2 3 4; do cp <folder of the videos>/gtcsj2026-sj-downtown-cam$n.mp4 $SJ/videos/cam$n.mp4; done
(cd $SJ && sha256sum -c --quiet SHA256SUMS && cd videos && sha256sum -c SHA256SUMS)    # cam1.mp4: OK ... cam4.mp4: OK
```

### Step 2. MTR mode only: the open Neural NDE image and a checkpoint

```bash
cd $REPO && docker build -f apps/deploy/city/Dockerfile.open-nnde -t terasim-metropolis:0.1.0-open-nnde .
curl -L -o ~/checkpoint_epoch_30.pth \
  https://huggingface.co/Student-Xiaoji/LLM-Augmented-MTR-models/resolve/main/100_percent/MTR/checkpoint_epoch_30.pth
echo "f14858a1ce6b4098eef8230db39f014b337ef5e158b591e0febe722d7ae4667a  $HOME/checkpoint_epoch_30.pth" | sha256sum -c -
deployment/brev/check-neural-nde.sh ~/checkpoint_epoch_30.pth          # PASS, about 3 minutes
```

The checkpoint is trained on the Waymo Open Motion Dataset: non-commercial use only
([Open Neural NDE](../../docs/developer-guide/open-neural-nde.md)).

### Step 3. Start TeraSim

```bash
mkdir -p $DEPLOY/cnde_map $DEPLOY/cnde_assets && cd $DEPLOY
cp $REPO/apps/deploy/city/phase1/{compose.yml,compose.cnde.yml} .
cp $REPO/deployment/brev/{compose.brev.yml,files-proxy.nginx.conf} . && cp $REPO/deployment/brev/env.brev.example .env
cp $SJ/terasim/{Metadata.json,scheduled_config.yaml} . && cp $SJ/map-package/* cnde_map/
sed -i "s|<instance-private-ip>|$IP|; s|<public-vss-host>|$PUBLIC|" .env compose.brev.yml
```

SUMO mode:

```bash
C="docker compose -f compose.yml -f compose.brev.yml"
```

MTR mode:

```bash
cp ~/checkpoint_epoch_30.pth cnde_assets/nn_weights.pth
C="docker compose -f compose.yml -f compose.cnde.yml -f compose.brev.yml"
```

Then, in both modes:

```bash
$C up -d
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:8100/openapi.json        # 200
curl -s --retry 10 --retry-connrefused 127.0.0.1:8200/v1/tmc/health
curl -s -X POST 127.0.0.1:8100/v1/city/plans -H 'content-type: application/json' \
  --data-binary @$SJ/terasim/current_plan.nema_plan.json                     # the intersection's current timing plan
docker run -d --name terasim-files-proxy --network host --restart unless-stopped \
  -v $DEPLOY/files-proxy.nginx.conf:/etc/nginx/nginx.conf:ro nginx:1.27-alpine   # the download links
```

MTR mode, check (prints `ready`):

```bash
$C exec -T terasim-service python - <<'PY'
from terasim_city.factories.cnde_behavior import missing_cnde_runtime
from terasim_city.maps.cnde_artifacts import missing_cnde_map_artifacts
print(missing_cnde_runtime() + missing_cnde_map_artifacts("/data/input/cnde_map/base.sumocfg") or "ready")
PY
```

### Step 4. Configure VSS, before its first start

```bash
git lfs install
GIT_LFS_SKIP_SMUDGE=1 git clone -q --branch v3.3.0 --depth 1 \
  https://github.com/NVIDIA-AI-Blueprints/video-search-and-summarization.git $VSS
cd $VSS && git lfs pull --include="deploy/**"
```

NVIDIA's smart-cities overlay, the "Smart City Customizations" cell of
`deploy/docker/scripts/deploy_smartcities_launchable.ipynb`, run as it is:

```bash
GOOGLE_MAPS_API_KEY=<your Google Maps API key> python3 - <<'PY'
import json, os
nb = json.load(open("deploy/docker/scripts/deploy_smartcities_launchable.ipynb"))
cell = next(c for c in nb["cells"]
            if c["cell_type"] == "code" and "# === Smart City Customizations ===" in "".join(c["source"]))
exec("".join(cell["source"]), {"REPO_DIR": os.getcwd(), "NVSTREAMER_VIDEO_DIR": "vss-nvstreamer-video-dir",
                               "GOOGLE_MAPS_API_KEY": os.environ["GOOGLE_MAPS_API_KEY"]})
PY
```

TeraSim's edits: the four videos as cameras `cam1` to `cam4`, detector labels and map matching on
the corridor's road network, an 8 MB Kafka receive buffer for Logstash, HAProxy idle timeouts of
2100 s and the route `/terasim/` to the download links, and the `terasim_mcp` tools and prompt of
the VSS agent ([VSS integration](../../docs/deployment/vss-integration.md), items 1 to 6; the
previous file stays as `config.yml.bak`):

```bash
python3 $REPO/deployment/brev/configure-vss.py $VSS $SJ http://$IP:9911/mcp      # one line per edit
```

### Step 5. Start VSS

`dev-profile.sh up` first removes the Compose project `vss` with its volumes and **every Docker
volume that no container uses**. The first start downloads the images and about 40 GB of models
(about 20 minutes).

```bash
export NGC_CLI_API_KEY=<your NGC API key> BREV_PUBLIC_HOST=$PUBLIC
echo "$NGC_CLI_API_KEY" | docker login nvcr.io -u '$oauthtoken' --password-stdin
cd $VSS/deploy/docker && ./scripts/dev-profile.sh up --profile alerts --mode verification -H RTXPRO6000BW
curl -s 127.0.0.1:8000/mcp/client/tool/list | python3 -c '
import json, sys
for c in json.load(sys.stdin)["mcp_clients"]:
    if c["function_group"] == "terasim_mcp": print(c["session_healthy"], len(c["tools"]), "tools")'   # True 8 tools
```

### Step 6. Cameras, calibration and road network

When `docker logs vss-rtvi-cv 2>&1 | grep 'Active sources'` shows the pipeline:

```bash
curl -s 127.0.0.1:31000/api/v1/live/streams | python3 -c '
import json, sys
for group in json.load(sys.stdin):
    for streams in group.values():
        for s in streams:
            if s["name"] in ("cam1", "cam2", "cam3", "cam4"): print(s["name"], s["url"])' |
while read name url; do
  curl -s -X POST 127.0.0.1:30888/vst/api/v1/sensor/add -H 'content-type: application/json' \
    -d "{\"sensorUrl\": \"$url\", \"name\": \"$name\"}"; echo
done
curl -s -X POST 127.0.0.1:8081/config/upload-file/calibration  -F configFiles=@$SJ/vss/calibration.sj4.json
curl -s -X POST 127.0.0.1:8081/config/upload-file/road-network -F configFiles=@$SJ/vss/road-network.json
docker logs vss-behavior-analytics 2>&1 | grep 'Loaded 4 sensors' | tail -1    # ... ['cam4', 'cam3', 'cam2', 'cam1']
```

Then `Active sources : 4`. Upload the two files again after a restart of `vss-behavior-analytics`.

### Step 7. Register the intersection

Counts come in 10-minute windows of the clock, each about 3 minutes after it ends. Register after
the first whole window since step 6 (cameras up at 12:04: after 12:23):

```bash
curl -s -X POST 127.0.0.1:8200/v1/tmc/intersections -H 'content-type: application/json' \
  --data-binary @$SJ/terasim/register-intersection.json
curl -s 127.0.0.1:8100/v1/city/snapshots/santa_clara_st      # one snapshot per window
```

### Step 8. Requests

| Mode | Request to the VSS agent |
| --- | --- |
| SUMO and MTR | Optimize the intersection of Market St and W Santa Clara St. |
| MTR | Optimize the intersection of Market St and W Santa Clara St using CNDE (Neural NDE) traffic. |

The first works in the VSS chat (`https://$PUBLIC`) and through the agent API. The second takes
about 15 minutes, so it goes through the agent API (the chat page ends a request after 300 s
without data):

```bash
curl -s 127.0.0.1:8000/generate -H 'content-type: application/json' \
  -d '{"messages": [{"role": "user", "content": "<request>"}]}' |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["value"].split("</agent-think>")[-1])'
```

Scheduled runs (SUMO traffic): `curl -s 127.0.0.1:8100/v1/city/scheduled/santa_clara_st/latest`.

## 3. Expected results

Against the current plan of step 3; in parentheses, a run of this guide on 2026-10-28:

| Request | Duration | Answer | Control delay |
| --- | --- | --- | --- |
| Ordinary (both modes) | 1 to 2 min (82 to 88 s) | plan, KPI table, NEMA splits, four download links: PDF report, plans, result | 5 to 27 % lower (5.7 %, 11.9 %) |
| Neural NDE (MTR mode) | about 15 min (867 s) | the same, with "Traffic model: CNDE (Neural NDE, learned driver behavior) simulated every candidate plan." | lower (8.8 %); within 5 % of SUMO for the same plan (4.4 %, 3.1 %); no red-light crossing (0 of 928 stop-line crossings) |
| Scheduled | every 600 s, 1 to 2 min (70 s) | NEMA plan and KPIs | as for ordinary requests |

## 4. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `dev-profile.sh up`: "Could not resolve the Brev secure link for port 7777" | `export BREV_PUBLIC_HOST=$PUBLIC` |
| No chat answer to the Neural NDE request | use the agent API; the run still completes (`127.0.0.1:8100/v1/city/optimizations`) |
| `Active sources : 0`, or analytics loaded other sensors | repeat step 6 |
| "No traffic data yet" | ask after the next window (registered too early, or TeraSim restarted: windows are kept in memory) |
| The agent cannot reach the intersection list or another TeraSim tool | TeraSim restarted after VSS, and the agent does not reconnect: `docker restart vss-agent` |
| Instance stopped and started | steps 5 and 6 again, then `$C restart terasim-service` (keeps plan and registration, drops the windows without cameras) |
| A download link opens a Brev login page | the secure link is private to the Brev organization: open it where the VSS page is open |

## 5. Stop and change

| Action | Command |
| --- | --- |
| Stop TeraSim, keep plan and registration | in `$DEPLOY`: `$C down` (`$C down -v` removes them), `docker rm -f terasim-files-proxy` |
| Change the mode | `$C down`, the other mode's block of step 3, `$C up -d`, `docker restart vss-agent`; traffic returns with the next window |
| Stop VSS, keep its data | in `$VSS/deploy/docker`: `docker compose -p vss down --remove-orphans` (`./scripts/dev-profile.sh down` removes it) |
| Detach the VSS agent | restore `config.yml.bak`, `docker restart vss-agent` |
| Neural NDE default mode (all vehicles predicted, about 39 min per request) | `compose.brev.yml`: `TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY: "0"`, `TERASIM_CITY_CNDE_MAX_WORKERS: "6"`, `TERASIM_CITY_TIMEOUT_S: "3600"`; `configure-vss.py ... --tool-call-timeout 3600 --proxy-timeout 4200s` before step 5 |
