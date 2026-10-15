---
name: orbit-observe
description: Instrument a service on Orbit and build its dashboards and alerts. Use when adding metrics, changing a dashboard, or writing an alert rule.
---

# Observability on Orbit

Prometheus, Alertmanager and Grafana are one platform app (`kube-prometheus-stack`, wave 3).
Dashboards are ConfigMaps the Grafana sidecar loads from any namespace — the per-project
ones are GENERATED into `clusters/apps/dashboard-<project>-<env>.yaml` from the catalog.
Alert rules are `PrometheusRule` objects in `clusters/platform/monitoring/rules.yaml`.

**Everything here is in git.** A dashboard drawn in the UI is not a dashboard; Grafana
refuses to overwrite a provisioned one anyway, and the next apply of the rendered ConfigMap
would revert it.

## Choosing the metric shape

Four questions, four different answers. Picking the wrong one is not a style error — it
produces a number that cannot be right.

| the question | the shape | why not the obvious thing |
|---|---|---|
| how many in the last day/hour | **Counter**, queried `sum(increase(x_total[24h]))` | increase per series FIRST, then sum. `increase(sum(...))` reads a pod restart as a reset and erases the day |
| how many since launch | **Gauge read from the app's own Postgres** | a counter resets with the pod and retention is 14 days; it cannot answer this at all |
| which are the top N | **Bounded top-N gauge from a GROUP BY**, refreshed on a timer | a label per entity is unbounded. ~1,450 localities or ~2,800 cities would make retention a fiction |
| is it still working | **A last-success timestamp gauge**, alerted as `time() - x > N` | `up` only says the process answers scrapes. A listener that has lost its upstream answers scrapes forever |

### Label values are identifiers, not display strings

Every label value a counter has ever emitted stays queryable for the whole retention
window. So:

- **A closed vocabulary, enumerated in code.** Never build a label from a request path, a
  city name, or anything a client controls — that is a remote unauthenticated OOMKill, and
  it has already happened here.
- **Stable ASCII keys** (`home`, `city`, `other`), never the text you want on screen. A
  reworded page name silently empties a panel *and* leaves the old series behind it for a
  fortnight. Map key → display in the panel.
- **Pre-register closed label sets at boot.** A labelled counter creates its series on
  first use, so a freshly deployed pod exports nothing during a quiet hour and a `rate()`
  over it returns empty — indistinguishable from a renamed metric.
- **Except a gauge that must not exist until it is read.** The all-time figure from
  Postgres stays absent during a database outage; publishing `0` would say "nothing has
  ever happened".

## Choosing the panel

This is where correct data gets drawn as a lie. Match the panel to the metric's *shape*:

| the metric is | draw it as | drawing it wrong looks like |
|---|---|---|
| a cumulative total (all-time gauge) | **bar gauge**, instant query, a ranking | a flat horizontal line at the total. It is not broken; it has no shape over time |
| a count in a bucket (`increase(x[1h])`) | **bars**, with the query's step **pinned to 1h** | at the dashboard's own step this is a *rolling* hour recomputed every few seconds: ~2,900 hair-thin bars merging into a blob that reads as an area chart |
| a rate or a level over time | **line** | fine |
| a single current value | **stat** | fine |

Two more that only show up once rendered:

- **A value-scaled colour scheme on a ranking makes the leader red**, and red means "bad"
  on every other panel. Use a fixed colour for anything that is not a health signal.
- **`bargauge` scales its type to fill the panel.** Two series produced a 120-pixel
  numeral. Set `text: {titleSize, valueSize}` explicitly.

### Every panel needs an `id`

Without one Grafana assigns a fresh id at load, and nothing can address the panel — no
stable deep link, no `/render/d-solo` image, no alert pointing at the graph it fired about.
New panels take the next free integer.

## Look at it. Do not reason about it.

The image renderer is deployed for this. **A dashboard change is not verified until it has
been rendered and looked at** — three panels shipped in one week that were syntactically
correct, queried the right metric, and were visually wrong.

```bash
# The instance's own identity, never a hostname typed from memory: lib-instance.sh
# reads instance/config.yaml and exports ORBIT_DOMAIN and ORBIT_CONFIG_DIR
# (= ~/.config/<instance>-platform), which is where the workstation keeps credentials.
. scripts/lib-instance.sh
GPW=$(cat "$ORBIT_CONFIG_DIR/grafana-admin-password")
curl -s -u "admin:$GPW" -o /tmp/panel.png \
  "https://grafana.$ORBIT_DOMAIN/render/d-solo/<uid>/x?panelId=<id>&width=1000&height=420&from=now-24h&to=now&theme=dark"
```

Then read the PNG. The `grafana` MCP server is also registered (read-only, Viewer service
account) for querying dashboards and metrics without shelling out.

A rendered panel that reads "No data" is either a component that emits nothing yet or a
query that can never match, and the two look identical. Tell them apart with
`orbit_dashboards_check`: it runs every generated panel's query and reports `ok`,
`no_data`, or `broken` with the reason. A dashboard change is not verified until the
render looks right AND the checker reports no `broken` panel.

## Editing a dashboard

There are two kinds, and the difference decides where you edit.

**Generated, and most of them are.** Every project+environment in the catalog gets a RED
dashboard written by the generator to `clusters/apps/dashboard-<project>-<env>.yaml`. It is
output: `make sync` rewrites it and `make check` fails if it drifted. Change what the
catalog declares (`metrics:`, `language:`, the components) or change the generator in
orbit-core — never the rendered file.

**Hand-written platform dashboards**, if the instance keeps any, are ConfigMaps under
`clusters/platform/config/monitoring/`. Nothing is shipped there by default; the shape is
one ConfigMap per dashboard, the JSON inside a block scalar. Parse it, change it, write it
back at the same indent — **do not `yaml.safe_dump` the whole document**, which reflows the
file into an unreadable 991-line diff.

```
generated:    edit catalog/projects.yaml → make sync → commit → pull request → RENDER IT
hand-written: edit the JSON in the ConfigMap → commit → pull request → RENDER IT
```

A dashboard or an alert is a commit like any other, so it lands the same way: **nobody
pushes directly to `dev` or `main`** — a branch and a reviewed pull request, and a shared
branch is never force-pushed. The one exception is orbitd's ship, which commits
`catalog/tags.yaml` straight to `main` as the org's GitHub App; nobody writes it by hand.

## Alert rules

`clusters/platform/monitoring/rules.yaml`. Only write what the stack's ~120 defaults miss —
crash-loops and node pressure are already covered and a second copy pages twice, then
drifts.

- **Scope to the production namespace.** Dev resets its fixtures, which legitimately
  deletes rows; a rule that pages about a test gets ignored when it pages about an outage.
- **Alert on the absence too.** A "no successful poll for 10 minutes" rule cannot fire when
  the metric does not exist. Pair it with `absent(...)`.
- **Disable a noisy default by NAME, not by group.** `defaultRules.disabled` takes
  individual rules; disabling the group takes the useful ones with it.
- **When a default rule is wrong about our design, say why in the values file.** The
  quota-overcommit rules assume a quota is a capacity reservation; here it is a runaway
  guard. Shrinking the quota to satisfy the alert would turn a backstop into a budget
  nobody chose.

## Verifying, and the ways it has lied

- **Grafana through `kubectl proxy` is ANONYMOUS.** A dashboard check there gets a 401 body
  containing no dashboard uid and reads a loaded dashboard as missing. Ask
  `grafana.$ORBIT_DOMAIN` with the admin credential.
- **Ask the sustained question, not the instantaneous one.** "Is any target down right
  now" is red during every rollout. `max_over_time(up[5m]) == 0` is what the alert asks and
  what a check should ask.
- **A stale alert lingers in Alertmanager** for minutes after its rule is removed. Wait for
  it to expire rather than concluding the change did not work.
- **Row-count gauges from `reltuples` under-report for hours** after a bulk insert. `ANALYZE`
  after a backfill, or a "the record shrank" rule sees a phantom drop.
- **A `grep -c` over an empty joined list counts the trailing newline as one.** Being right
  about zero matters more than being right about ten.

## Instrumentation rules (Phase 4 — hand-written spans, when at all)

The whole app contract is: **be in the catalog, log JSON to stdout.** Identity, the SDK,
dashboards, alerts and trace links are generated. `language:` on a component is what turns
on SDK injection; a web component's RED comes from the ingress that already sees it, a worker's from the spans it emits, and a component that emits neither gets only the restart and OOM panels.

Nothing observes the service until it is instrumented, so instrument the calls that
make up its RED — SDK spans for HTTP and database calls are now the signal a worker's
rate, errors and latency are built from, and leaving them out leaves the worker's panels
empty:

1. Business transactions (an order, a classification, a payment) — one span per unit.
2. Queue/cron work units — a ROOT span per unit; a trace that starts from a timer has
   no incoming context to continue.
3. In-process async hops the kernel cannot attribute.
4. Decisions: cache hit/miss, feature flag, retry — span EVENTS or attributes, not spans.
5. Runtime internals and caught-but-meaningful exceptions.

Discipline that keeps the data queryable:
- Span names are LOW-cardinality (`verb object`, route templates). IDs go in span
  attributes (fine) — never in metric labels (lethal: cardinality) and never in names.
- `trace_id` goes in Loki as a JSON FIELD in the line (structured metadata), never a label.
- `SetStatus(Error)` on error paths; a 4xx is Error on CLIENT spans only.
- Emit 2026 semconv names as data: `http.request.method`, `db.query.text`,
  `deployment.environment.name`. An agent writing instrumentation from training data will
  emit the 2023 names (`http.method`, `deployment.environment`) — reject those in review.
- The join key (`service.name`, `service.namespace`, `deployment.environment.name`,
  `service.version`) is set by the PLATFORM via env vars. Never set it in code: a second
  spelling breaks correlation silently — Grafana renders no error, the link just isn't.

Debugging with traces (the loop the naming discipline exists for): enumerate attribute
values → TraceQL search (`{resource.service.name="x-api"} && {span.http.route="/api/y"}`)
→ get-trace. Cross-service search: `{resource.service.name="traefik"} &&
{resource.service.name="<svc>"}` proves propagation end to end.
