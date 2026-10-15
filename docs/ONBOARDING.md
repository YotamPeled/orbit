# Onboarding an organization onto Orbit

An agent runs this through the `orbit-onboard` skill (`skills/orbit-onboard/SKILL.md`), which
keeps the human's part to accounts, tokens and the key backup; `orbit onboard status <dir>`
says which of the steps below are done and prints the one next command. Since V1 the skill
drives `orbit onboard run <dir> --form <form.yaml>`: the human fills one private form
(docs/V1.md §7) and makes the two GitHub App clicks, and the run does every step below in
order, idempotently, ending with a Recovery Kit file. This page stays the reference the run
and the skill are derived from, and the way to do one step by hand when the run stops.

This runbook IS the product's org-onboarding path. It is not designed prose: every step below was executed verbatim against a
real fresh org (`orbtest`, 2026-08-27) and every warning is a failure that actually
happened. The operator machine needs only Docker and git: every desk command below runs
inside the orbit-desk image through `scripts/desk.sh <command>` (kubectl, helm, sops, age,
promtool, tofu and the `orbit` CLI are in it). Time to live cluster: ~1 hour, most of it waiting on the platform
layer to apply and the stack above it to settle.

What an org brings:
- a GitHub org (free) or user account, and later its own GitHub App (one App per org: an
  App has one webhook URL and one installation, so it is never shared between orgs)
- a Hetzner account + API token; a Cloudflare zone + scoped API token
- **nothing from a secrets provider.** There is no vault: secrets live sealed in git as
  SopsSecret files under `clusters/platform/secrets/`, decrypted in place by the sops
  operator. Values are minted or sealed from the org's own machine (§5a); the only key
  material is the org's age keypair (§2)
- a Telegram bot token + chat id, an ACME email
- **a node of at least 8GB RAM** — the platform layer does not fit in 4GB (found by
  execution). Measured on 2026-10-11: the platform's containers request 3.5 GiB and the
  whole box uses 4.6 GiB with the V1 test tenant; 16GB (cx43) is comfortable. PRODUCT.md, "Sizing"

## 1. Instance repo from the template

```sh
mkdir <org>-platform && cd <org>-platform && git init -b main   # -b main: bare init
                                                                # gives master and the
                                                                # push fails confusingly
cp -r <orbit-core>/template/. .
$EDITOR instance/config.yaml        # from config.yaml.example — every field
make init                           # bootstraps core-sync from the pin, vendors core,
                                    # renders clusters/ from provisioning/ + config.
git add -A && git commit -m "instance from template" 
gh repo create <org>/<org>-platform --private --source . --push
```

**`make init` is meant to be run more than once, and this step is deliberately one of the
times it runs on values you do not have yet.** `clusters/` is generated output: every
config-derived file is re-rendered from `provisioning/clusters/` on every run, so a value
corrected in `instance/config.yaml` reaches the manifests on the next `make init` (before
v0.16.0 it did not — a `{{token}}` was filled once and frozen). Concretely, you will run it
again at least once:

- **after §3**, because `nodes[].ip` here is a GUESS — tofu has not created the server yet.
  Put tofu's real address in `instance/config.yaml` and re-run. (And after any later
  config correction, for the same reason: a corrected value reaches the manifests on
  the next run.)

Two rules that follow from `clusters/` being output, and are worth learning now rather than
after losing an edit: **edit the staged source under `provisioning/`, never the copy under
`clusters/`** — every generated file says so in its first three lines — and a re-run with
unchanged config moves nothing, so it is always safe to run.

## 2. Credentials directory (workstation-only, 0600, never in git)

`~/.config/<name>-platform/` — the instance's identity on this machine:
- `env` — HCLOUD_TOKEN, CLOUDFLARE_API_TOKEN (exported)
- `node-key`(+`.pub`) — a FRESH ssh keypair. Hetzner enforces account-wide fingerprint
  uniqueness: reusing a public key already registered on the same account fails with
  uniqueness_error, so same-account instances need their own key.
- `ssh-key-path` — one line: the path to node-key
- `age.key` — `age-keygen -o age.key`; the SOPS private key. Sealing needs only the
  public recipient (in the instance `.sops.yaml`), but opening a sealed file —
  `orbit secrets verify`, rotations, recovery — needs this key, and the sops operator
  holds a copy in-cluster (§5)
- a copy of `age.key` in a password manager, outside this directory and outside every
  git worktree. Losing every copy means sealed values can never be opened or re-sealed
  again — that backup is the whole recovery story, and no ceremony recreates it

## 3. Infrastructure — tofu creates everything

`tofu/instance.auto.tfvars`:
```hcl
server_id            = ""                      # fresh org: nothing to import
server_name          = "<name>-node-1"
ssh_key_name         = "<name>-operator"
ssh_public_key_path  = "~/.config/<name>-platform/node-key.pub"
ssh_private_key_path = "~/.config/<name>-platform/node-key"   # optional; default ~/.ssh/id_ed25519
                                                              # only builds the `tofu output ssh` strings
zone_ids             = { "<zone>" = "<cloudflare-zone-id>" }
```
Node size/place live in `instance/config.yaml` (`nodes[0].serverType/location`).
An ADOPTED (pre-existing) node instead gets an instance-side `tofu/import.tf` with an
import block — see the reference instance.

```sh
set -a; . ~/.config/<name>-platform/env; set +a
tofu -chdir=tofu init && tofu -chdir=tofu plan    # expect: adds only, 0 destroy
tofu -chdir=tofu apply                            # server, firewall, ssh key, and
                                                  # *.primary + *.dev wildcards (the
                                                  # dev wildcard is separate: DNS
                                                  # wildcards span one label)
```
Write the created IP into `instance/config.yaml` `nodes[0].ip`; commit and push.
A subzone primary (test.example.org inside zone example.org) works — the zone is
resolved as the dnsZones entry the domain sits inside. Note the primary APEX itself has
no record; only project hostnames under the wildcard are served.

## 4. Node bootstrap — ends at "kubectl works"

```sh
ORBIT_CONFIG_DIR=~/.config/<name>-platform scripts/bootstrap-node.sh
```
Idempotent. Kubelet reservations scale with node RAM (the 16GB-sized values once ate
half a 4GB box and everything sat Pending at 99% requested). Produces
`~/.kube/config-<name>-platform`; the API is firewalled, reachable only through the
ssh tunnel it prints.

## An owned node

A machine you already run, on the LAN. Skip §3: tofu is not run — no server, no
cloud firewall, no DNS records from it. Write the wildcard DNS records by hand at
the DNS provider (`*.<primary>` and `*.<dev>`), pointing at the node's LAN address,
and put that address in `instance/config.yaml` `nodes[0].ip`. Set `provider: owned`
on that node (absent still means hetzner). Enable sshd and put the operator's key
in root's `authorized_keys`. Then run §4 as usual.

Which verify rows need LAN reachability. On an owned node, all of `make verify`
does: the API is reachable only through the ssh tunnel §4 prints, and that
tunnel is itself ssh to the node's LAN address, so every live kubectl row in
every stage fails off the LAN (stage4 included). On top of the tunnel, the
rule: every row that dials the node's address over ssh or tcp, or a wildcard
hostname of the instance's domain — those skip the tunnel and hit the node or
its ingress directly. The per-stage list below is the complete inventory of
`scripts/verify/*.sh` against that rule.

- **stage2:** the ssh swap row and both inotify rows (live `max_user_instances` and
  the persisted sysctl.d file); the 443 SAN rows and the unrouted-host certificate
  probe (`openssl s_client` to the node's `:443`); the port-80 redirect probe
  (`http permanently redirects to https`, `curl --resolve` to `probe.<primary>:80`
  at the node's address); the 6443 probe (`6443 still closed from the internet`);
  each catalog prod hostname under live public surfaces (`curl https://<host>`).
- **stage3:** the metadata probe; both rule-existence rows (`raw/PREROUTING` and
  `raw/OUTPUT`); the counters row on the hetzner path (`metadata DROP counters are
  non-zero` — keyed off with a reason when `provider: owned`); the timer row; the
  pinned dev-basic-auth probes and the two unauthenticated probes that precede them
  (no credentials → 401, wrong credentials → 401, correct credentials pinned to
  the node's address → 200).
- **stage4:** no row dials the node or a wildcard hostname directly; every live
  row still needs the LAN, via the tunnel.
- **stage5:** every Grafana row that dials `grafana.<primary>` — the door (anonymous
  401, admin 200, default datasource), the dashboard-identity rows, the Loki
  log-pipeline rows asked through Grafana's proxy, Tempo datasource health, and the
  trace row (drives `https://grafana.<primary>/<marker>` then searches Tempo through
  the proxy, plus the correlation-link rows in that block); the live intake-door
  rows at `intake.<primary>` (certificate, POST `/webhook/github`, unpublished
  `/healthz` and `/metrics`, both traversal paths, root 404). Git-parse rows for
  the door do not dial the node; live kubectl rows in this stage still need the
  LAN, via the tunnel.

A later row that matches the rule needs the LAN even if this list has not been
updated.

## 5. Platform layer — the gap between "kubectl works" and "everything runs"

```sh
export KUBECONFIG=~/.kube/config-<name>-platform    # via its tunnel

# The one bootstrap secret (the key that opens the sealed files cannot itself be sealed):
kubectl create namespace sops
kubectl -n sops create secret generic sops-age \
  --from-file=age.agekey=$HOME/.config/<name>-platform/age.key
# The file KEY must be age.agekey and the NAMESPACE sops — the operator mounts
# exactly that.

# NOTHING ELSE IS FILLED IN HERE. Sealed values arrive through git: §5a seals them,
# the desk applies the files, and the operator decrypts each one into its plain Secret.
# There is no store to write and no credential to create at this point.
orbit platform plan .  # the diff about to land, classified per app
orbit platform apply . # writes the platform tree, CRDs first, then namespaces, then workloads
```

There is no watcher to install and no deploy key to mint: nothing watches the repo
from inside the cluster. The desk applies what git holds, in phase order, and a
commit nothing applied is a commit that changed nothing — the plan says so before
anything is written. Let the early phases (sops operator, CNPG, cert-manager,
traefik) settle before judging anything above them: chart admission hooks that fire
while cert-manager's webhook has no endpoints fail and need one re-apply after it
settles — observed, not hypothetical.

### 5a. Standing the secret plane up

**Run this once the `root` Application exists and before judging Grafana or `make
verify`.** The plane has two kinds of values: GENERATED ones — random tokens nothing
outside the cluster dictates — minted by the desk, and SUPPLIED ones — a Cloudflare
token, an htpasswd line, the dashboards' admin password (§7) — sealed by hand. Both
land as SopsSecret files under `clusters/platform/secrets/<namespace>/<name>.yaml`:
the sealed namespace is the directory, and the Secret the operator renders is named by
the file's `spec.secretTemplates`. Commit and push, then apply from the desk
(`orbit platform apply`); the operator decrypts each one into its plain Secret. `secrets.provider: sops` in
`instance/config.yaml` is what keeps it that way — no vault, no stores.

The desk's verbs (`orbit secrets`, on the operator's own machine, from the instance
checkout):

- `orbit secrets mint <dir> --name <secret> --namespace <ns> [--namespace <ns>...]
  [--key TOKEN]` seals one fresh random value into a file per namespace. Every file
  holds the same value, so run it once per namespace for values that must differ;
  `--rotate` re-mints over existing files. The value travels only in memory and on
  sops's stdin — never argv, never output.
- A supplied value is sealed by hand: write the file with the value in plaintext under
  the template's `data:`, then encrypt in place with the org's age recipient:
  `sops --encrypt --age <recipient from .sops.yaml> --encrypted-regex
  '^(data|stringData)$' -i clusters/platform/secrets/<ns>/<name>.yaml`. Afterwards
  every value is `ENC[...]` ciphertext — `make verify` (stage4) refuses anything else,
  so encrypt before committing, and the plaintext never reaches git.
- `orbit secrets verify <dir>` is the tripwire, replacing the old missing/list checks:
  with `SOPS_AGE_KEY_FILE` pointing at the operator's `age.key`, it decrypts every
  sealed file and compares key-set and values against the live Secrets, printing one
  `ns/name match|mismatch <reasons>|no-file|decrypt-failed` line per file and exiting
  non-zero on any mismatch. (`export` and `adopt` are the migration verbs that moved
  the old vault's Secrets into sealed files; a fresh org never runs them.)

**Environments cannot reach each other.** There is one sealed file per namespace and
the sealed namespace is the directory, so a dev file has no expressible way to become
prod's Secret.

**Step 1 — the generated values.** Mint every random token the tree needs before the
pods that read them sync — the shared renderer token first, because both Grafana pods
sit in `CreateContainerConfigError` until its Secret exists (the template pins
`imageRenderer.existingSecret`, so the chart cannot mint one itself):

```sh
orbit secrets mint . --name grafana-image-renderer-token --namespace monitoring
```

Commit and push each mint, then apply from the desk; the operator renders the Secret.
Re-running a mint whose files all exist writes nothing; `--rotate` is the word for a
rotation, and a rotation alone moves no pod — restart the consumers, which keep running
the old value until they do.

**Step 2 — the supplied values.** Nothing in the cluster can mint a Cloudflare token
or an object-storage key, so a human seals each one once, on his own machine: write the
SopsSecret file with the value in plaintext under the template's `data:` — one document
per file, `metadata.namespace` equal to the directory it lives in — then encrypt in
place with the org's age recipient and commit:

```sh
sops --encrypt --age <recipient from .sops.yaml> --encrypted-regex '^(data|stringData)$' \
  -i clusters/platform/secrets/<namespace>/<name>.yaml
```

Work `orbit secrets verify` down to all-match: every `no-file` line is a Secret the
tree asks for that no sealed file renders, and every `mismatch` line names what differs.
`SOPS_AGE_KEY_FILE` must point at the operator's `age.key`, or verify cannot open the
files it is comparing.

- **Never an argument, never a variable.** Argv is world-readable through
  `/proc/<pid>/cmdline`: paste a clipboard value straight into the file in the editor,
  encrypt immediately, and never hold it in a shell variable. The plaintext lives on
  disk only until the encrypt runs and never reaches git — stage4 refuses the commit
  if it does.
- **A rotation is a re-seal.** Seal the new value over the old one, encrypt, commit.
  No deploy and no sync for the value itself — the operator decrypts the new file in
  place — but restart the consuming pods when the application reads its environment
  only at startup: until they do they are still running the old value.
- **`DEV_BASIC_AUTH_USERS` gates the first dev sync.** Every non-prod namespace's
  `dev-basic-auth` Secret renders from it; with nothing sealed, the dev URL answers
  404 instead of 401. Seal the htpasswd value — pasted straight from the password
  manager into the file, then encrypted — BEFORE the first `make sync` after a catalog
  entry sets `dev.enabled: true`. After that sync, `orbit secrets verify` is the
  post-sync tripwire.

**Step 3 — prove it.** `make verify` (§6). The rows that matter here: every sealed
file age-sealed with no plaintext and namespaced as its directory, no `.env` file
committed, the sops operator running, every sealed file live with the secrets app's stored inventory present
and a healthy decryption report, every Secret the files' templates name existing, and
the retired plane's APIs gone — plus `orbit secrets verify` comparing key-set and values
file by file. Values are never read: existence and the operator's own status only.

#### 5a-cloud. Retired

SUPERSEDED — the hosted-vault plane (`secrets.provider: infisical-cloud`) was removed
with the vault tooling; there is no vault to point at, hosted or otherwise. The sealed
plane above is the only plane. (The retired plane's own document, SECRET-PROVISIONING.md,
is a removed record: docs/README.md, "Retired design records", says how to read one.)

## 6. Acceptance

```sh
make verify        # core stages 2, 3, 4 and 5-platform — all live checks armed, 0 failed
```
Pre-boot (before step 4) the live stages key off visibly; they arm from the kubeconfig
file's existence and the catalog's declarations — never from reachability.

`scripts/verify/stage4.sh` is the secret plane's oracle: every file under
`clusters/platform/secrets/` is one age-sealed SopsSecret with no plaintext and
namespaced as its directory, no `.env` file is committed, the sops operator is running,
every sealed file exists live with the secrets app's stored inventory present and a healthy decryption
report, every Secret the files' templates name exists, and the retired plane's APIs are
gone. Values are never read — existence and the operator's own status message only.

`scripts/verify/stage5-platform.sh` is the platform planes' oracle: the observability stack
and its alert path, the blackbox probes, the three NetworkPolicy lockdowns, the status page,
the log pipeline, the traces, the two webhook doors, and Grafana's login door and datasource.
It sorts between stage 4 and any stage the instance adds of its own, and
it keys a section off when the config gate that renders that section's artifact is closed —
so a bucket-less org sees the Loki and Tempo rows key off by name. Rows that need an operator
credential this file does not create (the Grafana admin password, the GitHub App webhook
secret) say so rather than passing quietly.

Then the first project: add a catalog entry with a hostname, `make sync`, push — orbitd
applies the rendered tree for pairs named in `orbitd.applyPairs`, and it is live with a
valid wildcard certificate about a minute later, and its existence arms the
surface/population/isolation checks. A pair left out of `applyPairs` is rendered and
never applied: nothing else picks it up, so a project that never goes live is usually
a pair that was never listed.

Application images are built on the operator machine and shipped by tag: nothing builds
in the cluster and nothing runs in GitHub Actions (docs/DEVELOPMENT.md, `skills/orbit-ship`).

## Same-account test instances (caveats that do not apply to real orgs)

A test org sharing the operator's Hetzner/Cloudflare accounts additionally needs: its
own ssh keypair (fingerprint uniqueness), a distinct hcloud ssh-key name and server
name, a local tunnel on a different port, and its own age keypair — sealed files are
per-org ciphertext, so sharing one would let either org open the other's values. Fine
for a throwaway under the same operator; a real org gets its own everything.

## Teardown of a test instance

Server deletion is a deliberate act by design (`prevent_destroy`): delete via the
Hetzner API, `tofu state rm hcloud_server.prod`, then `tofu destroy` sweeps DNS
records, firewall and ssh key. Archive the repo; keep the credentials directory until
the org is certainly never coming back.

## 7. Dashboard sign-in (after the platform tree is green)

There is no identity provider on the platform and no single sign-on. Grafana keeps
its own local login; there are no per-person accounts to
provision, no clients to register, and no `auth:` key in `instance/config.yaml`
— a config that still carries one is refused by `make init` before anything is
written. An older copy of this runbook provisioned the provider here and filled
that key in its step 8a; both are gone.

**Grafana.** Sign in as `admin` at `https://grafana.<primary>`. The password has
exactly two homes: the workstation file
`~/.config/<name>-platform/grafana-admin-password`, and the sealed Secret
`grafana-admin` in the cluster (the chart reads it through `admin.existingSecret`,
keys `admin-user` / `admin-password`). Both come from one value you choose now —
Grafana never becomes healthy without that Secret (the monitoring Namespace is
wave 2, the stack wave 3), so seal it with the §5a values, before §6.

Mint the value straight into the workstation file — never argv, never a shell
variable (§5a step 2's rule) — and keep a copy in the password manager alongside
`age.key` (§2): losing it locks you out of the dashboards, and the only reset is
sealing a new one.

```sh
openssl rand -base64 32 > ~/.config/<name>-platform/grafana-admin-password
chmod 600 ~/.config/<name>-platform/grafana-admin-password
```

Then seal the same value as `clusters/platform/secrets/monitoring/grafana-admin.yaml`
— one SopsSecret rendering Secret `grafana-admin` in namespace `monitoring` (the
sealed namespace is the directory, §5a):

```yaml
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
        admin-password: <paste the same value>
```

Encrypt in place with the org's age recipient and commit (§5a step 2), then apply
from the desk; the operator renders the Secret. `make verify` proves it: stage5
refuses an anonymous Grafana call, accepts the admin credential from the
workstation file, and checks the default datasource is the Prometheus every
generated dashboard names. Keep the workstation file — the authenticated rows
cannot run without it, and a check that cannot run is not a pass. The API form of
the same sign-in (user `admin`, password from that file) is how scripts and the
observe skill talk to Grafana.

**Argo CD.** Retired: the template no longer ships it, and there is no console to
open. An existing instance still carrying `clusters/platform/config/argocd/oidc.yaml`
loses it on the next `make init` (reported by path, like every retired destination;
a file the instance wrote itself in that directory survives). Nothing in this
runbook needs its console: the platform layer lands through the desk (§5) and tenant
deploys land through the ship flow (§6).
9. Bucket-less orgs: nothing to strip by hand. Withdrawing the bucket deletes what
    it rendered (Loki, Alloy, Tempo and their config) on the next `make init`; a
    bucket-less org must still NOT declare `lokiRuler` — Loki is not rendered, so there
    is no ruler to wire, and `make init` refuses the pair rather than scaffolding a
    `logs` trigger whose rule nothing can evaluate.

## 8. Agent plane provisioning (Phase D — the org's own Claude subscription)

> **V2.** The generator renders agents, but agents acting on their own are outside V1
> (docs/V1.md §9) and nothing in V1's verify suite exercises them. Declare one only when
> the owner asks for it.

An agent is a catalog entry that `scripts/catalog-sync.py` renders into a Deployment +
Service on the runtime image, in one namespace (`orbit-agents-prod`) for the whole plane.
There is no CRD layer and no control plane to install (kagent was dropped 2026-08-27), so
this section is entirely credentials and one catalog edit.

1. **The subscription token.** On a workstation logged into the org's Claude account, run
   `claude setup-token` — it returns a ~1-year OAuth token. Seal it into the sealed tree
   (§5a step 2) as `CLAUDE_CODE_OAUTH_TOKEN`. This is the ONLY sanctioned model credential
   (the agent plane's "Model auth" decision): the runtime refuses to start a task when an API key or
   any credential-routing variable is present, and refuses when this one is absent. Orbit
   never resells claude.ai access — each org brings its own subscription, and the token
   bills to it.
2. **A bearer per agent.** Every POST to an agent must present one, or the runtime answers
   503 and does nothing. For an agent named `<name>`, mint it with `orbit secrets mint`
   (§5a step 1) sealed under `AGENT_<NAME>_TOKEN` (upper case, hyphens become
   underscores: agent `hello-orbit` → `AGENT_HELLO_ORBIT_TOKEN`).
   It is a password, not an identity, and rotating it is a re-mint plus a pod restart —
   no value committed, no deploy.
3. **The runtime image pin.** `agentRuntime: {image, tag}` in `instance/config.yaml` —
   core publishes the image once for every instance under the core repo's owner, and
   the generator refuses to guess the reference. Pin the `main-<sha7>` tag; `main` moves.
   There is **one** pin for the whole plane; no per-agent image override exists (it would
   let a catalog entry choose the registry the mounted token is sent to).
   **Image pullability:** every agent pod pulls with the instance's `ghcr-pull` token,
   which is built from the INSTANCE org — but the image lives under the CORE org's
   package. If that package is private, every agent `ImagePullBackOff`s. Either make the
   `orbit-agent-runtime` package public, or grant the instance's GHCR token read access
   to it. Confirm with `kubectl -n orbit-agents-prod get events` after the first sync.
4. **If any agent declares `triggers:` — the intake router (Phase D.3).** Nothing below
   applies to an instance whose agents are all cron or manual; the router is not rendered
   at all until a trigger exists.
   - `intakeRuntime: {image, tag}` in `instance/config.yaml`, same story and same
     pullability caveat as `agentRuntime` above (the package is `orbit-intake` under the
     CORE org). The generator refuses to guess it.
   - Three sealed values, each minted with `orbit secrets mint` (§5a step 1) unless a
     source dictates the value. A missing one is not a silent gap: that source refuses
     every event 503.
     `INTAKE_ALERTMANAGER_TOKEN` (one value, two Secrets: the file Alertmanager's webhook
     receiver presents and the one the intake verifies must match — `orbit secrets verify`
     checks each sealed file against its live Secret, so a drift goes red),
     `INTAKE_GITHUB_SECRET` (hand-sealed: it must equal the webhook's *Secret* in GitHub,
     which signs `X-Hub-Signature-256`),
     and `INTAKE_A2A_TOKEN` (the bearer an operator or another agent presents to
     `POST /a2a/<agent>`).
   - **Which trigger paths are live in this cut:**
     - **`alert` (Alertmanager) — working.** Alertmanager runs in the `monitoring`
       namespace, which the plane's default-deny NetworkPolicy would otherwise drop; the
       generator renders a targeted rule (`orbit-agents-intake-allow-monitoring`) admitting
       `monitoring` to the intake pod on its port only. Point Alertmanager's webhook receiver
       at `http://orbit-agents-intake.orbit-agents-prod.svc/webhook/alertmanager`. This same
       rule also carries the `logs`/`metric` triggers (all funnel through Alertmanager)
       and lets Prometheus scrape the router's `/metrics`.
     - **The Alertmanager route is ONE line of wiring, and it is already in the template.**
       Every alertname a `metric`/`logs` trigger derives starts with `OrbitAgent`, so a
       single terminal route — `alertname =~ "^OrbitAgent.*"` → the `orbit-intake` webhook
       receiver, placed below the Watchdog null-route in
       `clusters/platform/apps/kube-prometheus-stack.yaml` — carries every trigger that
       exists and every trigger anyone declares later. A scaffolded orbit is born with it,
       so **no trigger ever needs an Alertmanager edit.** Without it the alert falls
       through to Telegram: a human is paged with a machine's work item and the agent
       never runs.
     - **An instance created BEFORE that route does a one-time migration — four parts, not
       one.** In `clusters/platform/apps/kube-prometheus-stack.yaml`: (1) the
       `alertname =~ "^OrbitAgent.*"` route, below the Watchdog null-route; (2) the
       `orbit-intake` receiver it names, `send_resolved: false`; (3) that receiver's bearer
       moved **off the inline `bearer_token` / `authorization.credentials`** the old
       instructions gave and onto `authorization.credentials_file:
       /etc/alertmanager/secrets/alertmanager-intake/token` — the chart renders this config
       into a Secret that the plan diff prints in plain text, so an inline bearer is
       the intake's credential published to anyone with the UI; (4) `alertmanager-intake`
       added to `alertmanagerSpec.secrets`, plus the matching sealed file
       `clusters/platform/secrets/monitoring/alertmanager-intake.yaml` rendering Secret
       `alertmanager-intake` (key `token`) — without the mount the file in
       (3) does not exist and Alertmanager will not start. Stage 3 checks all four, and
       they **arm with the first `metric`/`logs` trigger**: a catalog with none, or with
       only `alert` triggers, stays green, so a core bump alone never produces a red row
       there is nothing to do about.
     - **`alert` triggers are not covered by that route.** The generic route carries
       `OrbitAgent*` and nothing else, so an `alert` trigger matching a platform alertname
       (`OrbitSiteDown`, …) still needs **its own** hand-added terminal route to the
       `orbit-intake` receiver, keyed on that alertname. Stage 3's per-alertname coverage
       row is what tells you it is missing. An `alert` trigger's `match` must name an
       `alertname` at all — a label-only match (`{severity: critical}`) is refused by the
       generator, because it would also catch the plane's own generated alerts and because
       nothing in the shipped route could deliver it (the AGENT-PLANE.md record, "`alert`
       triggers and the shipped route").
     - `metric` triggers render a PrometheusRule and fire through Prometheus
       automatically. `logs` triggers are **gated on `lokiRuler: wired` in
       `instance/config.yaml`** — a Loki rule is evaluated by the Loki *ruler*, which needs
       `rulerConfig.alertmanager_url` set AND rule storage it actually reads. The template's
       Loki application carries both for every instance with the `logs` feature on (local
       rule storage, and the generated ConfigMaps mounted into `/etc/loki/rules/fake`; the
       chart's rules sidecar is off, so a ConfigMap's `loki_rule` label is vestigial), so on
       a scaffolded instance the line only has to be written. An instance that changed its
       Loki application must check the wiring first: until the line is there the generator
       refuses a `logs` trigger rather than
       render a live route in front of a rule that cannot fire. (This gate is the agent
       plane's. A log *monitor*, `orbit_monitor_upsert` with `kind: log`, needs no line:
       orbitd reads the wiring itself, docs/MONITORS.md.)
     - `github` triggers are **gated on `intakeIngress: wired` in `instance/config.yaml`**.
       The generator renders the intake ROUTE and never the DOOR — it emits no Ingress and
       no Certificate for the intake, deliberately — so on a stock instance GitHub cannot
       reach `/webhook/github` at all. The flag asserts the instance hand-wrote both halves
       of the door: an Ingress path-scoped to `/webhook/github` alone (the intake's
       `/healthz` and `/metrics` are its only unauthenticated routes and must stay
       unpublished) and a NetworkPolicy admitting the ingress controller's pods to the
       intake pods' port, pod-narrowed on both sides. Until the flag is set the generator
       refuses a `github` trigger rather than render a live route behind an edge it cannot
       vouch for (the AGENT-PLANE.md record, "the `github` gate"). **Since v0.16.0 the flag also
       RENDERS both halves** (staged in `provisioning/`), and withdrawing it deletes them,
       so it is a switch rather than a promise about files written by hand.
       - *Known limitation:* the rule admits the **whole `monitoring` namespace** (a
         `namespaceSelector`), not Alertmanager/Prometheus pods specifically — the operator
         chart labels its pods inconsistently, so a pod-level selector would silently drop
         scrapes on an upgrade. It is scoped to the intake port and **every intake path is
         still bearer/HMAC-authenticated**, so this is reach, not access: a compromised
         exporter in `monitoring` could open a socket to `intake:8080` but still cannot spend
         the subscription without a source credential. Tightening to a pod selector is a
         follow-up gated on stable operator labels.
     - **`a2a` — working.** An operator or another in-plane agent presents `INTAKE_A2A_TOKEN`
       to `POST http://orbit-agents-intake.orbit-agents-prod.svc/a2a/<agent>`; the caller is a
       neighbour, already covered by the plane's own-pods rule.
     - **`github` — works, once you open the door with one config line.** The route
       compiles and the signature is verified. Since core v0.16.0 the door itself —
       path-scoped to `/webhook/github`, with the Traefik guard chain — ships in the
       template, staged under `provisioning/`, so it is not copied out of another org. It
       is rendered ONLY while `instance/config.yaml` says `intakeIngress: wired`, and
       `make init` DELETES it when that is withdrawn — so the attestation and the artifact
       cannot disagree, and a fresh org has no public webhook Ingress at all. Opening it
       remains a deliberate exposure decision; it is now one line rather than two files.
5. **Scope the subscription token to the plane (defense in depth).** The generator
   already refuses any non-agent project that names `CLAUDE_CODE_OAUTH_TOKEN` or an
   `AGENT_*_TOKEN` — so nothing the generator renders can read a plane credential
   from another namespace. The rest of the scoping is structural: one sealed file per
   namespace, and the sealed namespace is the directory, so a Secret rendered for the
   plane is never mounted into a tenant pod.
6. **Declare the agent** under `agents:` in `catalog/projects.yaml` (the template file
   documents every key, `triggers:` included), then `make sync && make check`, commit, and
   push — orbitd applies the rendered tree for a pair named in `orbitd.applyPairs`, and
   nothing else picks up a pair left out.

Order matters: seal the values FIRST. The operator renders no Secret for a key that is
not sealed, so an agent declared ahead of its credentials parks in
CreateContainerConfigError — which reads as a broken image rather than a missing value.

Two failure modes that look identical from `kubectl get deploy` (both Available, both
useless) and are what stage3/stage4's agent checks exist to separate: a missing bearer
(every POST 503s) and a missing subscription token (tasks accepted, then refused —
stage4 reads the token straight out of the pod's env, because a wrong-bearer probe 401s
before the subscription check ever runs).
