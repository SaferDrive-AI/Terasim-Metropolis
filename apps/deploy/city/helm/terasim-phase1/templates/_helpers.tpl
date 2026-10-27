{{- define "terasim-phase1.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "terasim-phase1.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Per-component resource name: <fullname>-<component> (service|mcp|tmc|redis). */}}
{{- define "terasim-phase1.componentName" -}}
{{- printf "%s-%s" (include "terasim-phase1.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "terasim-phase1.chartLabel" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/* selectorLabels for one component; call with (dict "root" . "component" "mcp"). */}}
{{- define "terasim-phase1.selectorLabels" -}}
app.kubernetes.io/name: {{ include "terasim-phase1.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* full labels for one component. */}}
{{- define "terasim-phase1.labels" -}}
{{ include "terasim-phase1.chartLabel" .root }}
{{ include "terasim-phase1.selectorLabels" . }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/part-of: terasim-phase1
{{- end -}}

{{- define "terasim-phase1.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "terasim-phase1.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "terasim-phase1.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{/* In-cluster hostnames of the three services + redis (component names). */}}
{{- define "terasim-phase1.serviceHost" -}}{{ printf "%s-service" (include "terasim-phase1.fullname" .) }}{{- end -}}
{{- define "terasim-phase1.mcpHost" -}}{{ printf "%s-mcp" (include "terasim-phase1.fullname" .) }}{{- end -}}
{{- define "terasim-phase1.tmcHost" -}}{{ printf "%s-tmc" (include "terasim-phase1.fullname" .) }}{{- end -}}
{{- define "terasim-phase1.redisHost" -}}{{ printf "%s-redis" (include "terasim-phase1.fullname" .) }}{{- end -}}

{{/* External base URL from global.externalScheme + externalHost (no trailing slash). Empty if no host. */}}
{{- define "terasim-phase1.externalBase" -}}
{{- $h := .Values.global.externalHost | default "" | trim -}}
{{- if $h -}}
{{- printf "%s://%s" (.Values.global.externalScheme | default "https") $h -}}
{{- end -}}
{{- end -}}

{{/* MCP public URL for download links: explicit mcp.publicUrl, else externalBase + route.pathPrefix, else empty. */}}
{{- define "terasim-phase1.mcpPublicUrl" -}}
{{- if .Values.mcp.publicUrl -}}
{{- .Values.mcp.publicUrl | trimSuffix "/" -}}
{{- else -}}
{{- $base := include "terasim-phase1.externalBase" . -}}
{{- if $base -}}
{{- printf "%s%s" $base (.Values.route.pathPrefix | default "" | trimSuffix "/") -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* imagePullSecrets block from global.imagePullSecrets + optional created pull secret. */}}
{{- define "terasim-phase1.imagePullSecrets" -}}
{{- $secrets := .Values.global.imagePullSecrets | default (list) -}}
{{- if .Values.pullSecret.create -}}
{{- $secrets = append $secrets (dict "name" .Values.pullSecret.name) -}}
{{- end -}}
{{- if $secrets }}
imagePullSecrets:
{{- range $secrets }}
  - name: {{ .name }}
{{- end }}
{{- end -}}
{{- end -}}

{{/* Redis auth: the Secret holding the password (operator-provided or chart-created). */}}
{{- define "terasim-phase1.redisAuthSecretName" -}}
{{- if .Values.redis.auth.existingSecret -}}
{{- .Values.redis.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-redis-auth" (include "terasim-phase1.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Password written into the chart-created Secret: redis.auth.password when set;
otherwise the value already stored in the cluster (lookup, so every upgrade
keeps it); otherwise a fresh random one (first install — and every
`helm template` / `--dry-run`, where lookup sees no cluster).
*/}}
{{- define "terasim-phase1.redisAuthPassword" -}}
{{- if .Values.redis.auth.password -}}
{{- .Values.redis.auth.password -}}
{{- else -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace (include "terasim-phase1.redisAuthSecretName" .) -}}
{{- $current := "" -}}
{{- if $existing -}}
{{- $current = index (default (dict) $existing.data) .Values.redis.auth.secretKey | default "" -}}
{{- end -}}
{{- if $current -}}
{{- $current | b64dec -}}
{{- else -}}
{{- randAlphaNum 32 -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* env valueFrom block for the Redis password (call with the root context). */}}
{{- define "terasim-phase1.redisAuthSecretKeyRef" -}}
valueFrom:
  secretKeyRef:
    name: {{ include "terasim-phase1.redisAuthSecretName" . }}
    key: {{ .Values.redis.auth.secretKey }}
{{- end -}}

{{/*
Namespaces whose pods may call terasim-mcp (YAML list): networkPolicy.mcpClientNamespaces,
or — when that is empty — the namespace parsed out of tmc.vssBehaviorUrl if it is an
in-cluster Service URL (http://<svc>.<namespace>.svc[.cluster.local][:port][/path]).
*/}}
{{- define "terasim-phase1.mcpClientNamespaces" -}}
{{- $list := .Values.networkPolicy.mcpClientNamespaces | default (list) -}}
{{- if not $list -}}
{{- $url := .Values.tmc.vssBehaviorUrl | default "" | trim -}}
{{- $re := "^https?://[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\\.([a-z0-9](?:[-a-z0-9]*[a-z0-9])?)\\.svc(?:\\.cluster\\.local)?(?::[0-9]+)?(?:/.*)?$" -}}
{{- if regexMatch $re $url -}}
{{- $list = list (regexReplaceAll $re $url "${1}") -}}
{{- end -}}
{{- end -}}
{{- toYaml $list -}}
{{- end -}}

{{/*
Corridor files (corridorConfig): the TMC collector's spec (Metadata.json) and the
service's scheduled_config.yaml. Each comes from its inline value when set, else from the
file shipped in the chart (configs/). With corridorConfig.existingConfigMap the pods mount
that ConfigMap instead and the chart renders none of its own. A values file without
corridorConfig gets the shipped files.
*/}}
{{- define "terasim-phase1.corridorMetadataJson" -}}
{{- $c := .Values.corridorConfig | default dict -}}
{{- if $c.metadataJson -}}
{{- $c.metadataJson -}}
{{- else -}}
{{- .Files.Get "configs/Metadata.json" -}}
{{- end -}}
{{- end -}}

{{- define "terasim-phase1.corridorScheduledConfig" -}}
{{- $c := .Values.corridorConfig | default dict -}}
{{- if $c.scheduledConfigYaml -}}
{{- $c.scheduledConfigYaml -}}
{{- else -}}
{{- .Files.Get "configs/scheduled_config.yaml" -}}
{{- end -}}
{{- end -}}

{{/* Name of the ConfigMap the service and tmc pods mount at /config. */}}
{{- define "terasim-phase1.corridorConfigMapName" -}}
{{- $c := .Values.corridorConfig | default dict -}}
{{- if $c.existingConfigMap -}}
{{- $c.existingConfigMap -}}
{{- else -}}
{{- printf "%s-config" (include "terasim-phase1.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Pod annotation that changes with the corridor files, so an upgrade that changes them
restarts the service and tmc pods (both read the files at start-up). A ConfigMap the
operator manages is outside the chart: only its name enters the checksum.
*/}}
{{- define "terasim-phase1.corridorChecksum" -}}
{{- $c := .Values.corridorConfig | default dict -}}
{{- if $c.existingConfigMap -}}
{{- printf "configmap/%s" $c.existingConfigMap | sha256sum -}}
{{- else -}}
{{- printf "%s\n---\n%s" (include "terasim-phase1.corridorMetadataJson" .) (include "terasim-phase1.corridorScheduledConfig" .) | sha256sum -}}
{{- end -}}
{{- end -}}

{{/* corridor_id / intersections / distinct cameras of the spec the chart renders (YAML dict), for NOTES. */}}
{{- define "terasim-phase1.corridorSummary" -}}
{{- $spec := include "terasim-phase1.corridorMetadataJson" . | fromJson -}}
{{- $sensors := dict -}}
{{- range $it := (get $spec "intersections" | default list) -}}
{{- range $_heading, $sensor := (get $it "Assignments" | default dict) -}}
{{- $_ := set $sensors (toString $sensor) true -}}
{{- end -}}
{{- end -}}
{{- $scheduled := include "terasim-phase1.corridorScheduledConfig" . | fromYaml -}}
{{- $corridors := get $scheduled "corridors" | default dict -}}
{{- toYaml (dict "corridorId" (get $spec "corridor_id" | default "") "intersections" (len (get $spec "intersections" | default list)) "cameras" (len $sensors) "scheduled" (keys $corridors | sortAlpha)) -}}
{{- end -}}

{{/* Fail fast on corridor files the tmc collector or the service would refuse at start-up. */}}
{{- define "terasim-phase1.validateCorridorConfig" -}}
{{- $c := .Values.corridorConfig | default dict -}}
{{- if and $c.existingConfigMap (or $c.metadataJson $c.scheduledConfigYaml) -}}
{{- fail "corridorConfig: set either existingConfigMap or metadataJson / scheduledConfigYaml, not both." -}}
{{- end -}}
{{- if $c.metadataJson -}}
{{- if not (kindIs "string" $c.metadataJson) -}}
{{- fail "corridorConfig.metadataJson must be the file's text (e.g. --set-file corridorConfig.metadataJson=Metadata.json)." -}}
{{- end -}}
{{- $spec := $c.metadataJson | fromJson -}}
{{- if hasKey $spec "Error" -}}
{{- fail (printf "corridorConfig.metadataJson is not a JSON object: %s" (get $spec "Error")) -}}
{{- end -}}
{{- $corridorId := get $spec "corridor_id" -}}
{{- if not (and (kindIs "string" $corridorId) $corridorId) -}}
{{- fail "corridorConfig.metadataJson needs a non-empty corridor_id." -}}
{{- end -}}
{{- if not (get $spec "observation_interval") -}}
{{- fail "corridorConfig.metadataJson needs an observation_interval (e.g. \"10m\")." -}}
{{- end -}}
{{- $intersections := get $spec "intersections" -}}
{{- if not (and (kindIs "slice" $intersections) $intersections) -}}
{{- fail "corridorConfig.metadataJson needs a non-empty intersections list." -}}
{{- end -}}
{{- range $index, $it := $intersections -}}
{{- if not (kindIs "map" $it) -}}
{{- fail (printf "corridorConfig.metadataJson intersections[%d] must be an object." $index) -}}
{{- end -}}
{{- $id := get $it "intersection_id" | default "" | toString -}}
{{- if not $id -}}
{{- fail (printf "corridorConfig.metadataJson intersections[%d] needs an intersection_id." $index) -}}
{{- end -}}
{{- if not (and (hasKey $it "lat") (hasKey $it "lon")) -}}
{{- fail (printf "corridorConfig.metadataJson intersection %s needs lat and lon." $id) -}}
{{- end -}}
{{- range $table := list "Headings" "Movements" "Assignments" -}}
{{- $value := get $it $table -}}
{{- if not (and (kindIs "map" $value) $value) -}}
{{- fail (printf "corridorConfig.metadataJson intersection %s: %s must be a non-empty object." $id $table) -}}
{{- end -}}
{{- end -}}
{{- /* The collector reads a Movements row for every Assignments heading; a heading
       without one fails every collection tick of the whole corridor. */ -}}
{{- $movements := get $it "Movements" -}}
{{- range $heading, $_sensor := (get $it "Assignments") -}}
{{- $found := false -}}
{{- range $path, $_movement := $movements -}}
{{- if hasPrefix (printf "%s - " $heading) $path -}}
{{- $found = true -}}
{{- end -}}
{{- end -}}
{{- if not $found -}}
{{- fail (printf "corridorConfig.metadataJson intersection %s: Assignments heading %s has no Movements entry (\"%s - ...\"); the tmc collector would fail every collection tick." $id $heading $heading) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if $c.scheduledConfigYaml -}}
{{- if not (kindIs "string" $c.scheduledConfigYaml) -}}
{{- fail "corridorConfig.scheduledConfigYaml must be the file's text (e.g. --set-file corridorConfig.scheduledConfigYaml=scheduled_config.yaml)." -}}
{{- end -}}
{{- $scheduled := $c.scheduledConfigYaml | fromYaml -}}
{{- if hasKey $scheduled "Error" -}}
{{- fail (printf "corridorConfig.scheduledConfigYaml is not a YAML mapping: %s" (get $scheduled "Error")) -}}
{{- end -}}
{{- $corridors := get $scheduled "corridors" -}}
{{- if not (and (kindIs "map" $corridors) $corridors) -}}
{{- fail "corridorConfig.scheduledConfigYaml needs a non-empty corridors mapping." -}}
{{- end -}}
{{- range $id, $entry := $corridors -}}
{{- if not (kindIs "map" $entry) -}}
{{- fail (printf "corridorConfig.scheduledConfigYaml corridor %s must be a mapping." $id) -}}
{{- end -}}
{{- range $key := list "base_sumocfg" "corridor" -}}
{{- if not (hasKey $entry $key) -}}
{{- fail (printf "corridorConfig.scheduledConfigYaml corridor %s is missing %s." $id $key) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if not $c.existingConfigMap -}}
{{- $size := add (len (include "terasim-phase1.corridorMetadataJson" .)) (len (include "terasim-phase1.corridorScheduledConfig" .)) -}}
{{- if gt $size 1000000 -}}
{{- fail (printf "the corridor files are %d bytes; a ConfigMap holds at most 1 MiB." $size) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* service.osmDownload.enabled; true when a values file predating it leaves it out. */}}
{{- define "terasim-phase1.osmDownload" -}}
{{- dig "osmDownload" "enabled" true .Values.service -}}
{{- end -}}

{{/* Fail fast on unworkable configurations. */}}
{{- define "terasim-phase1.validate" -}}
{{- if not .Values.tmc.vssBehaviorUrl -}}
{{- fail "tmc.vssBehaviorUrl is REQUIRED: the in-cluster URL of the VSS behavior/VA-API endpoint the TMC collector pulls (e.g. http://vss-video-analytics-api.<vss-namespace>.svc:8081)." -}}
{{- end -}}
{{- if and .Values.route.enabled (not (include "terasim-phase1.externalBase" .)) -}}
{{- fail "route.enabled=true requires global.externalHost (the external host the download Route serves). Set it, or set route.enabled=false." -}}
{{- end -}}
{{- if not (kindIs "bool" .Values.scheduler.enabled) -}}
{{- fail (printf "scheduler.enabled must be true or false (a YAML boolean), got %q." (toString .Values.scheduler.enabled)) -}}
{{- end -}}
{{- if not (kindIs "bool" .Values.simMetrics.enabled) -}}
{{- fail (printf "simMetrics.enabled must be true or false (a YAML boolean), got %q." (toString .Values.simMetrics.enabled)) -}}
{{- end -}}
{{- if not (kindIs "bool" .Values.audit.enabled) -}}
{{- fail (printf "audit.enabled must be true or false (a YAML boolean), got %q." (toString .Values.audit.enabled)) -}}
{{- end -}}
{{- if not (kindIs "bool" (dig "osmDownload" "enabled" true .Values.service)) -}}
{{- fail (printf "service.osmDownload.enabled must be true or false (a YAML boolean), got %q." (toString (dig "osmDownload" "enabled" true .Values.service))) -}}
{{- end -}}
{{- $cadence := .Values.scheduler.cadenceSeconds -}}
{{- if not (or (kindIs "float64" $cadence) (kindIs "int64" $cadence) (kindIs "int" $cadence)) -}}
{{- fail (printf "scheduler.cadenceSeconds must be a number of seconds, got %q." (toString $cadence)) -}}
{{- end -}}
{{- if le (float64 $cadence) 0.0 -}}
{{- fail (printf "scheduler.cadenceSeconds must be > 0, got %v." $cadence) -}}
{{- end -}}
{{- include "terasim-phase1.validateCorridorConfig" . -}}
{{- if and .Values.pullSecret.create (or (not .Values.pullSecret.registry) (not .Values.pullSecret.username)) -}}
{{- fail "pullSecret.create=true requires pullSecret.registry and pullSecret.username." -}}
{{- end -}}
{{- if .Values.redis.auth.enabled -}}
{{- if and .Values.redis.auth.existingSecret .Values.redis.auth.password -}}
{{- fail "set either redis.auth.existingSecret or redis.auth.password, not both." -}}
{{- end -}}
{{- if not .Values.redis.auth.secretKey -}}
{{- fail "redis.auth.secretKey must not be empty." -}}
{{- end -}}
{{- if contains "\n" (.Values.redis.auth.password | toString) -}}
{{- fail "redis.auth.password must not contain newlines." -}}
{{- end -}}
{{- end -}}
{{- if .Values.networkPolicy.enabled -}}
{{- if and (not .Values.networkPolicy.redisClients) (not .Values.networkPolicy.extraIngressFrom.redis) -}}
{{- fail "networkPolicy.redisClients (or networkPolicy.extraIngressFrom.redis) must allow at least one peer, or terasim-service cannot reach Redis." -}}
{{- end -}}
{{- range .Values.networkPolicy.redisClients -}}
{{- if not (has . (list "service" "mcp" "tmc")) -}}
{{- fail (printf "networkPolicy.redisClients: unknown component %q (use service, mcp or tmc)." .) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
