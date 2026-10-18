{{/*
Names, labels, image references, and the refusals the schema cannot express.
*/}}

{{/*
The stem of every object name and of the app.kubernetes.io/name selector label.
It is written here, not taken from .Chart.Name, so that renaming the chart does
not rename the objects: `helm upgrade` would otherwise create new ones beside
the old, including a postgres StatefulSet with a new, empty volume claim.
Pinned by backend/tests/unit/chart/test_chart_rename.py.
*/}}
{{- define "controlvar.name" -}}
{{- default "experimently" .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "controlvar.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default "experimently" .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "controlvar.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "controlvar.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Selector labels for one component: pass (dict "ctx" $ "component" "api"). */}}
{{- define "controlvar.selectorLabels" -}}
app.kubernetes.io/name: {{ include "controlvar.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "controlvar.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "controlvar.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "controlvar.secretName" -}}
{{- default (printf "%s-secrets" (include "controlvar.fullname" .)) .Values.existingSecret }}
{{- end }}

{{/* The image tag: `<profile>-<appVersion>` unless a versioned tag is given. */}}
{{- define "controlvar.imageTag" -}}
{{- $tag := default (printf "%s-%s" .Values.profile .Chart.AppVersion) .Values.image.tag }}
{{- if not (hasPrefix (printf "%s-" .Values.profile) $tag) }}
{{- fail (printf "image.tag %q does not match profile %q: a mismatched image serves the wrong profile without an error" $tag .Values.profile) }}
{{- end }}
{{- $tag }}
{{- end }}

{{/* pass (dict "ctx" $ "image" .Values.image.api) */}}
{{- define "controlvar.image" -}}
{{- if .image.digest }}
{{- printf "%s@%s" .image.repository .image.digest }}
{{- else }}
{{- printf "%s:%s" .image.repository (include "controlvar.imageTag" .ctx) }}
{{- end }}
{{- end }}

{{/* The host people reach: publicBaseUrl without scheme (and without port). */}}
{{- define "controlvar.publicHost" -}}
{{- $u := urlParse (required "publicBaseUrl is required, e.g. https://controlvar.example.com" .Values.publicBaseUrl) }}
{{- (split ":" $u.host)._0 }}
{{- end }}

{{- define "controlvar.postgresHost" -}}
{{- if .Values.postgresql.bundled }}
{{- printf "%s-postgres" (include "controlvar.fullname" .) }}
{{- else }}
{{- required "postgresql.host is required when postgresql.bundled=false" .Values.postgresql.host }}
{{- end }}
{{- end }}

{{- define "controlvar.redisHost" -}}
{{- if .Values.redis.bundled }}
{{- printf "%s-redis" (include "controlvar.fullname" .) }}
{{- else }}
{{- required "redis.host is required when redis.enabled=true and redis.bundled=false" .Values.redis.host }}
{{- end }}
{{- end }}

{{/*
The env entries every API process gets beyond the ConfigMap: secrets, only
ever by reference. Required keys are not `optional`, so a missing key in an
existingSecret stops the pod with CreateContainerConfigError naming it.
*/}}
{{- define "controlvar.secretEnv" -}}
{{- $secret := include "controlvar.secretName" . }}
{{- $keys := list "SECRET_KEY" "FIRST_SUPERUSER_PASSWORD" "POSTGRES_USER" "POSTGRES_PASSWORD" }}
{{- if eq .Values.profile "full" }}
{{- $keys = append $keys "AUDIT_HMAC_KEY" }}
{{- end }}
{{- range $keys }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $secret }}
      key: {{ . }}
{{- end }}
{{- $optional := list "METRICS_TOKEN" }}
{{- if .Values.redis.enabled }}
{{- $optional = append $optional "REDIS_PASSWORD" }}
{{- end }}
{{- range $optional }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $secret }}
      key: {{ . }}
      optional: true
{{- end }}
{{- end }}

{{/*
The variables the chart pins, as explicit `env:` entries on both API
containers. Explicit `env` takes precedence over every `envFrom` source, so a
ConfigMap or Secret named in api.extraEnvFrom cannot override these. (A later
`envFrom` source DOES override an earlier one, which is why ENVIRONMENT and
PUBLIC_BASE_URL are here and not in the chart's own ConfigMap.)
pass (dict "ctx" $ "runMigrations" "true"|"false")
*/}}
{{- define "controlvar.pinnedEnvNames" -}}
{{- list "ENVIRONMENT" "PUBLIC_BASE_URL" "RUN_MIGRATIONS" "SEED" "SEED_FORCE" | toJson }}
{{- end }}

{{- define "controlvar.pinnedEnv" -}}
- name: ENVIRONMENT
  value: production
- name: PUBLIC_BASE_URL
  value: {{ .ctx.Values.publicBaseUrl | quote }}
- name: RUN_MIGRATIONS
  value: {{ .runMigrations | quote }}
# Demo seeds are never run through this chart.
- name: SEED
  value: ""
- name: SEED_FORCE
  value: ""
{{- end }}

{{/*
extraEnv may not reintroduce what the chart pins: in `env`, a later duplicate
name wins, and extraEnv is rendered after the pinned entries.
*/}}
{{- define "controlvar.extraEnv" -}}
{{- $pinned := include "controlvar.pinnedEnvNames" . | fromJsonArray }}
{{- range .Values.api.extraEnv }}
{{- if has .name $pinned }}
{{- fail (printf "api.extraEnv may not set %s: the chart pins it (migrations run once in the migrate init container, demo seeds are not available through this chart, and ENVIRONMENT/PUBLIC_BASE_URL come from the chart's values)" .name) }}
{{- end }}
{{- end }}
{{- with .Values.api.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "controlvar.podSecurity" -}}
runAsNonRoot: true
runAsUser: {{ .uid }}
runAsGroup: {{ .gid }}
fsGroup: {{ .gid }}
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "controlvar.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}
