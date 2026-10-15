# S7 (restore) shared helpers. The prod database of checkers-lab is the subject.
. "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
NS=checkers-lab-prod
db_primary(){ desk kubectl -n $NS get pod -l cnpg.io/cluster=checkers-lab-db,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}'; }
db_name(){ desk kubectl -n $NS get cluster checkers-lab-db -o jsonpath='{.spec.bootstrap.initdb.database}'; }
games(){ desk kubectl -n $NS exec "$(db_primary)" -c postgres -- psql -d "$(db_name)" -At -c "select count(*) from games" | tr -dc 0-9; }

# driver_db <tool> <env> [<backup>]: the DRIVER's own orbit_db_backup / orbit_db_restore of
# checkers-lab (admin desk session), for setup and teardown, never the agent under test.
# Waits for the job; prints "<job status> <the backup's file name, or what was restored>".
driver_db(){
  local tool=$1 env=$2 extra=""
  [ -n "${3:-}" ] && extra=",\"backup\":\"$3\""
  "$ORBIT_DESK" bash -c "
    for try in 1 2 3 4 5 6; do
      orbit call $tool --yes --json '{\"project\":\"checkers-lab\",\"env\":\"$env\"$extra}' > /tmp/d.json 2>&1
      J=\$(python3 -c 'import json;t=open(\"/tmp/d.json\").read();d=json.loads(t[t.index(\"{\"):]);print((d.get(\"evidence\") or {}).get(\"job_id\",\"\"))')
      [ -n \"\$J\" ] && break
      grep -qiE 'busy|transport' /tmp/d.json || break
      sleep 15
    done
    [ -n \"\$J\" ] || { echo \"no job: \$(head -c 300 /tmp/d.json | tr '\\n' ' ')\"; exit 0; }
    for i in \$(seq 1 30); do
      orbit call orbit_job_wait --json \"{\\\"job_id\\\":\\\"\$J\\\",\\\"timeout_s\\\":120}\" > /tmp/w.json 2>&1
      st=\$(python3 -c 'import json;t=open(\"/tmp/w.json\").read();print(json.loads(t[t.index(\"{\"):]).get(\"status\",\"\"))')
      case \$st in completed|failed|cancelled) break;; esac
    done
    python3 -c 'import json;t=open(\"/tmp/w.json\").read();d=json.loads(t[t.index(\"{\"):]);e=(d.get(\"result\") or {}).get(\"evidence\") or {};print(d.get(\"status\"), e.get(\"backup\") or e.get(\"restored\") or \"-\")'
  " 2>/dev/null | tr -d '\r' | grep -v '^Warning'
}
