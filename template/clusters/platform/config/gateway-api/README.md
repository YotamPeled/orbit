# Gateway API CRDs — vendored, not fetched

`standard-install.yaml` is the **Gateway API v1.6.0 standard channel**, copied verbatim from
the upstream release (`kubernetes-sigs/gateway-api`, Apache-2.0). Provenance, so that
"verbatim" is checkable rather than trusted:

| | |
|---|---|
| Source | `https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.0/standard-install.yaml` |
| sha256 | `a557172e8348f758479e9ee4000bbbb4b4aa48302a6b73461823ea5349bad56d` |
| Size | 1 170 953 bytes |

```sh
curl -sSL -o /tmp/gwapi.yaml \
  https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.0/standard-install.yaml
sha256sum standard-install.yaml /tmp/gwapi.yaml && cmp standard-install.yaml /tmp/gwapi.yaml
```

Nothing in the file is Orbit's and nothing in it is instance-specific — do not edit it.
On a version bump, replace it wholesale and update the three rows above, so the diff is the
upstream diff.

## Why it is vendored rather than fetched from the upstream URL at apply time

The platform's `Gateway`, `HTTPRoute` and `ReferenceGrant` objects need these CRDs
to exist before they can apply, and the traefik chart **also bundles a copy — an older one**.
CONFIRMED at chart 37.1.1: `helm template --include-crds` emits 28 CRDs, five of them
`*.gateway.networking.k8s.io` at `bundle-version: v1.3.0`; with `--skip-crds` it emits none.
Chart CRDs render unless skipped, so with the traefik chart left alone a traefik apply
**downgrades v1.6.0 to v1.3.0**. The two halves of the fix are:

1. this vendored bundle, applied by the `platform-config` Application at wave 2, and
2. `skipCrds: true` on the traefik Application (`apps/traefik.yaml`).

Neither works without the other. The consequence to own is that **Gateway API CRD upgrades
are now a deliberate platform change** — a traefik chart bump no longer moves them, in either
direction.

## Transferring CRD ownership on an EXISTING instance

**A fresh org is unaffected: it has never had a traefik-owned copy, both halves arrive
together in its first sync, and there is nothing to hand over.** Skip this section.

**A pin bump alone is also unaffected.** `core-manifest.txt` names no `clusters/` and no
`template/` path (SECRET-PROVISIONING §11: template changes are delivery mode **S**, seeded
once and never re-synced), so `scripts/core-sync.sh` does not carry either half into an
existing instance. This section applies only when an operator **hand-copies** these two
template changes into a live instance — which is the only way they get there.

**The hazard, stated exactly.** Both Applications run `automated: {prune: true}`. Flipping
`skipCrds: true` removes the five CRDs from the traefik Application's desired state, so a prune
removes them; `platform-config` is separately trying to adopt the v1.6.0 copies. Waves order
resources *within* an Application and do not make a hand-off *between* two Applications
atomic. Deleting a CRD deletes **every CR of that kind cluster-wide** — the `Gateway`, the
`HTTPRoute`s and the `ReferenceGrant` — so losing the race does not degrade a route,
it removes it.

Do it in **two commits, each synced and checked before the next**:

**Commit 1 — adopt, without letting anything prune.**

1. Add `config/gateway-api/standard-install.yaml` (this file). Do **not** touch
   `apps/traefik.yaml` yet.
2. Temporarily set `automated: {prune: false}` on **both** the `traefik` and the
   `platform-config` Applications, in this same commit.
3. Sync and wait. Both Applications now claim the same five CRDs; the last writer wins on
   content, which is what upgrades v1.3.0 → v1.6.0, and with pruning off neither can delete
   them. Confirm before continuing:

   ```sh
   kubectl get crd gateways.gateway.networking.k8s.io \
     -o jsonpath='{.metadata.annotations.gateway\.networking\.k8s\.io/bundle-version}{"\n"}'   # v1.6.0
   kubectl get gateway,httproute -A            # every object still present
   ```

**Commit 2 — hand over, then restore pruning.**

4. Set `skipCrds: true` on the traefik Application and restore `prune: true` on both.
5. Sync. Traefik's desired state no longer contains the CRDs, `platform-config`'s does, so
   there is no window in which nobody owns them. Re-run the two checks above.

If step 3's `bundle-version` still reads `v1.3.0`, traefik won the last write: sync
`platform-config` again before doing commit 2, and do not proceed until it reads `v1.6.0`.

**Rollback** at any point is `git revert` plus a sync; nothing here is destructive while
`prune: false` is in force, which is the whole reason it is two commits.

The desk's directory source reads only `*.yaml` / `*.yml` / `*.json`, so this README is not
applied to the cluster.
