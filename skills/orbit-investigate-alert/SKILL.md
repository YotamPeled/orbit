---
name: orbit-investigate-alert
description: "From an alert name or labels to a cause: alerts_active → monitor_status → the diagnose walk on the named component. Read orbit-operator first."
role: viewer
---

# Investigate an alert

Input: an alert name, or a set of labels, or nothing more than "something is firing".
Every tool here is viewer-role. A `FORBIDDEN` result means the token holds no Orbit role;
report it and stop.

## 1. What is firing right now?

`orbit_alerts_active()`. Items are `{name, severity, labels, starts_at, summary,
description}`; `name` is the `alertname` label. `description` is the rule's own account of
the cause and the next step: read it, and when it names a tool, call that tool. Pick the item you were asked about; with no name given,
take every item that is not a heartbeat (a `severity` of `none` is informational).
Record `starts_at`: it bounds every later query.

- The alert is not in the list: it resolved, or it is silenced (this tool hides silenced
  and inhibited alerts). Say which you cannot tell apart, and continue with step 2 to
  see whether the rule is still `pending`.
- `backend_unconfigured`: the server has no Alertmanager; go to step 2, which reads
  the rule state from Prometheus instead.
- `backend_error` with `retryable: true`: retry once, then report the gap.

## 2. Which monitor is it, and what does it watch?

`orbit_monitor_status(name=<alert name>)` takes one optional parameter, `name`: pass
the alert name from step 1, or omit it to get every declared monitor with its state.
The item carries `state` (`firing`, `pending`, `inactive`), `project`, `env`, `kind`,
`severity`, `summary`. `project` and `env` are the ids the diagnose walk needs; when they are empty, take them from the
alert's `labels` (a `namespace` label is `<project>-<env>`; confirm the split against
`orbit_project_list`).

- `not_found`: the alert is not a declared monitor (a platform-level rule, or a name
  typed wrong). Call `orbit_monitor_list()` to see what is declared; if it is not there,
  diagnose from the labels alone and say the rule is not one Orbit manages.
- `state: inactive` while step 1 showed it firing: the two backends disagree, usually
  a rule that was just changed; report both readings rather than picking one.
- `backend_unconfigured` naming the instance dir or Prometheus: report the gap and
  diagnose from the alert labels.
- The rule behind the alert: `orbit_monitor_status(name=...)` returns it in `definition`,
  the monitor's whole file: `query`, `condition` (`comparison`, `threshold`, `for`),
  `target` for a probe, `notify`. Quote the query and the threshold. `silences` on the
  same item lists the silences in force for the monitor (who, until when, why): a
  silenced monitor still reads `firing` here while nobody is messaged.

## 3. Diagnose the component

The alert's `labels` name the component in one of `component`, `container`, `pod`
(a pod name starts with `<project>-<component>-`) or `deployment`. With project, env
and component in hand, run the walk in `orbit-diagnose` from its step 1, using
`starts_at` from step 1 as the centre of the metrics and logs windows.

An alert whose labels carry only `alertname`, `prometheus` and `severity` is one of the
platform's own rules (its summary names what it measures, e.g. the platform memory
budget); `orbit_monitor_status` answers `not_found` for it because no monitor declares
it, and no tool returns its expression. Report it as a platform alert with its summary
quoted and, for the memory budget, the reading from `orbit-budget-review`.

If the labels name no component (a namespace-wide or node-wide alert), run
`orbit_workload_list(project, env)` and diagnose every pod whose `ready` is false or
whose `restarts` is above zero; if there is none, the alert is about the platform, not a
workload: read `orbit-budget-review`.

## The answer

1. **The alert**: name, severity, since when, and its summary, quoted from step 1.
2. **What it watches**: the monitor's `kind` and `summary` from step 2, or "not an
   Orbit-declared monitor".
3. **The cause**, with the diagnose walk's evidence per step, or "cause not
   determined" naming the step that failed and its `what_failed`.
4. **What a human should decide**: the fix, or that the alert is noise and the monitor
   needs tuning (monitor changes go through `orbit_monitor_upsert`, which needs an
   operator token: say what should change and leave it to someone holding one).
