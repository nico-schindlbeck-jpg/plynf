{{/*
SPDX-License-Identifier: Apache-2.0
Plinth — chart helpers.
*/}}

{{/* Chart name + chart version, for labels. */}}
{{- define "plinth.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Release name, truncated to 63 chars (k8s name limit). */}}
{{- define "plinth.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Per-service fullname: "<release>-<service>" if release name doesn't already
contain it, otherwise just "<service>". Final length capped at 63.
*/}}
{{- define "plinth.service.fullname" -}}
{{- $svc := .svc -}}
{{- $name := printf "plinth-%s" $svc -}}
{{- printf "%s" $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels shared by every Plinth resource. */}}
{{- define "plinth.labels" -}}
app.kubernetes.io/name: plinth
helm.sh/chart: {{ include "plinth.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: plinth
{{- end -}}

{{/* Per-service labels. Pass `dict "ctx" . "svc" "workspace"`. */}}
{{- define "plinth.service.labels" -}}
{{- $svc := .svc -}}
{{- with .ctx -}}
{{ include "plinth.labels" . }}
app.kubernetes.io/component: {{ $svc }}
plinth.dev/service: {{ $svc }}
{{- end -}}
{{- end -}}

{{/* Selector labels — stable subset of the labels above. */}}
{{- define "plinth.service.selectorLabels" -}}
{{- $svc := .svc -}}
{{- with .ctx -}}
app.kubernetes.io/name: plinth
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: plinth
plinth.dev/service: {{ $svc }}
{{- end -}}
{{- end -}}

{{/*
Image reference for a service.
Pass `dict "ctx" . "svc" "workspace" "config" .Values.workspace`.
*/}}
{{- define "plinth.image" -}}
{{- $config := .config -}}
{{- $ctx := .ctx -}}
{{- $registry := $ctx.Values.global.imageRegistry -}}
{{- $repo := $config.image.repository -}}
{{- $tag := default $ctx.Values.global.imageTag $config.image.tag -}}
{{- printf "%s/%s:%s" $registry $repo $tag -}}
{{- end -}}

{{/* Service account name. */}}
{{- define "plinth.serviceAccountName" -}}
{{- if .Values.global.serviceAccount.create -}}
{{- default (printf "%s-plinth" .Release.Name) .Values.global.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.global.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Storage class — explicit > global > "" (cluster default). */}}
{{- define "plinth.storageClass" -}}
{{- $svcClass := .svcClass -}}
{{- $globalClass := .ctx.Values.global.storageClass -}}
{{- if $svcClass -}}
{{ $svcClass }}
{{- else if $globalClass -}}
{{ $globalClass }}
{{- end -}}
{{- end -}}

{{/* Common pod spec security context. */}}
{{- define "plinth.podSecurityContext" -}}
{{- toYaml .Values.global.podSecurityContext -}}
{{- end -}}

{{/* Common container security context. */}}
{{- define "plinth.containerSecurityContext" -}}
{{- toYaml .Values.global.containerSecurityContext -}}
{{- end -}}
