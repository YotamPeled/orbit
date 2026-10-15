#!/usr/bin/env python3
"""No receiver carries a credential inline — every secret comes from a file.

A metric/logs trigger's alert reaches a human through a Telegram receiver and an
agent through a webhook receiver. Both receivers need credentials, and
Alertmanager gives every one of them two spellings: inline (`bot_token: ...`)
or from a file (`bot_token_file: /etc/alertmanager/secrets/...`). The inline
spelling puts the credential in the rendered config, in git, in every log that
prints the config — so the only acceptable spelling is the file one, on every
receiver type, not just the Telegram one a narrower row once checked.

    am-inline-credentials.py <alertmanager-config>

`alertmanager-config` is a plain alertmanager.yaml, as extracted from the
running Alertmanager's own `/api/v2/status` (`config.original`). That view
redacts every secret-typed VALUE as `<secret>` but keeps every KEY — so an
inlined token is still detectable by the key it leaves behind, and a correct
`bot_token_file` is visible in cleartext because a path is not a secret.

Two shapes are refused:

  * any field that carries a credential (`bot_token`, `api_key`,
    `auth_password`, `credentials`, ... — matched by name, on any receiver
    type) present in its inline form rather than as `<name>_file`;
  * any URL with what looks like a credential embedded in it: userinfo
    (`https://user:pass@host/`), a Telegram bot token in the path
    (`/bot123456:AA...`), or a long token-like run in the path or query,
    where a clean hook URL has only short path segments.

Prints one `ok `/`FAIL ` line per assertion — stage5-platform.sh turns them
into its own rows — and exits non-zero if any failed, so core CI can call it
directly. A FAIL names the FIELD it objects to and never the value: this runs
in CI, where stdout is a log kept for ever, and a checker that prints what it
found is the leak.
"""
import re
import sys
import urllib.parse

import yaml

sys.dont_write_bytecode = True

out = []


def check(label, cond):
    out.append(("ok " if cond else "FAIL ") + label)


def finish():
    print("\n".join(out))
    sys.exit(1 if any(line.startswith("FAIL") for line in out) else 0)


def abort(label):
    """A red row, and STOP. Carrying on over a config that cannot be read
    honestly would answer "no credential inline" about a file half of which
    was never evaluated — the fail-open answer, not a conservative one."""
    check(label, False)
    finish()


# Field names that carry a credential when present inline. Matched by suffix
# so a receiver type added next year gets the same answer; the `_file`
# spelling of each is the sanctioned one and is checked first.
SECRET_SUFFIXES = ("_token", "_key", "_password", "_secret")
SECRET_NAMES = frozenset({"password", "passwd", "pwd", "secret", "token",
                          "credentials"})

# Mappings that are only ever containers for the above: a scalar where the
# mapping belongs is a credential dumped inline, not a configuration.
CONTAINER_FIELDS = frozenset({"http_config", "authorization", "basic_auth",
                              "oauth2", "tls_config", "sigv4"})
# A header name that carries a credential when set inline.
CREDENTIAL_HEADER = re.compile(r"(?i)authorization|api[-_]?key|token|secret|password|cookie")
CREDENTIAL_PARAM = re.compile(r"(?i)(pass(word|wd|phrase)?|pwd|secret|client[-_]?secret|token|access[-_]?token|"
                              r"auth[-_]?token|api[-_]?key|apikey|key|private[-_]?key|auth|credentials?|"
                              r"sig(nature)?|session(id)?|bearer)")
# A field name is repeated in a diagnostic only when it has a config key's shape; any
# other key (a pasted line used as a key) is named by its position in the entry.
FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.#-]{0,80}")



import shown as display   # the shared display rule (scripts/verify/shown.py)
tokenish = display.tokenish


def shown_field(field, entry=None):
    if FIELD_NAME.fullmatch(field or ""):
        # a path component of a credential's shape (a header NAMED after a token) is
        # replaced, whatever grammar it passes (round 24)
        return ".".join("(a component of a credential's shape)" if tokenish(c) else c for c in field.split("."))
    if isinstance(entry, dict) and field in entry:
        return "#%d (a key not of a config-key shape)" % list(entry).index(field)
    return "(a key not of a config-key shape)"

# A URL is only ever clean when the credential travels beside it (a
# `credentials_file`) rather than inside it.
REDACTED = "<secret>"
# shown only when it reads as a NAME: a letter first and at most 24 characters — a pasted
# token (40 characters of ghp_…) never fits, so it is "#i" instead (round 23)
RECEIVER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,23}")
USERINFO = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]+@")
TELEGRAM_BOT_PATH = re.compile(r"/bot\d+:[^/\s?#]+")
TOKEN_RUN = re.compile(r"[A-Za-z0-9_.~+:\-]{24,}")


def url_embeds_credential(url):
    """Does this URL carry what looks like a credential?

    Only the path, query and fragment are read: the authority is a hostname,
    and matching there would turn a long subdomain into a finding. A clean
    hook URL (`http://orbit-intake.orbit:8080/alertmanager`) has no userinfo
    and only short path segments.
    """
    if not isinstance(url, str) or not url:
        return False
    if USERINFO.search(url):
        return True
    if url.strip() == REDACTED:
        # The running config shows a secret-typed URL as <secret> whatever it held: a
        # Slack api_url with its token inline looks exactly like a clean one, and so does
        # the intake's webhook URL (secret-typed since Alertmanager v0.33). Redaction is
        # not cleanliness; the value is proven from the GIT config (unredacted) at the
        # same receiver, block, index and field — see --desired — never from here.
        return True
    # Read as a URL, so a query or fragment that follows the authority with no slash in
    # between (`https://hooks.example?token=...`) is inspected like any other.
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return True          # unreadable as a URL: never certify it clean
    if parts.username is not None or parts.password is not None:
        return True
    if TELEGRAM_BOT_PATH.search(parts.path or "/"):
        return True
    # a parameter NAMED for a credential carries one whatever its length (`?password=pw`)
    for piece in (parts.query, parts.fragment):
        if piece and any(CREDENTIAL_PARAM.fullmatch(k.strip())
                         for k, _ in urllib.parse.parse_qsl(urllib.parse.unquote(piece), keep_blank_values=True)):
            return True
    pieces = [p for p in (parts.path, parts.query, parts.fragment) if p]
    # as written AND decoded: a percent-encoded credential is the same credential
    return any(TOKEN_RUN.search(p) for piece in pieces for p in (piece, urllib.parse.unquote(piece)))


def is_secret_field(name):
    return name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES)


def walk(node, report, parent=None):
    """Report every inline credential under this mapping. `report` takes the
    field name; values never reach it, so they can never reach stdout."""
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                continue
            if key.endswith("_file"):
                continue  # the sanctioned spelling: the secret travels by path
            if key == "key" and parent == "tls_config":
                # a TLS private key inline (`key:`, or its <secret> marker) is a credential;
                # `key_file` is the sanctioned spelling and was skipped above
                if value is not None:
                    report("tls_config.key")
                continue
            if key == "url" or key.endswith("_url"):
                if url_embeds_credential(value):
                    report(key)
                continue
            if is_secret_field(key):
                report(key)
                continue
            if key == "http_headers":
                # Alertmanager's per-header form: {<Header>: {values: [...], secrets: [...],
                # files: [...]}}. `secrets` is a credential inline by definition (the file
                # form is `files`); a header whose NAME is a credential carrier
                # (Authorization, X-Api-Key, ...) with inline `values` is the same thing.
                if isinstance(value, dict):
                    for hi, (hname, hval) in enumerate(value.items()):
                        shown_h = hname if isinstance(hname, str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", hname) else "#%d" % hi
                        if isinstance(hval, dict):
                            if hval.get("secrets"):
                                report("http_headers.%s.secrets" % shown_h)
                            if hval.get("values") and isinstance(hname, str) and CREDENTIAL_HEADER.search(hname):
                                report("http_headers.%s.values" % shown_h)
                        elif hval is not None:
                            report("http_headers.%s" % shown_h)
                else:
                    if value is not None:
                        report(key)
                continue
            if key in CONTAINER_FIELDS and not isinstance(value, (dict, list)):
                # A mapping belongs here (`http_config:` with an
                # `authorization:` under it); a scalar here is inline by
                # definition, whatever string it holds.
                if value is not None:
                    report(key)
                continue
            walk(value, report, key)
    elif isinstance(node, list):
        for item in node:
            walk(item, report, parent)


class StrictLoader(yaml.SafeLoader):
    """Refuses a repeated key: a mapping with two `url` keys reads back as the LAST one
    while the first (a credential-bearing URL) still stands in the file."""
    def construct_mapping(self, node, deep=False):
        seen = set()
        for k, _ in node.value:
            key = self.construct_object(k, deep=True)
            try:
                dup = key in seen
            except TypeError:
                raise yaml.constructor.ConstructorError(None, None, "unhashable key", k.start_mark)
            if dup:
                raise yaml.constructor.ConstructorError(None, None, "repeated key", k.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep)


def strict_load(text):
    return yaml.load(text, Loader=StrictLoader)


def load_config(path, what):
    try:
        config = strict_load(open(path, encoding="utf-8").read())
    except Exception as exc:                                  # noqa: BLE001
        abort(f"{what} {path} does not parse ({type(exc).__name__}; a repeated key counts), so "
              f"its receivers cannot be proven clean")
    if not isinstance(config, dict):
        abort(f"{what} {path} is not a mapping, so its receivers cannot be proven clean")
    return config


def desired_url(desired, name, block, i, field):
    """The git config's value for this receiver/block/index/field, or None."""
    for r in desired.get("receivers") or []:
        if isinstance(r, dict) and r.get("name") == name:
            entries = r.get(block)
            if isinstance(entries, list) and i < len(entries) and isinstance(entries[i], dict):
                return entries[i].get(field)
    return None


if __name__ == "__main__":
    argv = sys.argv[1:]
    desired = None
    if "--desired" in argv:
        # The GIT config (an Argo Application carrying the chart values), unredacted: a
        # <secret> URL in the running config is clean iff git's URL at the same place is.
        dpath = argv[argv.index("--desired") + 1]; del argv[argv.index("--desired"):argv.index("--desired") + 2]
        app = load_config(dpath, "the git Alertmanager Application")
        try:
            helm = app["spec"]["source"]["helm"]
            vals = helm.get("valuesObject") or strict_load(re.sub(r"\{\{[^{}]*\}\}", "PLACEHOLDER", helm["values"]))
            desired = vals["alertmanager"]["config"]
        except Exception as exc:                              # noqa: BLE001
            abort(f"the git Alertmanager Application {dpath} does not carry alertmanager.config "
                  f"({type(exc).__name__}; a repeated key counts)")
    if len(argv) != 1:
        sys.exit(__doc__)
    config = load_config(argv[0], "the Alertmanager config")
    receivers = config.get("receivers")
    if not isinstance(receivers, list) or not receivers:
        abort("the Alertmanager config names no receivers, so there is "
              "nothing to prove clean")
    checked = 0
    # Receivers inherit the global block (smtp_auth_password, slack_api_url, ...): a
    # credential inlined THERE is inlined for every receiver that inherits it.
    global_findings = []
    walk(config.get("global") or {}, global_findings.append)
    for field in global_findings:
        check(f"the global block carries an inlined credential in field '{shown_field(field, config.get('global'))}', "
              f"inherited by every receiver — read it from a file instead", False)
    for receiver in receivers:
        if not isinstance(receiver, dict):
            abort("an Alertmanager receiver is not a mapping, so it cannot "
                  "be proven clean")
        rname = receiver.get("name")             # identity: what git's receiver is matched by
        name = rname
        # SHOWN only when the name has the shape of one; anything else is "receiver #i"
        if not isinstance(name, str) or not RECEIVER_NAME.fullmatch(name) or tokenish(name):
            name = "#%d" % receivers.index(receiver)        # tokenish: a name of a credential's shape (round 25)
        for block, entries in receiver.items():
            if not isinstance(block, str) or not block.endswith("_configs"):
                continue
            if not isinstance(entries, list):
                abort(f"receiver '{name}' field '{shown_field(block, receiver)}' is not a list, so "
                      f"it cannot be proven clean")
            # the block name is config text: shown by shape (or position) in every row below
            bshown = shown_field(block, receiver)
            for i, entry in enumerate(entries):
                findings = []
                walk(entry, findings.append)
                for field in findings:
                    if desired is not None and isinstance(entry, dict) and entry.get(field) == REDACTED \
                            and (field == "url" or field.endswith("_url")):
                        want = desired_url(desired, rname, block, i, field)
                        fshown = shown_field(field, entry)
                        if isinstance(want, str) and want != REDACTED and not url_embeds_credential(want):
                            check(f"receiver '{name}' {bshown}[{i}] field '{fshown}' is redacted in the "
                                  f"running config and clean in git at the same place (equality with the "
                                  f"running value is the deployer's sync status to prove, not this checker's)", True)
                            continue
                        check(f"receiver '{name}' {bshown}[{i}] field '{fshown}' is redacted in the "
                              f"running config and git carries no clean URL at the same place — "
                              f"an inline secret-typed URL that cannot be proven clean", False)
                        continue
                    check(f"receiver '{name}' {bshown}[{i}] carries an inlined "
                          f"credential in field '{shown_field(field, entry)}' — read it from a "
                          f"file instead", False)
                checked += 1
    check(f"no receiver carries a credential inline ({checked} config "
          f"entries across {len(receivers)} receivers checked)", True)
    finish()
