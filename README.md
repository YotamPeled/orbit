# Orbit core

A DevOps platform you install per organization, on its own server, built so an AI agent
can operate it: **a repo + one catalog entry in → a production service out** — URL, TLS,
environments, deploys, Postgres with invisible credentials and PITR, sealed secrets,
metrics/logs/traces, generated dashboards, alerts, and a verify suite that proves all of
it by execution.

This repository is the **core**: the catalog schema, the generator, the service chart,
the verify framework, orbitd (the agent's MCP control surface), the desk image, the
skills, and the bootstrap/tofu modules. It carries no
instance strings — a verify check enforces that.

An **instance** (one per organization) is a small repo that pins a core release in
`orbit-core.lock`, vendors it with `scripts/core-sync.sh`, and adds: one
`instance/config.yaml`, its `catalog/projects.yaml`, and its sealed secrets.
See `template/` for the instance-repo skeleton and [docs/README.md](docs/README.md) for
the documents: what Orbit is (PRODUCT.md), onboarding, orbitd, the skills, the V1 finish
line and progress (V1.md), and the changelog.

License: Apache-2.0.

Contributing: `scripts/local-tests.sh` runs every offline test on your machine (there is no
CI) and turns on the repository's git hooks (`.githooks`). They refuse a commit or a push
that carries a personal string, a credential, another author or a co-author trailer;
`scripts/personal_strings.py` is the check and says what it refuses.
