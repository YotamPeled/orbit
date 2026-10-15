---
name: orbit-admin
description: "List principals and tokens, issue a principal with the least role, revoke a token, retire a principal; the confirmation round and the one-time credential. Needs the admin role. Read orbit-operator first."
role: admin
---

# Manage principals (admin only)

Every tool here requires the `admin` role. If your token holds `viewer` or `operator`,
these four tools are not in your tool list at all (`tools/list` hides tools above the
held role), and a call made anyway is refused before it runs with `FORBIDDEN`, naming
`required_role: admin` and your `held_role`, as an error result your session survives.
There is no lower-role path and nothing to search for: report that the job needs an
admin token, name the tool and the arguments an admin would use, and stop. Do not call
other tools to "check the token works"; the tool list already told you.

A **principal** is a machine identity: a name, a role, and a token lifetime. It
exchanges a client secret for access tokens at the server's token endpoint. A **token**
is one such access token, identified by its `jti`, which is the only handle revocation
takes.

## 1. See what exists: `orbit_auth_list()`

Two listings come back. `principals[]` is `{name, role, created, token_ttl_seconds}`;
`tokens[]` is `{jti, sub, role, issued_at, expires_at, revoked}` where `sub` is the
principal name. Before issuing anything, check the name is not already taken and the
role you are about to grant is not already held by a principal that could be reused.

## 2. Issue a principal: `orbit_auth_issue(name, role, ttl)`

`name` matches `^[a-z0-9][a-z0-9-]{0,62}$` and becomes both the token `sub` and the
client id. `role` is `viewer`, `operator` or `admin`. `ttl` is the lifetime in seconds
of every access token this principal is granted (bounded by the server; the tool's
input schema states the range).

**Least role first.** A principal that only reads gets `viewer`. A principal that
reads logs or traces and also ships is two principals, not one operator: observability
data is attacker-influenceable text, and the role split is the boundary that keeps a
prompt injected through a log line from reaching a change tool. Grant `admin` only to
a principal that will manage principals.

**The confirmation round.** The first call does not write anything: it returns an
input request (an elicitation under the key `confirm`, with the message naming the
name, role and ttl) and a `requestState`. The client answers `{confirm: true}` and
retries the same call with the answer and the echoed state; the state is sealed to
this principal, this tool and these exact arguments for ten minutes, so a changed
argument on the retry is rejected. `{confirm: false}`, a decline, or a retry without
the state returns `status: refused` and nothing was written. From the `orbit` CLI the
answer is `--yes` or `--no`; a non-interactive call without either exits 2.

**The one-time credential.** `status: applied` carries `evidence.client_id`,
`evidence.client_secret`, `evidence.role`, `evidence.token_ttl_seconds` and
`evidence.token_endpoint`. The secret is shown exactly once and is never readable
again (the server keeps only a hash). Hand it to its owner out of band, and do not
paste it into anything that persists (a ticket, a log, a chat). If it is lost, retire
the principal (`orbit_auth_retire(name)`, section 4) and issue a new one.

`status: failed` with a `store` step: the name exists or the store refused; the
step's `detail` says which.

## 3. Revoke a token: `orbit_auth_revoke(jti)`

Take `jti` from `orbit_auth_list`'s `tokens[]`, never from memory. The same
confirmation round applies; the message names the `jti`. `status: applied` with a
`revocations` step of `appended` means the next request bearing that token is
refused; `already revoked` means it was, and nothing changed (the call is idempotent).
`evidence.known_token: false` means the id was not one this server issued: the
revocation is recorded anyway, but check the id you were given.

Revocation is per token, not per principal: a principal keeps its secret and can mint
a new token. To take the whole identity out of service, retire it instead (next
section): revocation alone never stops the next exchange.

## 4. Retire a principal: `orbit_auth_retire(name)`

Take `name` from `orbit_auth_list`'s `principals[]`, never from memory. The same
confirmation round applies; the message names the principal. `status: applied` means
three things at once: the secret no longer mints (an exchange with it is refused),
every token the principal minted is revoked (the next request bearing one is
refused), and the name is gone from `principals[]`. `evidence.revoked_tokens` says
how many of its tokens this call revoked; `evidence.known_principal: false` means
the name was not there. The call is idempotent: retiring a name that is not there
changes nothing, and retiring twice is the same as retiring once.

One refusal: the last admin principal cannot be retired, since nobody could issue
another afterwards. That refusal is an error and changes nothing — neither the
principal nor its tokens. Issue another admin first, then retire this one.

## The answer

Report what changed with the tool's `status` and `steps`, the `jti`, `name` or
`client_id` involved, and, for an issue, that the secret was handed over and where.
Never include the secret in the report.
