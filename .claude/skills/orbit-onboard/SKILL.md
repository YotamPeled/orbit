---
name: orbit-onboard
description: "Model an existing project as an Orbit catalog entry: components, workload classes, database, domains, metrics. Use when adding a project to Orbit."
---

# Onboarding a project onto Orbit

Your output is a **catalog entry** for `catalog/projects.yaml`. Nothing else. What the app
repo must satisfy to be deployable is `orbit-develop`; getting the entry live and proving it
landed is `orbit-ship`.

Orbit derives everything from the catalog — routing, TLS, namespaces, quotas, the database,
probes, the portfolio card. So onboarding is deciding *what to declare*, and the failure to
avoid is declaring something the platform will then enforce badly.

## 1. Read the repo first

Establish from the code, never from assumption:

- **What runs.** One process or several? A server, a background loop, a scheduled job?
- **What it listens on**, and whether it has a health endpoint.
- **What it stores.** A database? Files? Nothing?
- **What it needs at runtime** — environment variables, secrets, external APIs.
- **What UID its image runs as.** The chart pins 65532 and refuses to render a component
  that would run as root.
- **Whether any state lives in the process.** This is the question most often skipped, and
  it decides `singleton` below.

## 2. One component per process

`kind` is the shape:

| kind | Produces | Use when |
|---|---|---|
| `web` | Deployment + Service + Ingress path | it serves HTTP |
| `worker` | Deployment only | a background loop, no inbound traffic |
| `cron` | CronJob (needs `schedule`) | it runs on a schedule and exits |

`language:` (`python` · `dotnet` · `nodejs` · `java`) is the other key worth knowing here:
it is what turns on OTel SDK injection — the operator's webhook reads the annotation the
chart renders from it and adds the runtime's SDK — so connected traces are a catalog fact
rather than an app change. Omit it and the service emits no spans: a web component keeps
its Traefik RED, but a worker's panels stay empty until the app is instrumented by hand
(see `orbit-observe`), and `orbit_dashboards_check` reports those panels as `no_data`.

**`singleton: true` applies to anything that becomes a Deployment — `web` as well as
`worker`.** It pins one replica and swaps the strategy to `Recreate`. (A `cron` is already
serial; `concurrencyPolicy: Forbid` is set for it, and `singleton` there does nothing.) Set it whenever losing in-process state would be visible to a
user. A game server that holds every live game in an in-process dictionary is a web
component *and* a singleton: a second replica hands a player a game it has never heard of,
and a rolling update drops every game in flight. A few seconds of downtime per deploy is
the correct trade for that; two overlapping pods is not.

Note the second thing `singleton` costs: a rolling `web` component gets the platform's
graceful-roll drain (a `preStop` sleep so it keeps serving while the routers drop it), and
a singleton does not — under `Recreate` there is no second pod to serve during the swap,
so a drain would lengthen the roll without shortening the outage. Choose `singleton` for
the state, knowing its deploy is a visible gap rather than a seam.

## 3. Workload class — the decision that matters most

`class` sets the CPU request, and **the ratio between requests is the ratio of CPU under
contention**. That ratio is the entire isolation mechanism.

| class | CPU request | Choose when |
|---|---|---|
| `interactive` | 250m | a person is waiting on the response |
| `batch` | 20m | nobody is waiting — imports, refreshes, renders, backups |
| `background` | 10m | daemons, exporters, static file servers |

A low request on batch work is **not** throttling. There is no CPU limit anywhere in the
platform, so a batch job takes every idle core and yields within one scheduling period the
moment an interactive request arrives. Fast when the box is quiet, invisible when it is busy.

Asking for `interactive` for something nobody waits on is how a nightly import ends up
competing with a live page.

**Memory is the opposite.** `memory:` sets request *and* limit together. Size it to observed
usage plus headroom. It also decides survival: these pods are Burstable, and the kernel's
OOM score scales inversely with the memory *request*, so an under-declared critical service
is one you have volunteered for death.

## 4. Databases are declared, never configured

```yaml
database:
  size: 2Gi
  memory: 256Mi
```

That is the whole interface. It renders a CloudNativePG `Cluster` and nothing else that
you name: CloudNativePG creates the database, the role and the password, and publishes them
in a Secret named `<project>-db-app` which the chart declares automatically — so components
may reference it without repeating it. What BACKS IT UP is not named here either: that
follows from the instance's `objectStorage`, not from this entry — see the bullets below.

**No database password is ever chosen by a person.** Do not seal one into the repo, do not
invent a connection string. Components read the generated Secret:

```yaml
env:
  PGHOST:     {secret: widgets-db-app, key: host}
  PGPASSWORD: {secret: widgets-db-app, key: password}
```

Each environment gets its own Cluster and its own credentials. Sharing one would let a dev
deploy write prod's data — the boundary the per-environment sealed Secrets exist to hold.

Backups depend on one thing you do not declare here: whether the instance has a platform
bucket (`objectStorage.buckets.platform` in `instance/config.yaml`).

- **No bucket** — every environment gets a nightly `pg_dump` to a local PVC plus a dump on
  every deploy. A recovery point of a day, on the same box as the database.
- **A bucket** — a **prod** database additionally gets continuous WAL archiving and
  scheduled base backups (a CNPG `ObjectStore` + `ScheduledBackup`), automatically, with
  no catalog key: a recovery point in seconds and PITR. The local dump block is then
  superseded and does NOT render for that environment — the bucket is the backup. Dev is
  untouched either way: its data is disposable, and archiving it would put the bucket
  credential in the dev store for nothing.

(Corrected 2026-09-07: this section used to say backups were local dumps and never WAL
archiving. Verified by rendering the same catalog entry for prod and dev against an
instance declaring a platform bucket.)

## 5. Secrets are pointers

```yaml
secrets:
  - name: widgets-app
    keys: [API_TOKEN]        # sealed in git, same names in the Secret
  - name: widgets-db-app
    from: external           # produced elsewhere; declared, not created
```

Every Secret a pod may reference must be declared here or the chart refuses to render. That
guard is not pedantry: Kubernetes parks a pod with a missing `secretKeyRef` in
`CreateContainerConfigError`, which reads like a scheduling hiccup, and a missing `envFrom`
Secret is skipped in **total silence** — so a typo ships and the first symptom is an
application authenticating as nobody.

Values never appear in the catalog. See `orbit-ship` for adding and rotating them.

## 6. Domains

The instance's domains are in `instance/config.yaml`; `source scripts/lib-instance.sh`
exports them as `$ORBIT_DOMAIN` (primary) and `$ORBIT_DEV_DOMAIN`. Never type a hostname
from memory — this skill is core and runs in every org.

- `<id>.<primary domain>` is automatic — the wildcard record and the platform certificate
  already cover it. Nothing to declare, nothing to wait for.
- `environments.dev.enabled: true` gives `<id>.<dev domain>`, basic-auth protected
  automatically, because a dev URL is a public URL.
- `expose: false` on an environment gives it no public door at all: no hostname, no
  Ingress, no certificate, no probe. Declare it for something only its neighbours call —
  a webhook sink, a worker's admin port — because the default is to invent
  `<id>.<primary domain>` for anything without an explicit hostname, which publishes an
  unauthenticated endpoint whose only protection is that nobody guessed the name. Naming a
  hostname beside it is refused rather than guessed at.
- A custom domain goes under `customDomains`. Inside a zone Orbit runs DNS for, it gets
  DNS-01 — issuable *before* traffic moves, which is what makes a cutover a DNS change alone
  with the old system still standing as the rollback. Otherwise HTTP-01, which requires the
  domain to already point here.

## 7. Metrics — counters for rates, the database for totals

Declare `metrics: {port, path}` and expose Prometheus format.

**What a web component serves is public.** Its Ingress routes the whole of `path` to it, so
with `path: /` the page at `/metrics` and the health checks (`probePath`, `readinessPath`)
answer on its public hostname too: in prod to anyone, in dev behind the basic-auth door.
Prometheus and the kubelet do not use that door (they reach the pod inside the cluster),
so nothing needs it open. If the numbers or the health output should not be public, serve
metrics on a port of their own (`metrics: {port: 9090}`; only `port` is routed) or give
the component a narrower `path`. Never put a secret in either.

- **Rates and windows** (today, this week) → **counters**. `increase(x_total[24h])` handles
  pod restarts correctly, which is exactly what counters are for.
- **All-time totals** → **a row in your own database**, re-exported as a gauge. A counter
  resets on every deploy and retention is 14 days, so "all time" is a property of the data,
  not of the monitoring system.
- **Rankings** (top cities, top users) → **`GROUP BY` in the database**, exposing only the
  top N. A label per city mints a time series per city; thousands of them hurt a 16 GB box
  to answer a question SQL answers for free.

Say what the number *is*. A lifetime gauge that counts games **started**, not finished, and
only since the service moved to Orbit, has to say so wherever it is drawn: the system it
replaced usually kept a different figure, or none, so there is nothing to backfill. A panel
that implies otherwise is a false claim on a dashboard.

`metrics:` also renders a ServiceMonitor. Its CRD arrives with `kube-prometheus-stack`,
which the template ships and `make init` renders into `clusters/platform/apps/` at sync
wave 3 — comfortably before `services` at wave 5 — so on a scaffolded instance the CRD
exists before the first tenant syncs and the key is safe to declare from the start.
(Corrected 2026-09-07: this section used to say the CRD was absent until the monitoring
stack "lands", which was true of the reference org before the stack was in the template and
is not true of an org scaffolded from it.) A `cron` component may not declare `metrics:`
at all — its pod exists for seconds, so a scrape of it reports nothing and no error; the
chart refuses to render it.

## 8. Agents are catalog entries too

An AI agent is not a project. It goes under `agents:`, a sibling of `projects:`, and the
generator renders every agent into one namespace (`orbit-agents-prod`) on the platform's
runtime image — a Deployment + Service for a `mode: deployment` agent, a CronJob with no
Service for a `mode: cronjob` one. You write what it is, never how it runs:

```yaml
agents:
  - name: deploy-watcher     # DNS label ≤22 chars
    prompt: |                # required; long instructions belong in a skill instead
      You watch ships and explain failures.
    skills: [orbit-ship]     # directories under .claude/skills, mounted read-only
    class: background        # background unless a person is waiting on the answer
    memory: 512Mi
```

`model:` defaults to `claude-sonnet-4-6`; `image`/`tag` default to the instance's
`agentRuntime` pin. The key set is closed — an unknown key is refused, because a value
pasted into a catalog entry is a credential committed in plaintext.

`mode: cronjob` **is** rendered: with a `schedule:` it produces a CronJob on the same
runtime image, with no Service and no door, and `concurrencyPolicy` defaults to `Forbid`
because each concurrent run holds the org's subscription token. (Corrected 2026-09-07:
this section used to say a scheduled agent was refused.) `triggers:` is what makes an
agent event-driven — `alert`, `github`, `a2a`, `metric`, `logs`, a closed schema per type
— and declaring any of them renders the intake event router into the plane; `cron` is not
a trigger type, because `mode: cronjob` already is one. The router costs nothing until the
first trigger exists, and needs `intakeRuntime` pinned in `instance/config.yaml`, with
`github` and `logs` each gated on a further instance attestation. Agents are V2 in
orbit-core's docs/V1.md §9 — rendered by the generator but not part of what V1 tests —
so declare one only when the owner asks for it, after reading `docs/ONBOARDING.md` §8
in orbit-core at the tag in this repo's `orbit-core.lock`.

Two credentials are the operator's, not yours, and both must be sealed into
`clusters/platform/secrets/<namespace>/<name>.yaml` BEFORE the entry is committed (the
operator renders no Secret for a key that is not sealed):
`CLAUDE_CODE_OAUTH_TOKEN` once for the plane, and `AGENT_<NAME>_TOKEN` per agent — the
bearer every caller must present, without which the runtime refuses every task with 503.
See `docs/ONBOARDING.md` §8 in orbit-core, at the tag in `orbit-core.lock`.

## 9. Output

The commented block at the top of `catalog/projects.yaml` is a worked entry — one web
component, one worker, a database, a supplied secret and a custom domain — in the shape
most first projects have. Read it before writing a new one; the decisions above are what
it is made of.

1. The catalog entry, ready to paste, with a one-line comment on each non-obvious choice.
2. Required app-repo changes — from `orbit-develop`, referenced not restated.
3. Anything the repo did not answer, stated as a question rather than a guess.

Do not edit `catalog/projects.yaml` yourself unless asked. Modelling and deploying are
separate on purpose: **a catalog commit is a deploy.** So when you are asked, that commit
lands the way every deploy lands: **nobody pushes directly to `dev` or `main`** — a branch
and a reviewed pull request, and a shared branch is never force-pushed. The one exception
is orbitd's ship, which commits `catalog/tags.yaml` straight to `main` as the org's GitHub
App; nobody writes that file by hand.
