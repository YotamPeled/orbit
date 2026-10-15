---
name: orbit-start
description: "The front door: what Orbit is, where this instance lives, how a branch becomes a deploy, and which skill to read next. Read first."
---

# Start here

## What Orbit is

A per-organization platform: **a repo plus one catalog entry in, a production service
out** — URL, TLS, environments, CI→deploy, Postgres with credentials no person ever sees,
secrets, metrics/logs/traces, generated dashboards, alerts, and a verify suite that proves
all of it by execution. One cluster per org, so isolation is physical rather than a
permissions matrix somebody maintains. Everything is declared in one catalog file and
rendered by a generator; **nothing reaches the cluster except through a commit**, which is
what makes `git log` the deploy history and `git revert` the rollback.

## Where you are

Two repositories, and they are not the same thing:

- **The instance repo** — this org's own: `instance/config.yaml`, `catalog/projects.yaml`,
  the generated `clusters/` tree, and a vendored copy of core. Its address is
  `instance/config.yaml` → `git.repoUrl`.
- **orbit-core** — the product: the catalog schema, the generator, the service chart, the
  verify framework, these skills. The instance pins one release in `orbit-core.lock` and
  vendors it with `scripts/core-sync.sh`. **Never edit a vendored file in the instance
  repo** — the fix belongs in core, then a pin bump.

Read the identity from the config file, never from memory. Core files (these skills
included) carry no hostname of their own:

```bash
. scripts/lib-instance.sh
# ORBIT_INSTANCE_NAME  ORBIT_DOMAIN  ORBIT_DEV_DOMAIN  ORBIT_REGISTRY
# ORBIT_REPO_URL  ORBIT_GITHUB_ORG  ORBIT_CONFIG_DIR (= ~/.config/<instance>-platform)
export KUBECONFIG=~/.kube/config-${ORBIT_INSTANCE_NAME}-platform
```

`ORBIT_CONFIG_DIR` is where this workstation keeps the instance's credentials — 0600,
never in git, referenced by path and never printed.

## Branch → environment

**`main` deploys to prod, `dev` deploys to dev**, and the catalog is what says so —
`environments.<env>.branch`, per environment, per project.

Two lists have to agree, and this is the part that surprises people:

- **The catalog** decides which branch an environment deploys FROM.
- **The tag** says which commit an image came from: `<branch>-<sha7>`, built on the
  operator machine from a clean, pushed commit. Nothing is built on push: a commit has no
  image until somebody builds it, and no image until somebody ships it.

## The URLs

Every platform surface is a name under this instance's primary domain — `$ORBIT_DOMAIN`,
so read them, do not memorise them:

| URL | What it is |
|---|---|
| `https://grafana.$ORBIT_DOMAIN` | metrics, logs, traces, the generated dashboards |
| `https://orbit.$ORBIT_DOMAIN` | orbitd: the MCP control surface agents operate the instance through (`/mcp`), and `/healthz` |
| `https://<id>.$ORBIT_DOMAIN` | a project, one name per catalog id; `<id>.$ORBIT_DEV_DOMAIN` for its dev environment, basic-auth protected |

No address and no dashboard applies the repo to the cluster. Tenant pairs are
applied by orbitd itself, and only when the instance runs with its apply switch on
and the pair is listed (`orbitd.applyPairs`) — with the switch off the job is
refused with nothing applied and nothing else picks it up. The platform layer is
applied from the desk with `orbit platform apply`.

`intake.$ORBIT_DOMAIN` (the agent plane's event door) exists only where the instance has
declared `intakeIngress: wired` — a fresh org has no public webhook door at all.

## How a repo gets an image

Images are built on the operator machine, never in CI and never in the cluster: from a
clean working tree whose commit is already pushed, `docker buildx build --platform
linux/amd64 -t <image>:<branch>-<sha7> --push .`, then the tag is shipped (operator skill
`orbit-ship`, step 0, has the exact rules).

1. The tag is **`<branch>-<sha7>`**, pushed to the component's `image` in the catalog.
   A branch with a `/` in it cannot become a docker tag; build from a branch without one.
2. Deploys ship through orbitd (`orbit_deploy_ship`): it takes a built tag and commits it
   to `catalog/tags.yaml` in the instance repo as the org's GitHub App.
3. orbitd applies the commit to the cluster. `git log catalog/tags.yaml` is the deploy history.

Tags are bumped by the ship and never by hand; `catalog/projects.yaml` is the file people
edit.

## How to get a login

Grafana signs in with a local admin account whose password the
operator holds — it is the sealed `grafana-admin` Secret. There is no
single sign-on: ask the operator for the login, and there is no second console to
ask about.

## Which skill to read next

| You are about to | Read |
|---|---|
| put an existing project on Orbit | `orbit-onboard` — what to declare in the catalog, and why |
| write or change the app's own code | `orbit-develop` — the contract the code must satisfy |
| get a change live, or work out why one is not | `orbit-ship` — catalog → sync → push → orbitd apply → proof |
| add a metric, a dashboard or an alert | `orbit-observe` |

Three things worth knowing before any of them: **a catalog commit is a deploy**;
generated files under `clusters/` are output — edit the catalog or the chart, never the
render; and **nobody pushes directly to `dev` or `main`** — every change reaches them
through a reviewed pull request, and a shared branch is never force-pushed. The one
exception is orbitd's ship, which commits `catalog/tags.yaml` straight to `main` as the
org's GitHub App; nobody writes that file by hand.
