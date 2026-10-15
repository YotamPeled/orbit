---
name: orbit-onboard
description: "Onboard an organization onto Orbit end to end: the human fills one private form and makes two GitHub App clicks; `orbit onboard run` does every other step (instance repo, credentials, server, node, buckets, sealed values, platform, orbitd's first credential, acceptance, Recovery Kit), and `orbit onboard status` confirms each step. No value ever passes through the conversation."
role: viewer
---

# Onboard an organization

You are standing up Orbit for one organization, from nothing to a live platform with its
first project served, in about an hour of wall time, most of it waiting. The human does
three things a machine cannot: fills the onboarding form in their own editor, clicks
"Create GitHub App" and "Install", and moves the Recovery Kit into their password manager.
`orbit onboard run` does everything else. You run it, read its step lines, and confirm with
`orbit onboard status`. You never see a value. That division is the whole of this skill's
safety, so hold it without exception:

- Never ask for a token, a password or a key in the conversation, and if one is pasted,
  say it must be rotated and do not use it.
- Never print, `cat`, `echo`, `export` or copy a credential file, the form or the Recovery
  Kit; never put a value in a shell variable or on a command line. The run moves values
  file to file and its output names steps, files and key names only. A line that seems
  to carry a value is itself the finding: stop and report it.
- Never read a Secret's contents from the cluster. Existence by name is the only question.
- Paths are fine anywhere: the credentials directory, the form, `SOPS_AGE_KEY_FILE`, a
  kubeconfig.

## Before you start: what the human brings

Ask once, up front, for confirmation that these exist; do not proceed on a guess.

- A GitHub organization (or user) for the instance repo, which the `gh` login on this
  workstation owns (it creates the repo and the App there); that login's token also becomes the registry pull secret, so it needs
  `read:packages`.
- A Hetzner account with an API token and an Object Storage key pair; either an existing
  server to adopt (it is rebuilt in place, its disk wiped) or an explicit yes to buying a
  new one. Buying costs money: never set `create_server` without that yes in the
  conversation.
- A Cloudflare zone and a token scoped to it (DNS edit, zone read).
- A Telegram bot token and chat id for alerts; an email for certificates and contacts.
- A machine of at least 8 GB of RAM (measured floor; the platform does not fit in 4).
- A password manager for the Recovery Kit.

Nothing from a secrets service: there is no vault. Values live sealed in git and are
decrypted in place by the sops controller in the cluster.

## 1. The instance checkout, from the template

```sh
mkdir <name>-platform && cd <name>-platform && git init -b main
cp -r <orbit-core>/template/. .
cp instance/config.yaml.example instance/config.yaml
$EDITOR instance/config.yaml          # set instance.name to <name>; leave the rest
make core-sync                        # vendors the pinned core: scripts/desk.sh and the rest
```

`-b main` matters: a bare `git init` gives `master` and the push fails confusingly. Set
only `instance.name` (the desk derives the credentials directory from it, and it must equal
the form's instance name); leave every other example value in place: the run's `config`
step replaces exactly those, never a value someone already changed. The repository on
GitHub is created by the run.

## 2. The form (the human's part)

The human creates the credentials directory and writes the form there, in their own
terminal and editor; the desk mounts that directory, so the form is reachable from it.

```sh
mkdir -p -m 700 ~/.config/<name>-platform
$EDITOR ~/.config/<name>-platform/form.yaml && chmod 600 ~/.config/<name>-platform/form.yaml
```

```yaml
domain: acme.example            # the Cloudflare zone; the instance's primary domain
github: {owner: acme-org}
hetzner: {token: ..., server_type: cx43, location: fsn1, server_id: 123, rebuild: true}   # or create_server: true
cloudflare: {token: ..., zone: acme.example}
acme_email: ops@acme.example
instance: {name: acme}          # optional; default: the domain's first label
# optional, all three:
features: {logs: true, dashboards: true, databases: true}       # one left out is off
object_storage: {access_key: ..., secret_key: ..., location: fsn1}   # for logs, traces, backups
telegram: {bot_token: ..., chat_id: "123"}                      # for alerts-telegram
```

**Which features.** A platform is `core` (the doors and certificates, sealed secrets,
metrics and alerts, orbitd: about 750 MiB resident on the reference node) plus the optional
features the form turns on. A form with no `features:` is core alone. Each costs memory on
the node for as long as it is on; put this table in front of the human and let them
choose. Any of them can be turned on or off later with one command (`orbit-features`).

| feature | what it gives | needs | memory |
|---|---|---|---|
| `logs` | every pod's logs kept 7 days and searchable | `object_storage` | 184 MiB |
| `traces` | request traces, linked from logs and metrics | `object_storage` | 269 MiB |
| `dashboards` | Grafana, a dashboard per project environment | | 237 MiB |
| `uptime` | every public door checked from outside each minute | | 10 MiB |
| `databases` | a project may declare a PostgreSQL database | | 32 MiB, plus each database |
| `backups` | prod databases archive to the bucket (point-in-time restore) | `object_storage`, `databases` | 14 MiB |
| `alerts-telegram` | every alert also sent to a Telegram chat | `telegram` | none |

The numbers are the registry's (`provisioning/features.yaml`, `memory_mib`): resident
memory measured on an idle platform. Without `logs`, logs are read from the node and last
as long as the pod; without `dashboards`, metrics and alerts are read through orbitd's
tools; without `backups`, a database gets a nightly dump on the node. The run refuses, at
its start and naming the form field, a feature turned on without what it needs.

`<name>` is that instance name. A form that is not private, or a field not of its shape, is
refused with the field's name, never its value. `object_storage` and `telegram` are left
out entirely when no chosen feature needs them (a block that is there must be whole). The form is only read; delete it after the
run completes and the Recovery Kit is stored, since its tokens now also live in `env` and
the sealed files.

## 3. The run

From the instance checkout, through the desk, with the App flow's port published:

```sh
ORBIT_DESK_PORTS=8765 scripts/desk.sh orbit onboard run /instance \
  --form /root/.config/<name>-platform/form.yaml
```

The whole run takes 40 to 60 minutes and several steps outlast one command (`platform`
waits up to ten minutes for orbitd after applying every app; `acceptance` is `make verify`,
about twelve). So do not run it as one foreground command that a time limit will cut: start
it writing to a log and read the log until it ends.

```sh
ORBIT_DESK_PORTS=8765 scripts/desk.sh orbit onboard run /instance   --form /root/.config/<name>-platform/form.yaml > onboard-run.log 2>&1 &
tail -n 3 onboard-run.log        # again every few minutes, until `recovery-kit:` or `STOPPED`
```

Each step's line reaches the log the moment the step ends; between lines a long step is
silent (the server's rebuild about five minutes, `platform` up to twenty, `acceptance` about
twelve). Wait with a foreground command each time (`sleep 240; tail -n 3 onboard-run.log`),
not with a background task or a monitor: in a session nobody is typing into, ending your
turn ends you, and the run is left with nobody reading it.

Do not end your turn while the run is going, and do not start a second one beside it. If a
command of yours was cut off, the run itself may still be alive: look at the log before
running anything again.

One line per step, in order: `config`, `credentials`, `repository`, `github-app`,
`infrastructure`, `node`, `buckets`, `sops-key`, `secrets`, `platform`, `orbitd-credential`,
`acceptance`, `recovery-kit`. Every step is idempotent and keeps what exists (a re-run
never rotates a key or re-seals a value), so after a stop you fix the cause and run the
same command again; it resumes where it stopped. `--only <step>` runs one step.

- `github-app` prints a local URL, `http://127.0.0.1:8765/`, and waits up to 30 minutes.
  Give the human that URL: they open it in their browser, click "Create GitHub App", then
  "Install" on the page GitHub shows next (the instance repo is preselected). Nothing is
  typed; the App's id, key and installation id land in `github-app.json`, mode 600.
- `infrastructure` refuses without `server_id` or `create_server` in the form, and refuses
  `server_id` without `rebuild: true`: adopting a server wipes its disk (Hetzner's rebuild,
  same address, no spend) and authorizes the run's node key through cloud-init. Say so
  before the run. Its DNS records, firewall and ssh key from an earlier life are imported
  into tofu rather than created again, so DNS does not change. The rebuild happens once; a
  resumed run skips it.
- `acceptance` runs `make verify`; a stop names the count of FAIL lines. Read them with
  `scripts/desk.sh make verify`, never by guessing.
- `recovery-kit` writes `recovery-kit.txt` (mode 600) in the credentials directory: the
  sops age private key, the dashboard password and orbitd's first sign-in credential. Tell
  the human to move it into the password manager and delete it from disk. Losing every
  copy of the age key means every sealed value is unopenable forever; no ceremony
  recreates it.

A `STOPPED` line names the step and the reason. Read it, fix what it names (often a form
field, a token's scope, or a tunnel), and run again. If the same step stops twice for the
same reason, report the line and stop; do not invent a route around it.

## 4. Confirm, then the first project

```sh
scripts/desk.sh orbit onboard status /instance --accept
```

One line per step, `done`, `todo`, `blocked` or `unmeasured`, and a single `next:` line
with the one command that moves it. The exit code is 0 only when every step is done, the
acceptance suite included, and then the last line begins `complete:`. `blocked` on the node
step means the API did not answer through the kubeconfig: usually the tunnel is closed, and
the line says how to open it. `blocked` on a later step means the cluster answered something
other than yes or no for one object, or the dashboard sign-in got no usable answer; the
line says which. `--json` gives the same report as a document. The only `todo` left after a
complete run is `first-project` (section "By hand", step 8).

## By hand: what each run step does

The run is the runbook below, automated. Use these sections to understand a `STOPPED`
line, or to do one step by hand when the run cannot (and then run again: it sees the work
done and moves on). The status loop works the same either way: after every step, run
`orbit onboard status .` and do its `next:` line.


### 1. The instance repo, from the template

```sh
mkdir <name>-platform && cd <name>-platform && git init -b main
cp -r <orbit-core>/template/. .
$EDITOR instance/config.yaml          # from config.yaml.example: every field
make init                             # pins the core, vendors it, renders clusters/
git add -A && git commit -m "instance from template"
gh repo create <org>/<name>-platform --private --source . --push
```

`-b main` matters: a bare `git init` gives `master` and the push fails confusingly.
`clusters/` is output, rendered from `provisioning/` and the config on every `make init`;
edit the staged source under `provisioning/`, never the copy, and re-run `make init` after
any config correction. You will run it again after step 3, because the node's address is a
guess until the server exists. The status line names any field still carrying the
example value.

### 2. The credentials directory

`~/.config/<name>-platform/`, mode 700, never in git. Tell the human what to create and let
them do it in their own terminal and editor; you verify with the status line only.

```sh
mkdir -p -m 700 ~/.config/<name>-platform && cd ~/.config/<name>-platform
ssh-keygen -t ed25519 -f node-key -N ''      # a FRESH key: Hetzner refuses a reused fingerprint
echo "$PWD/node-key" > ssh-key-path
age-keygen -o age.key && chmod 600 age.key   # the sops private key
$EDITOR env                                  # export HCLOUD_TOKEN=... / export CLOUDFLARE_API_TOKEN=...
chmod 600 env
age-keygen -y age.key                        # the PUBLIC recipient: goes into the repo's .sops.yaml
```

The public recipient is not secret and belongs in `.sops.yaml`; the private key must also
be copied into the password manager now. Losing every copy means every sealed value is
unopenable forever; no ceremony recreates it. The status line reports each file as
present, private, and (for `env`) naming both token keys.

### 3. Infrastructure

Write `tofu/instance.auto.tfvars` (server and key names, the two key paths, the zone ids;
node size and place live in the config under `nodes[0]`), then:

```sh
( set -a; . ~/.config/<name>-platform/env; set +a
  tofu -chdir=tofu init && tofu -chdir=tofu plan && tofu -chdir=tofu apply )   # plan: adds only, 0 destroy
tofu -chdir=tofu output server_ipv4
```

Write the address as `nodes[0].ip` in `instance/config.yaml`, run `make init` again, commit
and push. The apply creates the server, its firewall and ssh key, and the two DNS wildcards
(the dev wildcard is separate: a wildcard spans one label). The parentheses are the
exception to the rule that no value enters your environment: the tokens are exported inside
that subshell only, the tofu commands read them there, and they are gone when it closes.
Never source `env` in your own shell, and never echo, `env`, or `export -p` inside the
parentheses.

### 4. The node

```sh
ORBIT_CONFIG_DIR=~/.config/<name>-platform scripts/bootstrap-node.sh
```

Idempotent; ends when `kubectl works`. It writes `~/.kube/config-<name>-platform` and
prints the tunnel: the API is firewalled, reachable only through
`ssh -i <key> -N -L 6443:127.0.0.1:6443 root@<ip>` kept open in another terminal. The first
contact accepts the new host key (it cannot be known yet); a changed key on a known host
still refuses. Kubelet reservations scale with the node's memory.

### 5. The platform tree

The one value that cannot be sealed is the key that opens the sealed files:

```sh
export KUBECONFIG=~/.kube/config-<name>-platform
kubectl create namespace sops
kubectl -n sops create secret generic sops-age --from-file=age.agekey=$HOME/.config/<name>-platform/age.key
orbit platform plan .      # the diff about to land, per app
orbit platform apply .     # CRDs, then namespaces, then workloads, in phase order
```

The file key must be `age.agekey` and the namespace `sops`. Nothing watches the repo from
inside the cluster: the desk applies what git holds. Let the early apps settle (the sops
controller, the database controller, cert-manager, traefik) before judging the rest; an
admission hook that fires while cert-manager's webhook has no endpoints fails and needs one
re-apply after it settles. The status line counts apps by their stored inventory.
`orbit onboard run` does those re-applies itself (up to five passes a minute apart, a line for each
that did not hold); if it still stops at `platform`, the last pass is in `platform-apply.log` in the
credentials directory: search it for `"reason"`.

### 6. The sealed values

Generated values are minted by the desk; supplied ones are sealed by the human. Both land
as one sealed file per namespace under `clusters/platform/secrets/<namespace>/<name>.yaml`.

```sh
orbit secrets mint . --name grafana-image-renderer-token --namespace monitoring   # first: both dashboard pods wait on it
git add -A && git commit -m "mint" && git push && orbit platform apply .
```

A supplied value (a Cloudflare token, an object-storage key, the dev basic-auth line) is
sealed by the human: they write the sealed file with the value under the template's `data:`
in their editor, then encrypt it in place before anything else happens:

```sh
sops --encrypt --age <recipient from .sops.yaml> --encrypted-regex '^(data|stringData)$' -i clusters/platform/secrets/<namespace>/<name>.yaml
```

A project's namespaces (`<project>-<env>`) need three Secrets the platform already holds,
and each is a copy made at the desk with no value read (`orbit secrets copy . --file
clusters/platform/secrets/orbit/<name>.yaml --namespace <namespace>`): `ghcr-pull` in every
namespace, `dev-basic-auth` in every non-prod one, and `orbit-s3` in `<project>-prod` when
the project has a database and the platform a bucket. Commit them with the catalog entry and
apply them (`orbit platform apply --app secrets .`) before the first ship. The commit gate
refuses a sealed file that lacks a key its project declares, and names the keys.

Then commit, push, apply. The tripwire, with the key given as a path:

```sh
SOPS_AGE_KEY_FILE=$HOME/.config/<name>-platform/age.key orbit secrets verify .
```

One line per sealed file, `match`, `mismatch`, `no-file` or `decrypt-failed`; work it down
to all-match. The status verb's `secrets` line is the other half: it renders the platform
tree (about a minute) and names each Secret the rendered objects refer to, in the reference
forms the platform's charts use (volumes, env, pull secrets, the operators' selectors and
lists, Traefik's auth), that no sealed file renders, so a value nobody has sealed yet is a
todo before anything decrypts. A reference form the verb does not know is not caught there,
nor by `orbit secrets verify` (which judges the sealed files that exist): a platform
component missing a Secret shows in `make verify` where a row checks that component, and
otherwise only in the diagnose skill. The dev
basic-auth value must be sealed before the first `make sync` that enables a dev environment,
or the dev URL answers not-found instead of asking for a password. A rotation is a re-seal
plus a restart of the pods that read the value at startup.

### 7. Dashboard sign-in

There is no identity provider; the dashboards keep a local login whose password has exactly
two homes, a private workstation file and a sealed Secret, both from one value the human
chooses now, and a copy in the password manager:

```sh
openssl rand -base64 32 > ~/.config/<name>-platform/grafana-admin-password
chmod 600 ~/.config/<name>-platform/grafana-admin-password
```

The human seals the same value as the file below, encrypts it in place, commits; you apply.
The dashboards are not healthy until the Secret it renders exists.

```yaml
# clusters/platform/secrets/monitoring/grafana-admin.yaml
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
  name: grafana-admin
  namespace: monitoring
spec:
  secretTemplates:
    - name: grafana-admin
      data:
        admin-user: admin
        admin-password: <the same value, pasted by the human, then encrypted in place>
```

Keep the workstation file: the acceptance rows that sign in cannot run without it, and a
check that cannot run is not a pass. The `dashboard-sign-in` line is `done` only when the
verb has signed in to the dashboard with the workstation file (a rendered Secret says
nothing about its content); `refused` means the dashboard rejected the workstation
password (the file or the sealed value has changed since they were one, or the sealed
user key is not the dashboard's local login), and the line's command redoes the step
with one value in both homes.

### 8. Acceptance, then the first project

```sh
orbit onboard status . --accept      # runs `make verify`: every stage, all live checks armed
```

The `acceptance` line is `done` only when the suite passed; read the failing rows in
`make verify` itself when it is not.

The daemon applies nothing until it is told to. Set `orbitd.applyMode: "on"` (quoted) in
`instance/config.yaml`, leave `orbitd.applyPairs` empty (every pair) or list the pairs it
may apply, then render, commit and apply the daemon's own configuration:

```sh
make init && git add -A && git commit -m "applier on" && git push
orbit platform apply --app platform-config .
```

The desk's `orbit` client is not signed in to the new orbitd yet (a call answers
`Authentication required`). Sign it in once with the first credential the run wrote, file to
stdin, nothing typed or printed:

```sh
scripts/desk.sh bash -c 'orbit login --url https://orbit.<domain>/mcp < "$ORBIT_CONFIG_DIR/orbitd-admin.json"'
```

Then add the first catalog entry (the worked example at the top of `catalog/projects.yaml`
shows every field), `make sync`, commit and push; orbitd applies the pair and the service is
live with a valid certificate about a minute later. The `first-project` line is `done` when
the pair's Application is rendered (under `clusters/orbit/` when `orbitd.applyPairs` lists
it, under `clusters/apps/` otherwise; an empty list means every pair), the applier is on
and either lists the pair or lists nothing, its namespace
and stored inventory exist, the inventory's commit carries the pair's current declaration
(so a change you pushed and the daemon has not applied yet is a todo, not a stale pass),
and every workload the inventory lists is as applied: a Deployment, StatefulSet or
DaemonSet with its rollout complete, a Job completed, a CronJob not suspended, a database
Cluster healthy. Each missing piece is its own `next:` line.

## A test organization on the same accounts

A throwaway sharing the human's Hetzner and Cloudflare accounts needs its own ssh keypair,
distinct key and server names, a tunnel on another port, and its own age keypair; sealed
files are per-organization ciphertext. Teardown is deliberate by design: delete the server
through the Hetzner API, `tofu state rm hcloud_server.prod`, then `tofu destroy` sweeps the
DNS records, firewall and key. Keep the credentials directory until the organization is
certainly never coming back.

## When something is stuck

Run `orbit onboard status .` and read the `next:` line before reasoning. If a step stays
`todo` after its command, report the failing rows (each names objects, never a value; a
row that seems to carry a value is itself the finding to report) and stop; do not invent a
route around it. If a value was ever printed or pasted, say so and have the human rotate
it before continuing.
