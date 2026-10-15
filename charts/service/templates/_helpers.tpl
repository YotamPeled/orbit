{{/* <project>-<component>, unique within the per-environment namespace. */}}
{{- define "service.name" -}}
{{- printf "%s-%s" .root.Values.project .comp.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
The `orbit.local/` label-key prefix is the product's own namespace, not any org's domain.
It was the FIRST org's domain until 2026-09-07 (`<that org>.xyz/env`, `/class`, `/project`,
`/component`, `/agent`), which meant every scaffolded instance carried one organisation's
hostname in its API schema and the extraction oracle needed a standing exemption to stay
green. The migration is DONE, dated 2026-09-07, and the exemption is gone with it: the
prefix is a constant of the chart now and no longer derived from anything.
*/}}
{{- define "service.labels" -}}
app.kubernetes.io/name: {{ .comp.name }}
app.kubernetes.io/instance: {{ .root.Values.project }}
app.kubernetes.io/part-of: {{ .root.Values.project }}
app.kubernetes.io/managed-by: argocd
orbit.local/env: {{ .root.Values.env }}
orbit.local/class: {{ .comp.class | default .root.Values.defaultClass }}
{{- end -}}

{{- define "service.selectorLabels" -}}
app.kubernetes.io/name: {{ .comp.name }}
app.kubernetes.io/instance: {{ .root.Values.project }}
{{- end -}}

{{/*
Resources. Built from the component's class rather than accepting a wholesale
`resources` block: an override that replaces the default is how the mandatory memory
limit gets silently dropped. A memory LIMIT is emitted unconditionally and CPU is
always request-only, regardless of what a component asks for.

`memory` is the RESERVATION (the request). `memoryLimit` is optional and defaults to
it, so every component that does not name one keeps the historical request == limit
shape byte for byte. Naming one splits the two, which is the only honest way to declare
a workload whose steady state and whose peak are different numbers — see values.yaml
("Memory: the request is the steady state, the limit is the ceiling") for when that is
the right declaration and when it is an excuse.
*/}}
{{- define "service.resources" -}}
{{- $c := .comp -}}
{{- $classes := .root.Values.resourceClasses -}}
{{- $class := get $classes ($c.class | default .root.Values.defaultClass) -}}
{{- $mem := $c.memory | default .root.Values.defaultMemory -}}
requests:
  cpu: {{ $c.cpu | default $class.cpu }}
  memory: {{ $mem }}
limits:
  memory: {{ $c.memoryLimit | default $mem }}
{{- end -}}

{{- define "service.priorityClass" -}}
{{- $classes := .root.Values.resourceClasses -}}
{{- $class := get $classes (.comp.class | default .root.Values.defaultClass) -}}
{{ $class.priorityClassName }}
{{- end -}}

{{/*
Which components get the graceful-roll treatment (preStop drain + a sized grace period).

Exactly the set that SERVES HTTP THROUGH A SERVICE AND ROLLS: `kind: web` with a `port`
is the same predicate templates/ingress.yaml uses to build `$webs`, and `not singleton`
removes the Recreate workloads. Both halves matter.

  * No HTTP port -> nothing holds a load-balancer reference to the pod that can go
    stale. A worker's only client is its own scheduler; a CronJob's pod is gone by
    design. A preStop sleep there buys nothing and delays every termination by 5s.
    (The predicate is `port`, not "an Ingress was rendered": the race is between
    endpoint removal and whoever is load-balancing over those endpoints, and Traefik
    reaching a tenant is only the commonest case of that. The agent plane's intake is
    reached over a hand-written Ingress and by Alertmanager in-cluster, and it has the
    same window either way.)
  * Recreate (singleton) -> there is no second pod to serve during the swap. Kubernetes
    deletes the old pod, waits for it to be GONE, and only then creates the new one, so
    a preStop sleep does not shrink the outage — it moves it 5 seconds later and makes
    the roll 5 seconds longer. A singleton alert listener is that shape.
*/}}
{{- define "service.drains" -}}
{{- $c := .comp -}}
{{- if and (eq ($c.kind | default "web") "web") $c.port (not $c.singleton) -}}true{{- end -}}
{{- end -}}

{{/*
A Kubernetes quantity as a number of bytes, so two values written in different units
can be ordered. The chart schema already restricts the form; this only has to compare
two strings that already parse. Binary suffixes are matched before decimal ones so
"512Mi" is mebibytes, not milli-something plus an "i".
*/}}
{{- define "service.quantityBytes" -}}
{{- $q := . | toString -}}
{{- $unit := "" -}}
{{- range $u := list "Ei" "Pi" "Ti" "Gi" "Mi" "Ki" "E" "P" "T" "G" "M" "k" -}}
{{- if and (eq $unit "") (hasSuffix $u $q) -}}{{- $unit = $u -}}{{- end -}}
{{- end -}}
{{- $num := trimSuffix $unit $q -}}
{{- if not (regexMatch "^[0-9]+(\\.[0-9]+)?$" $num) -}}
{{- fail (printf "not a Kubernetes quantity: %s" $q) -}}
{{- end -}}
{{- $mul := 1.0 -}}
{{- if eq $unit "k" -}}{{- $mul = 1000.0 -}}
{{- else if eq $unit "M" -}}{{- $mul = 1000000.0 -}}
{{- else if eq $unit "G" -}}{{- $mul = 1000000000.0 -}}
{{- else if eq $unit "T" -}}{{- $mul = 1000000000000.0 -}}
{{- else if eq $unit "P" -}}{{- $mul = 1000000000000000.0 -}}
{{- else if eq $unit "E" -}}{{- $mul = 1000000000000000000.0 -}}
{{- else if eq $unit "Ki" -}}{{- $mul = 1024.0 -}}
{{- else if eq $unit "Mi" -}}{{- $mul = 1048576.0 -}}
{{- else if eq $unit "Gi" -}}{{- $mul = 1073741824.0 -}}
{{- else if eq $unit "Ti" -}}{{- $mul = 1099511627776.0 -}}
{{- else if eq $unit "Pi" -}}{{- $mul = 1125899906842624.0 -}}
{{- else if eq $unit "Ei" -}}{{- $mul = 1152921504606846976.0 -}}
{{- end -}}
{{- mulf ($num | float64) $mul -}}
{{- end -}}

{{/*
Shared pod spec, so a CronJob and a Deployment running the same image cannot drift in
how they are configured.
*/}}
{{- define "service.podSpec" -}}
{{- $c := .comp -}}
{{- $root := .root -}}
{{- /* Every Secret a pod may reference must have been declared in `secrets`. Kubernetes
     does not fail a pod whose secretKeyRef points at nothing — it holds it in
     CreateContainerConfigError, which reads as a scheduling hiccup rather than a
     configuration error, and `envFrom` is worse: a missing Secret there is skipped in
     silence and the process starts with the variable simply absent. A typo in a secret
     name would therefore ship, and the first symptom would be an app authenticating as
     nobody. Refusing to render is the only place this is cheap to catch. */ -}}
{{- $declared := dict -}}
{{- range $s := ($root.Values.secrets | default list) -}}
{{- $_ := set $declared $s.name true -}}
{{- end -}}
{{- /* A declared database produces exactly one Secret, named by CloudNativePG rather than
     by us. Declaring it here instead of making the catalog repeat it keeps the one thing
     a project cannot get wrong out of the catalog: the name derives from the same project
     id the Cluster does, so the two cannot drift — while a typo in a component's
     `secret:` still fails the guard below. */ -}}
{{- if $root.Values.database -}}
{{- $_ := set $declared (printf "%s-db-app" $root.Values.project) true -}}
{{- end -}}
{{- range $k, $v := ($c.env | default dict) -}}
{{- if kindIs "map" $v -}}
{{- if not (hasKey $declared $v.secret) -}}
{{- fail (printf "component %q sets env %s from Secret %q, which is not declared in `secrets` — add it there, or the pod ships referencing a Secret that may never exist" $c.name $k $v.secret) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $s := ($c.envFromSecrets | default list) -}}
{{- if not (hasKey $declared $s) -}}
{{- fail (printf "component %q pulls envFrom Secret %q, which is not declared in `secrets` — a missing envFrom Secret is skipped silently, so this must be caught here" $c.name $s) -}}
{{- end -}}
{{- end -}}
{{- /* The job-runner component (templates/rbac.yaml) gets its OWN ServiceAccount — the
     only one on the platform whose token is mounted — because it is the only workload
     that calls the Kubernetes API. Everything else keeps the project ServiceAccount,
     whose token is deliberately not mounted. The selector is a ROOT value, never a
     component key: component dicts pass through from the catalog, so a component key here
     would let a hand-written catalog project attach itself to a Role. */ -}}
{{- /* `{{- end -}}`, with the right-trim: this replaced a bare
     `serviceAccountName: {{ ... }}` line, and the non-trimming form emits the newline
     `end` sits on — which lands as an indented BLANK LINE inside every pod spec on the
     platform, because podSpec is nindent'ed by its caller. Byte-identical output for a
     release that sets no rbac is the property that keeps "regenerate and diff" honest. */ -}}
{{- $sa := $c.serviceAccountName | default $root.Values.project -}}
{{- if and $root.Values.rbac $root.Values.rbac.jobRunner (eq $root.Values.rbac.jobRunner $c.name) -}}
{{- $sa = printf "%s-%s" $root.Values.project $c.name -}}
{{- end -}}
{{- /* The graceful-roll budget. `default` is deliberately not used to read these: sprig
     treats 0 as empty, so a deliberate `preStopSeconds: 0` would be silently rewritten
     to 5 — the same trap the cron controls above document. */ -}}
{{- $drainCfg := $root.Values.drain | default dict -}}
{{- $preStop := 5 -}}
{{- if hasKey $drainCfg "preStopSeconds" -}}{{- $preStop = $drainCfg.preStopSeconds -}}{{- end -}}
{{- $grace := 30 -}}
{{- if hasKey $drainCfg "gracePeriodSeconds" -}}{{- $grace = $drainCfg.gracePeriodSeconds -}}{{- end -}}
{{- $drains := include "service.drains" (dict "root" $root "comp" $c) -}}
{{- /* A component `drain:` (integer seconds) is that component's own
     terminationGracePeriodSeconds. Workers get the grace period and no preStop — they
     drain on SIGTERM. Web keeps the chart-level preStop sleep; the value must still
     exceed it. Cron is refused: the pod is gone by design. Absent, the render is the
     chart-level drain for web and nothing for everyone else — byte-identical to today.
     hasKey, not `default`: sprig treats 0 as empty. */ -}}
{{- if hasKey $c "drain" -}}
{{- if eq ($c.kind | default "web") "cron" -}}
{{- fail (printf "component %q is kind: cron and sets drain: a CronJob's pod is gone by design" $c.name) -}}
{{- end -}}
{{- $grace = $c.drain -}}
{{- end -}}
{{- /* The grace period is a wall clock that starts at DELETE and covers preStop AND the
     app's own shutdown after SIGTERM. If it does not exceed the sleep, the kubelet
     SIGKILLs the pod during (or the instant after) its own drain — every in-flight
     request dies, which is the failure this whole block exists to prevent, arriving as
     a config that looks like it is doing the right thing. Component drain uses the same
     comparison against the same preStop; only the name in the message changes. */ -}}
{{- if and $drains (le (int $grace) (int $preStop)) -}}
{{- if hasKey $c "drain" -}}
{{- fail (printf "component %q: drain (%v) must be greater than drain.preStopSeconds (%v) — the grace period starts at delete and must cover the preStop sleep plus the app's own shutdown, or the pod is SIGKILLed mid-drain" $c.name $grace $preStop) -}}
{{- else -}}
{{- fail (printf "component %q: drain.gracePeriodSeconds (%v) must be greater than drain.preStopSeconds (%v) — the grace period starts at delete and must cover the preStop sleep plus the app's own shutdown, or the pod is SIGKILLed mid-drain" $c.name $grace $preStop) -}}
{{- end -}}
{{- /* `{{- end -}}`, right-trimmed, for the reason spelled out above `$sa`: the
     non-trimming form emits the newline `end` sits on, which lands as an indented blank
     line inside EVERY pod spec on the platform — worker, cron and singleton included,
     none of which this change is supposed to touch. Caught by diffing the render of
     every live Application against the previous chart, which is the check that keeps
     "only web components move" true. Fourth time this file has been broken by a trim. */ -}}
{{- end -}}
{{- /* A memory-backed emptyDir is charged to the container's cgroup. sizeLimit at or
     above memoryLimit is an OOMKill waiting for a real file; scratch with no
     memoryLimit has no ceiling at all. Same two messages as the generator. Fully
     trimmed so a component without scratch still renders byte-identically. */ -}}
{{- with $c.scratch -}}
{{- if not $c.memoryLimit -}}
{{- fail (printf "component %q: scratch requires memoryLimit — a memory-backed emptyDir is charged to the container's cgroup, and without a limit the volume can grow until the node is the thing that dies" $c.name) -}}
{{- end -}}
{{- $scratchBytes := include "service.quantityBytes" .sizeLimit | trim -}}
{{- $limitBytes := include "service.quantityBytes" $c.memoryLimit | trim -}}
{{- if ge ($scratchBytes | float64) ($limitBytes | float64) -}}
{{- fail (printf "component %q: scratch.sizeLimit (%v) must be smaller than memoryLimit (%v) — a memory-backed emptyDir is charged to the container's cgroup; a sizeLimit at or above the limit is an OOMKill waiting for a real file" $c.name .sizeLimit $c.memoryLimit) -}}
{{- end -}}
{{- end -}}
serviceAccountName: {{ $sa }}
priorityClassName: {{ include "service.priorityClass" (dict "root" $root "comp" $c) }}
{{- if or $drains (hasKey $c "drain") }}
{{- /* Sized to cover the preStop sleep plus the application's own shutdown. Rendered
     only for the components that drain, so every other workload on the platform keeps
     the Kubernetes default and its render stays byte-identical. A component `drain:`
     is the other way onto this line: a worker that must finish in-flight work. */}}
terminationGracePeriodSeconds: {{ $grace }}
{{- end }}
{{- /* Only reference a pull secret when one is configured. Naming `ghcr-pull` unconditionally
     while no such Secret existed made every pod log FailedToRetrieveImagePullSecret; it
     stayed invisible because the only image deployed so far is public. The first private
     image would have gone straight to ImagePullBackOff with the cause buried in events. */ -}}
{{- with $root.Values.imagePullSecret }}
imagePullSecrets:
  - name: {{ . }}
{{- end }}
{{- /* Drop nil overrides before merging. mergeOverwrite writes a nil straight through, so
     `securityContext: {runAsUser: null}` erased the chart's pinned UID and handed identity
     selection back to whatever USER the image declares — an undocumented escape from the
     hardened default that looked like a no-op in the catalog. */ -}}
{{- $override := dict -}}
{{- range $k, $v := ($c.securityContext | default dict) -}}
{{- if not (kindIs "invalid" $v) }}{{- $_ := set $override $k $v -}}{{- end -}}
{{- end -}}
{{- $sec := mergeOverwrite (deepCopy $root.Values.podSecurityContext) $override -}}
{{- /* The override exists so stock images with a fixed UID (nginx 101, postgres 999) can
     run — not so a component can opt out of hardening. Namespaces also enforce the
     restricted PodSecurity standard, but failing here names the component. */ -}}
{{- /* `default` treats 0 as empty in sprig, so `$sec.fsGroup | default 1` returns 1 for
     exactly the value being guarded against. Compare the key's presence instead. */ -}}
{{- /* Every comparison goes through toString. Helm parses values via sigs.k8s.io/yaml, so
     YAML integers arrive as float64 — `has 0 $list` uses deep equality and int 0 never
     equals float64 0, which made the supplementalGroups clause dead code that could not
     fire. PodSecurity `restricted` does not constrain supplementalGroups either, so there
     was no backstop underneath it. */ -}}
{{- /* runAsGroup: 0 is deliberately NOT guarded. GID 0 with a non-zero UID is the standard
     UBI/OpenShift pattern — the root GROUP grants no privilege on its own, and the arbitrary
     UID it pairs with is what makes those images work. Rejecting it blocked a legitimate
     configuration while supplementalGroups: [0], which really does grant group-root access
     to mounted volumes, sailed through. The guard now matches the actual risk. */ -}}
{{- /* NOTHING may follow the `{{- end }}` below except `securityContext:` on its own line.
     A trailing `-}}` there right-trims the newline and glues the two together, which
     silently breaks the chart for EVERY input. That has now happened twice. */ -}}
{{- $rootGroups := list -}}
{{- range ($sec.supplementalGroups | default list) -}}
{{- if eq (toString .) "0" }}{{- $rootGroups = append $rootGroups . -}}{{- end -}}
{{- end -}}
{{- if or (eq (toString $sec.runAsUser) "0") (not $sec.runAsNonRoot)
      (and (hasKey $sec "fsGroup") (eq (toString $sec.fsGroup) "0"))
      $rootGroups -}}
{{- fail (printf "component %q would run as root: securityContext may set a non-zero runAsUser/fsGroup/supplementalGroups, never 0, and never runAsNonRoot:false" $c.name) -}}
{{- end }}
securityContext: {{- toYaml $sec | nindent 2 }}
containers:
  - name: {{ $c.name }}
    {{- /* An image that already carries a tag or a digest must not get ":latest" welded on
         the end. `nginx:1.2` rendered as `nginx:1.2:latest` and a digest ref as
         `...@sha256:ab:latest` — both render green and ImagePullBackOff at runtime. The
         dispatch path rejects these; a hand-edited catalog had nothing stopping it. */ -}}
    {{- $ref := $c.image -}}
    {{- $base := last (splitList "/" $ref) -}}
    {{- if or (contains "@" $ref) (contains ":" $base) -}}
    {{- /* `{{- end }}`, NOT `{{- end -}}`. The right-trim form eats the newline separating
         this from `image:` and renders `- name: db-backupimage: ghcr.io/...` as one glued
         line. It survived this long because only the tagged-image branch reaches it and
         no catalog component can take that branch — the schema forbids a colon in
         `image:`. The chart's own database backup job was the first thing to get here.
         Third time this file has been broken by a trim; stage3 now parses the render. */ -}}
    {{- if $c.tag }}{{- fail (printf "component %q pins a tag or digest in `image: %s` and also sets `tag: %s` — put the tag in one place" $c.name $ref $c.tag) -}}{{- end }}
    image: {{ $ref }}
    {{- else }}
    image: {{ $ref }}:{{ $c.tag | default "latest" }}
    {{- end }}
    imagePullPolicy: IfNotPresent
    {{- if $drains }}
    {{- /* The departure edge. Endpoint removal is NOT synchronous with SIGTERM: the
         kubelet begins termination and the EndpointSlice write, the router's watch, and
         the router's own config reload all happen concurrently and later. Traefik's
         `providers.providersThrottleDuration` alone is 2s by default (we do not override
         it), so for a couple of seconds after a pod stops accepting connections Traefik
         is still round-robining requests at it — which is what a maxUnavailable:0 roll
         measured at 1s resolution showed as ~3s of 502 with roughly half of requests
         failing (a two-server pool, one server dead). Sleeping here keeps the process
         listening across that window while the pod is already being removed from every
         router's view; when the sleep ends and SIGTERM finally arrives, nothing is being
         sent to it any more.
         The NATIVE sleep action, not `exec: [sh, -c, sleep 5]`: these images are
         distroless and run as 65532 — most have no shell and several have no
         /bin/sleep, so the exec form fails, the kubelet logs FailedPreStopHook, and the
         pod dies immediately. The hook then silently does nothing, which is worse than
         not having one. SleepAction has been GA since 1.32; this platform runs 1.36. */}}
    lifecycle:
      preStop:
        sleep:
          seconds: {{ $preStop }}
    {{- end }}
    {{- with $c.command }}
    command: {{ toYaml . | nindent 6 }}
    {{- end }}
    {{- with $c.args }}
    args: {{ toYaml . | nindent 6 }}
    {{- end }}
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: {{ $c.readOnlyRootFilesystem | default false }}
      capabilities:
        drop: [ALL]
    {{- $ports := list }}
    {{- if and $c.port (ne ($c.kind | default "web") "cron") }}
    {{- $ports = append $ports (dict "name" "http" "containerPort" (int $c.port)) }}
    {{- end }}
    {{- if and $c.metrics $c.metrics.port (ne (int $c.metrics.port) (int ($c.port | default 0))) }}
    {{- $ports = append $ports (dict "name" "metrics" "containerPort" (int $c.metrics.port)) }}
    {{- end }}
    {{- with $ports }}
    ports: {{ toYaml . | nindent 6 }}
    {{- end }}
    env:
      - name: ENVIRONMENT
        value: {{ $root.Values.env | quote }}
      {{- /* Identity is set by the platform, never the app (OBSERVABILITY.md Decision
           5): service.name/namespace/environment/version are the join key across all
           three signals, and inconsistency breaks correlation SILENTLY — Grafana
           renders no error, the link simply doesn't appear. Every workload gets them,
           instrumented or not: the SDKs read the same variables. Only the NEW
           deployment.environment.name is emitted — the 2023 name is deprecated. */}}
      - name: OTEL_SERVICE_NAME
        value: {{ printf "%s-%s" $root.Values.project $c.name | quote }}
      - name: OTEL_RESOURCE_ATTRIBUTES
        value: {{ printf "service.namespace=%s,deployment.environment.name=%s,service.version=%s" $root.Values.project $root.Values.env ($c.tag | default "untagged") | quote }}
      {{- /* A value is either literal or a reference to a key in a Kubernetes Secret. The
           reference names a Secret and nothing else — not Infisical, not SOPS. Both
           producers terminate in an ordinary Secret, so swapping one for the other is
           invisible here, which is what lets the secrets backend change without touching
           a single application's configuration. */ -}}
      {{- range $k, $v := $c.env }}
      {{- if kindIs "map" $v }}
      - name: {{ $k }}
        valueFrom:
          secretKeyRef:
            name: {{ $v.secret }}
            key: {{ $v.key }}
      {{- else }}
      - name: {{ $k }}
        value: {{ $v | quote }}
      {{- end }}
      {{- end }}
    {{- with $c.envFromSecrets }}
    envFrom:
      {{- range . }}
      - secretRef:
          name: {{ . }}
      {{- end }}
    {{- end }}
    {{- if $c.probePath }}
    readinessProbe:
      httpGet:
        path: {{ $c.readinessPath | default $c.probePath | quote }}
        port: {{ $c.port }}
      initialDelaySeconds: 3
      periodSeconds: 5
    livenessProbe:
      httpGet:
        path: {{ $c.probePath | quote }}
        port: {{ $c.port }}
      initialDelaySeconds: 20
      periodSeconds: 20
      failureThreshold: 6
    {{- end }}
    resources: {{- include "service.resources" (dict "root" $root "comp" $c) | nindent 6 }}
    {{- if or $c.persistence $c.configMap $c.scratch }}
    volumeMounts:
      {{- with $c.persistence }}
      - name: data
        mountPath: {{ .mountPath | quote }}
      {{- end }}
      {{- with $c.configMap }}
      - name: content
        mountPath: {{ .mountPath | quote }}
        readOnly: true
      {{- end }}
      {{- with $c.scratch }}
      - name: scratch
        mountPath: {{ .mountPath | quote }}
      {{- end }}
    {{- end }}
{{- if or $c.persistence $c.configMap $c.scratch }}
volumes:
  {{- with $c.persistence }}
  - name: data
    persistentVolumeClaim:
      claimName: {{ include "service.name" (dict "root" $root "comp" $c) }}
  {{- end }}
  {{- with $c.configMap }}
  {{- /* Content served from a ConfigMap rather than baked into an image. Right for a
       page generated from the catalog: a catalog edit updates the page with no rebuild,
       and there is no registry in the loop. Not right for application code. */}}
  - name: content
    configMap:
      name: {{ .name | quote }}
      {{- /* `items` projects each key to its own path INSIDE the mount, subdirectories
           included. That is what lets one ConfigMap carry a skills library the agent
           runtime can actually discover — it reads <skill>/SKILL.md and nothing else, so
           the default flat projection (one file per key, named by the key) mounts
           cleanly, lists nothing, and teaches the agent nothing. Kubernetes rejects an
           absolute path or one containing '..', and the schema refuses both earlier. */ -}}
      {{- with .items }}
      items: {{ toYaml . | nindent 8 }}
      {{- end }}
  {{- end }}
  {{- with $c.scratch }}
  - name: scratch
    emptyDir:
      medium: Memory
      sizeLimit: {{ .sizeLimit }}
  {{- end }}
{{- end }}
{{- end -}}
