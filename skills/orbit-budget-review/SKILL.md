---
name: orbit-budget-review
description: "Read the platform's memory budget and health: what headroom, surge_fits, days_left and unready deployments mean, and the rules of thumb for raising a concern. Read orbit-operator first."
role: viewer
---

# Review the budget and platform health

Two viewer tools with no arguments read the cluster directly; neither needs the
instance repository or an observability backend. A third, `orbit_size_recommend`, reads
Prometheus and says what one project's components should ask for (section 3). A `FORBIDDEN` result means the token
holds no Orbit role. Every number below is a rule of thumb you state, never a fact
about this cluster; the cluster's own numbers come only from the tools.

## 1. `orbit_platform_budget()`

A listing of one row per namespace plus a `_total` row, and `node_allocatable_mi`.
Per row: `requests_mi` and `limits_mi` (what the pods ask for and may grow to),
`used_mi` (live usage, `null` when the metrics API did not answer), `quota_hard_mi` and
`quota_used_mi` (`null` when the namespace has no ResourceQuota).
`quota_used_mi` is the quota's own `used` figure as the cluster reports it, and it can
exceed `requests_mi`: the quota charges every pod it admitted, this row sums the pods
active now, each at its effective request (the larger of its app containers' sum and its
biggest init container, as the scheduler counts it). Read `quota_used_mi` against `quota_hard_mi` for
headroom and `used_mi` against `requests_mi` for truth; do not reconcile the two sums.

Read it as three comparisons:

- **Reservation**: `_total.requests_mi` against `node_allocatable_mi`. The scheduler
  places on requests, so this is how full the node is. Above roughly 80% a new
  component or a surge pod will start failing to schedule; say so.
- **Overcommit**: `_total.limits_mi` against `node_allocatable_mi`. Limits above
  allocatable is normal; limits several times allocatable means one bad hour of
  memory growth ends in OOM kills across namespaces, not one.
- **Truth vs plan**: `used_mi` against `requests_mi` per namespace. Usage far below
  requests is reserved memory nobody uses; usage above requests is a component whose
  request is a guess, and it is the first to be evicted under pressure.

`backend_error` with `retryable: true`: retry once. A row with every value zero is a
namespace with no pods, not an error.

## 2. `orbit_platform_health()`

Four lists:

- `nodes[]`: `allocatable_mi` and `requested_mi` per node. Same reservation reading as
  above, per node.
- `quotas[]`, one per namespace that has a quota: `hard_mi`, `used_mi`,
  `headroom_mi` (hard minus used) and `largest_component_mi`. **`surge_fits`** is
  `headroom_mi >= largest_component_mi`: whether one more pod of the biggest component
  fits inside the quota. When it is `false`, a rolling update of that component cannot
  start its new pod before the old one stops; a deploy will stall or the rollout will
  report a progress deadline. Raise it before anyone ships to that environment.
- `certificates[]`: `not_after` and **`days_left`** per certificate. Below 14 days,
  renewal has been failing (automatic renewal normally runs well before that); below 7
  is urgent. `null` means the certificate has no recorded expiry (issuance pending or
  failed): treat it as the more serious case.
- `readiness[]`: only deployments with unavailable replicas, as `{namespace, name,
  replicas, ready, unavailable}`. An empty list is the healthy answer. Any entry is a
  component to diagnose (`orbit-diagnose`, with the namespace split into project and
  env via `orbit_project_list`).

## 3. `orbit_size_recommend(project, env)`

Ask it when the budget shows a namespace whose `used_mi` is far from its `requests_mi`,
when the node is past the reservation rule of thumb and something has to shrink, or
when asked whether a project is sized sensibly. One item per component with a container
running now; `memory` and `cpu` are judged separately, each with what is set today,
what was used over the window (seven days unless you pass `window_days`), a `verdict`
and a one-sentence `reason`:

- `lower` or `raise`: `suggestion` is more than a tenth away from what is set today.
  Quote the suggestion and the reason.
- `leave`: within a tenth. Nothing to do.
- `no_suggestion`: it will not guess, and `reason` says why: less than a day of
  history, gaps in the data, or memory that sits at its limit without being killed.
  That last one is not "fine": memory use does not show the need of a runtime that
  manages its own heap, which can fail inside its limit with no kill and no restart.
  Read its error rate and its own heap metrics (`orbit-diagnose`) and say it may need
  more.

It only advises: it changes nothing, and no suggestion is below the highest memory use
it saw. A change is applied with `orbit_catalog_set` (`orbit-change-service`, the
operator role) and judged again a week later, when the window holds only the new size.

## The answer

Lead with what needs a decision, if anything: a namespace where `surge_fits` is
false, a certificate under the thresholds, a deployment in `readiness`, a node past
the reservation rule of thumb. Then the numbers you read, quoted from the tools with
their field names, so the reader can check them. If both tools returned nothing
alarming, say "no concern" and give the reservation and the smallest headroom as the
two numbers to remember. Do not propose limit changes as if you could apply them;
this walk is written for a viewer, whose token carries no change tools: describe the
change for a human with the instance repository to make.
