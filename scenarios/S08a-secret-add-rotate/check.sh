#!/usr/bin/env bash
# Pass: the dev api's pod template takes API_TOKEN from a Secret declared in the catalog;
# that Secret's sealed file was committed at least twice since the start (added, then
# rotated); the live Secret holds the key; every running dev api pod started after the
# live Secret last changed (so it reads the rotated value); prod was not touched.
. "$(dirname "$0")/../lib.sh"
PASS=0 FAIL=0
ok(){ echo "  ok    $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
base=$(cat "$SCENARIO_OUT/base")
ref=$(desk kubectl -n checkers-lab-dev get deploy checkers-lab-api -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="API_TOKEN")].valueFrom.secretKeyRef.name}/{.spec.template.spec.containers[0].env[?(@.name=="API_TOKEN")].valueFrom.secretKeyRef.key}')
sname=${ref%%/*}; skey=${ref##*/}
[ -n "$sname" ] && [ -n "$skey" ] && ok "the dev api reads API_TOKEN from Secret $sname key $skey" || bad "the dev api has no API_TOKEN from a Secret"
( cd "$ORBIT_INSTANCE_DIR" && git checkout -q main && git pull -q --ff-only )
grep -q "name: $sname" "$ORBIT_INSTANCE_DIR/catalog/projects.yaml" 2>/dev/null && [ -n "$sname" ] \
  && ok "the catalog declares Secret $sname" || bad "the catalog does not declare the Secret"
f="clusters/platform/secrets/checkers-lab-dev/$sname.yaml"
n=$( cd "$ORBIT_INSTANCE_DIR" && git log --format=%H "$base"..HEAD -- "$f" | wc -l )
[ -n "$sname" ] && [ "$n" -ge 2 ] && ok "the sealed file $f changed in $n commits (added, then rotated)" \
  || bad "the sealed file $f changed in $n commit(s) since the start, not 2 or more"
grep -q 'ENC\[' "$ORBIT_INSTANCE_DIR/$f" 2>/dev/null && ok "the sealed file holds ciphertext" || bad "the sealed file is missing or not sealed"
has=$(desk kubectl -n checkers-lab-dev get secret "$sname" -o jsonpath="{.data.$skey}" | wc -c)
[ "$has" -gt 8 ] && ok "the live Secret holds $skey" || bad "the live Secret does not hold $skey"
# the last write to the Secret's data, from its managedFields; every api pod must be younger
changed=$(desk kubectl -n checkers-lab-dev get secret "$sname" -o json --show-managed-fields | python3 -c '
import json, sys
d = json.load(sys.stdin)
ts = [e.get("time", "") for e in d["metadata"].get("managedFields") or [] if "f:data" in (e.get("fieldsV1") or {})]
print(max(ts) if ts else "")')
starts=$(desk kubectl -n checkers-lab-dev get pod -l app.kubernetes.io/name=api -o jsonpath='{range .items[*]}{.status.startTime}{"\n"}{end}')
old=$(awk -v c="$changed" 'NF && $1 < c' <<<"$starts" | wc -l)
[ -n "$changed" ] && [ -n "$starts" ] && [ "$old" -eq 0 ] && ok "every running dev api pod started after the Secret last changed ($changed)" \
  || bad "$old dev api pod(s) started before the Secret last changed ($changed)"
p=$(desk kubectl -n checkers-lab-prod get deploy checkers-lab-api -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="API_TOKEN")].name}')
[ -z "$p" ] && ok "production has no API_TOKEN (untouched)" || bad "production was given API_TOKEN"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
