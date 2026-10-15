# Orbit documentation

## Living documents — these describe the system as it is

| document | what it answers |
|---|---|
| [V1.md](V1.md) | the V1 finish line, the rules of the run, and the **only** progress tracker |
| [PRODUCT.md](PRODUCT.md) | what Orbit is, the architecture, core vs instance, measured sizing |
| [ONBOARDING.md](ONBOARDING.md) | bringing up a new instance, step by step |
| [ORBITD.md](ORBITD.md) | orbitd: the MCP control surface, its roles, tools and decisions |
| [SKILLS.md](SKILLS.md) | the operator skills an agent loads to operate an instance |
| [MONITORS.md](MONITORS.md) | monitors: the body of one, the three kinds, who hears it, silences, the platform's own alerts |
| [DEVELOPMENT.md](DEVELOPMENT.md) | how an app's code reaches production (local build, ship) |
| [METHOD.md](METHOD.md) | how work on Orbit is done and judged (evidence, review) |
| [CHANGELOG.md](CHANGELOG.md) | every release, newest first |

## Retired design records

These documents recorded designs and decisions that were superseded (the vault/ESO
secret plane, ArgoCD, Zitadel, agentgateway, the console, the context lake) or plans that
have been carried out. They were removed on 2026-10-04 so that nothing in `docs/` reads
as current when it is not. Code comments still cite some of them by name and section
(for example "OBSERVABILITY.md Decision 5", "DESIGN-v2 §3.4") as the reason a line is
the way it is. This repository's history begins at v1.0.5, its first public release; the
records are in the private history it was cut from, not here.

| record | what it was |
|---|---|
| ROADMAP.md | the phase plan A→H and, inside it, the release history (now CHANGELOG.md) |
| DESIGN-v2.md | the ArgoCD-to-desk handover design (§2 budget, §3 orbitd envelopes, §7 monitors, §8 retirements, §9 platform layer) |
| OBSERVABILITY.md | the LGTM decision record (Decisions 1–6: identity set by the platform, Traefik as trace root, generated RED dashboards) |
| AGENT-PLANE.md | the agent plane design (agents, triggers, intake, model auth, brakes) — V2 in V1.md §9 |
| SECRET-PLANE.md, SECRET-PROVISIONING.md | the vault/ExternalSecret secret plane, replaced by sops+age |
| TEMPLATE-CATCHUP.md | the plan that brought `template/` level with the first instance |
| CONSOLE.md, CONTEXT-LAKE.md | the console UI and the context lake — V2 in V1.md §9 |
| LOOP-STATE.md | the build log of the September loop |
| research/ | research memos behind the secret plane and the intake |
