#!/usr/bin/env bash
# Stage 4 — secrets: the sops plane. Assumes stage3.sh passed.
#
# Every Secret a pod can reference comes from a SopsSecret file the desk applies
# from clusters/platform/secrets/<namespace>/<name>.yaml, and the sops operator
# decrypts each one into a plain Secret. Git holds the sealed files, never values.
set -uo pipefail
cd "$(dirname "$0")/../.."
# the python blocks below import the shared display rule (scripts/verify/shown.py)
export PYTHONPATH="$(pwd)/scripts/verify${PYTHONPATH:+:$PYTHONPATH}"
. "$(dirname "$0")/../lib-instance.sh" || exit 2
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config-${ORBIT_INSTANCE_NAME}-platform}"
PASS=0; FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
# THREE THINGS DECIDE WHAT THIS STAGE ASSERTS, and each is read once, here.
#
#   PRE-BOOT — no kubeconfig FILE (the stage3 convention, and stage5-platform's): the org
#     was scaffolded and never bootstrapped, so every LIVE row keys off with a visible
#     line. Keyed on the file, never on reachability, so a bootstrapped instance whose
#     cluster is down still fails loudly and deleting the kubeconfig to quieten the suite
#     is self-announcing.
PREBOOT=0
# KUBECONFIG is a LIST (colon-separated) the way kubectl reads it: pre-boot means no
# entry is a readable non-empty file. An entry that exists but is empty or unreadable is
# neither a cluster nor pre-boot, and the stage refuses rather than guess.
kubeconfig_present(){ local f any=1; set -f; IFS=: ; for f in $1; do IFS=$' \t\n'   # set -f: the list is split, never glob-expanded
    if [ -s "$f" ] && [ -r "$f" ]; then any=0
    elif [ -e "$f" ]; then echo "kubeconfig $f exists but is empty or unreadable — neither a cluster nor pre-boot; fix or remove it" >&2; exit 2; fi
  done; IFS=$' \t\n'; set +f; return $any; }
kubeconfig_present "$KUBECONFIG" || PREBOOT=1
ICLUSTER=0
if [ "$PREBOOT" != 1 ]; then
  # Bounded and non-interactive for the reasons stage5-platform gives at the same probe:
  # a black-holed endpoint must not hang the suite, and a kubeconfig with no usable
  # credential must not prompt on stdin in an unattended run.
  kubectl --request-timeout=20s get --raw /version >/dev/null 2>&1 </dev/null && ICLUSTER=1
  if [ "$ICLUSTER" = 1 ]; then
    ok "the cluster answers — every live row below is a real answer, not a failed question"
  else
    bad "a kubeconfig exists at $KUBECONFIG but the cluster did not answer (/version is unreadable) — THIS is the row that says so, once and loudly; every live row below keys off on it, because an unreachable API server and a missing component are indistinguishable from out here"
  fi
fi
LIVEOFF=0
[ "$PREBOOT" = 1 ] && LIVEOFF=1
[ "$PREBOOT" != 1 ] && [ "$ICLUSTER" != 1 ] && LIVEOFF=1
keyoff_live(){
  if [ "$PREBOOT" = 1 ]; then
    ok "PRE-BOOT: $1 — they arm from the kubeconfig FILE, which does not exist yet"
  else
    ok "UNREACHABLE: $1 — the kubeconfig exists and the cluster did not answer, so this is keyed off, NOT passing; the single reachability FAIL above is the whole verdict"
  fi
}
if [ "$PREBOOT" = 1 ]; then
  echo "  note  PRE-BOOT: no kubeconfig at $KUBECONFIG — every live row below keys off visibly; bootstrap arms them"
fi
echo "  note  the secret plane is sops: sealed SopsSecret files under clusters/platform/secrets/, decrypted in place by the sops operator"

SC=$(mktemp -d); trap 'rm -rf "$SC"' EXIT

echo "== git: every file under clusters/platform/secrets/ is one sealed SopsSecret =="
# One sealed file per Secret, sealed with the org's age key: a sops metadata block is
# present, every value under data:/stringData: is sops ciphertext (never plaintext),
# exactly one SopsSecret document per file, and the sealed namespace is the directory
# the file lives in. Key NAMES are reported; values never are.
SOPSCHECK=$(python3 - . <<'PY'
import os, re, stat, subprocess, sys, yaml
import shown as display   # the shared display rule (scripts/verify/shown.py)

K8S_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
# a namespace is a DNS LABEL (no dots), not a subdomain: the API server refuses `team.prod`
NS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
class StrictLoader(yaml.SafeLoader):
    """A mapping that repeats a key is refused: a YAML reader keeps the LAST value, so a
    plaintext first occurrence would hide behind a sealed second one."""
    def construct_mapping(self, node, deep=False):
        seen = set()
        for k_node, _ in node.value:
            k = self.construct_object(k_node, deep=True)
            try:
                dup = k in seen
            except TypeError:
                raise yaml.constructor.ConstructorError(None, None, "an unhashable mapping key", k_node.start_mark)
            if dup:
                raise yaml.constructor.ConstructorError(None, None, "a duplicate mapping key", k_node.start_mark)
            seen.add(k)
        return super().construct_mapping(node, deep)

def files():
    proc = subprocess.run(["git", "ls-files", "--", "clusters/platform/secrets"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return [l for l in proc.stdout.splitlines() if l.strip()]

AGE_RECIPIENT = re.compile(r"age1[a-z0-9]{20,}")
AGE_ARMOR = re.compile(r"-----BEGIN AGE ENCRYPTED FILE-----\n([A-Za-z0-9+/=]+\n)+-----END AGE ENCRYPTED FILE-----")
ENC = re.compile(r"ENC\[AES256_GCM,data:[A-Za-z0-9+/=_.\-]+,iv:[A-Za-z0-9+/=_.\-]+,tag:[A-Za-z0-9+/=_.\-]+,type:(str|int|float|bool|comment)\]")
bad = []
count = 0
targets = []
rendered = []
rel_list = files()
if rel_list is None:
    print("FAIL git ls-files could not list clusters/platform/secrets — the sealed tree is UNCHECKED, not proven sealed")
    sys.exit(1)
# the desk applies what is ON DISK: an untracked file under the sealed tree is an input
# every row here would miss, so it is red until it is added (and checked) or removed.
# IGNORED files included (no --exclude-standard): a .gitignore or info/exclude entry
# hides a file from git, not from the renderer.
unt = subprocess.run(["git", "ls-files", "--others", "--", "clusters/platform/secrets"],
                     capture_output=True, text=True)
if unt.returncode != 0:
    print("FAIL git could not list untracked files under clusters/platform/secrets — the sealed tree is UNCHECKED")
    sys.exit(1)
for ui, rel in enumerate(sorted(l for l in unt.stdout.splitlines() if l.strip())):
    # a file NAME is the filesystem's text: shown only when every component is path-shaped
    shown = rel if all(display.fits(p, "file-name") for p in rel.split("/")) else \
        "untracked file #%d under clusters/platform/secrets (a name not of a path shape, or of a credential's shape; not repeated)" % ui
    bad.append("%s is UNTRACKED under the sealed tree (ignored files count) — an input the renderer reads and no row here checks; add it (and it is checked) or remove it" % shown)
if not rel_list:
    print("FAIL no SopsSecret files under clusters/platform/secrets — an org with no sealed tree has no secret plane at all, and a missing directory reads the same as an empty one")
    sys.exit(1)
# The placeholder init writes so git keeps the (otherwise empty) sealed directory:
# not a sealed file, and a fresh org ships only it. Anything else that is not a
# sealed SopsSecret is still red below — this skips the placeholder, never the check.
# ...and ONLY an empty regular file earns the exemption: a .gitkeep with bytes in it is
# a file that was never checked, whatever its name (its content is not read)
for pi, rel in enumerate(r for r in rel_list if r.split("/")[-1] == ".gitkeep"):
    # the path is git's text: shown only when every component is path-shaped
    if not all(display.fits(p, "file-name") or p == ".gitkeep" for p in rel.split("/")):
        bad.append("placeholder #%d under clusters/platform/secrets has a path not of a path shape, or of a credential's shape (not repeated) — rename its directory" % pi)
        continue
    try:
        st = os.lstat(rel)
        odd = not stat.S_ISREG(st.st_mode) or st.st_size != 0
    except OSError:
        odd = True
    if odd:
        bad.append("%s is a placeholder that is not an empty regular file — a .gitkeep carries nothing; anything else here is a sealed file to check, under a real name" % rel)
rel_list = [rel for rel in rel_list if rel.split("/")[-1] != ".gitkeep"]
identity = {}
rendered_by = {}
def check_one(rel, ti):
    # a tracked file's NAME is git's text: it reaches a row only when every path
    # component is path-shaped, else the file is named by position and skipped
    if not all(display.fits(p, "file-name") for p in rel.split("/")):
        bad.append("tracked file #%d under clusters/platform/secrets has a name not of a path shape, or of a credential's shape (not repeated) — rename it" % ti)
        return
    # a symlink is not a sealed file: it points anywhere (outside the tree included), and
    # what it points at is not what git tracks — refused before anything is read
    try:
        st = os.lstat(rel)
    except OSError as exc:
        bad.append("%s is tracked but unreadable (%s)" % (rel, type(exc).__name__))
        return
    if not stat.S_ISREG(st.st_mode):
        bad.append("%s is not a regular file (a symlink, or a special file) — a sealed file is a regular file in this tree, nothing else" % rel)
        return
    try:
        text = open(rel, encoding="utf-8").read()
    except (OSError, UnicodeDecodeError) as exc:
        bad.append("%s is tracked but unreadable (%s)" % (rel, type(exc).__name__))
        return
    _check_doc(rel, text)

def _check_doc(rel, text):
    global count
    try:
        docs = list(yaml.load_all(text, Loader=StrictLoader))
    except Exception:
        bad.append("%s does not parse as YAML, or repeats a key (a duplicate key hides a plaintext value behind a sealed one)" % rel)
        return
    if len(docs) != 1 or not isinstance(docs[0], dict):
        bad.append("%s holds %d documents — one SopsSecret per file" % (rel, len(docs)))
        return
    doc = docs[0]
    # the operator watches ONE group/version: a SopsSecret document under another
    # apiVersion is nobody's, renders nothing, and must not become a live target (round 21)
    if doc.get("kind") != "SopsSecret" or doc.get("apiVersion") != "isindir.github.com/v1alpha3":
        bad.append("%s is not a SopsSecret of the operator's API version (isindir.github.com/v1alpha3; its kind or apiVersion is something else) — one SopsSecret per file" % rel)
        return
    meta = doc.get("metadata") or {}
    ns, name = meta.get("namespace") or "", meta.get("name") or ""
    parts = rel.split("/")
    want_ns = parts[3] if len(parts) >= 5 else ""
    if not ns or not name:
        bad.append("%s seals a Secret with no namespace/name — the live rows cannot address it" % rel)
        return
    # Names reach kubectl as arguments: only a Kubernetes object name (DNS-1123
    # subdomain) does, so a value shaped like an option can never be read as one.
    if not isinstance(ns, str) or not NS_LABEL.fullmatch(ns):
        bad.append("%s seals a namespace that is not a Kubernetes namespace name (a DNS label: lowercase, digits, dashes, no dots, 63 at most)" % rel)
        return
    if not isinstance(name, str) or not K8S_NAME.fullmatch(name) or len(name) > 253:
        bad.append("%s seals a Secret whose name is not a Kubernetes object name" % rel)
        return
    if ns != want_ns:
        bad.append("%s seals namespace %s but lives under %s/ — the sealed namespace is the directory" % (rel, ns, want_ns))
        return
    # ONE file per sealed identity: two files sealing namespace/name are two producers of
    # one Secret, and whichever the operator applied last is the one a pod reads — the
    # live rows would certify the entry they happened to find (round 17)
    if (ns, name) in identity:
        bad.append("%s seals %s/%s, which %s already seals — one sealed file per Secret" % (rel, ns, name, identity[(ns, name)]))
        return
    identity[(ns, name)] = rel
    # The sops metadata must have the shape sops writes: an age list, each entry a
    # recipient (an age public key) and its encrypted file key, which sops stores as an
    # ARMORED age file (`-----BEGIN AGE ENCRYPTED FILE-----` ... `-----END ...-----`),
    # not as an ENC[...] envelope — those wrap the document's values. A truthy value of
    # any other shape is hand-written, not sealed. (Round 3, 2026-09-13: a check that
    # demanded ENC[AGE1 here failed every file the real sealer writes.)
    sops = doc.get("sops")
    age = sops.get("age") if isinstance(sops, dict) else None
    if not isinstance(age, list) or not age or not all(
            isinstance(a, dict) and isinstance(a.get("recipient"), str) and AGE_RECIPIENT.fullmatch(a["recipient"])
            and isinstance(a.get("enc"), str) and AGE_ARMOR.fullmatch(a["enc"].strip()) for a in age):
        bad.append("%s carries no sops age metadata block of the shape sops writes (a list of recipient + enc) — committed plaintext or hand-written, not sealed" % rel)
        return
    # Every value sealed under data:/stringData:, anywhere in the document, must be
    # sops ciphertext. Anything else is a plaintext value in git.
    plain = []
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("data", "stringData"):
                    # a data: that is not a map of key -> ciphertext is a plaintext value
                    # (or a shape sops never writes) whatever it holds
                    if not isinstance(value, dict):
                        plain.append("%s(not a map)" % key)
                        continue
                    for i, (k, v) in enumerate(value.items()):
                        if not isinstance(v, str) or not ENC.fullmatch(v):
                            # a key is repeated only when it has a Secret key's shape;
                            # anything else is named by position (a "key" may hold any text)
                            plain.append(k if display.fits(k, "key")
                                         else "%s key #%d (its name is not of a Secret key's shape)" % (key, i))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(doc)
    if plain:
        bad.append("%s holds plaintext under data:/stringData: for key(s) %s — seal every value, commit no plaintext" % (rel, sorted(set(plain))))
        return
    # The Secrets the operator actually renders: one per entry in
    # spec.secretTemplates, named by the template — never the file's own name.
    templates = ((doc.get("spec") or {}).get("secretTemplates") or [])
    if not templates:
        bad.append("%s declares no secretTemplates — it renders no Secret, and a file that renders nothing certifies an empty set" % rel)
        return
    tnames = [t.get("name") if isinstance(t, dict) else "" for t in templates]
    if any(not n for n in tnames):
        bad.append("%s carries a secretTemplate with no name — the live rows cannot address it" % rel)
        return
    odd = [i for i, n in enumerate(tnames, 1) if not isinstance(n, str) or not K8S_NAME.fullmatch(n) or len(n) > 253]
    if odd:
        bad.append("%s: secretTemplate %s has a name that is not a Kubernetes object name" % (rel, odd[0]))
        return
    # ONE producer per rendered Secret: two templates (in one file or two) naming the same
    # Secret are two producers, and the operator applies whichever came last — never a
    # union of their keys (round 18). After the shape check above: a name a row repeats
    # has been validated first (round 19)
    for tname in tnames:
        if (ns, tname) in rendered_by:
            bad.append("%s renders Secret %s/%s, which %s already renders — one producer per rendered Secret" % (rel, ns, tname, rendered_by[(ns, tname)]))
            return
        if tnames.count(tname) > 1:
            bad.append("%s renders Secret %s/%s twice — one producer per rendered Secret" % (rel, ns, tname))
            return
    for tname in tnames:
        rendered_by[(ns, tname)] = rel
    count += 1
    targets.append("%s %s" % (ns, name))
    for tname in tnames:
        rendered.append("%s %s" % (ns, tname))
for ti, rel in enumerate(sorted(rel_list)):
    try:
        check_one(rel, ti)
    except Exception as exc:          # a scalar where a map was expected, a recursive document
        shown = rel if all(display.fits(p, "file-name") for p in rel.split("/")) else \
            "tracked file #%d under clusters/platform/secrets (a name not of a path shape, or of a credential's shape; not repeated)" % ti
        bad.append("%s cannot be checked (%s) — malformed, and refused rather than skipped" % (shown, type(exc).__name__))

if bad:
    print("FAIL " + "; ".join(bad[:8]))
    sys.exit(1)
if count == 0:
    # a tree with a placeholder and nothing sealed: nothing was checked. Before bootstrap
    # that is the expected state of a fresh instance (the kubeconfig does not exist yet,
    # and the ceremony seals the first file); once a cluster exists, an instance always
    # seals at least its cert-manager token, so "0 files, all sealed" is not green.
    print("EMPTY the sealed plane holds no SopsSecret file at all (only placeholders) — nothing is sealed, nothing was checked")
    sys.exit(0)
print("OK %d sealed SopsSecret file(s), every one age-sealed with no plaintext and namespaced as its directory" % count)
print("TARGETS")
for t in targets:
    print(t)
print("RENDERED")
for t in rendered:
    print(t)
PY
)
SOPSMSG=$(printf '%s\n' "$SOPSCHECK" | head -1)
printf '%s\n' "$SOPSCHECK" | awk '/^TARGETS$/{s=1;next}/^RENDERED$/{s=0} s' > "$SC/targets.txt"
printf '%s\n' "$SOPSCHECK" | sed -n '/^RENDERED$/,$p' | tail -n +2 > "$SC/rendered.txt"
case "$SOPSCHECK" in
  OK*)    ok "${SOPSMSG#OK }" ;;
  EMPTY*) if [ "$PREBOOT" = 1 ]; then ok "[UNVERIFIED] PRE-BOOT: ${SOPSMSG#EMPTY } — expected before the first seal; red once a cluster exists";
          else bad "sealed tree: ${SOPSMSG#EMPTY }"; fi ;;
  *)      bad "sealed tree: ${SOPSMSG#FAIL }" ;;
esac
# .env files are the failure mode sealed Secrets replace; there must be none, anywhere.
# The listing lands in a private file and is read WHOLE: `grep -q` closes its input at
# the first match, and a git that dies on a closed pipe (or fails outright) left a late
# .env unseen — a listing that could not be taken is UNCHECKED, never clean (round 21).
# NUL-delimited (-z): git QUOTES a name with a byte outside ASCII ("caf\303\251/.env"),
# and a quoted name's suffix is not its suffix — the base name is judged on the bytes
# git holds (round 22)
LSF=$(mktemp); ENVN=unreadable
if git ls-files -z > "$LSF" 2>/dev/null; then
  ENVN=$(python3 -c 'import re, sys; print(sum(1 for n in open(sys.argv[1], "rb").read().split(b"\0") if n and re.fullmatch(rb"\.env(\..*)?", n.rsplit(b"/", 1)[-1])))' "$LSF" 2>/dev/null || echo unreadable)
fi
rm -f "$LSF"
case "$ENVN" in
  0) ok "no .env file is committed anywhere in the repo";;
  ''|*[!0-9]*) bad "the committed file list could not be read (git ls-files failed) — the .env check is UNCHECKED, not clean";;
  *) bad "a .env file is committed";;
esac
# No recovery backup is sanctioned: the one sanctioned file was the retired identity
# provider's masterkey backup, and it left with the provider. Any file still under
# recovery/ is unsanctioned, so the red line names every file it saw — that list is
# what the operator deletes. An if/else, not a `&& ... || ...` chain: the row's sense
# (files are red, empty is green) must read off the structure, not off grep's exit code.
# the listing is git's text: a private file, and a name is repeated only when every
# component is path-shaped, the rest counted
# a listing git could not take is UNCHECKED, never an empty directory (round 22); -z:
# git quotes a name with a byte outside ASCII, and a quoted name is not the name
RECF=$(mktemp); REC_ROW=UNREADABLE
if git ls-files -z 'recovery/*' > "$RECF" 2>/dev/null; then
REC_ROW=$(python3 - "$RECF" <<'PY'
import re, sys
import shown as display   # the shared display rule (scripts/verify/shown.py)
names = [n.decode("utf-8", "replace") for n in open(sys.argv[1], "rb").read().split(b"\0") if n]
good = [n for n in names if all(display.fits(p, "file-name") for p in n.split("/"))]
print("%d %s%s" % (len(names), " ".join(good), " (+%d name(s) not of a path shape, or of a credential's shape)" % (len(names) - len(good)) if len(names) != len(good) else ""))
PY
)
fi; rm -f "$RECF"
case "$REC_ROW" in ''|UNREADABLE) REC_ROW=UNREADABLE;; esac
if [ "$REC_ROW" = UNREADABLE ]; then
  bad "the recovery/ listing could not be read (git ls-files failed) — the recovery-backup row is UNCHECKED, not clean"
elif [ "${REC_ROW%% *}" = 0 ]; then
  ok "recovery/ holds no files — no recovery backup is sanctioned"
else
  bad "recovery/ holds ${REC_ROW%% *} unsanctioned file(s) — delete them, no recovery backup is sanctioned: ${REC_ROW#* }"
fi

echo "== live: the sops operator is running =="
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the operator, sealed-file and secret-existence rows key off"
else
  # The operator is the only workload in ns sops: one ready Deployment there is the
  # operator being available, whatever the chart names it.
  # the listing goes to a private file; kubectl's own failure is UNCHECKED, not "no
  # Deployment"; a ready count is a whole integer, not a line that starts with one (round 25)
  RDYF=$(mktemp)
  if kubectl get deploy -n sops -o jsonpath='{range .items[*]}{.status.readyReplicas}{"\n"}{end}' > "$RDYF" 2>/dev/null; then
    RDY=$(python3 -c 'import re, sys
print(sum(1 for l in open(sys.argv[1], errors="replace") if re.fullmatch(r"[0-9]{1,9}", l.rstrip("\n")) and int(l) >= 1))' "$RDYF" 2>/dev/null || echo unreadable)
  else
    RDY=unreadable
  fi
  rm -f "$RDYF"
  if [ "$RDY" = unreadable ]; then
    bad "the Deployment listing in ns sops could not be read (kubectl failed; its message is not repeated) — the operator row is UNCHECKED, not clean"
  elif [ "$RDY" -ge 1 ]; then
    ok "the sops operator is available — ns sops has a ready Deployment doing the decrypting"
  else
    bad "no ready Deployment in ns sops — sealed files are not being decrypted into Secrets"
  fi
fi

echo "== live: every sealed file exists live and the sync reports it healthy =="
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the sealed-file existence and sync-health rows key off"
else
  MISS=""
  SEEN=0
  while read -r ns name; do
    [ -n "$ns" ] && [ -n "$name" ] || continue
    SEEN=$((SEEN+1))
    kubectl get sopssecret "$name" -n "$ns" -o name >/dev/null 2>&1 \
      || MISS="$MISS $ns/$name"
  done < "$SC/targets.txt"
  if [ "$SEEN" -eq 0 ]; then
    bad "no sealed targets to check live — the git row above says why, and zero files is not zero failures"
  elif [ -z "$MISS" ]; then
    ok "all $SEEN sealed file(s) in git exist live as SopsSecrets"
  else
    bad "sealed file(s) with no live SopsSecret:$MISS — the desk has not applied them, or the sync pruned them"
  fi
  # The app that applies the sealed tree has a stored inventory, or the existence row
  # above is certifying objects nothing keeps in step with git. Existence only.
  kubectl -n orbit get configmap orbit-platform-inventory-secrets >/dev/null 2>&1 \
    && ok "the secrets app carries a stored inventory — the desk applied the sealed tree" \
    || bad "the secrets app has no stored inventory — sealed files in git are not what the cluster holds (run orbit platform apply --app secrets .)"
fi

echo "== live: every sealed file decrypts =="
# A file that stops decrypting after a rotation leaves the old Secret in place and the
# sync Healthy — the existence rows cannot see it. The SopsSecret's own .status.message
# is the operator's decryption report, not Secret data, so reading it is
# existence-grade. One kubectl call per file, reading .status only.
# Allowlist, not denylist: the operator (sops-secrets-operator,
# internal/controllers/sopssecret_controller.go) writes exactly one string for health,
# STATUS_HEALTHY = "Healthy", and a distinct message for every other state (decryption,
# child-secret, ownership and suspend messages, Unknown Error). Only "Healthy" is green.
# Two red buckets, because an unread status is not a failed decryption: a non-Healthy
# message names a decryption failure, while an empty status or a failed read names the
# file whose status could not be read (the operator has not reported yet, the field is
# absent, or the call returned nothing). The next state the operator invents fails
# closed instead of passing green.
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the sealed-file decryption rows key off"
else
  DFAIL=""
  DNOSTAT=""
  while read -r ns name; do
    [ -n "$ns" ] && [ -n "$name" ] || continue
    # The status message is the operator's text (a decryption error quotes what it
    # read): it goes to a scratch file and only a fixed CATEGORY reaches the shell, so
    # a traced run (bash -x) prints the category, never the message.
    if kubectl get sopssecret "$name" -n "$ns" -o jsonpath='{.status.message}' > "$SC/stat.raw" 2>/dev/null; then
      case "$(python3 -c 'import sys; s=open(sys.argv[1]).read().strip(); print("NOSTAT" if not s else "HEALTHY" if s == "Healthy" else "UNHEALTHY")' "$SC/stat.raw" 2>/dev/null)" in
        HEALTHY)   ;;
        UNHEALTHY) DFAIL="$DFAIL $ns/$name";;
        *)         DNOSTAT="$DNOSTAT $ns/$name";;
      esac
    else
      DNOSTAT="$DNOSTAT $ns/$name"
    fi
  done < "$SC/targets.txt"
  if [ -n "$DFAIL" ]; then
    bad "sealed file(s) the operator does not report Healthy:$DFAIL — the live Secrets may be stale copies from before a decryption failure"
  fi
  if [ -n "$DNOSTAT" ]; then
    bad "sealed file(s) whose decryption status could not be read:$DNOSTAT — the operator has not reported a status for them, or the read failed; an unread status is not a healthy decryption"
  fi
  if [ -z "$DFAIL$DNOSTAT" ]; then
    ok "every sealed file reports the operator's healthy decryption status"
  fi
fi

echo "== live: every Secret a sealed file names exists =="
# Existence only: `kubectl get secret NAME -n NS -o name` — never a rendering of
# the object and never a .data read — a value on this stage's stdout is a value
# in CI logs.
# The names are the operator's: one Secret per entry in spec.secretTemplates, named by
# the template — never the sealed file's own metadata.name.
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the named-Secret existence rows key off"
else
  SMISS=""
  SSEEN=0
  while read -r ns name; do
    [ -n "$ns" ] && [ -n "$name" ] || continue
    SSEEN=$((SSEEN+1))
    kubectl get secret "$name" -n "$ns" -o name >/dev/null 2>&1 \
      || SMISS="$SMISS $ns/$name"
  done < "$SC/rendered.txt"
  if [ "$SSEEN" -eq 0 ]; then
    bad "no sealed targets to check live — the git row above says why, and zero files is not zero failures"
  elif [ -z "$SMISS" ]; then
    ok "all $SSEEN Secret(s) the sealed files name exist"
  else
    bad "sealed Secret(s) with no live Secret:$SMISS — the operator has not rendered them"
  fi
fi

echo "== live: the retired plane's APIs are gone =="
# The retired plane's API group, named in halves so this absence check does not itself
# name that plane on a live line: the group is assembled below, and its kinds are not
# named anywhere in this file. ESO is deleted, so nothing may serve them.
old_group="external-"
old_group="${old_group}secrets.io"
if [ "$LIVEOFF" = 1 ]; then
  keyoff_live "the retired-API absence row keys off"
else
  # discovery output is the cluster's text: it lands in a private file (never a shell
  # variable a traced shell prints), and only resource NAMES of the shape the API server
  # emits (lowercase dns-ish labels joined by dots) come back, the rest counted
  DISCF=$(mktemp)
  kubectl api-resources --api-group="$old_group" -o name > "$DISCF" 2>/dev/null; DISCRC=$?
  SERVED=$(python3 -c '
import re, sys
import shown as display   # the shared display rule (scripts/verify/shown.py)
names = [l.strip() for l in open(sys.argv[1], errors="replace") if l.strip()]
good, note = display.shown_list(names, "object-name", "resource name")
print(" ".join(good + ([note] if note else [])))' "$DISCF" 2>/dev/null)
  rm -f "$DISCF"
  if [ "$DISCRC" -ne 0 ]; then
    bad "could not ask whether the retired plane's API group is served (discovery exited $DISCRC) — UNANSWERED, not absent; a failed question is red, never a quiet pass"
  elif [ -z "$SERVED" ]; then
    ok "the retired plane's API group is absent from the cluster — nothing serves it"
  else
    bad "the retired plane's API group is still served:$SERVED — the old operator is not deleted"
  fi
fi

echo
echo "stage4: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
