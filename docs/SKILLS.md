# The operator skills

`skills/<name>/SKILL.md`, one per job an agent does while operating Orbit through
orbitd's MCP tools. Tools are verbs (one call, typed input, an evidence-shaped result);
skills are procedures (which tools in what order, how to read the evidence, what to do
when a step fails, when to stop and ask). A skill carries no fact about any
installation: every project id, hostname, namespace and number comes from a tool at
run time, and `scripts/verify/no-instance-strings.py` checks the directory for leaks
the same way it checks the rest of core (`skills` is in `core-manifest.txt`).

| skill | job |
|---|---|
| `orbit-operator` | orientation: what Orbit is, roles, the evidence envelopes, which skill for which job. Read first. |
| `orbit-diagnose` | why a component is unhealthy or restarted |
| `orbit-investigate-alert` | from a firing alert to its cause |
| `orbit-budget-review` | memory budget, quota headroom, certificates, readiness |
| `orbit-deploy-review` | what runs where, what is desired, how it got there (admin role for the diff and reconcile steps) |
| `orbit-ship` | ship a tag of a component and prove it landed (operator role) |
| `orbit-rollback` | roll back to the previous or a named tag; restart as the smaller hammer (operator role) |
| `orbit-admin` | principals and tokens (admin role only) |
| `orbit-features` | which optional features are on, what each needs and costs; switching one (admin role) |
| `orbit-restore-db` | bring a database's data back to a point in time or a backup: the moment from the ledger and logs, the destroyer rolled back, a compare-only dry run, then `orbit_db_restore` (admin role) |
| `orbit-change-service` | change a project's catalog entry with `orbit_catalog_set` (replicas, memory, hostnames, components), wait for it and prove it; back up a database; remove a project (operator role; removal needs admin) |
| `orbit-onboard` | onboard an organization: instance repo, credentials, server, node, platform tree, sealed values, sign-in, first project; `orbit onboard status` after every step |

Monitors (define, change, delete, silence, unsilence) have no skill of their own: they
are a section of `orbit-operator`, and `docs/MONITORS.md` is their reference. Jobs with no
tool behind them (authoring a new project's catalog entry, upgrading core, rotating a
secret) are desk work; `orbit-operator`'s table says where each lives.

## Loading them into a host

- **Claude Code**: copy or symlink each `skills/*` directory into the session's
  `.claude/skills/` (for example `ln -s "$PWD"/skills/* .claude/skills/`), then connect
  the `orbitd` MCP server with a token of the role the job needs.
- **Any other host**: give the agent every `skills/*/SKILL.md` (twelve today, the table
  above) and have it read `orbit-operator` first; its table routes to the rest.

## The acceptance rule

A throwaway session holding only these skills and an MCP connection to the real orbitd
must explain the system and diagnose a component without help. Every confusion it shows
is a defect in a skill or in a tool description, and is fixed before the phase closes.
`scripts/test_skills.py` keeps the skills honest against the registry: every tool a
skill names is registered, every registered tool is named by a skill, every skill has
`name` and `description` frontmatter, and `orbit-operator` names every sibling.

The harness is `scripts/skills-acceptance.sh [--auto-confirm] <mcp-config> <skills-dir>
<out-dir>` (its header documents the inputs and outputs). The operator skills confirm
through the input-required round, which headless Claude Code does not answer;
`--auto-confirm` puts `scripts/mcp-confirm-proxy.py` between the session and the
server, answers each round once with `confirm: true`, and records every round in
`<n>-<slug>.confirmations.jsonl` (the transcript header counts them).
`ACCEPTANCE_NEVER_CONFIRM="env=prod"` makes the proxy refuse a round for that
environment instead: the rail that keeps an acceptance run in non-production.
