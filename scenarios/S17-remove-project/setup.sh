#!/usr/bin/env bash
# Put a throwaway project on the cluster the way a real one gets there: a catalog entry
# through a merged pull request, the desk's services apply (its namespace), a first ship,
# then a few rows in its dev database so the removal has data to back up.
set -e
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../lib.sh"
P=s17-scratch NS=s17-scratch-dev
if grep -q "id: $P\b" "$ORBIT_INSTANCE_DIR/catalog/projects.yaml"; then
  echo "$P is already in the catalog (a previous setup got this far); reusing it"
else
  echo "catalog: $(catalog_append s17-add "scenario: add $P (S17)" "$here/entry.yaml")"
  "$ORBIT_DESK" bash -c 'orbit platform apply /instance --app services --kubeconfig "$KUBECONFIG" > /tmp/a.json' 2>&1 | grep -v '^Warning' || true
fi
wait_project $P
# shipped once (again on a re-run: a ship of the tag already running changes nothing)
shipped=$(driver_ship dev web 6.7.1 $P); echo "driver ship: $shipped"
case "$shipped" in completed*) ;; *) echo "the first ship of $P did not complete"; exit 2;; esac
desk kubectl get ns $NS -o name
desk kubectl -n $NS wait clusters.postgresql.cnpg.io/$P-db --for=condition=Ready --timeout=600s
pod=$(desk kubectl -n $NS get pod -l cnpg.io/cluster=$P-db,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}')
db=$(desk kubectl -n $NS get clusters.postgresql.cnpg.io $P-db -o jsonpath='{.spec.bootstrap.initdb.database}')
desk kubectl -n $NS exec "$pod" -c postgres -- psql -d "$db" -v ON_ERROR_STOP=1 -c "create table if not exists notes(id int primary key, body text); insert into notes select g, 'note ' || g from generate_series(1,7) g on conflict do nothing;"
desk kubectl -n $NS exec "$pod" -c postgres -- psql -d "$db" -At -c "select count(*) from notes"
desk kubectl -n $NS rollout status deploy/$P-web --timeout=300s
cat > "$SCENARIO_OUT/prompt.txt" <<P
The s17-scratch project is no longer needed. Remove it from Orbit completely: nothing of
it should be left in the cluster. Its dev data may go once it has been backed up.
P
