# TeraSim Metropolis with NVIDIA Metropolis VSS on NVIDIA Brev

Reproduces the reference deployment on one GPU instance: VSS 3.3.0 counts the traffic of the
intersection of Market St and W Santa Clara St, San José, from four recorded camera videos, and
TeraSim optimizes its signal timing when the VSS agent is asked and every 10 minutes.

| Mode | Traffic in TeraSim's simulations | Additional inputs |
| --- | --- | --- |
| SUMO | SUMO's driver model, for every optimization | none |
| MTR | SUMO's driver model; on request (`traffic_model="cnde"`), Neural NDE traffic driven by the public MTR network for every candidate plan | the open Neural NDE image (step 2) and an MTR checkpoint, for research use only |

**Sample data.** The four camera videos (GTC 2026 San José recordings) and the corridor package
`sj-reference-corridor-0.2.0` are not part of this repository: email haotian@inchor.ai.

## 1. Prerequisites

| Item | Value |
| --- | --- |
| Instance | NVIDIA Brev, AWS `g7e.12xlarge`: 2 × NVIDIA RTX PRO 6000 Blackwell Server Edition (96 GB), 48 vCPU, about 500 GiB memory, about 1 TB disk with 200 GB free |
| Brev secure link | Brev console, Secure Links, "+ HTTP port", destination port `7777`: the VSS web page and the download links of the answers |
| GPUs | GPU 1: VSS language and vision-language models (about 71 GB); GPU 0: VSS computer vision (about 5 GB) and, in MTR mode, the Neural NDE trials (up to about 18 GB) |
| Software | NVIDIA driver for CUDA 12.8 or newer, Docker with the NVIDIA Container Toolkit, Docker Compose v2, git, git-lfs, python3. VSS 3.3 lists Ubuntu 24.04, NVIDIA driver 595.58.03 or newer, Docker 28.3.3 to below 29.5.0, Docker Compose 2.39.1 or newer; validated on Ubuntu 22.04 with Docker 29.8.2 and Docker Compose 5.6.0 |
| Keys | an NGC API key with access to the VSS 3.3 containers and models (NVIDIA Developer Program); a Google Maps API key (the VSS map view) |

## 2. Steps

The steps use these variables; set them again in every new shell:

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

This checkpoint was trained on the Waymo Open Motion Dataset: non-commercial use only, see
[Open Neural NDE (MTR)](../../docs/developer-guide/open-neural-nde.md).

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

NVIDIA's smart-cities overlay: the "Smart City Customizations" cell (section 6.1) of NVIDIA's
notebook `deploy/docker/scripts/deploy_smartcities_launchable.ipynb`, run as it is:

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

TeraSim's edits:

```bash
python3 $REPO/deployment/brev/configure-vss.py $VSS $SJ http://$IP:9911/mcp      # one line per edit
```

| Edit | Files under `deploy/docker` |
| --- | --- |
| The four videos, streamed as cameras `cam1` to `cam4` | `vss-nvstreamer-video-dir/videos/dev-profile-alerts/` |
| Detector labels `two_wheeler`, `Vehicle`, `Person`; map matching of these classes on the corridor's road network, from the OpenStreetMap cache of the sample data | `developer-profiles/dev-profile-alerts/`: `deepstream/configs/rtdetr-960x544-labels.txt`, `vss-behavior-analytics/`, `compose.yml` |
| Kafka receive buffer of 8 MB for Logstash | `services/infra/elk/logstash/pipelines/kafka/mdx-logstash.conf` |
| HAProxy idle timeouts of 2100 s; `/terasim/` to the download links of step 3 | `services/infra/haproxy/haproxy.cfg.template` |
| The `terasim_mcp` tools of the VSS agent and its prompt: items 1 to 6 of "VSS agent configuration" in [VSS integration](../../docs/deployment/vss-integration.md), MCP URL of this instance, `tool_call_timeout` 1800 s; the previous file is kept as `config.yml.bak` | `developer-profiles/dev-profile-alerts/vss-agent/configs/config.yml` |

### Step 5. Start VSS

`dev-profile.sh up` first removes the Compose project `vss` with its volumes and **every Docker
volume on the machine that no container uses**. The first start downloads the VSS images and about
40 GB of models: about 20 minutes on the reference instance.

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

`docker logs vss-rtvi-cv 2>&1 | grep 'Active sources'` then shows `Active sources : 4`. Upload the
two files again after any restart of `vss-behavior-analytics`.

### Step 7. Register the intersection

The traffic counts come in 10-minute windows of the clock (12:00, 12:10, ...), each about 3
minutes after it ends. Register when one whole window has passed since step 6 (cameras up at
12:04: after 12:23):

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

The first request works in the VSS chat (`https://$PUBLIC`) and through the agent API. The second
takes about 15 minutes and goes through the agent API: the chat page ends a request after 300 s
without data, and the agent sends none while a tool call runs.

```bash
curl -s 127.0.0.1:8000/generate -H 'content-type: application/json' \
  -d '{"messages": [{"role": "user", "content": "<request>"}]}' |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["value"].split("</agent-think>")[-1])'
```

Scheduled runs (every 600 s, SUMO traffic): `curl -s 127.0.0.1:8100/v1/city/scheduled/santa_clara_st/latest`.

## 3. Expected results

Against the current timing plan of step 3, over the 10-minute windows of the recordings:

| Request | Duration | Result |
| --- | --- | --- |
| Ordinary (both modes) | 1 to 2 minutes | answer: plan sentence, KPI table, NEMA splits, four download links (PDF report, plans, result); control delay 5 to 27 % lower |
| Neural NDE (MTR mode) | about 15 minutes | the same answer with the line "Traffic model: CNDE (Neural NDE, learned driver behavior) simulated every candidate plan."; no vehicle crosses on red; control delay within 5 % of SUMO's for the same plan |
| Scheduled | every 600 s, 1 to 2 minutes each | NEMA plan and KPIs at `/v1/city/scheduled/santa_clara_st/latest` |

Measured on 2026-10-28 with this guide: ordinary requests 82 to 88 s, control delay 5.7 % and 11.9 %
lower; Neural NDE request 867 s, 8.8 % lower; instrumented re-runs of its current and best plan:
none of 928 stop-line crossings on red, control delay 4.4 % and 3.1 % below SUMO's for the same
plans; scheduled runs 70 s.

## 4. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `dev-profile.sh up` stops: "Could not resolve the Brev secure link for port 7777" | `export BREV_PUBLIC_HOST=$PUBLIC` (step 5) |
| No answer in the chat for the Neural NDE request | use the agent API (step 8); the run still completes and is listed at `127.0.0.1:8100/v1/city/optimizations` |
| `Active sources : 0`, or the behavior analytics log shows other sensors | repeat step 6 |
| The answer says there is no traffic data yet | registered before a whole window had passed, or TeraSim restarted (its windows are kept in memory only); ask again after the next window |
| The agent answers that it cannot reach the intersection list or another TeraSim tool | TeraSim was restarted after VSS started, and the VSS agent does not reconnect to a restarted MCP server: `docker restart vss-agent` (about 20 s) |
| The instance was stopped and started | TeraSim starts by itself and keeps its plan and registration. Run steps 5 and 6 again, then `$C restart terasim-service` to drop the windows counted while the cameras were down (an optimization averages the windows of the last hour); traffic returns with the next window |
| A download link opens a Brev login page | the secure link is private to the Brev organization: open it in the browser that shows the VSS page |

## 5. Shutdown

In `$DEPLOY`: `$C down` stops TeraSim and keeps its plan and registration (`$C down -v` removes
them); `docker rm -f terasim-files-proxy`. To change the mode: `$C down`, the other mode's block of
step 3, `$C up -d`, `docker restart vss-agent`; traffic returns with the next window.
In `$VSS/deploy/docker`: `docker compose -p vss down --remove-orphans` stops VSS and keeps its
data (`./scripts/dev-profile.sh down` removes it). To detach the VSS agent from TeraSim, restore
`config.yml.bak` and `docker restart vss-agent`.

Neural NDE in its default mode (all vehicles predicted, about 39 minutes per request on one RTX PRO
6000) instead of the fast mode: in `compose.brev.yml` set `TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY:
"0"`, `TERASIM_CITY_CNDE_MAX_WORKERS: "6"` and `TERASIM_CITY_TIMEOUT_S: "3600"`, and run
`configure-vss.py` with `--tool-call-timeout 3600 --proxy-timeout 4200s` before step 5.
