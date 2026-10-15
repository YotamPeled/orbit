---
name: orbit-ship
description: "Get a change live on Orbit and prove it landed: edit catalog, regenerate, push, watch orbitd apply, verify. Use when deploying or debugging a failed deploy."
---

# Shipping a change on Orbit

**A deploy is a commit.** Nothing reaches the cluster by hand. If you find yourself reaching
for `kubectl apply`, the change belongs in git. `kubectl` is for reading.

```
edit catalog/projects.yaml → make sync → commit → pull request → orbitd applies → prove it landed
```

## 1. Edit the catalog

`catalog/projects.yaml` is the only file you edit. If a change appears to need editing a
generated file under `clusters/apps/`, it does not — that file is overwritten on the next
sync. Change the catalog, or change the chart.

Image tags are the exception: orbitd's ship (`orbit_deploy_ship`) writes them to
`catalog/tags.yaml`, never a person. That split exists so a deploy is one obvious line and
`git log catalog/tags.yaml` is the deploy history, instead of every deploy looking like a
configuration change. The image behind a tag is built on the operator machine from a
clean, pushed commit (operator skill `orbit-ship`, step 0); nothing builds on push.

## 2. Regenerate and read the diff

```bash
make sync     # catalog/projects.yaml -> clusters/apps/*.yaml + clusters/apps/namespaces.yaml
make check    # fails if the generated files are not what the catalog generates (committed or not)
```

The diff **is** the deploy plan: which manifests change, which hostnames, which resources.
That review surface is the entire reason generated files are committed rather than rendered
at sync time. Read it before committing.

## 3. Commit and push

Commit to a branch and open a pull request: **nobody pushes directly to `dev` or `main`**,
and no shared branch is ever force-pushed. Reviewing that pull request is reviewing the
deploy plan from step 2 — here they are the same object. The one exception is orbitd's
ship, which commits `catalog/tags.yaml` straight to `main` as the org's GitHub App; nobody
writes that file by hand.

A new project or environment needs three things the catalog does not render, before or
with that pull request:

1. **Its sealed Secrets, one set per namespace** (`<project>-<env>`), under
   `clusters/platform/secrets/<namespace>/`. Each is a COPY of a sealed file the platform
   already holds, made at the desk with no value read:

   ```sh
   orbit secrets copy . --file clusters/platform/secrets/orbit/ghcr-pull.yaml --namespace <namespace>        # every namespace
   orbit secrets copy . --file clusters/platform/secrets/orbit/dev-basic-auth.yaml --namespace <namespace>   # every non-prod namespace
   orbit secrets copy . --file clusters/platform/secrets/orbit/orbit-s3.yaml --namespace <project>-prod      # prod, when the project has a database and the instance a bucket
   ```

   Without `dev-basic-auth` the dev door answers 404 instead of 401; without `orbit-s3`
   the production database runs and archives nothing (the commit gate refuses a sealed
   file that lacks a key its project declares, and `make verify` finds a stalled archive).
   Whatever the project's own `secrets:` declares is sealed by whoever holds the value
   (below, "Secrets").
2. **Its namespace-level objects, from the desk** once merged: `orbit platform apply .
   --app services` (namespace, quota, NetworkPolicy, dashboard, probe) and `orbit
   platform apply . --app secrets` (the sealed files above). The `.` is the instance
   directory and is required (`/instance` inside the desk; without it the command is a
   usage error). orbitd applies the workloads.
   Both BEFORE the first ship: a ship into a namespace whose `ghcr-pull` is not applied
   yet waits on `ImagePullBackOff` until its budget runs out and ends `LANDED NOT DEPLOYED`.
3. **A first ship** per component: until a tag exists the component renders idle (no
   replicas, a suspended CronJob), so nothing is pulled and nothing pages.

## 4. Watch the rollout — and read the running image

After the merge, wait for orbitd to apply it — do not end your turn to "wait", and do not
`sleep`. The `orbit_deploy_wait` tool blocks until the reconcile loop has applied the merge
commit to the pair, or says why it will not (`failed`, `refused` by the commit gate with
the failing check's sentence, or `off`). Call it once per environment changed, with the
merge commit, and again while it answers `waiting`:

```bash
git checkout main && git pull --ff-only && git rev-parse HEAD   # the merge commit to wait for
scripts/desk.sh orbit call orbit_deploy_wait --json '{"project":"<project>","env":"<env>","commit":"<merge commit>","timeout_s":120}'
```

Before merging, run the checks orbitd will run on the commit, so a refusal is found at the
desk and not on the box: `scripts/desk.sh orbit check all /instance`.

Nothing watches the repo from inside the cluster: orbitd applies the rendered
objects itself, and only for pairs named in `orbitd.applyPairs`. A push for any
other pair changed nothing — nothing else picks it up. With the apply switch off
the job is refused with nothing applied and the record says
`LANDED NOT DEPLOYED apply`.

```bash
# lib-instance.sh exports the instance's identity from instance/config.yaml; the
# kubeconfig bootstrap-node.sh writes is named for it. Never a hostname or a path
# typed from memory — this file is core and runs in every org.
. scripts/lib-instance.sh
export KUBECONFIG=~/.kube/config-${ORBIT_INSTANCE_NAME}-platform
kubectl -n <project>-<env> get deploy <project>-<component> \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

The running image is the honest check: the bump is not live until the pods carry
it. Two ways a quick read lies, both of which have cost time here:

- **orbitd reads a checkout that lags the push by about a minute.** An image read
  immediately after a push shows the world before it. Compare the running image to
  the tag you shipped; if it is the old one, wait past the lag and read again
  rather than pushing a second time.
- **A pod running the new image is not a working service.** "The pods started" is
  not "the service works." Finish with an external check, always (step 5).

If the new image never arrives, look before assuming patience will help:

```bash
kubectl -n <project>-<env> get pods
kubectl -n <project>-<env> describe pod <pod>    # Events is usually the answer
```

A rollout that never completes while the site is up is often a `PersistentVolumeClaim`
in `Pending`: the storage class binds on first consumer, so a volume nothing mounts holds the
rollout short indefinitely.

## 5. Prove it is serving

```bash
curl -sI https://<host>/
curl -s https://<host>/<a real path> | head
```

For a protected dev environment, **assert 401 specifically** — not merely "not 200". Traefik
answers **404** when a middleware or its credential Secret is missing, which looks like a
working guard and is the absence of one. (The dev username is `orbit`.)

Then the cumulative suites, which catch collateral damage:

```bash
make verify
```

Never weaken a check to make it pass. If a check is wrong, fix the check and make it fail
first — a check nobody has seen go red is indistinguishable from one that does nothing.

## Secrets

Values live sealed in git as SopsSecrets. Git holds sealed files, never plaintext.

**Adding one.** For a fresh random credential, mint it sealed straight into the
right namespace: `orbit secrets mint <instance-dir> --name <name> --namespace <namespace>`
writes `clusters/platform/secrets/<namespace>/<name>.yaml` with the org's age key, the
value travelling only on sops's stdin, never in plaintext. For a value someone hands you
(a bot token, a webhook secret), seal it into that same path yourself with the org's age
key, on the operator's own machine, never pasted in plaintext. `orbit secrets verify`
compares the sealed files with the live Secrets and names anything asked for that is not
sealed. Then declare it:

```yaml
secrets:
  - name: widgets-app
    keys: [API_TOKEN]
```

and reference it from a component as `env: {API_TOKEN: {secret: widgets-app, key: API_TOKEN}}`.
A component's `env` renders into every environment. For a variable only ONE environment
gets — a dev-only credential, a dev feature flag — put it in that environment's overlay
instead: `environments: {dev: {components: {api: {env: {API_TOKEN: {secret: widgets-app,
key: API_TOKEN}}}}}}` merges key by key over the component's env in dev alone (a `null`
value removes a key there), and the Secret then needs sealing only in that namespace.
Every Secret a pod references must be declared, or the chart refuses to render — because
Kubernetes will not fail it at runtime: a missing `secretKeyRef` parks the pod in
`CreateContainerConfigError` and a missing `envFrom` Secret is skipped in total silence.

**Rotating one.** For a minted credential, `orbit secrets mint <instance-dir> --name <name> --namespace <namespace> --rotate`
seals a fresh random value over the old files and commit the result — without `--rotate`
mint leaves existing files alone. For a hand-sealed value, seal the new value over the
old one and commit. **No deploy, no sync for the value itself** — the operator decrypts
the new file in place. Restart the
consuming pods if the application reads its environment only at startup — and note
that until they do they are still running the old value.

**Reusing one in another namespace.** `orbit secrets copy <instance-dir> --file
clusters/platform/secrets/<namespace>/<name>.yaml --namespace <other>` rewrites only
`metadata.namespace` through sops, which re-signs the file — the ciphertext travels
untouched, the values are never opened outside the tool, and the verb prints the copy's
path. The operator renders the copy. It refuses a non-sealed source, its own namespace, and an
existing target. The PITR drill uses it for the bucket credential.

**Environments cannot reach each other.** There is one sealed file per namespace and
the sealed namespace is the directory, so a dev file has no expressible way to become
prod's Secret. There is deliberately **no sealed file for `local`** — that environment
is the laptop, and a cluster route to it would make laptop-only values
cluster-reachable.

**Database credentials are not here.** CloudNativePG generates them into `<project>-db-app`.
Nothing to add, nothing to rotate by hand.

**How the sealed files open.** Each file carries a `sops` block naming the org's age
recipients; the operator holds the private key and decrypts in place. `make verify`
(stage4) proves every file under `clusters/platform/secrets/` is sealed, namespaced
as its directory, present live with a healthy sync — and that every value under the
sealed directory is ciphertext and no `.env` file is committed.

## Rolling back

`git revert`, landed the way everything else lands: a branch and a reviewed pull request,
never a direct push to `dev` or `main` and never a force-push over the bad commit. orbitd
applies backwards the same way it went forwards.

There is no faster path through a console: the revert is the rollback. Never leave the
cluster and git disagreeing — the reconcile loop re-applies what git holds, so a
hand edit that no commit carries is overwritten on the next tick.

## Removing a project

The supported way is orbitd's `orbit_project_remove` tool (the `orbit-change-service`
skill): one call deletes the catalog entry, backs up every database and proves each backup
restores, and only then deletes the namespaces. Use it. What follows is the same work by
hand, for when orbitd itself is down.

Two halves, in this order. First the catalog: delete the project's entry from
`catalog/projects.yaml` (and its lines in `catalog/tags.yaml`), `make sync`, and land it
through a pull request like any other change. That stops orbitd rendering the project, but
it does not delete what is already running: orbitd prunes only inside a pair that still
renders, and the platform prune never deletes a Namespace.

Then the cluster, from the desk, on the merged main:

```bash
scripts/desk.sh scripts/remove-project.sh <project>         # lists what would go; changes nothing
scripts/desk.sh scripts/remove-project.sh <project> --yes   # backs up, verifies, removes
```

With `--yes` it first dumps every database the project has into the config directory's
`backups/`, restores each dump into a scratch database and compares every table's row
count; a dump that does not restore, or restores differently, stops it before anything
is deleted. Then it prunes the `services` app, deletes the
project's monitoring objects and namespaces, and ends with `REMOVED: <project>` only when
nothing labelled with the project is left. Removing a project with a production database
needs a human go: show the dry run's list and ask. Its archive stays in the bucket, so the
name is written down in `catalog/retired.yaml` (`orbit_project_remove` does it; for an
entry deleted by hand the script refuses and prints the line to add), and a project made
under the name again archives under a path of its own.

## Upgrading orbit-core

The instance runs one core release: `orbit-core.lock` (`ref:`) pins the vendored files,
and `instance/config.yaml` `coreRegistry.orbitdTag` / `deskTag` pin the two images. An
upgrade moves all three to the same release, lands as one pull request, and is applied
from the desk. Read the release's entry in core's `docs/CHANGELOG.md` first: it names
anything the instance must do by hand (a non-vendored file such as the `Makefile`).

```bash
git checkout main && git pull --ff-only && git checkout -b upgrade/core-<release>
# orbit-core.lock: ref: <release>; instance/config.yaml: orbitdTag and deskTag: "<release>"
scripts/desk.sh make core-sync      # vendors the release (the desk itself is now the new image)
scripts/desk.sh make init           # re-renders the provisioning templates from the config
scripts/desk.sh make core-check     # "vendored core matches <release>"
scripts/desk.sh make check          # catalog and manifests agree
git add -A && git commit -m "core: upgrade to orbit-core <release>" && git push -u origin HEAD
```

Open the pull request and merge it once both checks above passed. Then apply, app by app,
only what the plan says changed. The plan of the whole platform takes about four minutes:
run it in the foreground with a ten-minute limit and wait for its lines; do not end your
turn while it runs, and do not apply before you have read it.

```bash
scripts/desk.sh bash -c 'orbit platform plan /instance --summary --kubeconfig "$KUBECONFIG" 2>&1 >/dev/null | grep -v empty=true'
scripts/desk.sh bash -c 'echo "prune <app>" | orbit platform apply /instance --app <app> --prune --kubeconfig "$KUBECONFIG" >/dev/null'
```

The orbitd image lives in the `platform-config` app: applying it restarts orbitd alone;
tenant workloads keep serving. Prove the upgrade: `curl -s https://orbit.<domain>/healthz`
reports the release, and `scripts/desk.sh make verify` ends with every stage passed
(stage6 compares the running orbitd with the pin).

## Certificates

Every door is served by one wildcard certificate per domain, issued by cert-manager over
DNS-01 and renewed automatically well before it expires (`orbit_platform_health`
lists `days_left` for each). To force a renewal — after rotating the DNS provider's
token, or to prove renewal works — add an `Issuing` condition to the Certificate, which
is what `cmctl renew` does; cert-manager then issues a new certificate and swaps the
Secret in place, with no gap in service:

```bash
scripts/desk.sh kubectl get certificates -A          # find the wildcard: its namespace and name
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
scripts/desk.sh kubectl -n <namespace> patch certificate <name> --subresource=status --type=json -p \
  '[{"op":"add","path":"/status/conditions/-","value":{"type":"Issuing","status":"True","reason":"ManuallyTriggered","message":"renewal forced from the desk","lastTransitionTime":"'"$now"'"}}]'
scripts/desk.sh kubectl -n <namespace> wait certificate <name> --for=condition=Ready --timeout=600s
```

Prove it from outside: the certificate a door serves now has a new `notBefore`
(`openssl s_client -connect <host>:443 -servername <host> </dev/null | openssl x509 -noout -dates`).
The certificate authority limits re-issues of the same names to a handful a week: do not
force one twice in a row.

## Failure modes, roughly in the order you will meet them

| Symptom | Cause | Fix |
|---|---|---|
| Push landed, nothing changes | pair not in `orbitd.applyPairs`, or the apply switch off | list the pair; the refused record names the setting |
| Push landed, old image still running | the checkout orbitd reads lags the push ~a minute | wait past the lag, read the image again |
| Push landed, nothing changes, pair listed | namespace missing | apply `namespaces.yaml` |
| The live object always differs from the manifest and no push closes the gap | a CRD defaults fields into etcd that the manifest omits | write the defaults out explicitly |
| `Progressing` forever while the site is up | a `Pending` PVC nothing mounts | give it a consumer |
| Pods `Pending`, `FailedCreate` | PriorityClass or quota missing | check `platform-config` applied |
| A workload with no resources is never scheduled | the namespace quota makes requests mandatory | declare `resources` on it |
| `ImagePullBackOff` | the tag is not in the registry | check it exists before blaming the cluster |
| `CrashLoopBackOff` immediately | the image will not run as the chart's UID | set `securityContext.runAsUser` in the catalog |
| Ingress 404 | no Ingress for that host, or a middleware is missing | `kubectl -n traefik logs deploy/traefik \| grep -i error` |
| Dev env 404 instead of 401 | the basic-auth Secret is missing | the guard is absent, not merely broken |
| Chart refuses to render | two web components share a path, or an undeclared Secret | the message names the component |
| `make check` fails | generated files were stale (it lists them and has rewritten them) | read the diff, commit the result |

Traefik's log is the most useful place for routing problems; it names the exact router and
middleware involved.

## What needs a human

Stop and ask before:

- **Any DNS change that moves live traffic.** Cutovers are gated deliberately: the system
  the domain points at today stays up, because it *is* the rollback, and the certificate is
  issued before anything moves (DNS-01, `orbit-onboard` §6) so the cutover is a DNS change
  alone. A domain already serving live traffic is read-only until someone decides otherwise.
- **Deleting anything** — a namespace, a PVC, a service. Pruning is an explicit act
  under a commit that shows what it removes: the tenant reconcile prunes only objects
  it names, the platform apply only with `--prune` and a typed confirmation.
- **Anything that costs money** — a volume, a load balancer, a node, a paid tier.

## Things that are true and easy to forget

- **Prod deploys from `main`, dev from `dev`**, unless the catalog says otherwise per
  environment. A push to an unclaimed branch builds an image and deploys nowhere — reported,
  and not an error.
- **With the apply switch off nothing lands.** The job is refused, the record says
  `LANDED NOT DEPLOYED apply`, and no other component picks the pair up.
- **A dispatch says which image it built, not which project it belongs to.** The platform
  checks the calling repository against the catalog's `repo:` before bumping a tag.
- **CPU is never limited.** If a service seems CPU-starved the answer is its `class`, not a
  limit.
- **Memory request equals limit.** Raising `memory:` raises both and makes the pod *harder*
  to evict, not easier.
