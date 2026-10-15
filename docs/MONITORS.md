# Monitors

A monitor is one alert a project's operator declares: "tell me when this number crosses
that line", "tell me when this address stops answering", "tell me when this appears in the
logs". It is a file in the instance repository, `monitors/<name>.yaml`, written and
removed through orbitd's tools. orbitd renders each file into what already evaluates
alerts on the platform: a PrometheusRule (a metric monitor), a blackbox Probe plus the
rule that watches it (a probe monitor), or a rule in the Loki ruler's ConfigMap (a log
monitor). Nothing else needs to be installed.

The platform's own alerts (a restarting container, a full disk, a refused commit) are
not monitors. They ship with the template in `clusters/platform/monitoring/rules.yaml`
and are listed at the end.

## The tools

| tool | role | what it does |
|---|---|---|
| `orbit_monitor_list` | viewer | the monitors that are defined: name, project, env, kind, severity, summary, notify |
| `orbit_monitor_status` | viewer | firing, pending or inactive for one or all; the last transition; the whole `definition`; the `silences` in force |
| `orbit_monitor_upsert` | operator | create or replace one (the whole body); `dry_run` renders without writing |
| `orbit_monitor_delete` | operator | delete one and exactly its objects |
| `orbit_monitor_silence` | operator | quiet one monitor's alert for 1 minute to 7 days, with a reason |
| `orbit_monitor_unsilence` | operator | lift every silence in force for one monitor |
| `orbit_alerts_active` | viewer | what is firing now, the platform's own alerts included (silenced alerts are hidden) |

The operator skill (`skills/orbit-operator/SKILL.md`, "Monitors") is the procedure an
agent follows; this page is the reference.

## The body

One mapping. Every field:

| field | required | value |
|---|---|---|
| `name` | yes | a DNS label (lowercase letters, digits, `-`, at most 63 characters). It is the file's name and the alert's name |
| `project`, `env` | yes | a project and one of its environments, as the catalog has them. They become the alert's `project` and `env` labels |
| `kind` | yes | `metric`, `probe` or `log` |
| `query` | metric, log | one line of PromQL (metric) or of LogQL that yields a number (log), at most 4096 characters |
| `target` | probe | an `http://` or `https://` address with a public host: no credentials in it, no cluster-internal name, no private address |
| `module` | no (probe) | the blackbox check; `http_2xx` when left out |
| `condition.comparison` | metric, log | one of `>` `>=` `<` `<=` `==` `!=` |
| `condition.threshold` | metric, log | a number |
| `condition.for` | yes | how long it must hold before the alert fires: a Prometheus duration (`90s`, `5m`, `1h30m`) |
| `severity` | yes | `page`, `warn` or `info` |
| `summary` | yes | the sentence a person reads when it fires, at most 1024 characters |
| `notify` | no | `["telegram"]` sends the alert to the instance's chat; left out or `[]`, the alert is recorded and nobody is messaged |
| `runbook` | no | a URL, carried on the alert |

A metric monitor fires while `(query) comparison threshold` has held for `for`. A probe
monitor fires while its target has not answered for `for`. A log monitor is the metric
rule, evaluated by Loki.

```yaml
name: api-errors
project: demo
env: prod
kind: metric
query: sum(rate(http_requests_total{namespace="demo-prod",status=~"5.."}[5m]))
condition: {comparison: ">", threshold: 1, for: 5m}
severity: page
summary: demo's api is answering 5xx
notify: [telegram]
```

```yaml
name: api-door
project: demo
env: prod
kind: probe
target: https://demo.example.org/readyz
condition: {for: 2m}
severity: page
summary: demo's door does not answer
notify: [telegram]
```

```yaml
name: api-panics
project: demo
env: prod
kind: log
query: sum(count_over_time({namespace="demo-prod"} |= "panic" [5m]))
condition: {comparison: ">", threshold: 0, for: 1m}
severity: warn
summary: demo's api logged a panic
```

A body the validator refuses is a tool error naming each field, before anything is
written.

## What each kind needs

| kind | feature it needs | when the feature is off |
|---|---|---|
| metric | none: Prometheus is part of the core | |
| probe | `uptime` (the blackbox exporter) | refused: nothing would run the probe |
| log | `logs` (Loki and its ruler) | refused: nothing would evaluate the rule |
| any, with `notify: [telegram]` | `alerts-telegram` | refused: nobody would be messaged |

### Log monitors

A log monitor's rule is evaluated by the Loki ruler. The template wires that ruler for
every instance that has the `logs` feature on: `clusters/platform/apps/loki.yaml` gives
it local rule storage, Alertmanager's address, and the mount of the ConfigMap
`orbit-monitors-logs` that orbitd writes. orbitd reads that file and accepts a log
monitor when the wiring is there; nothing has to be declared.

An instance whose Loki application was changed by hand so that orbitd cannot recognise
the wiring, and whose ruler does read the ConfigMap, says so with one line in
`instance/config.yaml`:

```yaml
lokiRuler: wired
```

It is a statement, not a switch: it wires nothing. With the `logs` feature off there is no
Loki and `make init` refuses the line.

## What a monitor can see

A query is not confined to its own project. It may read any series Prometheus holds and
any log stream Loki holds, another project's included, and a probe may fetch any public
address. `project` and `env` say whom the alert is about; they do not limit what it
watches. That is why writing a monitor needs the operator role, the same role that can
ship to any project. Write queries that select the project's own namespace
(`namespace="<project>-<env>"`).

## Who hears it, and silences

`notify: [telegram]` routes the alert to the instance's chat. `severity: page` maps to
Alertmanager's `critical`, `warn` to `warning`, `info` to `info`. An alert that fires is
always recorded by orbitd (`orbit_monitor_status`'s last transition,
`orbit_alerts_active`), whoever is or is not messaged.

`orbit_monitor_silence(name, duration, reason)` writes an Alertmanager silence matching
that monitor only. The monitor keeps evaluating; the silence stops the message.
`orbit_monitor_status` lists a monitor's silences in force (who made each, until when,
why), and `orbit_monitor_unsilence(name)` lifts them before they run out. A silence that
runs out lifts itself.

## Removal

`orbit_monitor_delete` removes the file and the objects it rendered. `orbit_project_remove`
removes a project's monitors with the project: their files in the removal commit, their
rules and probes before anything they watch is deleted. A monitor never outlives its
project.

`make verify` counts a probe monitor's target as a declared probe.

## The platform's own alerts

These are not monitors and cannot be edited through the tools. Among them:

| alert | fires when | scope |
|---|---|---|
| `PlatformContainerRestarted` | a container restarted more than twice in 15 minutes, or once after an out-of-memory kill | the platform's namespaces and every `*-prod` namespace. **Not dev**: a container that crash-loops in a dev namespace raises no alert. Declare a metric monitor on `kube_pod_container_status_restarts_total` for a dev environment that should page |
| `OrbitWorkloadMemoryAtLimit` | a workload sits at its memory limit | project namespaces |
| `OrbitCommitRefused` | the commit gate refused the instance repository's newest commit: nothing can be shipped until a commit passes | the instance |
| `OrbitPlatformMemoryOverBudget` | the platform's own pods hold more memory than budgeted | the platform |

The full list is the rules file itself.
