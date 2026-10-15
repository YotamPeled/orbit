#!/usr/bin/env bash
# Stage 7 — the operating scenarios' deterministic guarantees, re-proven live on every
# verify (docs/V1.md §1.3: every passing scenario is a regression stage, or an offline
# test in CI). The scenarios themselves need an agent and run as a suite
# (scenarios/run-all.sh); what they proved about the PLATFORM is checked here, with no
# agent, in a few minutes:
#
#   * S11 guardrails: a viewer cannot ship, a ship asks for confirmation, a confirmation
#     replayed with other arguments or by another token is refused, a revoked token and an
#     expired one are refused (scripts/verify/guardrails.py, throwaway principals issued
#     through the desk's own orbit session and retired after);
#   * S12 isolation: a tenant pod reaches its own namespace but not the platform's
#     backends, not orbitd's Service and not its project's production; a component that
#     declared its egress cannot reach the internet;
#   * S13 doors: every public door redirects plain http to https.
set -uo pipefail
cd "$(dirname "$0")/../.."
export PYTHONPATH="$(pwd)/scripts/verify${PYTHONPATH:+:$PYTHONPATH}"
. "$(dirname "$0")/../lib-instance.sh" || exit 2
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config-${ORBIT_INSTANCE_NAME}-platform}"
PASS=0; FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
kubeconfig_present(){ local f any=1; set -f; IFS=: ; for f in $1; do IFS=$' \t\n'   # set -f: the list is split, never glob-expanded
    if [ -s "$f" ] && [ -r "$f" ]; then any=0
    elif [ -e "$f" ]; then echo "kubeconfig $f exists but is empty or unreadable — neither a cluster nor pre-boot; fix or remove it" >&2; exit 2; fi
  done; IFS=$' \t\n'; set +f; return $any; }
if ! kubeconfig_present "$KUBECONFIG"; then
  echo "stage7-scenarios: 0 passed, 0 failed (pre-boot — no kubeconfig at $KUBECONFIG; bootstrap arms this stage)"
  exit 0
fi
TMP=$(mktemp -d); trap 'kubectl delete pod -A -l orbit.local/verify=stage7 --wait=false >/dev/null 2>&1; rm -rf "$TMP"' EXIT
if ! kubectl --request-timeout=20s get --raw /version >/dev/null 2>&1 </dev/null; then
  bad "a kubeconfig exists at $KUBECONFIG but the cluster did not answer — every row below is UNCHECKED, not clean"
  echo; echo "stage7-scenarios: $PASS passed, $FAIL failed"; exit 1
fi

# The project, its environments and one deployed component, from git: the first tags.yaml
# key whose component has a Deployment. Shapes only: a catalog id is [a-z0-9-].
python3 - > "$TMP/target" <<'PY'
import re, yaml
try:
    tags = (yaml.safe_load(open("catalog/tags.yaml")) or {}).get("tags") or {}
except Exception:
    tags = {}
ID = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
for key, tag in tags.items():
    parts = str(key).split("/")
    if len(parts) == 3 and all(ID.fullmatch(x) for x in parts) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", str(tag)):
        print(*parts, tag)
PY
read -r P E C TAG < <(while read -r p e c t; do
  kubectl -n "$p-$e" get deploy "$p-$c" >/dev/null 2>&1 </dev/null && { echo "$p $e $c $t"; break; }
done < "$TMP/target")

echo "== the guardrails hold (S11) =="
if [ -z "${P:-}" ]; then
  ok "no deployed component in catalog/tags.yaml yet — the guardrail rows key off until one is shipped"
elif ! command -v orbit >/dev/null 2>&1; then
  bad "the \`orbit\` desk CLI is not on PATH — the guardrails CANNOT be probed (run make verify through scripts/desk.sh)"
else
  python3 scripts/verify/guardrails.py --issue "$P" "$E" "$C" "$TAG" > "$TMP/guards.log" 2>&1 </dev/null
  while IFS= read -r line; do
    case "$line" in "  ok    "*) ok "${line#  ok    }";; "  FAIL  "*) bad "${line#  FAIL  }";; esac
  done < "$TMP/guards.log"
  grep -qE '^  (ok|FAIL) ' "$TMP/guards.log" || bad "the guardrail probe produced no verdict: $(grep -v '^ *$' "$TMP/guards.log" | tail -1 | cut -c1-160)"
fi

echo "== tenants are isolated (S12) =="
# probe <namespace> <labels-json> <url> -> "<http code>:<curl exit>". The curl waits 45 s:
# this CNI adds a new pod to its rule sets on its next sync, so a pod dialling in its
# first seconds is under no rule yet (docs/V1.md §9).
probe_start(){ # <key> <namespace> <labels-json> <url>
  python3 - "$1" "$2" "$3" "$4" > "$TMP/$1.json" <<'PY'
import json, sys
key, ns, labels, url = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4]
labels = dict(labels, **{"orbit.local/verify": "stage7"})
script = 'sleep 45; curl -s -o /dev/null -w "%{http_code}" --max-time 8 -k "$0"; echo ":$?"'
print(json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "verify-" + key, "namespace": ns, "labels": labels},
  "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
    "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
    "containers": [{"name": "probe", "image": "curlimages/curl:8.10.1", "command": ["sh", "-c", script, url],
      "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "16Mi"}},
      "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}}))
PY
  kubectl apply -f "$TMP/$1.json" >/dev/null 2>&1 </dev/null
}
probe_result(){ # <key> <namespace>
  kubectl -n "$2" wait "pod/verify-$1" --for=jsonpath='{.status.phase}'=Succeeded --timeout=120s >/dev/null 2>&1 </dev/null
  kubectl -n "$2" logs "verify-$1" 2>/dev/null </dev/null | tail -1
  kubectl -n "$2" delete pod "verify-$1" --wait=false >/dev/null 2>&1 </dev/null
}
blocked(){ case "$1" in *:28|*:7) return 0;; *) return 1;; esac; }
first_svc(){ # <namespace> -> "<name>:<port>" of a Service with a ready endpoint
  kubectl -n "$1" get endpoints -o json 2>/dev/null </dev/null | python3 -c '
import json, sys
for e in json.load(sys.stdin)["items"]:
    for s in e.get("subsets") or []:
        if s.get("addresses") and s.get("ports"):
            print("%s:%s" % (e["metadata"]["name"], s["ports"][0]["port"])); sys.exit()'
}
if [ -z "${P:-}" ]; then
  ok "no tenant workload yet — the isolation rows key off until one runs"
else
  NS="$P-$E"; PROD="$P-prod"
  own=$(first_svc "$NS")
  targets=("own|$NS|{}|http://${own%%:*}:${own##*:}/" \
           "prometheus|$NS|{}|http://kube-prometheus-stack-prometheus.monitoring.svc:9090/-/ready" \
           "loki|$NS|{}|http://loki.loki.svc:3100/ready" \
           "alertmanager|$NS|{}|http://kube-prometheus-stack-alertmanager.monitoring.svc:9093/-/ready" \
           "orbitd|$NS|{}|http://orbitd.orbit.svc:80/healthz")
  if [ "$E" != prod ] && kubectl get ns "$PROD" >/dev/null 2>&1 </dev/null; then
    prodsvc=$(first_svc "$PROD"); [ -n "$prodsvc" ] && targets+=("prod|$NS|{}|http://${prodsvc%%:*}.$PROD.svc:${prodsvc##*:}/")
  fi
  narrowed=$(kubectl -n "$NS" get networkpolicy -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null </dev/null | sed -n 's/^egress-//p' | head -1)
  if [ -n "$narrowed" ]; then
    targets+=("narrowed|$NS|{\"app.kubernetes.io/name\":\"$narrowed\",\"orbit.local/egress\":\"declared\"}|https://example.com/" \
              "open|$NS|{}|https://example.com/")
  fi
  # two at a time: a tenant namespace's quota admits a couple of probe pods beside its own
  i=0
  for t in "${targets[@]}"; do
    IFS='|' read -r key ns labels url <<<"$t"
    probe_start "$key" "$ns" "$labels" "$url"
    i=$((i+1))
    if [ $((i % 2)) -eq 0 ] || [ "$i" -eq "${#targets[@]}" ]; then
      for t2 in "${targets[@]:$(( (i-1) / 2 * 2 )):2}"; do
        IFS='|' read -r k2 ns2 _ _ <<<"$t2"
        echo "$k2 $(probe_result "$k2" "$ns2")" >> "$TMP/probes"
      done
    fi
  done
  r(){ awk -v k="$1" '$1 == k {print $2}' "$TMP/probes"; }
  if [ -n "$own" ] && ! blocked "$(r own)" && [ -n "$(r own)" ]; then
    ok "control: a pod in $NS reaches its own ${own%%:*} ($(r own)) — the probes can see a connection"
    for k in prometheus loki alertmanager orbitd prod; do
      [ -z "$(r "$k")" ] && continue
      blocked "$(r "$k")" && ok "a pod in $NS cannot reach $k ($(r "$k"))" || bad "a pod in $NS reached $k ($(r "$k"))"
    done
    if [ -n "$narrowed" ]; then
      if blocked "$(r open)"; then
        bad "an unnarrowed pod in $NS could not reach the internet either ($(r open)) — the narrowing row proves nothing"
      else
        blocked "$(r narrowed)" && ok "a pod wearing $narrowed's declared egress cannot reach the internet ($(r narrowed))" \
          || bad "a pod wearing $narrowed's declared egress reached the internet ($(r narrowed))"
      fi
    else
      ok "no component in $NS declares egress — the narrowing row keys off"
    fi
  else
    bad "control: a pod in $NS could not reach its own ${own%%:*:-service} ($(r own)) — every isolation row is UNCHECKED"
  fi
fi

echo "== every public door redirects http to https (S13) =="
# the doors are Ingresses (the tenants', Grafana's, orbitd's) and IngressRoutes
kubectl get ingress -A -o json 2>/dev/null </dev/null > "$TMP/ingress.json"
kubectl get ingressroute -A -o json 2>/dev/null </dev/null > "$TMP/ingressroute.json"
python3 - "$TMP/ingress.json" "$TMP/ingressroute.json" > "$TMP/hosts" <<'PY'
import json, re, sys
hosts = set()
for path in sys.argv[1:]:
    try:
        items = json.load(open(path)).get("items", [])
    except ValueError:
        continue
    for i in items:
        spec = i.get("spec") or {}
        for rule in spec.get("rules") or []:
            if rule.get("host"):
                hosts.add(rule["host"])
        for r in spec.get("routes") or []:
            hosts.update(re.findall(r"Host\(`([^`]+)`\)", r.get("match", "")))
for h in sorted(hosts):
    if re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", h):
        print(h)
PY
if [ ! -s "$TMP/hosts" ]; then
  ok "no Ingress or IngressRoute publishes a host — the redirect rows key off"
else
  while read -r h; do
    [ -n "$h" ] || continue
    # 000 is "no answer at all" (the connection was not made): asked again, twice, before it
    # is a finding. One dropped connection on the operator machine's own link turned a
    # healthy box red here (docs/V1.md §8). An ANSWER that is not the redirect is judged at
    # once and never asked again.
    for _try in 1 2 3; do
      got=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 15 "http://$h/" </dev/null)
      case "$got" in 000*) [ "$_try" = 3 ] || sleep "${ORBIT_VERIFY_RETRY_S:-5}";; *) break;; esac
    done
    case "$got" in 30[178]\ https://"$h"*) ok "http://$h/ redirects to https ($got)";; *) bad "http://$h/ does not redirect to https ($got)";; esac
  done < "$TMP/hosts"
fi

echo
echo "stage7-scenarios: $PASS passed, $FAIL failed"
exit $((FAIL > 0))
