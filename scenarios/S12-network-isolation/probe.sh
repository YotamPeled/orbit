#!/usr/bin/env bash
# probe <namespace> <labels-json> <url>: run one throwaway curl pod in <namespace> with the
# given pod labels (to stand where a real component stands), and print "<http code>:<curl
# exit>": curl exits 28 on a timeout (8 s) and 7 on a refusal — no connection — while a
# connection to something that is not HTTP (Postgres) exits 52 or 56. The pod meets the
# restricted Pod Security profile and the namespace quota; it is deleted after.
# The curl waits 45 s first (PROBE_DELAY): k3s's policy controller adds a new pod's IP to
# its rule sets on its next sync, so a pod that dials in its first seconds is in no
# allowed source set yet (its in-namespace calls are refused) and under no egress rule
# yet (a narrowed pod reaches the internet). Measured on the test box, 2026-10-05.
#   . probe.sh; probe checkers-lab-dev '{}' http://...
probe(){
  local ns=$1 labels=$2 url=$3 name overrides code
  name="s12-probe-$(date +%s%N | tail -c 7)"
  overrides=$(python3 - "$labels" "$url" <<'PY'
import json, sys
labels, url = json.loads(sys.argv[1]), sys.argv[2]
script = 'sleep "${PROBE_DELAY:-45}"; curl -s -o /dev/null -w "%{http_code}" --max-time 8 -k "$0"; echo ":$?"'
print(json.dumps({"apiVersion": "v1", "metadata": {"labels": labels}, "spec": {
    "restartPolicy": "Never", "automountServiceAccountToken": False,
    "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
    "containers": [{"name": "probe", "image": "curlimages/curl:8.10.1", "command": ["sh", "-c", script, url],
                    "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"memory": "16Mi"}},
                    "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}}))
PY
)
  desk kubectl -n "$ns" run "$name" --image=curlimages/curl:8.10.1 --restart=Never --overrides="$overrides" >/dev/null
  desk kubectl -n "$ns" wait "pod/$name" --for=jsonpath='{.status.phase}'=Succeeded --timeout=120s >/dev/null 2>&1 \
    || desk kubectl -n "$ns" wait "pod/$name" --for=jsonpath='{.status.phase}'=Failed --timeout=5s >/dev/null 2>&1
  code=$(desk kubectl -n "$ns" logs "$name" 2>/dev/null | tail -1)
  desk kubectl -n "$ns" delete pod "$name" --wait=false >/dev/null 2>&1
  printf '%s' "${code:-000:none}"
}
