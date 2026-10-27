# TeraSim Metropolis with NVIDIA Metropolis VSS on NVIDIA Brev

Reproduces the reference deployment, validated on VSS 3.2.1 and 3.3.0: VSS counts the traffic of
a representative downtown San José intersection from four recorded camera feeds, and TeraSim
optimizes its signal timing when asked through the VSS agent, every 10 minutes, and with Neural
NDE traffic on request.

## 1. Prerequisites

| Item | Value |
| --- | --- |
| Instance | NVIDIA Brev, AWS `g7e.12xlarge`: 2 × NVIDIA RTX PRO 6000 Blackwell Server Edition (96 GB), 48 vCPU, about 500 GiB memory, about 1 TB root disk; port 7777 shared in the Brev console |
| GPUs | GPU 1: VSS language and vision-language models; GPU 0: VSS computer vision and the Neural NDE trials |
| Neural NDE | fast mode (the default here), 8 trials at a time: 24 candidates in about 12 minutes; a 16 GB GPU is enough for TeraSim alone, 24 GB or more when it shares the GPU with the VSS computer vision. Default mode: about 39 minutes on one RTX PRO 6000 |
| Software | NVIDIA driver with CUDA 12.8 or newer, Docker with the NVIDIA Container Toolkit, Docker Compose v2, an NGC API key. VSS 3.2.1: Ubuntu 22.04 / VSS 3.3: VSS lists Ubuntu 24.04, NVIDIA driver 595.58.03 or newer, Docker 28.3.3 or newer and below 29.5.0, Docker Compose 2.39.1 or newer; validated on Ubuntu 22.04 with Docker 29.8.2 and Docker Compose 5.6.0 |
| VSS | NVIDIA Metropolis VSS 3.2.1 or 3.3.0: the `video-search-and-summarization` repository at that release, developer profile `alerts` with the smart-cities overlay of NVIDIA's smart-cities launchable notebook, not started yet |
| Inputs | the four delivered files (`README.md` of the package) and the San José recordings provided by NVIDIA (`cam1` to `cam4`) |

Warning: the deployment script of VSS 3.3 (`dev-profile.sh up`) deletes every Docker volume on the
machine that no container uses; on a machine with an existing deployment, back them up before upgrading.

## 2. Steps

### Step 1. Check, unpack and load the delivered files

In the folder that holds them:

```bash
sha256sum -c checksums/*.sha256
unzip -q terasim-metropolis-phase1-0.1.0.zip && unzip -q sj-reference-corridor-0.1.0.zip
docker load -i terasim-metropolis-0.1.0-images.tar.gz
W=$PWD PKG=$PWD/terasim-metropolis-phase1-0.1.0 SJ=$PWD/sj-reference-corridor-0.1.0
CORRIDOR=$(python3 -c "import json; print(json.load(open('$SJ/terasim/Metadata.json'))['corridor_id'])")
```

Or, from the public repository and image, in place of `terasim-metropolis-phase1-0.1.0.zip` and
the `docker load` line:

```bash
git clone https://github.com/SaferDrive-AI/Terasim-Metropolis.git terasim-metropolis
docker pull ghcr.io/saferdrive-ai/terasim-metropolis:0.1.0
docker tag ghcr.io/saferdrive-ai/terasim-metropolis:0.1.0 terasim-metropolis:0.1.0
```

In the steps below, `$PKG/source/terasim-metropolis` is then the clone and `$PKG/deployment`
its `deployment/` folder. The Neural NDE image (`terasim-metropolis:0.1.0-cnde`), the checkpoint
`nn_weights.pth` (in `$W`) and the San José reference corridor package (`$SJ`) are obtained
separately.

### Step 2. TeraSim deployment directory

```bash
mkdir -p ~/terasim/cnde_map ~/terasim/cnde_assets && cd ~/terasim
cp $PKG/source/terasim-metropolis/apps/deploy/city/phase1/{compose.yml,compose.cnde.yml} .
cp $PKG/deployment/brev/{compose.brev.yml,files-proxy.nginx.conf} . && cp $PKG/deployment/brev/env.brev.example .env
cp $SJ/terasim/{Metadata.json,scheduled_config.yaml} . && cp $SJ/map-package/* cnde_map/
cp $W/nn_weights.pth cnde_assets/
PUBLIC=<full host name of the address Brev shares for port 7777>
sed -i "s|<instance-private-ip>|$(hostname -I | awk '{print $1}')|; s|<public-vss-host>|$PUBLIC|" .env compose.brev.yml
```

### Step 3. Start TeraSim

```bash
C="docker compose -f compose.yml -f compose.cnde.yml -f compose.brev.yml"
$C up -d
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:8100/openapi.json       # 200
curl -s 127.0.0.1:8200/v1/tmc/health
$C exec terasim-service python -c "from terasim_city.factories.cnde_behavior import missing_cnde_runtime as r; from terasim_city.maps.cnde_artifacts import missing_cnde_map_artifacts as m; print(r() + m('/data/input/cnde_map/base.sumocfg') or 'ready')"
```

### Step 4. Current timing plan

```bash
curl -s -X POST 127.0.0.1:8100/v1/city/plans -H 'content-type: application/json' \
  --data-binary @$SJ/terasim/current_plan.nema_plan.json
```

### Step 5. Proxy of the download links

```bash
docker run -d --name terasim-files-proxy --network host --restart unless-stopped \
  -v ~/terasim/files-proxy.nginx.conf:/etc/nginx/nginx.conf:ro nginx:1.27-alpine
```

### Step 6. VSS files, before the first start of VSS

```bash
cd <VSS repository>/deploy/docker && P=developer-profiles/dev-profile-alerts
mkdir -p vss-nvstreamer-video-dir/videos/dev-profile-alerts
for n in 1 2 3 4; do cp <recording of camera n> vss-nvstreamer-video-dir/videos/dev-profile-alerts/cam$n.mp4; done
printf 'background\ntwo_wheeler\nVehicle\nPerson\nroad_sign\n' > $P/deepstream/configs/rtdetr-960x544-labels.txt
cp -r $SJ/vss/osm-cache $P/vss-behavior-analytics/
python3 - $P/vss-behavior-analytics/configs/vss-behavior-analytics-kafka-config.json $SJ/vss/behavior-analytics-graph.json <<'PY'
import json, sys
config, graph = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
config["coordinateReferenceSystem"]["roadNetwork"]["graph"].update(graph)
for item in config["app"]:
    if item["name"] == "mapMatchingClasses":
        classes = json.loads(item["value"])
        classes += [c for c in ("Vehicle", "Person", "two_wheeler") if c not in classes]
        item["value"] = json.dumps(classes, separators=(",", ":"))
json.dump(config, open(sys.argv[1], "w"), indent=2)
PY
L=services/infra/elk/logstash/pipelines/kafka/mdx-logstash.conf
grep -q receive_buffer_bytes $L || sed -i 's/^\(\s*\)consumer_threads => [0-9]*$/&\n\1receive_buffer_bytes => 8388608/' $L
cp $P/vss-agent/configs/config.yml $P/vss-agent/configs/config.yml.bak
```

Then edit three files:

- `$P/compose.yml`, service `vss-behavior-analytics-alerts`, under `volumes:`, add
  `- $VSS_APPS_DIR/developer-profiles/dev-profile-alerts/vss-behavior-analytics/osm-cache:/behavior-analytics/cache`
- `services/infra/haproxy/haproxy.cfg.template`: `timeout client` and `timeout server` from the
  timeouts table below, and:

```text
# after the existing backends
backend bk_terasim_files
    server s1 "${HOST_IP}:13002" check

# in frontend fe_http, before the /phoenix rules
    acl p_terasim path_beg /terasim/
    use_backend bk_terasim_files if h_main p_terasim
```

- `$P/vss-agent/configs/config.yml`: items 1 to 6 of "VSS agent configuration" in
  `$PKG/source/terasim-metropolis/docs/deployment/vss-integration.md`, with
  `url: http://<instance-private-ip>:9911/mcp` (the address of step 2) and `tool_call_timeout`
  from the timeouts table.

| Timeout of a Neural NDE request (measured tool call: 705 to 716 s) | File | Value |
| --- | --- | --- |
| `tool_call_timeout` of the `terasim_mcp` tool group | VSS agent `config.yml` | `1800` |
| `TERASIM_CITY_TIMEOUT_S` | `~/terasim/compose.brev.yml` | `1800` |
| `timeout client`, `timeout server` | `haproxy.cfg.template` | `2100s` |

Default (full prediction) mode instead of the fast mode: in `compose.brev.yml` set
`TERASIM_CITY_CNDE_PREDICT_CONTROL_ONLY: "0"` and `TERASIM_CITY_CNDE_MAX_WORKERS: "6"`, and raise
the three timeouts to `3600`, `3600` and `4200s` (about 39 minutes per request on one RTX PRO 6000).

### Step 7. Start VSS

```bash
export BREV_PUBLIC_HOST=$PUBLIC BREV_LINK_DOMAIN=${PUBLIC#*.}
./scripts/dev-profile.sh up --profile alerts --mode verification -H RTXPRO6000BW
```

VSS 3.2.1: uses `BREV_LINK_DOMAIN` / VSS 3.3: uses `BREV_PUBLIC_HOST`, the full host name of step 2.

### Step 8. Cameras, calibration and road network

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
docker logs vss-behavior-analytics 2>&1 | grep -E 'Road network created|Loaded 4 sensors'
```

Upload the two files again after any restart of `vss-behavior-analytics`.

### Step 9. Register the intersection

After the first complete 10-minute window of the cameras:

```bash
curl -s -X POST 127.0.0.1:8200/v1/tmc/intersections -H 'content-type: application/json' \
  --data-binary @$SJ/terasim/register-intersection.json
curl -s 127.0.0.1:8100/v1/city/snapshots/$CORRIDOR      # one snapshot per 10-minute window
curl -s 127.0.0.1:8000/mcp/client/tool/list             # lists the terasim_mcp tools
```

### Step 10. Requests

Send the two requests of `$SJ/README.md` to the VSS agent. The ordinary request works in the VSS
chat (the address Brev shares for port 7777) and through the agent API. The Neural NDE request
(about 12 minutes) goes through the agent API: the chat page ends a request after 300 s without
data, and the agent sends none while a tool call runs.

```bash
curl -s 127.0.0.1:8000/generate -H 'content-type: application/json' \
  -d '{"messages": [{"role": "user", "content": "<request>"}]}' |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["value"].split("</agent-think>")[-1])'
```

Scheduled runs: `curl -s 127.0.0.1:8100/v1/city/scheduled/$CORRIDOR/latest`.

## 3. Expected results

Against the current timing plan of step 4:

| Request | Duration | Result |
| --- | --- | --- |
| Ordinary | about 2 minutes | control delay about 5 to 27 % lower; answer with plan, KPI table, NEMA splits and download links |
| Neural NDE (CNDE), fast mode | about 12 minutes | no vehicle crosses on red; control delay within 3 % of SUMO's for the same plan; answer line "Traffic model: CNDE (Neural NDE, learned driver behavior) simulated every candidate plan." |
| Scheduled | every 600 s, 1 to 2 minutes each | NEMA plan and KPIs at `/v1/city/scheduled/<corridor>/latest` |

Ranges over the 10-minute windows of the recordings, VSS 3.2.1 and 3.3.0
(`evidence/brev-reference-2026-10-23/`, `evidence/vss-reference-deployment/`,
`evidence/vss-3.3.0-2026-10-26/`).

## 4. Shutdown and rollback

In `~/terasim`: `docker compose -f compose.yml -f compose.cnde.yml -f compose.brev.yml down` stops
TeraSim, `docker compose -f compose.yml -f compose.brev.yml up -d` runs it with SUMO traffic only;
restoring `config.yml.bak` and `docker restart vss-agent` detaches VSS.
