# How code reaches production

An app's code goes from a commit to a running pod in three moves, all from the operator
machine, with no CI in the path: **build locally, push to GHCR, ship the tag through
orbitd.** The same three moves serve a human and an agent; the agent's version is the
`orbit-ship` skill.

## 1. Build — on the operator machine, from a clean, pushed commit

```sh
git status --porcelain                # must print nothing: no uncommitted change
git fetch && git status -sb            # the commit must already be on the remote
sha=$(git rev-parse --short=7 HEAD); branch=$(git rev-parse --abbrev-ref HEAD)
docker buildx build --platform linux/amd64 \
  -t ghcr.io/<owner>/<image>:${branch}-${sha} --push .
```

The tag is `<branch>-<sha7>`. The rule that the tree is clean and the commit pushed is what
makes a tag mean one thing: anyone can check out `<sha7>` and get exactly what runs. An
image built from an uncommitted tree has no source anyone else can read, so it is never
shipped. `--platform linux/amd64` because the node is amd64 whatever the laptop is.

The push needs a GitHub token with `write:packages` (`gh auth refresh -s write:packages`,
then `gh auth token | docker login ghcr.io -u <user> --password-stdin`). The cluster pulls
with its own read-only credential (the sealed `ghcr-pull` Secret in each namespace).

## 2. Ship — `orbit_deploy_ship`, through orbitd

```
orbit_deploy_ship(project, env, components, tag, dry_run=false, expect=<digest>)
```

orbitd checks the tag's manifest exists in the registry (a tag that does not exist is
refused before anything changes), asks for confirmation, then commits the tag bump to
`catalog/tags.yaml` in the instance repo as the org's GitHub App. The commit carries
`Orbit-Action: ship`, the actor, `Source-Commit:` the sha in the tag and `Image-Digest:`
the manifest digest. orbitd's applier then renders the pair and applies it, waits for the
rollout, and runs the post-ship assertions (the pods run the image, are ready, and the
public door answers). Rollback is `orbit_deploy_rollback`: the same path to the previous
tag in the ledger.

`scripts/ship.sh <project> <env> <component> <tag>` is the same flow from the desk, without
orbitd: it mints a short-lived installation token, shallow-clones the instance repo with
the token as `http.extraheader` (never argv, never logs) and runs that clone's
`scripts/bump-and-push.sh`.

## 3. Promote — the same tag, the next environment

dev and prod are separate pairs. Promotion is shipping the tag that dev runs to prod; the
image is not rebuilt, so prod runs the bytes dev ran.

## What V1 does not do

Building in the cluster, deploying on push, an on-box registry, generated compose and
devcontainer files, per-PR preview environments and a local observability stack were all
designed at one point and are recorded as V2 (docs/V1.md §9). None of them exists.
