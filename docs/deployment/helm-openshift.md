# Helm on OpenShift

The Helm chart `apps/deploy/city/helm/terasim-phase1` (chart version 0.6.0, app version 0.1.0)
deploys the TeraSim Metropolis side of a VSS integration on OpenShift or Kubernetes. VSS itself
is deployed separately.

## What it deploys

One image, three Deployments and a Redis:

| Component | Command | Port | Role |
| --- | --- | --- | --- |
| `<release>-service` | default | 8000 | REST API `/v1/city/*`, optimization runs |
| `<release>-mcp` | `terasim-agent-skill` | 9911 | MCP `/mcp` for the VSS agent, downloads at `/files` |
| `<release>-tmc` | `terasim-tmc-service` | 8200 | TMC collector pulling the VSS behavior API |
| `<release>-redis` | `redis-server` (from the same image by default) | 6379 | Snapshot buffer and job store of the service, password-protected |

Plus three PersistentVolumeClaims (service output, MCP artifacts, TMC data), a ConfigMap with
the corridor configuration (`configs/scheduled_config.yaml`) and the TMC corridor specification
(`configs/Metadata.json`), a Secret with the Redis password, one NetworkPolicy per component
and, on OpenShift, a Route that exposes only `/files` (edge TLS, HTTP redirected to HTTPS).

## Prerequisites

1. OpenShift 4.19 or later with `oc` logged in as namespace admin, and Helm 3. Plain
   Kubernetes works too (no Route; see [Kubernetes](#plain-kubernetes)).
2. **No SCC grant.** The image runs as a non-root user with an arbitrary uid in group 0, and
   the chart sets a `restricted-v2` compatible security context on every pod: non-root, all
   capabilities dropped, no privilege escalation, `RuntimeDefault` seccomp, read-only root
   filesystem with `emptyDir` volumes at `/tmp` and `$HOME`.
3. **The image** `<registry>/terasim-metropolis:0.1.0`, pullable by the cluster: an existing
   pull Secret in `global.imagePullSecrets`, or `pullSecret.create=true` with inline
   credentials.
4. **A StorageClass** for the PVCs (`global.storageClass`, empty for the cluster default).
5. **NetworkPolicy enforcement** by the cluster network plugin (OVN-Kubernetes and OpenShift SDN
   enforce it).

## Install

```bash
oc new-project terasim

helm upgrade --install terasim-phase1 apps/deploy/city/helm/terasim-phase1 \
  -n terasim \
  --set image.repository=<registry>/terasim-metropolis \
  --set image.tag=0.1.0 \
  --set global.imagePullSecrets[0].name=<pull-secret> \
  --set global.storageClass=<rwo-storageclass> \
  --set global.externalHost=terasim.apps.<cluster-domain> \
  --set tmc.vssBehaviorUrl=http://vss-video-analytics-api.<vss-namespace>.svc:8081
```

Two values are required per environment; `helm template` / `helm lint` fail fast without them:

- `tmc.vssBehaviorUrl`: the in-cluster URL of the VSS video-analytics API Service (port 8081)
  that the TMC collector pulls. Its `<vss-namespace>` also tells the NetworkPolicy which
  namespace may call the MCP server.
- `global.externalHost`: the host of the download Route, for example
  `terasim.apps.<cluster-domain>`; download links then read
  `https://<externalHost>/files/<artifact_id>/<file>`. Without a Route, set
  `route.enabled=false` and, when readers need the download links, `mcp.publicUrl` (see
  [Plain Kubernetes](#plain-kubernetes)).

Use the release name `terasim-phase1` (or set `fullnameOverride`) for short object names:
`terasim-phase1-service`, `terasim-phase1-mcp`, and so on.

Verify:

```bash
oc get pods -n terasim -l app.kubernetes.io/instance=terasim-phase1
helm test terasim-phase1 -n terasim
```

`helm test` checks that the service and the TMC collector answer, that the MCP port accepts
connections and that Redis refuses an unauthenticated `PING`. The chart's `NOTES` print the
in-cluster MCP URL, the download URL and the Redis Secret.

## Main values

| Value | Default | Meaning |
| --- | --- | --- |
| `image.repository`, `image.tag`, `image.pullPolicy` | `terasim-metropolis`, `"0.1.0"`, `IfNotPresent` | The image (one image for every component); set the repository to `<registry>/terasim-metropolis` |
| `global.externalHost`, `global.externalScheme` | `""`, `https` | Download Route host; scheme of the download links |
| `global.imagePullSecrets`, `global.storageClass` | `[]`, `""` | Pull secrets; StorageClass of every PVC |
| `service.windowS` | `3600` | `TERASIM_CITY_WINDOW_S`: seconds of snapshots one optimization aggregates |
| `service.maxConcurrency` | `8` | `TERASIM_CITY_MAX_CONCURRENCY`: worker slots (one SUMO process each); size `service.resources` to match |
| `service.osmDownload.enabled` | `true` | `TERASIM_CITY_OSM_DOWNLOAD`; `false` when maps ship as files and the cluster has no Internet access |
| `service.storage.size` | `5Gi` | Service PVC: run history, map store, plan store, audit log |
| `scheduler.enabled`, `scheduler.cadenceSeconds` | `false`, `3600` | Built-in scheduler (`TERASIM_CITY_SCHEDULER_ENABLED`, `TERASIM_CITY_CADENCE_S`) |
| `simMetrics.enabled` | `true` | Simulated signal-performance measures after every run (`TERASIM_CITY_SIM_METRICS`) |
| `audit.enabled` | `true` | Audit log endpoints (`TERASIM_CITY_AUDIT_ENABLED`) |
| `mcp.heartbeatS` | `"2.0"` | Seconds between progress notifications of a running tool call |
| `mcp.publicUrl` | `""` | Base of the download links; empty derives it from the Route |
| `mcp.artifacts.size` | `2Gi` | MCP artifacts PVC |
| `tmc.vssBehaviorUrl` | required | VSS behavior API URL |
| `tmc.settleLagS` | `180` | Seconds a window must have ended before it is counted |
| `tmc.countedObjectTypes` | `""` (`Vehicle,two_wheeler`) | Detector labels counted as traffic |
| `redis.auth.enabled`, `redis.auth.existingSecret`, `redis.auth.password` | `true`, `""`, `""` | Redis password: generated, explicit, or from your Secret |
| `redis.image` | `""` | Empty runs the `redis-server` of the TeraSim image; set to use another image |
| `route.enabled`, `route.pathPrefix` | `true`, `""` | The `/files` Route |
| `networkPolicy.enabled`, `networkPolicy.mcpClientNamespaces` | `true`, `[]` | Per-component ingress policies; the VSS agent's namespace(s) |
| `coScheduleServiceAndMcp` | `true` | Schedules the service and the MCP pods on one node (they share the output PVC on ReadWriteOnce storage) |
| `corridorConfig.metadataJson`, `corridorConfig.scheduledConfigYaml`, `corridorConfig.existingConfigMap` | `""`, `""`, `""` | The corridor files (see [Corridor configuration](#corridor-configuration)); empty = the files shipped in the chart |

The switches are YAML booleans (`--set scheduler.enabled=true`); the chart rejects other values.

## Corridor configuration

The ConfigMap mounted at `/config` carries the two corridor files:

- `scheduled_config.yaml`, the corridor entries of the service (see
  [Configuration system](../developer-guide/configuration.md#corridor-configuration)): map,
  signals and optimization budget.
- `Metadata.json`, the TMC corridor specification of the cameras (see
  [VSS integration](vss-integration.md#corridor-specification-metadatajson)).

As shipped (`configs/` in the chart) they configure the synthetic demo corridor
`demo_arterial` on the map baked into the image, with intersections `i1`, `i2`, `i3` and four
cameras each. For another corridor, set the files through values (no new chart or image):

| Value | Default | Meaning |
| --- | --- | --- |
| `corridorConfig.metadataJson` | `""` | The specification's text (`--set-file corridorConfig.metadataJson=Metadata.json`); empty = the shipped file |
| `corridorConfig.scheduledConfigYaml` | `""` | The scheduled configuration's text (`--set-file corridorConfig.scheduledConfigYaml=scheduled_config.yaml`); empty = the shipped file |
| `corridorConfig.existingConfigMap` | `""` | A ConfigMap you manage (keys `Metadata.json`, `scheduled_config.yaml`), mounted instead; the chart renders no ConfigMap of its own |

```bash
helm upgrade --install terasim-phase1 apps/deploy/city/helm/terasim-phase1 -n terasim \
  -f values-site.yaml -f generated/terasim/values-corridor.yaml
```

An upgrade that changes the files restarts the service and TMC pods (pod annotation
`checksum/corridor-config`); with `existingConfigMap`, restart both Deployments after changing
the ConfigMap. The chart fails fast on files the services would refuse at start-up. A map the
image does not bake is registered at run time (`upload_map`, `POST /v1/city/maps`, or the
generator's `register-map.sh`), and new intersections are registered with the TMC collector.
The camera inventory generator `apps/deploy/city/camera-config/camcfg.py` writes the values
file, the ConfigMap and the registration scripts from one inventory; see
[Corridor and camera configuration](corridor-configuration.md).

## Redis authentication

The bundled Redis requires a password kept in a Secret. The Redis container reads it from an
environment variable into a `0600` file on its `/tmp` volume; the service reads it as
`TERASIM_REDIS_PASSWORD`. The MCP server and the TMC collector use the service's REST API and
hold no Redis client.

| Value | Default | Meaning |
| --- | --- | --- |
| `redis.auth.enabled` | `true` | Password protection |
| `redis.auth.existingSecret` | `""` | Name of a Secret you manage (recommended with GitOps tools) |
| `redis.auth.secretKey` | `redis-password` | Key inside the Secret |
| `redis.auth.password` | `""` | Explicit password for the chart-created Secret; empty generates 32 random characters |

The chart-created Secret `<release>-redis-auth` is generated at the first install, reused on
every upgrade and kept on uninstall, so a re-install keeps the password. To rotate it, change
the value (or your Secret), then restart Redis and the service together (the snapshot buffer is
ephemeral and refills with the next TMC window):

```bash
oc rollout restart deploy/terasim-phase1-redis deploy/terasim-phase1-service -n terasim
```

## NetworkPolicies

Four ingress policies, one per component:

| Policy | Allows ingress from | Port |
| --- | --- | --- |
| `<release>-redis` | the components in `networkPolicy.redisClients` (default: the service) | 6379 |
| `<release>-service` | pods in the release namespace | 8000 |
| `<release>-tmc` | pods in the release namespace | 8200 |
| `<release>-mcp` | pods in the release namespace, the VSS agent's namespace(s), and the OpenShift router for the `/files` Route | 9911 |

The VSS namespace is `networkPolicy.mcpClientNamespaces`, or derived from `tmc.vssBehaviorUrl`
when that is an in-cluster Service URL (`http://<svc>.<namespace>.svc:<port>`).
`networkPolicy.extraIngressFrom.<component>` appends further peers (for example a monitoring
namespace). Egress is not restricted: the TMC collector calls the VSS namespace, the service may
download OpenStreetMap data, and every pod needs DNS.

## Connect the VSS agent

Point the VSS agent's MCP client at the in-cluster MCP Service, and configure the agent as in
[VSS integration](vss-integration.md):

```text
http://terasim-phase1-mcp.<terasim-namespace>.svc:9911/mcp
```

## Plain Kubernetes

The chart renders the Route only on clusters that serve `route.openshift.io/v1`. On other
clusters set `route.enabled=false` and, when readers need the download links, expose the MCP
Service's `/files` path with your own ingress and set `mcp.publicUrl` to its address. The
synchronous optimization can take minutes: give any proxy in front of the service or the MCP
server read timeouts of at least 1,800 s. If new PVCs are not group-writable on your storage,
set `podSecurityContext.fsGroup` (for example `1001`); on OpenShift, where the SCC assigns them,
do not set `runAsUser` or `fsGroup`.
