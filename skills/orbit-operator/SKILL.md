---
name: orbit-operator
description: "Orientation for an agent operating Orbit through orbitd's MCP tools: what Orbit is, roles and scope errors, how to read every evidence envelope, and which skill to read for each job. Read first."
role: viewer
---

# Operating Orbit

## What Orbit is

Orbit is a per-organization platform that turns a catalog entry into a running service:
URL, TLS, environments, deploys, a database, secrets, metrics, logs, traces and alerts.
Every desired state is a commit in the instance repository (the catalog, the tags file,
the monitors), and a generator renders it into what the cluster runs. `orbitd` is the one
process you operate it through: an MCP server whose tools read the cluster and the
observability backends and, for the roles that may, change the desired state. You never
touch the cluster directly; you hold a token, you call tools, you read their evidence.

## Roles and scope errors

Three roles, each containing the ones below it: `viewer` (read-only tools), `operator`
(viewer plus the change tools), `admin` (everything, including principals). Your token
carries one role; `tools/list` shows only what it may call.

**Which role you hold** is read from that list, not guessed: if `orbit_auth_list`,
`orbit_auth_issue`, `orbit_auth_revoke` and `orbit_auth_retire` are absent, the token
is below `admin`; if
`orbit_deploy_ship`, `orbit_deploy_rollback` and `orbit_workload_restart` are absent
too, it is `viewer`; present without the admin three, it is `operator`. A tool that is
not in your list does not exist for you: do not search the host's tool index or the
skill files for it, and do not guess names. Every tool this server has is named in
these skills, spelled `orbit_<verb>`.

A call above your role is refused before it runs, and the refusal reaches you as an
ordinary error result: `code: FORBIDDEN`, with `required_role` and `held_role` in
`evidence` and `next_action` naming the scope to present. Your session survives it, so you
can read the reason and act on it. The fix is not a retry: report which role the task needs,
and stop or hand over. Do not look for another tool that does the same thing at a lower
role; there is none.

Only a token that is missing or unusable is refused before the protocol, as a 401: there is
no session to answer in, so that one reaches you as a transport failure rather than a result.

A tool whose feature is off is refused a third way: `code: FEATURE_OFF`, with
`evidence.feature` naming the optional feature the tool belongs to and `next_action`
saying how an admin turns it on. The platform is not broken and your role is not too
low: that part of it is switched off here (`orbit-features`).

A name no tool answers to is refused a different way: an error result with
`code: UNKNOWN_TOOL`, naming no role and no scope. The fix is `tools/list` and the
spelling, not another token: no credential makes that name callable.

## Reaching a tool from the desk client

An agent that has the desk client binary and a URL rather than a host holding the
connection logs in once, then calls tools. The login stores a session for the URL,
and a call with no stored session is refused before any request leaves the machine,
so on a clean machine the login comes first:

`orbit login --url <url>`

For example, `orbit login --url https://orbit.example.invalid/mcp` — sign in with the full interface address ending in `/mcp`, not the bare host, since the server issues tokens only for that exact address, so append `/mcp` to the host you were handed. Once exactly one session is stored, `orbit tools` and `orbit call` need no `--url`; with several stored, pass `--url` to pick one, and the bare host or the full address both name the same session.

Then invoke a tool by naming it and passing the arguments as one JSON object:

`orbit call <tool> --url <url> --json '{...}'`

`--json` carries the arguments and selects the machine form, the answer's document alone
on stdout. Without it the desk prints the human form: for an answer that carries a
`status` (a change, a dry run, a job) one line, `<tool>: <status>` with `code=` and
`what_failed=` parts (and one more label the summary owns) when the answer carries them,
and the steps and evidence only under `--json`; for any other answer `<tool>: <the document, compact JSON>`. A
failure is the sentence below.

A tool that changes something asks for confirmation, which an agent with no
terminal cannot answer interactively. Answer it on the call itself: `--yes`
confirms and `--no` refuses, which is what the shipping and rollback walks assume:

`orbit call orbit_deploy_ship --url <url> --json '{"project": "...", "env": "..."}' --yes`

## The evidence shapes

Every tool answers in one of these. Read the fields, not the prose; a tool's own
description says which fields its answer carries.

- **Listing** `{items, total, returned, truncated, next_cursor}`. `truncated: true` means
  the page or the cap cut the result; `next_cursor` is what you pass as `cursor` to get
  the rest. A conclusion drawn from a truncated listing is provisional until you page, or
  you say it is partial. `total` may exceed `returned`.
- **Failure** (`isError: true` on the wire) `{code, what_failed, evidence, next_action,
  retryable}`. The desk prints it as one sentence, `orbit: <tool> refused (<code>):
  <what_failed>; next: <next_action>`, or as the envelope itself under `--json`, and
  exits non-zero: 3 for an authentication failure, `FORBIDDEN` included; 2 for a usage
  error; 4 when the surface could not be reached; 1 for every other refusal. A success
  exits 0.
  An argument the tool's schema refuses (a missing required field, a wrong type) never
  reaches the tool: the protocol answers with a sentence and no envelope, and the desk
  shapes it into one, code `invalid_arguments`, `what_failed` the sentence, exit 2.
  `retryable: true` means the backend was reachable but the call failed (retry once,
  then report). `retryable: false` means retrying changes nothing: follow `next_action`.
  The codes you meet most often: `not_found` (a wrong project/env/component or an id no
  tool returned: list first), `backend_unconfigured` (the server lacks that backend; the
  variable is named in `evidence`; report it, nothing you do fixes it),
  `backend_error` (the backend answered with an error; `status` and `body` are in
  `evidence`), `FORBIDDEN` (above), `UNKNOWN_TOOL` (a name the surface does not offer:
  check the spelling against `tools/list`; no other token fixes it). The surface writes
  others, and a code not named here is an ordinary refusal rather than something
  unexpected: whatever the code, `next_action` says what to do and is what you follow,
  and where it is absent `what_failed` and `evidence` say what happened.
- **Verdict** from a mutating tool `{status, steps, commit, evidence, source}`. `status` is
  `applied`, `refused`, `failed`, `dry_run` or `running`; `steps` says which step
  stopped and why. Mutating tools ask for confirmation first (an input request the
  host answers, never you); a declined confirmation is `refused` and nothing was
  written. `running` means a job was started: `evidence.job_id` is its handle, and
  `orbit_job_wait` returns the final envelope as the job record's `result`.
  `source` is `{commit, age_s, lag_note}` for the git-sync checkout the call read:
  `age_s` is seconds since the sync time git-sync recorded when its record beside the
  checkout can be read, and then a sync that fetched nothing new resets it too, so a
  large value says the sidecar has not synced for that long -- neither lag nor the
  commit's age; when the record is there but cannot be read it is that file's own
  change time; when there is no record it is the time the checkout's HEAD file was
  last written, so then a large value says only that HEAD has not been written for
  that long; `lag_note` carries
  the bound on the lag itself.
- **Document**: one thing fetched whole -- a rendered manifest, a catalog schema, the
  platform's health, a job record. Its fields are its own, named in the tool's
  description, and there is no `items` to page. A job record's `status` is the job's,
  not a verdict: `pending` with `poll_after_ms` means ask again after that many
  milliseconds, and the verdict is in `result` once the job is done. Read the fields the
  description names; where the document carries a `source` or a `path`, quote it with
  any conclusion you draw from it.

## How the desk prints a value

A value from the server is printed as the server sent it, with the changes that keep a
value from passing for the desk's own words: a byte with no printable form is written
as its name (`\x0a` for a line break, `\u2028`; an emoji is printable and prints as
itself); a backslash the value contains is doubled; `(` and `)` in a value always carry
a backslash, and `,` `:` `;` carry one where the desk could have written them -- followed
by a space, or at the end of the value -- so `a,b` and `10:30` print as they are and
`healthy state\:` at the end of a value is the value's own colon; one of the desk's
reserved phrases (the labels of its own summary line, `code=` and `what_failed=` among
them, and `requires role `, `token holds `, `outside typed origin `) carries a backslash
wherever it starts inside a value, not only at the beginning. Read through the
backslashes; `--json` gives the untouched document.

## What you do not have

- **A clock.** No tool returns the current time as such; `orbit_metrics_query(query=
  "time()")` with no range does, as one unlabelled series whose single `[timestamp,
  value]` pair is the unix time (any instant query's pair carries the evaluation time
  the same way), and `orbit_deploy_status` / `orbit_workload_describe` carry `now`. Otherwise take time bounds from evidence: an
  alert's `starts_at`, a history entry's `at`, a log line's `ts` (unix nanoseconds), a
  pod's `age` and a component's `pod_age` (seconds before the call). Tools that take a
  window accept unix seconds or RFC 3339, and `orbit_metrics_query` with `start` and no
  `end` runs up to now.
- **The file system.** These skills are read by invoking them (the host's skill
  mechanism), never by listing or grepping files; a host that refuses a shell is the
  normal case.
- **Resources.** The server also publishes a few MCP resources (`orbit://…`) that carry
  the same payloads as the tools. Use the tools; the skills are written over them.

## The rule

Nothing is done until a tool's evidence says so. "The deploy should be fine" is not a
finding; `orbit_deploy_status` showing `drift: false` and a pod-spec image carrying the
desired tag and `rollout: complete` is. When a step's tool fails, say which step and quote `what_failed`; do not
fill the gap with a guess. Every fact about this installation (project ids, environment
names, component names, namespaces, hostnames, numbers) comes from a tool at run time:
start with `orbit_project_list`, and use only ids it returned.

Never end your turn to wait. A session that says "I will check again in a minute" and
stops does not come back, and a bare `sleep` is refused. Wait with something that
blocks until the condition holds: `orbit_job_wait` for a job a tool started, and
`orbit_deploy_wait` (project, env, and the commit) for a catalog change orbitd has yet
to apply. `orbit_deploy_wait` returns `applied`, or `waiting` when its
timeout ran out (call it again), or says why the commit will not be applied: `failed`
(a tick failed on it), `refused` (the commit gate refused it: `gate.check` and
`gate.sentence`), `off` (orbitd applies nothing for that pair). Report only once the wait
has returned something other than `waiting`.

## Which skill for which job

| I want to …                                            | read skill …              |
|--------------------------------------------------------|---------------------------|
| know why a component is unhealthy or why it restarted  | `orbit-diagnose`          |
| go from a firing alert to its cause                    | `orbit-investigate-alert` |
| check memory headroom, quota, certificates, readiness  | `orbit-budget-review`     |
| see what runs where, what is desired, and how it got there | `orbit-deploy-review` |
| list, issue, revoke or retire principals and tokens (admin) | `orbit-admin`        |
| see which optional features are on (logs, traces, dashboards, ...), or switch one (admin) | `orbit-features` |
| ship a tag and prove it landed (operator role)         | `orbit-ship`              |
| roll back, or restart a workload (operator role)       | `orbit-rollback`          |
| restore a database's data to a point in time or a backup (admin role) | `orbit-restore-db` |
| change a service's replicas, memory, hostnames or components; back up its database (operator role) | `orbit-change-service` |
| remove a project entirely (admin role)                 | `orbit-change-service`, "Removing a project" |
| onboard an organization from nothing to its first live project | `orbit-onboard`      |
| add a service or a project (author a catalog entry)    | no tool: the instance repository's `.claude/skills/orbit-ship/SKILL.md`, steps 1 to 3: the entry, then (for a new project or environment) its three copied Secrets and the two desk applies, BEFORE the first ship |
| upgrade the instance to a newer orbit-core tag         | no tool: the instance repository's `.claude/skills/orbit-ship/SKILL.md`, "Upgrading orbit-core" (a pull request, then desk applies) |
| force a TLS certificate renewal                        | no tool: the instance repository's `.claude/skills/orbit-ship/SKILL.md`, "Certificates" (a desk command) |
| define, change, delete, silence or unsilence a monitor (operator role) | see "Monitors" below |
| see what changed in a project's configuration, and when | `orbit_catalog_history(project)`: the commits that changed its catalog entry (the ledger lists image tags only) |
| stop a ship, rollback or restart that is stuck waiting (operator role) | `orbit-ship`, step 4: the tool `orbit_job_cancel` stops the wait and frees the pair; it undoes nothing |
| rotate a secret                                        | no tool: `orbit secrets set` at the desk |

The rows with no tool behind them: do not search `tools/list` for one, and say so
when asked, naming where the thing does live. Rotating a secret is a desk command
run by a person with the sealing key, not something a token can call.

The two operator skills need the `operator` role:
with a `viewer` token their tools are not in your list, and the right answer to "ship
this" is that the job needs an operator token, not a search. The read-only tools not
owned by a skill above are `orbit_db_status` (whether the database is up: `state`;
whether it is archived, the earliest moment it can be restored to, the last backups) and `orbit_traces_find` / `orbit_traces_get` (Tempo traces by
service and tags); `orbit_job_status` reads the same job record `orbit_job_wait`
waits for. Their own descriptions are enough to call them. `orbit_db_status` exists
while the `databases` feature is on, and the two trace tools while the `traces` feature
is on (`orbit-features`).
Tempo knows a service by the `service.name` the product's telemetry sets, which no tool
lists directly; the `service` label of `traces_spanmetrics_calls_total` (through
`orbit_metrics_query`) names the ones Tempo has seen, and `total: 0` from
`orbit_traces_find` does not tell a wrong name from no traces, so read that label first.

## Monitors

A monitor is one declared alert: its definition lives in the instance repository
and orbitd renders it to a PrometheusRule, a Loki ruler rule or a blackbox Probe.
Four tools change them, all `operator` role; two viewer tools read them back.

**The body.** One mapping, three kinds:

```
{"name": "api-errors", "project": "demo", "env": "prod", "kind": "metric",
 "query": "sum(rate(http_requests_total{namespace=\"demo-prod\",status=~\"5..\"}[5m]))",
 "condition": {"comparison": ">", "threshold": 1, "for": "5m"},
 "severity": "page", "summary": "demo's api is answering 5xx", "notify": ["telegram"]}

{"name": "api-door", "project": "demo", "env": "prod", "kind": "probe",
 "target": "https://demo.example.org/readyz", "condition": {"for": "2m"},
 "severity": "page", "summary": "demo's door does not answer", "notify": ["telegram"]}

{"name": "api-panics", "project": "demo", "env": "prod", "kind": "log",
 "query": "sum(count_over_time({namespace=\"demo-prod\"} |= \"panic\" [5m]))",
 "condition": {"comparison": ">", "threshold": 0, "for": "1m"},
 "severity": "warn", "summary": "demo's api logged a panic"}
```

`name` is a DNS label and the alert's name. `project` and `env` must be in the catalog.
`kind` is `metric` (a PromQL `query`), `log` (a LogQL `query` that yields a number) or
`probe` (an http(s) `target` with a public host; optional `module`, default
`http_2xx`). `condition` is `comparison` (one of `> >= < <= == !=`), `threshold` (a
number) and `for` (a Prometheus duration: how long it must hold); a probe takes only
`for` and fires when the target stops answering. `severity` is `page`, `warn` or
`info`. `summary` is the sentence a person reads. `notify` is `["telegram"]` or left
out; `runbook` is an optional URL. A probe needs the `uptime` feature, a log monitor
the `logs` feature, `notify: ["telegram"]` the `alerts-telegram` feature
(`orbit-features`); a body that needs one that is off is refused, naming it.

**What a monitor may watch.** A query is not confined to its `project`: it may read any
series or log stream the platform holds, another project's included, and a probe may
fetch any public address. `project` and `env` say whom the alert is about (they become
its labels), not what it can see. A monitor is an operator-role act for that reason;
write queries that select the project's own namespace (`namespace="<project>-<env>"`).

**Role first.** If `orbit_monitor_upsert` is not in your tool list, your token is
below `operator`: read with `orbit_monitor_list` and `orbit_monitor_status`, and
hand any change to an operator token. There is no lower-role tool that writes a
monitor; do not search for one.

- `orbit_monitor_upsert`: define a monitor or replace one that exists. Pass the
  whole monitor body; an invalid body is a tool error naming the field, before
  anything is written — fix the body, do not retry around it. `dry_run: true`
  renders without confirming or writing; do that first when the requester wants
  to see the plan. Otherwise the call asks for confirmation (it names the
  monitor; the host answers, never you) and starts a job: `confirm`, `validate`,
  `commit`, `apply`. `evidence.job_id` is reclaimed with `orbit_job_wait`.
  Who hears it fire is the body's `notify`: `["telegram"]` sends the alert to the
  human chat (the `alerts-telegram` feature must be on); left out, the alert is only
  recorded (`orbit_monitor_status`, `orbit_alerts_active`) and nobody is messaged.
  A requester who asks to be alerted wants the chat; `evidence.reaches` tells you
  which one you made.
- `orbit_monitor_delete`: remove a monitor the requester names. An unknown name
  is `not_found`: list first with `orbit_monitor_list`, do not guess. It
  confirms (naming every object that will go away) and prunes only that
  monitor's objects. A monitor that is only noisy is not a delete; see below.
  A name that is `not_found` in `orbit_monitor_list` but still shows in
  `orbit_alerts_active` is a delete that did not finish: delete it again; the call
  names the objects left in the cluster and removes them.
- `orbit_monitor_silence`: quiet one monitor's alert in Alertmanager for
  `duration` (`<n>[mhd]`, from 1 minute to 7 days) with a non-empty `reason`,
  recorded on the silence. No confirmation round; the silence id comes back in
  evidence. The monitor keeps evaluating underneath — a silence hides the page,
  it does not fix or remove anything. Use it for a known-noisy window (a load
  test, an incident already being worked), bounded and with a reason a stranger
  would accept; when the window ends, the alert speaks again on its own.
- `orbit_monitor_unsilence`: lift every silence in force for one monitor before it
  runs out. No confirmation; `evidence.lifted` is the ids, `[]` when it was not
  silenced. If the monitor is firing, whoever its `notify` names hears it again
  within minutes.

Read back with the viewer tools: `orbit_monitor_list` for what is defined (with each
one's `notify`), `orbit_monitor_status` (with a name, or bare for all) for firing,
pending or inactive, the last transition, the monitor's whole `definition` (change it
and pass it to `orbit_monitor_upsert`) and its `silences` in force. Nothing about a
monitor is done until one of those two says so.

A project's monitors go with it: `orbit_project_remove` deletes their files and their
rules before it deletes anything they watch.
