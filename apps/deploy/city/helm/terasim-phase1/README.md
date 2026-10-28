# terasim-phase1 — Helm chart (OpenShift-ready)

Deploys the TeraSim Metropolis signal-optimization stack that the NVIDIA Metropolis
VSS (Video Search and Summarization) agent calls over MCP. **VSS itself is deployed
separately by the operator** (this chart is only the TeraSim side). It follows the
conventions of NVIDIA's OpenShift VSS chart (`global.externalHost/externalScheme`,
Route with edge TLS, no hardcoded namespace) and runs under the default
`restricted-v2` SCC.

Chart version `0.6.1`, application version (image tag) `0.2.0`.

## What it deploys

One image (`terasim-metropolis`), three commands + a bundled Redis:

| Component | Command | Port | Role |
| --- | --- | --- | --- |
| terasim-service | (default) | 8000 | REST `/v1/city/*` + SUMO/Bayesian evaluator |
| terasim-mcp | `terasim-agent-skill` | 9911 | MCP `/mcp` for the VSS agent + `/files` downloads |
| terasim-tmc | `terasim-tmc-service` | 8200 | periodic TMC generator (pulls VSS behavior) |
| redis | `redis-server` (same image by default) | 6379 | terasim-service's snapshot buffer / job store (ephemeral, password-protected) |

Plus PVCs (service output, mcp artifacts, tmc data), a ConfigMap
(`scheduled_config.yaml` + `Metadata.json`: the corridor, see "Corridor
configuration"), a Secret holding the Redis password, four NetworkPolicies (one per component) and an OpenShift **Route**
exposing only `/files` (edge TLS, HTTP→HTTPS redirect).

## Prerequisites

1. OpenShift 4.19+, `oc` logged in with namespace-admin, Helm 3.
2. **SCC** — none to grant. The image runs as a non-root user with an arbitrary uid
   in group 0, and the chart sets a restricted-v2-compatible security context on
   every pod (runAsNonRoot, all capabilities dropped, no privilege escalation,
   RuntimeDefault seccomp, read-only root filesystem with emptyDir scratch at
   `/tmp` and `$HOME`).
   ```bash
   oc new-project terasim
   ```
   An `anyuid` grant does not break anything (the pods then run as the image's
   uid 1001).
3. **Image** must be reachable from the cluster (push `terasim-metropolis:0.2.0` to
   a registry the cluster can pull from, or import it). Provide pull access via
   `global.imagePullSecrets` (an existing Secret) or `pullSecret.create=true` with
   inline credentials.
4. **Maps** — the image bakes the synthetic demo corridor's SUMO map at
   `/data/input/map` (`demo.sumocfg`: an E-W arterial with the signals J1, J2 and
   J3) and a demo scenario at `/data/input/demo_scenario`. Other maps are
   registered at run time (`POST /v1/city/maps`, MCP `upload_map`) or downloaded
   from OpenStreetMap when no stored map covers a request
   (`service.osmDownload.enabled`).
5. **NetworkPolicy enforcement** is provided by the cluster's CNI
   (OVN-Kubernetes and OpenShift SDN enforce it; a CNI without NetworkPolicy
   support accepts the objects but does not enforce them). Nothing to install.

## Install

```bash
helm upgrade --install terasim-phase1 apps/deploy/city/helm/terasim-phase1 \
  -n terasim --create-namespace \
  --set image.repository=<registry>/terasim-metropolis --set image.tag=0.2.0 \
  --set global.imagePullSecrets[0].name=<pull-secret> \
  --set global.storageClass=<rwo-storageclass> \
  --set global.externalHost=terasim.apps.<cluster-domain> \
  --set tmc.vssBehaviorUrl=http://vss-video-analytics-api.<vss-namespace>.svc:8081
```

Use release name `terasim-phase1` (or set `fullnameOverride`) for clean object
names; otherwise names read `<release>-terasim-phase1-*`.

**Required per environment:** `tmc.vssBehaviorUrl` (the operator's VSS
`vss-video-analytics-api` Service, port 8081) and, for download links,
`global.externalHost`. `helm install`/`upgrade`/`template` fail fast with a message
naming the value when one is missing (`helm lint` lists the same messages).
The `<vss-namespace>` inside `tmc.vssBehaviorUrl` also tells the NetworkPolicy
which namespace may call the MCP (see below); set
`networkPolicy.mcpClientNamespaces` explicitly if the VSS agent runs elsewhere.

## Corridor configuration (corridorConfig)

The service and the TMC collector read the corridor from two files, mounted read-only
at `/config`:

| File | Read by | Content |
| --- | --- | --- |
| `Metadata.json` | terasim-tmc (`TMC_SPEC_FILE`) | the corridor spec in the contract of NVIDIA's reference TMC script: per intersection its location, `Headings` (road segment of the behavior objects → direction of travel), `Movements` (heading pair → turn) and `Assignments` (approach → camera sensor) |
| `scheduled_config.yaml` | terasim-service (`TERASIM_CITY_SCHEDULED_CONFIG`) | per corridor its map (`base_sumocfg`), signals (`corridor.tls_ids`) and optimization budget; the synchronous optimize call uses the entry's values as defaults, the built-in scheduler optimizes every entry |

By default both are the files shipped in the chart's `configs/`: the synthetic demo
corridor `demo_arterial` on the baked map, intersections `i1`, `i2` and `i3` (signals
`J1`, `J2`, `J3`), four camera sensors each (`i1-cam1` … `i3-cam4`), and runs that
simulate 30 minutes (`sim_end: 1800`) with up to 24 trials on 4 SUMO workers. Another
corridor, more intersections or more cameras need neither a new chart nor a new image:

| Value | Default | Meaning |
| --- | --- | --- |
| `corridorConfig.metadataJson` | `""` | the spec's text (`--set-file corridorConfig.metadataJson=Metadata.json`); empty = the shipped file |
| `corridorConfig.scheduledConfigYaml` | `""` | the scheduled configuration's text (`--set-file corridorConfig.scheduledConfigYaml=scheduled_config.yaml`); empty = the shipped file |
| `corridorConfig.existingConfigMap` | `""` | name of a ConfigMap you manage (keys `Metadata.json` and `scheduled_config.yaml`), mounted instead; the chart then renders no ConfigMap of its own |

The corridor id of `Metadata.json` must be a key of `scheduled_config.yaml` (the install
notes warn otherwise). `apps/deploy/city/camera-config/camcfg.py` generates both files
from one camera inventory (corridor → intersections → cameras), together with
`values-corridor.yaml` (these values, for `-f`), `corridor-configmap.yaml` (for
`existingConfigMap`), the script that registers the intersections with the TMC
collector, the VST stream registration and the VA-API calibration entries; see
`apps/deploy/city/camera-config/README.md`.

```bash
helm upgrade --install terasim-phase1 apps/deploy/city/helm/terasim-phase1 -n terasim \
  -f values-site.yaml -f generated/terasim/values-corridor.yaml
```

The service and tmc pods carry the files' checksum (`checksum/corridor-config`), so an
upgrade that changes them restarts both pods, which read the files at start-up. With
`existingConfigMap` the chart cannot see the content: restart both Deployments after
changing it (the install notes print the command). `helm install`/`upgrade`/`template`/
`lint` fail on files the services would refuse at start-up: not JSON / YAML, no corridor
id, an empty intersection list or table, an `Assignments` heading without a `Movements`
row, a corridor without `base_sumocfg` or `corridor`, or more than 1 MiB together. New
intersections are counted once they are registered with the TMC collector
(`register-intersections.sh` from the generator, or `POST /v1/tmc/intersections`).

## Scheduled runs, simulated signal metrics, the audit log and map downloads

Rendered into terasim-service's environment:

| Value | Default | Environment variable | Meaning |
| --- | --- | --- | --- |
| `scheduler.enabled` | `false` | `TERASIM_CITY_SCHEDULER_ENABLED` | `true`: the service optimizes every corridor of the corridor configuration (`scheduled_config.yaml`) once per cadence when it has recent traffic counts for it. `false`: runs start on request (the synchronous optimize call, which the VSS agent can also invoke on its own cadence, or the asynchronous API). |
| `scheduler.cadenceSeconds` | `3600` | `TERASIM_CITY_CADENCE_S` | Seconds between two scheduling passes (a number > 0); hourly by default. |
| `simMetrics.enabled` | `true` | `TERASIM_CITY_SIM_METRICS` | Simulated signal-performance measures of every completed run (baseline and optimized plan re-simulated with virtual detectors: two extra SUMO runs, holding one of the `service.maxConcurrency` worker slots while they run), served at `/v1/city/optimizations/<run_id>/sim_metrics`. `false`: not computed (that endpoint answers 404). |
| `service.osmDownload.enabled` | `true` | `TERASIM_CITY_OSM_DOWNLOAD` | `true`: when a request names no map and no shipped or registered map covers its area, the service downloads OpenStreetMap data for it from Overpass (its only outbound call). `false`: never downloaded; such a request gets 422 naming the setting, while shipped (`/data/input`) and registered (`POST /v1/city/maps`) maps keep working. Set `false` when the maps ship as files. |
| `audit.enabled` | `true` | `TERASIM_CITY_AUDIT_ENABLED` | Audit log of operator activity: feedback on plans (accept / reject / needs changes, optional comment), plan views and downloads and client sessions, recorded at `POST /v1/city/audit/events` with the user an authenticating proxy names (`X-Forwarded-User` / `X-Forwarded-Email` / `X-Remote-User`) or the name the client sends; reviewed at `GET /v1/city/audit/events`, exported at `/v1/city/audit/events.csv`, summarized at `/v1/city/audit/summary`. Kept on the output PVC as `/data/output/audit/events-YYYY-MM.jsonl` (append-only JSON Lines; nothing is pruned automatically). `false`: those endpoints answer 404. |

The switches are YAML booleans (`--set scheduler.enabled=true`, not
`--set-string`); `helm install`/`template` fail on any other value.

## Redis authentication

The bundled Redis starts with `requirepass`. The password lives in a Kubernetes
Secret and reaches the pods only as `secretKeyRef` environment variables: the
Redis container's launcher writes it into a `0600` config file on its `/tmp`
emptyDir (so it is neither in the pod spec nor on the process command line) and
`exec`s `redis-server` as PID 1; `terasim-service` reads `TERASIM_REDIS_PASSWORD`
(`terasim_service.utils.base.redis_connection_kwargs_from_env`). `terasim-mcp`
and `terasim-tmc` hold no Redis client — they use the service's REST API — and
get no password. The image's embedded single-container Redis (loopback only) is
not used by the chart.

| Value | Default | Meaning |
| --- | --- | --- |
| `redis.auth.enabled` | `true` | `false` renders an unauthenticated Redis (not recommended; NOTES warns) |
| `redis.auth.existingSecret` | `""` | Name of a Secret you manage; the password is read from key `redis.auth.secretKey`. Recommended for GitOps. |
| `redis.auth.secretKey` | `redis-password` | Key inside the Secret (yours or the chart's) |
| `redis.auth.password` | `""` | Explicit password for the chart-created Secret (`--set-string`; no newlines). Empty → generated. |

Chart-managed Secret (`<release>-redis-auth`): generated at the first install
(32 random alphanumerics), **reused on every `helm upgrade`** (the chart looks
the existing Secret up) and **kept on `helm uninstall`**
(`helm.sh/resource-policy: keep`), so a re-install of the same release keeps the
same password; delete the Secret by hand to start over. `helm template` /
`--dry-run` cannot look anything up and print a fresh random value — expected.
GitOps tools that render offline (Argo CD) would see a new password on every
sync: use `redis.auth.existingSecret` there.

Read the password / rotate it:
```bash
oc get secret terasim-phase1-redis-auth -n terasim -o jsonpath='{.data.redis-password}' | base64 -d
# rotate: set a new value (helm upgrade ... --set-string redis.auth.password=<new>, or
# edit your own Secret), then restart both consumers together:
oc rollout restart deploy/terasim-phase1-redis deploy/terasim-phase1-service -n terasim
```
Redis reads the password at start-up and the service when it connects, so the
two Deployments restart together; the buffer is ephemeral and refills with the
next TMC window.

`helm test` includes a negative check: an unauthenticated `PING` from the test
pod must not be answered with `PONG` (it is either blocked by the NetworkPolicy
or answered `NOAUTH`).

## NetworkPolicy

Four ingress-only policies (`networkPolicy.enabled`, default `true`; rendered
when the cluster has `networking.k8s.io/v1`):

| Policy (selects) | Allows ingress from | Port |
| --- | --- | --- |
| `<release>-redis` | the TeraSim pods listed in `networkPolicy.redisClients` (default `["service"]`) | 6379 |
| `<release>-service` | any pod in the release namespace (terasim-mcp, terasim-tmc, `helm test`) | 8000 |
| `<release>-tmc` | any pod in the release namespace | 8200 |
| `<release>-mcp` | any pod in the release namespace; the VSS agent's namespace(s); when `route.enabled`, the OpenShift router that terminates the `/files` Route | 9911 |

- **VSS namespace**: `networkPolicy.mcpClientNamespaces` (namespace names,
  matched on the `kubernetes.io/metadata.name` label). When empty, the chart
  derives it from `tmc.vssBehaviorUrl` if that is an in-cluster Service URL
  (`http://<svc>.<namespace>.svc[.cluster.local]:<port>`), i.e. the namespace
  the VSS chart is installed in. NOTES prints what was resolved and warns when
  nothing was (then only same-namespace pods can reach `/mcp`).
- **Router**: `networkPolicy.routerNamespaceSelectors` defaults to the OpenShift
  `network.openshift.io/policy-group: ingress` namespace label plus
  `policy-group.network.openshift.io/host-network: ""` (Ingress Controllers on
  the host network under OVN-Kubernetes).
- **Extra peers**: `networkPolicy.extraIngressFrom.<redis|service|mcp|tmc>`
  takes verbatim `NetworkPolicyPeer` entries (e.g. a monitoring namespace).
- **Egress is left open** (no `Egress` policyType): terasim-tmc pulls the VSS
  behavior API in the operator's VSS namespace, terasim-service may fetch
  OpenStreetMap extracts from Overpass when no stored map covers a request, and
  every pod needs cluster DNS; those targets differ per environment. A
  default-deny egress policy with explicit allows for the VSS Service, DNS and (if
  used) the Overpass endpoint can be layered on by the operator.
- Kubelet readiness/liveness probes are not subject to NetworkPolicy on the
  OpenShift CNIs, so the probes keep working.

## VSS agent wiring (operator side)

Point the VSS agent's `terasim_mcp` group at the in-cluster MCP Service:
```
http://terasim-phase1-mcp.terasim.svc:9911/mcp
```
Then apply the `config.yml` additions (tool group, prompt steps 1–4, response
format rule) described in `VSS-WIRING.md` (bundled with the chart).

## Verify

```bash
oc get pods -n terasim -l app.kubernetes.io/instance=terasim-phase1
oc get networkpolicy,secret -n terasim -l app.kubernetes.io/instance=terasim-phase1
helm test terasim-phase1 -n terasim
```

`helm test` checks that terasim-service and terasim-tmc answer over HTTP, that
terasim-mcp accepts TCP connections, and that Redis refuses an unauthenticated
`PING`.

## Security context and Redis image

`podSecurityContext` / `securityContext` in `values.yaml` are the restricted-v2
defaults; do not add `runAsUser`/`fsGroup` on OpenShift (the SCC assigns them per
namespace). On plain Kubernetes, if new PVCs are not group-writable, set
`podSecurityContext.fsGroup` (e.g. `1001`).

The bundled Redis runs the `redis-server` shipped in the TeraSim image
(`redis.image: ""`, the default): one image to pull, scanned with the release,
non-root. To use an external Redis image instead set `redis.image` (e.g.
`redis:7-alpine`); that image has no numeric `USER`, so outside restricted-v2
(plain Kubernetes, or an `anyuid` grant) also set `redis.runAsUser` (`999` for
the official image). The launcher script only needs `/bin/sh`, `sed` and
`redis-server` from that image.

## Storage note

`terasim-service` (RW) and `terasim-mcp` (RO) share the output PVC so the MCP can
read the plan store for the PDF report. On ReadWriteOnce storage the two pods are
pinned to the same node (`coScheduleServiceAndMcp: true`, the default). If
`global.storageClass` is ReadWriteMany, set it `false`.
