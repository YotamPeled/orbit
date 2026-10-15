# orbitd — the control surface

orbitd is the one process every agent operates Orbit through: an MCP server over Streamable
HTTP, and the OAuth 2.1 resource server and minimal authorization server that guard it. This
document covers the transport, the auth flow end to end, the token format, the roles, the
tool contract, the admin tools, and the `orbit` CLI (an MCP client in the same package).
Cluster-facing tools and the deployment manifests are separate jobs. Code: `services/orbitd/`.

## Decisions

**MCP revision 2026-07-28, on the official SDK.** `mcp` 2.2.0 (PyPI, released 2026-09-07)
implements 2026-07-28 natively: `LATEST_PROTOCOL_VERSION == "2026-07-28"`; the per-request
`_meta` envelope (`io.modelcontextprotocol/protocolVersion`, `clientCapabilities`,
`clientInfo`) in place of `initialize`; `server/discover`; the `MCP-Protocol-Version`,
`Mcp-Method` and `Mcp-Name` header mirror with `-32020 HeaderMismatch` whenever a header
disagrees with the envelope — the method, the name, or the version in either direction —
and `-32022 UnsupportedProtocolVersion` when both version carriers agree on a version the
surface does not know; a request with no `clientCapabilities` key is answered with the
ordinary `-32602` code, not one of ours;
`ttlMs`/`cacheScope` on the cacheable list results (`CacheHint`); the multi-round-trip
`InputRequiredResult` with a sealed `requestState`; and DNS-rebinding/Origin validation.
It does **not** implement the Tasks extension (`io.modelcontextprotocol/tasks`), which the
design already anticipated (§3.4: the job handle now, `taskId` when the SDK lands it).
Nothing in this job needed a delta layer; `server.py` asserts the SDK's revision at import
so a dependency bump that moves it is noticed. Pinned: `mcp==2.2.0`.

The SDK renamed `FastMCP` to `MCPServer` in 2.x; the 1.x API is gone.

**JWTs with PyJWT + cryptography (EdDSA).** The SDK's auth module supplies the resource-server
half (`BearerAuthBackend`, `RequireAuthMiddleware`, the `TokenVerifier` protocol, the RFC 9728
route helper) and an authorization-server half built around `authorization_code`,
`refresh_token` and the RFC 7523 jwt-bearer grant. It has no `client_credentials` grant and
does not verify JWTs itself. So: orbitd implements the `TokenVerifier` with PyJWT 2.13
(`EdDSA`, `cryptography` for the Ed25519 key) and writes the `/token` endpoint and the RFC 8414
document by hand, while every bearer check on the MCP path is the SDK's middleware. Pinned:
`PyJWT==2.13.0`, `cryptography>=45`.

**Stateless, JSON responses.** Every request is one POST, one JSON body back
(`json_response=True`). Nothing between calls lives in the process; the confirmation step's
state rides in the sealed `requestState` the client echoes, and the SDK seals it with a key
derived from the signing key, so a restart between the two rounds does not lose it.

## The auth flow, end to end

```
 admin (MCP)              orbitd                                   new principal
   │ orbit_auth_issue       │                                           │
   │ (name, role, ttl) ───► │ InputRequiredResult: "Issue ...?"          │
   │ ◄──────────────────────│                                           │
   │ retry + inputResponses │                                           │
   │   + requestState ────► │ store principal (role, sha256(secret))    │
   │ ◄── client_id, secret  │                                           │
   │ ─ ─ hand over out of band ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ►│
                            │ POST /token grant_type=client_credentials │
                            │ ◄─────────────────── Basic client_id:secret
                            │ Ed25519 JWT, 30 d ─────────────────────► │
                            │                                           │
                            │ ◄── POST /mcp  Authorization: Bearer <jwt>│
                            │     verify sig/aud/iss/exp, jti not revoked
                            │     role gate on the tool, then dispatch  │
```

1. **Bootstrap.** `orbitd serve` creates the Ed25519 signing key if absent so a fresh volume
   can become Ready. `orbitd bootstrap` (run once, with the same environment as the server)
   then prints one admin credential to stdout: `{client_id, client_secret, token_endpoint,
   resource}`. It still generates the key if absent (a local run that never called serve).
   The store keeps only the SHA-256 of the secret. Bootstrap is one-time: it refuses (exit 2)
   whenever any admin principal exists in the store, whatever `--name` is given, so a second
   admin can only come from `orbit_auth_issue` with its confirmation step.
2. **A client discovers where to authenticate.** An unauthenticated `POST /mcp` gets `401` with
   `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="<origin>/.well-known/oauth-protected-resource/mcp"`.
   That document (RFC 9728, also served at the root well-known path) names the authorization
   server, which is the same origin; its RFC 8414 document lists `token_endpoint`,
   `grant_types_supported: ["client_credentials"]`, the scopes, and a `jwks_uri`.
3. **A principal gets a token.** `POST /token` with `grant_type=client_credentials` and
   `client_secret_basic` or `client_secret_post` authentication. Optional `scope` narrows the
   principal's role (never widens); optional `resource` (RFC 8707) must be the canonical MCP
   URL. Response: `{access_token, token_type: "Bearer", expires_in, scope}`; the token's `jti`
   is written to the issued-token ledger so `orbit_auth_list` can name it.
4. **Every MCP request is verified.** The bearer middleware calls the verifier: signature
   against the server's key, `aud` = the canonical MCP URL, `iss` = the issuer URL, `exp` with
   30 s leeway, required claims present, `jti` absent from the revocation file (read on every
   call, so a revoke needs no restart). Any failure is `401`; a token whose scopes name no
   Orbit role is `401` too.
5. **The role gate decides the tool.** One registry, consulted before dispatch, and the
   refusal is answered in the protocol: HTTP 200 with an `isError` result. A call above the
   token's role is `{code: "FORBIDDEN", evidence: {required_role, required_scope,
   held_role}, next_action: "present a token whose scope includes orbit:admin"}`. A name the
   registry does not declare is `{code: "UNKNOWN_TOOL", next_action: "list the tools this
   surface offers with tools/list"}` — no role and no scope are named, because no
   authorization could make that call work, and nothing points at the authorization
   metadata for the same reason. A `resources/read` for a URI with no declaration behind it
   is answered the same way. Both refusals are recorded, whichever path the caller took.

   The forbidden HTTP status is no longer answered for these calls: they used to arrive
   before the protocol from a gate in front of it that read the
   routing headers. Every client library sets those headers, so every agent met that gate —
   and those tore the session down, leaving a transport error with no code, no next action
   and no session to take one in. An agent that mistyped a tool name lost its session rather
   than correcting itself. Authentication is different and has not moved: a caller with no
   token or an unusable one is refused with `401` before the protocol, because there is no
   session to answer in.

   `tools/list` is filtered to what the token may call and marked `cacheScope: private`,
   `ttlMs: 60000`.
6. **Revocation.** `orbit_auth_revoke(jti)`, after confirmation, appends the id to the
   revocation file. The next request bearing that token is `401`. The file is one `jti` per
   line, the shape the ConfigMap mount will have. `orbit_auth_retire(name)`, after
   confirmation, removes the principal — its secret stops minting tokens — and appends
   every token id the ledger holds under its name to the same file, so the tokens it
   already minted are `401` too. Retiring the last admin is refused, since nobody could
   issue another.

## Token format

Ed25519-signed JWT (`alg: EdDSA`, `kid` = first 16 hex of SHA-256 of the raw public key; the
public key is published at `/.well-known/jwks.json` as an `OKP`/`Ed25519` JWK).

| claim | value |
|---|---|
| `iss` | the issuer URL (`ORBITD_ISSUER_URL`, default: the MCP URL's origin) |
| `aud` | the canonical MCP URL (`ORBITD_MCP_URL`), e.g. `https://mcp.<domain>/mcp` |
| `sub` | the principal's name (also its `client_id`) |
| `scope` | the role's scopes, space-separated: admin → `orbit:viewer orbit:operator orbit:admin` |
| `role` | the role name, for readers |
| `iat`, `exp` | issue time and expiry; `exp - iat` is the principal's TTL (default 30 days) |
| `jti` | 16 random bytes, base64url; the revocation handle |

Tokens are never accepted for another audience and never forwarded: orbitd holds its own
cluster and git credentials, and a caller's token stops here.

## Roles

| role | scopes carried | may call |
|---|---|---|
| viewer | `orbit:viewer` | read-only tools (`readOnlyHint: true`) |
| operator | viewer + `orbit:operator` | viewer tools plus ship, rollback, restart, monitors, secrets |
| admin | operator + `orbit:admin` | everything, including `orbit_auth_*`, catalog apply, reconcile |

Roles nest as scopes, so the check for a tool is one membership test on the tool's declared
role. The filtered `tools/list` is a convenience; the gate on `tools/call` is the control.

## The tool contract

Every tool is declared through `Registry.tool(role=..., read_only=..., destructive=...,
idempotent=...)` in `roles.py`, which is the only place a tool says who may call it. The gate
denies by default: a `tools/call` naming a tool without a registry declaration (whether or not
the SDK knows the name) is refused before dispatch for every caller including admins, with
the not-offered refusal — no role and no scope named, at either tier — and `tools/list`
never shows it.
The registry refuses a name outside `orbit_[a-z0-9_]+` (≤ 64), a tool without a description, a
read-only tool marked destructive, and a mutating tool that does not promise the shared
envelope in its return type. Input schemas come from the function signature (pydantic);
output schemas are advertised. Annotations are set on every tool with `openWorldHint: false`.

Envelopes:

- mutating: `{status: applied|refused|failed|dry_run|running, steps: [{name, status, detail}], commit, evidence{}}`
- lists: `{items[], total, returned, truncated, next_cursor}`
- actionable failure (`isError: true`): `{code, what_failed, evidence{}, next_action, retryable}`

Tools encode no workflow; skills do (SKILLS-ADDENDUM).

## Tools in this job

| tool | role | annotations | what |
|---|---|---|---|
| `orbit_platform_health()` | viewer | read-only, idempotent | the server's own state; a stub until the platform probes land |
| `orbit_deploy_status(project, env)` | viewer | read-only, idempotent | desired tag vs pod-spec images; `running_digests` from imageID |
| `orbit_auth_issue(name, role, ttl)` | admin | mutating | confirm → register a principal, return its secret once |
| `orbit_auth_revoke(jti)` | admin | destructive, idempotent | confirm → append to the revocation list |
| `orbit_auth_retire(name)` | admin | destructive, idempotent | confirm → remove the principal, revoke every token it minted |
| `orbit_auth_list()` | admin | read-only | principals and issued tokens with revocation state |
| `orbit_deploy_ship(project, env, components, tag, dry_run, expect)` | operator | mutating | confirm → job: ship.sh per component, wait, assert |
| `orbit_deploy_rollback(project, env, component, to, dry_run)` | operator | destructive | confirm → job: ship the previous (or `to`) tag |
| `orbit_workload_restart(project, env, component, dry_run)` | operator | destructive | confirm → job: patch `orbit.dev/restarted-at`, wait, assert |
| `orbit_platform_diff(project?, env?)` | admin | read-only, idempotent | render the desired objects and diff them against live; persist the day's report (`report_path`, `date`, `owned`, `changed[].diffs`) |

### `orbit_deploy_status` image fields

`orbit_deploy_status(project, env)` compares `catalog/tags.yaml` to the images the pod
spec pins. Per component:

| field | source |
|---|---|
| `desired_tag` | `catalog/tags.yaml` key `project/env/component` |
| `running_images` | every `spec.containers[*].image` on active pods (app containers, not init), in spec order |
| `running_digests` | every `status.containerStatuses[*].imageID` (the digest the kubelet pulled) |
| `drift` | true when `desired_tag` is set and no spec image carries it |

`status.containerStatuses[].image` is not used: the kubelet reports whichever tag name
its image cache holds for the digest, so two tags of the same digest would look like
drift.

### Shadow diff

orbitd is the applier: it applies the rendered objects itself, for the project/env pairs
listed in `orbitd.applyPairs`, and nothing else picks a change up. The shadow diff predates
that and stays as a drift check: orbitd renders the desired objects every day and diffs
them against what is live. `orbit_platform_diff` runs it on demand (one pair, or every
project/env the catalog declares). A runner thread started from `serve` does the same
5 minutes after start and then every 24 hours. The diff writes nothing to the cluster.

The same compare runs offline, with no kube and no instance directory:

```
python -m orbitd.shadow_offline <rendered.json> <live.json>
```

Each file is a JSON list of objects. stdout is one report (`empty`, `missing`, `extra`,
`owned`, `changed`). Live-reading (`read_live`) and diffing (`compare_objects`) are
separate; the tool and the daily runner call the same diff.

Each report is stored at `<ORBITD_STATE_DIR>/shadow/<project>-<env>/<UTC date>.json`
(one file per day; a later run the same day overwrites). `ORBITD_STATE_DIR` defaults to
the directory that holds the signing key (`/var/lib/orbitd` in the pod).

Report shape:

| field | what |
|---|---|
| `empty` | true when `missing`, `extra` and `changed` are all empty |
| `rendered` | `{kind, namespace, name}` for every desired object |
| `live_matched` | those objects that were found live |
| `missing` | rendered, not live |
| `extra` | live objects labelled `app.kubernetes.io/instance=<project>` that no rendered object names and no rendered owner claims |
| `owned` | live objects whose `ownerReferences` kind+name is a rendered object, with that owner |
| `changed` | per object, `paths` plus `diffs: [{path, rendered, live}]` (values JSON-encoded, cut at 200 characters) |
| `render_sha` | sha256 of the rendered list (kind, namespace, name order) |
| `streak_days` | consecutive daily reports with `empty: true` ending today |
| `path` / `report_path` | the stored JSON path |
| `date` | the UTC date of that file |

Live is normalised before compare: `status`, `managedFields`, `resourceVersion`, `uid`,
`creationTimestamp`, `generation`, and annotations/labels set by kubectl or a previous applier are
dropped. Fields the server defaults that the render omitted are treated as equal.
Quantities (memory, CPU) compare numerically.

**The seven-day rule** was the gate for switching the applier on: seven consecutive UTC
days of empty reports for every catalog pair, ending today, with a missing day or a
non-empty report resetting the streak. The applier is on; `streak_days` is still reported
and a broken streak now means drift to investigate, not a switch to hold back.

**The confirmation step.** `issue`, `revoke` and `retire` return an `InputRequiredResult` on the first
round: one `elicitation/create` under the key `confirm` asking for `{confirm: boolean}`, plus
a `requestState`. The client answers and retries the same call with `inputResponses` and the
echoed state. The SDK seals the state to the principal, the method, the tool name, a digest
of the arguments and a ten-minute window, so a confirmation cannot be replayed against other
arguments or by another token (the retry is rejected as a protocol error). A retry that
carries an answer without the state, a `decline`/`cancel`, or `confirm: false` all yield
`status: refused` with nothing written. Only `accept` with `confirm: true` applies.

## HTTP surface

| path | auth | what |
|---|---|---|
| `POST /mcp` (the path of `ORBITD_MCP_URL`) | bearer | the MCP endpoint, stateless, JSON responses |
| `GET /.well-known/oauth-protected-resource[/mcp]` | none | RFC 9728 |
| `GET /.well-known/oauth-authorization-server` | none | RFC 8414 (no `authorization_endpoint`: no grant uses one) |
| `GET /.well-known/jwks.json` | none | the signing public key |
| `POST /token` | client credentials | the only grant |
| `GET /healthz` | none | liveness, version, protocol revision |

Host and Origin are validated (`ORBITD_ALLOWED_HOSTS`, `ORBITD_ALLOWED_ORIGINS`; defaults are
the MCP URL's host and origin), so Traefik must pass `Host` and `Origin` through untouched,
along with the `MCP-*`/`Mcp-*` headers.

## Configuration

| variable | meaning |
|---|---|
| `ORBITD_MCP_URL` | canonical MCP URL; the token audience and the resource identifier (required) |
| `ORBITD_SIGNING_KEY` | path to the Ed25519 private key PEM; `serve` creates it if absent (required) |
| `ORBITD_PRINCIPALS` | path to the JSON principal store and token ledger (required) |
| `ORBITD_REVOCATIONS` | path to the revocation list, one `jti` per line (required) |
| `ORBITD_ISSUER_URL` | RFC 8414 issuer; default the MCP URL's origin |
| `ORBITD_TOKEN_TTL_SECONDS` | default token lifetime; 2592000 (30 days) |
| `ORBITD_ALLOWED_HOSTS`, `ORBITD_ALLOWED_ORIGINS` | comma lists for DNS-rebinding protection |
| `PORT` | listen port, default 8080 |
| `ORBITD_INSTANCE_DIR` | checkout of the instance repo (catalog, tags, monitors). Optional |
| `ORBITD_PROMETHEUS_URL` | Prometheus base URL. Optional |
| `ORBITD_LOKI_URL` | Loki base URL. Optional |
| `ORBITD_TEMPO_URL` | Tempo base URL. Optional |
| `ORBITD_ALERTMANAGER_URL` | Alertmanager base URL. Optional |
| `ORBITD_KUBECONFIG` | kubeconfig path; absent means in-cluster config |
| `ORBITD_STATE_DIR` | directory for persisted shadow reports; default the signing-key directory |

Nothing instance-specific is in the code; every value above comes from the environment.
The six backend variables are not required at import. A tool that needs a backend that
is not set returns the actionable failure `backend_unconfigured` naming the variable.
`ORBITD_KUBECONFIG` is the exception: leaving it unset selects in-cluster config, not
an unconfigured failure. Observed state is the Kubernetes API plus Prometheus, Loki,
Tempo and Alertmanager; desired state is the instance git repo.

## The `orbit` CLI

`orbit` is an MCP client in the same package (`orbitd.client`). Every `call` is one
`tools/call` over Streamable HTTP with the official SDK; `tools` is `tools/list`. There is
no second code path.

```
orbit login --url https://mcp.example/mcp
orbit tools
orbit call orbit_platform_health --json
orbit orbit_platform_health --json          # same call: a tool name is a subcommand
orbit call orbit_auth_issue --yes --json --arg name=desk --arg role=viewer
```

`login` reads `{client_id, client_secret}` from stdin (bootstrap output or an issued
principal) or from `ORBIT_CLIENT_ID` / `ORBIT_CLIENT_SECRET_FILE`, exchanges them at the
token endpoint (`client_credentials`), and stores `{url, token, claimed_expires_at,
claimed_role}` in `$XDG_CONFIG_HOME/orbit/<host>.json` (`<host>_<port>.json` when the address names a port), mode 0600. It prints role and
expiry; it never prints the secret or the token.

`call` takes `--arg key=value` (repeatable) and/or `--json '{...}'` as arguments, and
`--json` with no value to print `structuredContent` as JSON. The default is a one-line
human summary.

Mutating tools return an `InputRequiredResult` first. `call` prints the elicitation
message, then: `--yes` answers accept/`confirm: true` without asking; `--no` answers
`confirm: false` (the mutation is refused); on a tty it asks `confirm (y/N):`; non-tty
without `--yes`/`--no` exits 2, naming those flags. A refused mutation (`status: refused`)
exits 1 with the envelope. An issued `client_secret` appears once, in the JSON output.

Exit codes: 0 ok; 1 the tool returned `isError` or `status: refused` (the failure
envelope on the error stream: a document when the caller passed `--json`, a sentence
otherwise), including a name the surface does not offer, which is a wrong name and
not a credential to fix; 2 usage; 3 auth — a 401, or a refusal whose code is `FORBIDDEN`,
which names the role required and the role held; 4 transport. The desk decides between 1
and 3 by the refusal's code, not by the transport: the two refusals arrive the same way.

## Running the tests

```
cd services/orbitd && python3 -m pytest -q
```

On an interpreter that cannot import the pinned SDK (no pip, or PEP 668), the root
`conftest.py` builds `services/orbitd/.venv` from `pyproject.toml` and re-runs the same
invocation under it. Tests use no network: the ASGI app is driven in-process through
httpx2, with the official SDK client negotiating via `server/discover`.

## Deployment

Build the image from the repository root (the context has to include `scripts/`):

```
docker buildx build --platform linux/amd64 -f services/orbitd/Dockerfile   --build-arg ORBITD_VERSION=v0.33.0 -t ghcr.io/<owner>/orbitd:v0.33.0 --push .
```

The image installs helm v3.19.0 (official tarball, sha256-pinned, no plugins) so
orbitd can `helm template` the service chart for the shadow diff. Helm's cache
directories point at `/tmp`.


Staged sources live in `template/provisioning/clusters/platform/config/orbitd/`
and `make init` renders them into an instance's `clusters/platform/config/orbitd/`
unconditionally (no gate). `platform-config` already recurses that tree.

| file | what |
|---|---|
| `namespace.yaml` | namespace `orbit` |
| `rbac.yaml` | ServiceAccount `orbitd`, ClusterRole/Binding `orbitd-readonly` (get/list/watch on every kind the chart and the generator render, plus the phase-3 kinds), ClusterRole/Binding `orbitd-operate` (`apps/deployments` `patch` only; namespaces are guarded in code) |
| `pvc.yaml` | 1Gi ReadWriteOnce claim on the instance's default storage class, mounted at `/var/lib/orbitd` |
| `deployment.yaml` | one replica, image `{{coreRegistry.owner}}/orbitd:{{coreRegistry.orbitdTag}}`, 128Mi request / 192Mi limit, `runAsUser: 10001`, `readOnlyRootFilesystem`; git-sync sidecar (`registry.k8s.io/git-sync/git-sync:v4.7.1`, 32Mi/64Mi, uid 10001) follows `main` of the instance repo (full history, so the deploy ledger can read `git log` of the tags file) into `/var/lib/instance/current`; `ORBITD_INSTANCE_DIR` is that path. The orbitd container also gets `ORBIT_CONFIG_DIR=/etc/github-app`, `ORBIT_REGISTRY_TOKEN_FILE=/etc/registry/ghcr-pull-token`, `ORBIT_INSTANCE_CONFIG=/var/lib/instance/current/instance/config.yaml`, `ORBIT_ACTOR=orbitd`, `TMPDIR=/scratch`; the github-app secret (read-only), the registry secret at `/etc/registry` (read-only), and a `scratch` emptyDir (`sizeLimit: 256Mi`) at `/scratch` for ship clones |
| `service.yaml` | Service + Ingress for host `orbit.<primary>` (Traefik class, TLS via the platform wildcard; no Ingress `tls:` block) |
| sealed Secrets | `clusters/platform/secrets/orbit/`: `ghcr-pull` (dockerconfig), `orbitd-github-app` (`GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`, projected as git-sync files `app-id`, `installation-id`, `private-key.pem` and as the names the ship scripts read, `github-app-id`, `github-app-installation-id`, `github-app-key.pem`), `orbitd-registry` (`GHCR_PULL_TOKEN` as file `ghcr-pull-token`), `orbit-alert-webhook` — each a SopsSecret the instance seals (docs/ONBOARDING.md §5a) |

The PVC holds the signing key, the principal store and the revocation list. The
Deployment rolls with `Recreate` because the claim is ReadWriteOnce. The kube
client authenticates with this ServiceAccount's projected token; nothing else is
auto-mounted.

The public host is `orbit.<primary>`. Pin `coreRegistry.orbitdTag` in
`instance/config.yaml` to a release tag (`v0.33.0`), never `main`, which moves; the image is
built with `--build-arg ORBITD_VERSION=<tag>` so `/healthz` reports it, and `make verify`
stage6 checks the running pod and `/healthz` against the pin.

### Bootstrap, once

`orbitd serve` creates the Ed25519 signing key on the PVC if it is absent
(mode 0600), so the pod becomes Ready with an empty principal store. After
the pod is Ready, create the first admin principal with the same environment
the server already has:

```
kubectl exec -n orbit deploy/orbitd -- orbitd bootstrap
```

That command prints one admin credential to stdout: `{client_id, client_secret,
token_endpoint, resource}`. Copy it into the operator's password manager (or
whatever out-of-band store this org uses). The process keeps only the SHA-256 of
the secret; the printed secret is not written to the cluster and must not be
committed. A second run refuses. Further principals come from `orbit_auth_issue`.
