"""Synthetic tests for live-default tables, owned extras, diffs, and the offline entry."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import orbitd.shadow as shadow
from orbitd.shadow import (
    DIFF_VALUE_LIMIT, _APPLIER_EXACT, _DEFAULTS, _IGNORE, compare_objects,
    _changed_paths, _normalise_live, _unescape,
)


def _paths(rendered: dict, live: dict) -> list[str]:
    return _changed_paths(
        _normalise_live(rendered), _normalise_live(live),
        kind=str(rendered.get("kind") or ""),
        api_version=str(rendered.get("apiVersion") or ""),
    )


def _deploy() -> dict:
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {
            "name": "web", "namespace": "demo-prod",
            "labels": {"app.kubernetes.io/instance": "demo"},
        },
        "spec": {"replicas": 1, "template": {"spec": {
            "serviceAccountName": "demo",
            "containers": [{
                "name": "c", "image": "x",
                "livenessProbe": {"httpGet": {"path": "/h", "port": 1}},
                "readinessProbe": {"httpGet": {"path": "/h", "port": 1}},
            }],
        }}},
    }


def test_service_target_port_defaults_to_port():
    rendered = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": "s", "namespace": "demo-prod"},
        "spec": {"ports": [{"name": "http", "port": 9402}]},
    }
    live = deepcopy(rendered)
    live["spec"]["ports"][0]["targetPort"] = 9402
    live["spec"]["ports"][0]["protocol"] = "TCP"
    assert _paths(rendered, live) == []
    live["spec"]["ports"][0]["targetPort"] = 80
    paths = _paths(rendered, live)
    assert any(p.endswith("/targetPort") for p in paths), paths


def test_networkpolicy_empty_ingress_matches_omitted():
    rendered = {
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": "np", "namespace": "demo-prod"},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []},
    }
    live = deepcopy(rendered)
    live["spec"].pop("ingress")
    assert _paths(rendered, live) == []


def test_probe_server_defaults_are_equal():
    rendered = _deploy()
    live = deepcopy(rendered)
    container = live["spec"]["template"]["spec"]["containers"][0]
    container["livenessProbe"]["httpGet"]["scheme"] = "HTTP"
    container["livenessProbe"]["successThreshold"] = 1
    container["livenessProbe"]["timeoutSeconds"] = 1
    container["readinessProbe"]["failureThreshold"] = 3
    container["readinessProbe"]["httpGet"]["scheme"] = "HTTP"
    container["readinessProbe"]["successThreshold"] = 1
    container["readinessProbe"]["timeoutSeconds"] = 1
    live["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = 30
    live["spec"]["template"]["spec"]["serviceAccount"] = "demo"
    assert _paths(rendered, live) == []


def test_probe_scheme_https_is_a_change():
    rendered = _deploy()
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["containers"][0]["livenessProbe"]["httpGet"]["scheme"] = (
        "HTTPS"
    )
    paths = _paths(rendered, live)
    assert any(p.endswith("/livenessProbe/httpGet/scheme") for p in paths), paths


def test_configmap_scheme_key_is_a_change():
    rendered = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cm"},
        "data": {"k": "v"},
    }
    live = deepcopy(rendered)
    live["data"]["scheme"] = "HTTP"
    paths = _paths(rendered, live)
    assert any(p.endswith("/scheme") for p in paths), paths


def test_job_server_defaults_are_equal():
    rendered = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "once", "namespace": "demo-prod"},
        "spec": {"template": {"spec": {
            "serviceAccountName": "demo",
            "containers": [{"name": "c", "image": "x"}],
        }}},
    }
    live = deepcopy(rendered)
    live["spec"]["manualSelector"] = False
    live["spec"]["podReplacementPolicy"] = "TerminatingOrFailed"
    live["spec"]["suspend"] = False
    live["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = 30
    live["spec"]["template"]["spec"]["serviceAccount"] = "demo"
    assert _paths(rendered, live) == []


def test_job_pod_replacement_other_is_a_change():
    rendered = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": "once"},
        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}},
    }
    live = deepcopy(rendered)
    live["spec"]["podReplacementPolicy"] = "Failed"
    paths = _paths(rendered, live)
    assert any(p.endswith("/podReplacementPolicy") for p in paths), paths


def test_pvc_binding_annotations_and_volume_name_are_ignored():
    rendered = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": "data", "namespace": "demo-prod"},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "resources": {"requests": {"storage": "1Gi"}}},
    }
    live = deepcopy(rendered)
    live["metadata"]["annotations"] = {
        "pv.kubernetes.io/bind-completed": "yes",
        "pv.kubernetes.io/bound-by-controller": "yes",
        "volume.beta.kubernetes.io/storage-provisioner": "local",
        "volume.kubernetes.io/selected-node": "node-1",
        "volume.kubernetes.io/storage-provisioner": "local",
    }
    live["metadata"]["finalizers"] = ["kubernetes.io/pvc-protection"]
    live["spec"]["volumeMode"] = "Filesystem"
    live["spec"]["volumeName"] = "pvc-not-a-default"
    assert _paths(rendered, live) == []


def test_pvc_rendered_volume_name_mismatch_is_a_change():
    rendered = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": "data"},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "resources": {"requests": {"storage": "1Gi"}},
                 "volumeName": "desired-pv"},
    }
    live = deepcopy(rendered)
    live["spec"]["volumeName"] = "other-pv"
    report = compare_objects([rendered], [live], project="demo", env="prod")
    assert report.empty is False
    assert report.changed
    paths = report.changed[0].paths
    assert any(p.endswith("/volumeName") for p in paths), paths
    by_path = {d.path: d for d in report.changed[0].diffs}
    assert by_path["/spec/volumeName"].rendered == '"desired-pv"'
    assert by_path["/spec/volumeName"].live == '"other-pv"'


def test_probe_prober_path_default_is_equal():
    rendered = {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "Probe",
        "metadata": {"name": "p", "namespace": "monitoring"},
        "spec": {"module": "http_2xx", "prober": {"url": "blackbox:9115"}},
    }
    live = deepcopy(rendered)
    live["spec"]["prober"]["path"] = "/probe"
    assert _paths(rendered, live) == []


def test_probe_prober_path_other_is_a_change():
    rendered = {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "Probe",
        "metadata": {"name": "p", "namespace": "monitoring"},
        "spec": {"module": "http_2xx", "prober": {"url": "blackbox:9115"}},
    }
    live = deepcopy(rendered)
    live["spec"]["prober"]["path"] = "/other"
    paths = _paths(rendered, live)
    assert "/spec/prober/path" in paths, paths


def test_externalsecret_finalizers_are_ignored():
    rendered = {
        "apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
        "metadata": {"name": "s", "namespace": "demo-prod"},
        "spec": {"refreshInterval": "1h"},
    }
    live = deepcopy(rendered)
    live["metadata"]["finalizers"] = [
        "externalsecrets.external-secrets.io/externalsecret-cleanup",
    ]
    assert _paths(rendered, live) == []


def test_configmap_service_account_alias_is_a_change():
    rendered = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cm"},
        "data": {"serviceAccountName": "app"},
    }
    live = deepcopy(rendered)
    live["data"]["serviceAccount"] = "app"
    paths = _paths(rendered, live)
    assert any(p.endswith("/serviceAccount") for p in paths), paths


def test_deployment_live_finalizer_is_a_change():
    rendered = _deploy()
    live = deepcopy(rendered)
    live["metadata"]["finalizers"] = ["review.example/hold"]
    paths = _paths(rendered, live)
    assert any(p.endswith("/finalizers") for p in paths), paths


def test_live_only_argocd_sync_options_is_a_change():
    rendered = _deploy()
    live = deepcopy(rendered)
    live["metadata"].setdefault("annotations", {})[
        "argocd.argoproj.io/sync-options"
    ] = "Prune=false"
    paths = _paths(rendered, live)
    assert "/metadata/annotations/argocd.argoproj.io~1sync-options" in paths, paths


def test_live_only_kubectl_restarted_at_is_a_change():
    rendered = _deploy()
    live = deepcopy(rendered)
    live["metadata"].setdefault("annotations", {})[
        "kubectl.kubernetes.io/restartedAt"
    ] = "2026-01-01T00:00:00Z"
    paths = _paths(rendered, live)
    assert (
        "/metadata/annotations/kubectl.kubernetes.io~1restartedAt" in paths
    ), paths


def test_ignore_and_default_tables_are_exact_keys_and_paths():
    def assert_pointer(pointer: str) -> None:
        assert pointer.startswith("/"), pointer
        assert not pointer.endswith("/"), pointer
        segs = pointer.lstrip("/").split("/")
        assert segs and all(segs), pointer
        for seg in segs:
            name = _unescape(seg)
            assert name == "*" or "*" not in name, pointer

    for _av, _kind, pointer, _value in _DEFAULTS:
        assert_pointer(pointer)
    for _av, _kind, pointer in _IGNORE:
        assert_pointer(pointer)
    for key in _APPLIER_EXACT:
        assert key
        assert not key.endswith("/"), key
        assert "*" not in key, key
        assert "/" in key, key
    assert not hasattr(shadow, "_APPLIER_PREFIXES")


def test_cluster_operator_defaults_are_equal():
    rendered = {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": "db", "namespace": "demo-prod"},
        "spec": {
            "instances": 1,
            "bootstrap": {"initdb": {"database": "app", "owner": "app"}},
            "postgresql": {"parameters": {"max_connections": "100"}},
            "storage": {"size": "1Gi"},
        },
    }
    live = deepcopy(rendered)
    live["spec"]["affinity"] = {"podAntiAffinityType": "preferred"}
    live["spec"]["bootstrap"]["initdb"]["encoding"] = "UTF8"
    live["spec"]["bootstrap"]["initdb"]["localeCType"] = "C"
    live["spec"]["bootstrap"]["initdb"]["localeCollate"] = "C"
    live["spec"]["enablePDB"] = True
    live["spec"]["failoverDelay"] = 0
    live["spec"]["logLevel"] = "info"
    live["spec"]["maxSyncReplicas"] = 0
    live["spec"]["minSyncReplicas"] = 0
    live["spec"]["monitoring"] = {
        "customQueriesConfigMap": [{"key": "queries", "name": "cnpg-default-monitoring"}],
        "disableDefaultQueries": False,
        "enablePodMonitor": False,
    }
    live["spec"]["postgresGID"] = 26
    live["spec"]["postgresUID"] = 26
    live["spec"]["postgresql"]["parameters"]["archive_mode"] = "on"
    live["spec"]["postgresql"]["parameters"]["wal_level"] = "logical"
    live["spec"]["postgresql"]["syncReplicaElectionConstraint"] = {"enabled": False}
    live["spec"]["primaryUpdateMethod"] = "restart"
    live["spec"]["probes"] = {"liveness": {"isolationCheck": {
        "connectionTimeout": 1000, "enabled": True, "requestTimeout": 1000,
    }}}
    live["spec"]["replicationSlots"] = {
        "highAvailability": {"enabled": True, "slotPrefix": "_cnpg_"},
        "synchronizeReplicas": {"enabled": True},
        "updateInterval": 30,
    }
    live["spec"]["smartShutdownTimeout"] = 180
    live["spec"]["startDelay"] = 3600
    live["spec"]["stopDelay"] = 1800
    live["spec"]["storage"]["resizeInUseVolumes"] = True
    live["spec"]["switchoverDelay"] = 3600
    assert _paths(rendered, live) == []


def test_cluster_log_level_other_is_a_change():
    rendered = {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": "db"},
        "spec": {"instances": 1},
    }
    live = deepcopy(rendered)
    live["spec"]["logLevel"] = "debug"
    paths = _paths(rendered, live)
    assert any(p.endswith("/logLevel") for p in paths), paths


def test_objectstore_sidecar_defaults_are_equal():
    rendered = {
        "apiVersion": "barmancloud.cnpg.io/v1", "kind": "ObjectStore",
        "metadata": {"name": "store", "namespace": "demo-prod"},
        "spec": {"instanceSidecarConfiguration": {"resources": {
            "requests": {"cpu": "20m"},
        }}},
    }
    live = deepcopy(rendered)
    live["spec"]["instanceSidecarConfiguration"]["logLevel"] = "info"
    live["spec"]["instanceSidecarConfiguration"]["retentionPolicyIntervalSeconds"] = 1800
    assert _paths(rendered, live) == []


def test_service_account_alias_mismatch_is_a_change():
    rendered = _deploy()
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["serviceAccount"] = "other"
    paths = _paths(rendered, live)
    assert any(p.endswith("/serviceAccount") for p in paths), paths


def test_owned_cronjob_job_is_not_extra():
    cron = {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {
            "name": "nightly", "namespace": "demo-prod",
            "labels": {"app.kubernetes.io/instance": "demo"},
        },
        "spec": {"schedule": "* * * * *", "jobTemplate": {"spec": {
            "template": {"spec": {"containers": [{"name": "c", "image": "x"}]}},
        }}},
    }
    spawned = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {
            "name": "nightly-283041", "namespace": "demo-prod",
            "labels": {"app.kubernetes.io/instance": "demo"},
            "ownerReferences": [{
                "apiVersion": "batch/v1", "kind": "CronJob",
                "name": "nightly", "controller": True,
            }],
        },
        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}},
    }
    report = compare_objects([cron], [deepcopy(cron), spawned], project="demo", env="prod")
    assert report.empty is True
    assert report.extra == []
    assert len(report.owned) == 1
    assert report.owned[0].kind == "Job"
    assert report.owned[0].name == "nightly-283041"
    assert report.owned[0].owner.kind == "CronJob"
    assert report.owned[0].owner.name == "nightly"


def test_unlabelled_live_object_is_not_extra():
    rendered = _deploy()
    default_sa = {
        "apiVersion": "v1", "kind": "ServiceAccount",
        "metadata": {"name": "default", "namespace": "demo-prod"},
    }
    report = compare_objects(
        [rendered], [deepcopy(rendered), default_sa], project="demo", env="prod",
    )
    assert report.extra == []
    assert report.owned == []
    assert report.empty is True


def test_stray_labelled_service_is_extra():
    rendered = _deploy()
    stray = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {
            "name": "stray", "namespace": "demo-prod",
            "labels": {"app.kubernetes.io/instance": "demo"},
        },
        "spec": {"ports": [{"port": 80}]},
    }
    report = compare_objects(
        [rendered], [deepcopy(rendered), stray], project="demo", env="prod",
    )
    assert report.empty is False
    assert [e.name for e in report.extra] == ["stray"]


def test_changed_diffs_carry_json_encoded_values_cut_at_limit():
    rendered = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cm", "namespace": "demo-prod"},
        "data": {"k": "short", "long": "x" * 400},
    }
    live = deepcopy(rendered)
    live["data"]["k"] = "other"
    live["data"]["long"] = "y" * 400
    report = compare_objects([rendered], [live], project="demo", env="prod")
    assert report.empty is False
    by_path = {d.path: d for d in report.changed[0].diffs}
    assert by_path["/data/k"].rendered == '"short"'
    assert by_path["/data/k"].live == '"other"'
    assert len(by_path["/data/long"].rendered) == DIFF_VALUE_LIMIT
    assert len(by_path["/data/long"].live) == DIFF_VALUE_LIMIT
    assert by_path["/data/long"].rendered.startswith('"xxx')
    assert report.changed[0].paths == [d.path for d in report.changed[0].diffs]


def test_rendered_without_namespace_matches_live_namespace():
    rendered = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "cm", "labels": {"app.kubernetes.io/instance": "demo"}},
        "data": {"k": "v"},
    }
    live = deepcopy(rendered)
    live["metadata"]["namespace"] = "demo-prod"
    report = compare_objects([rendered], [live], project="demo", env="prod")
    assert report.empty is True
    assert report.missing == []
    assert report.live_matched[0].namespace == "demo-prod"


def test_shadow_offline_prints_empty_report(tmp_path):
    rendered = [_deploy()]
    live = [deepcopy(rendered[0])]
    live[0]["spec"]["template"]["spec"]["containers"][0]["livenessProbe"]["httpGet"]["scheme"] = (
        "HTTP"
    )
    rpath = tmp_path / "rendered.json"
    lpath = tmp_path / "live.json"
    rpath.write_text(json.dumps(rendered))
    lpath.write_text(json.dumps(live))
    proc = subprocess.run(
        [sys.executable, "-m", "orbitd.shadow_offline", str(rpath), str(lpath)],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["empty"] is True
    assert body["missing"] == []
    assert body["extra"] == []
    assert body["changed"] == []
    assert "owned" in body


def test_shadow_offline_refuses_missing_file_and_bad_argv(tmp_path):
    rpath = tmp_path / "rendered.json"
    rpath.write_text("[]")
    missing = tmp_path / "no-such.json"
    proc = subprocess.run(
        [sys.executable, "-m", "orbitd.shadow_offline", str(rpath), str(missing)],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 2
    proc = subprocess.run(
        [sys.executable, "-m", "orbitd.shadow_offline"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 2
    proc = subprocess.run(
        [sys.executable, "-m", "orbitd.shadow_offline", str(rpath)],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 2


def test_statefulset_and_daemonset_pod_defaults_are_equal():
    rendered = {
        "apiVersion": "apps/v1", "kind": "StatefulSet",
        "metadata": {"name": "db", "namespace": "demo-prod"},
        "spec": {
            "serviceName": "db",
            "selector": {"matchLabels": {"app": "db"}},
            "template": {"spec": {"containers": [{
                "name": "c", "image": "x",
                "env": [{"name": "POD_NS", "valueFrom": {
                    "fieldRef": {"fieldPath": "metadata.namespace"},
                }}],
            }]}},
            "volumeClaimTemplates": [{
                "metadata": {"name": "data"},
                "spec": {"accessModes": ["ReadWriteOnce"],
                         "resources": {"requests": {"storage": "1Gi"}}},
            }],
        },
    }
    live = deepcopy(rendered)
    live["spec"]["podManagementPolicy"] = "OrderedReady"
    live["spec"]["revisionHistoryLimit"] = 10
    live["spec"]["persistentVolumeClaimRetentionPolicy"] = {
        "whenDeleted": "Retain", "whenScaled": "Retain",
    }
    live["spec"]["volumeClaimTemplates"][0]["spec"]["volumeMode"] = "Filesystem"
    live["spec"]["volumeClaimTemplates"][0]["status"] = {"phase": "Pending"}
    spec = live["spec"]["template"]["spec"]
    spec["restartPolicy"] = "Always"
    spec["dnsPolicy"] = "ClusterFirst"
    spec["schedulerName"] = "default-scheduler"
    spec["terminationGracePeriodSeconds"] = 30
    spec["hostNetwork"] = False
    spec["containers"][0]["terminationMessagePath"] = "/dev/termination-log"
    spec["containers"][0]["terminationMessagePolicy"] = "File"
    spec["containers"][0]["env"][0]["valueFrom"]["fieldRef"]["apiVersion"] = "v1"
    assert _paths(rendered, live) == []

    ds = {
        "apiVersion": "apps/v1", "kind": "DaemonSet",
        "metadata": {"name": "node", "namespace": "demo-prod"},
        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}},
    }
    live_ds = deepcopy(ds)
    live_ds["metadata"]["annotations"] = {
        "deprecated.daemonset.template.generation": "1",
    }
    live_ds["spec"]["revisionHistoryLimit"] = 10
    live_ds["spec"]["updateStrategy"] = {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0},
    }
    live_ds["spec"]["template"]["spec"]["restartPolicy"] = "Always"
    live_ds["spec"]["template"]["spec"]["schedulerName"] = "default-scheduler"
    assert _paths(ds, live_ds) == []


def test_priorityclass_and_crd_conversion_defaults_are_equal():
    rendered = {
        "apiVersion": "scheduling.k8s.io/v1", "kind": "PriorityClass",
        "metadata": {"name": "orbit-platform"},
        "value": 1000,
    }
    live = deepcopy(rendered)
    live["globalDefault"] = False
    live["preemptionPolicy"] = "PreemptLowerPriority"
    assert _paths(rendered, live) == []

    crd = {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "widgets.example.io"},
        "spec": {"group": "example.io"},
    }
    live_crd = deepcopy(crd)
    live_crd["spec"]["conversion"] = {"strategy": "None"}
    live_crd["status"] = {"storedVersions": ["v1"], "conditions": []}
    assert _paths(crd, live_crd) == []


def test_service_account_token_secret_data_is_ignored():
    rendered = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {
            "name": "reviewer", "namespace": "vault",
            "annotations": {"kubernetes.io/service-account.name": "reviewer"},
        },
        "type": "kubernetes.io/service-account-token",
    }
    live = deepcopy(rendered)
    live["data"] = {"ca.crt": "Y2E=", "namespace": "bnM=", "token": "dG9r"}
    live["metadata"]["annotations"]["kubernetes.io/service-account.uid"] = "uid-1"
    live["metadata"]["labels"] = {"kubernetes.io/legacy-token-last-used": "2026-01-01"}
    assert _paths(rendered, live) == []


def test_secret_stringdata_matches_live_data():
    rendered = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "id", "namespace": "eso"},
        "stringData": {"identityId": "abc"},
    }
    live = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "id", "namespace": "eso"},
        "type": "Opaque",
        "data": {"identityId": "YWJj"},
    }
    assert _paths(rendered, live) == []


def test_helm_hook_is_hooks_not_extra():
    rendered = _deploy()
    hook = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {
            "name": "init", "namespace": "demo-prod",
            "annotations": {"helm.sh/hook": "pre-install"},
            "labels": {"app.kubernetes.io/instance": "demo"},
        },
        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}},
    }
    report = compare_objects(
        [rendered], [deepcopy(rendered), hook], project="demo", env="prod",
    )
    assert report.extra == []
    assert len(report.hooks) == 1
    assert report.hooks[0].name == "init"
    assert report.empty is True


def test_statefulset_volume_claim_is_owned_not_extra():
    sts = {
        "apiVersion": "apps/v1", "kind": "StatefulSet",
        "metadata": {"name": "db", "namespace": "demo-prod",
                     "labels": {"app.kubernetes.io/instance": "demo"}},
        "spec": {
            "selector": {"matchLabels": {"app": "db"}},
            "template": {"spec": {"containers": [{"name": "c", "image": "x"}]}},
            "volumeClaimTemplates": [{
                "metadata": {"name": "data"},
                "spec": {"accessModes": ["ReadWriteOnce"]},
            }],
        },
    }
    pvc = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": "data-db-0", "namespace": "demo-prod",
            "labels": {"app": "db", "app.kubernetes.io/instance": "demo"},
        },
        "spec": {"accessModes": ["ReadWriteOnce"]},
    }
    report = compare_objects(
        [sts], [deepcopy(sts), pvc], project="demo", env="prod",
    )
    assert report.extra == []
    assert len(report.owned) == 1
    assert report.owned[0].name == "data-db-0"
    assert report.owned[0].owner.kind == "StatefulSet"
    assert report.owned[0].owner.name == "db"
    assert report.empty is True


def test_crd_declared_defaults_and_controller_stamps():
    es = {
        "apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
        "metadata": {"name": "s", "namespace": "demo-prod"},
        "spec": {"target": {"template": {}}},
    }
    live_es = deepcopy(es)
    live_es["spec"]["target"]["template"]["mergePolicy"] = "Replace"
    live_es["metadata"]["annotations"] = {"force-sync": "1"}
    assert _paths(es, live_es) == []

    css = {
        "apiVersion": "external-secrets.io/v1", "kind": "ClusterSecretStore",
        "metadata": {"name": "infisical"},
        "spec": {"provider": {"infisical": {"secretsScope": {}}}},
    }
    live_css = deepcopy(css)
    live_css["spec"]["provider"]["infisical"]["secretsScope"]["expandSecretReferences"] = True
    live_css["status"] = {"conditions": []}
    assert _paths(css, live_css) == []

    pw = {
        "apiVersion": "generators.external-secrets.io/v1alpha1", "kind": "Password",
        "metadata": {"name": "p", "namespace": "demo-prod"},
        "spec": {},
    }
    live_pw = deepcopy(pw)
    live_pw["spec"]["encoding"] = "raw"
    assert _paths(pw, live_pw) == []

    rule = {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
        "metadata": {"name": "r", "namespace": "monitoring"},
        "spec": {"groups": []},
    }
    live_rule = deepcopy(rule)
    live_rule["metadata"]["annotations"] = {"prometheus-operator-validated": "true"}
    assert _paths(rule, live_rule) == []

    deploy = _deploy()
    live_d = deepcopy(deploy)
    live_d["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
        "kubectl.kubernetes.io/restartedAt"
    ] = "2026-01-01T00:00:00Z"
    live_d["metadata"].setdefault("annotations", {})["updatedAt"] = "2026-01-01"
    assert _paths(deploy, live_d) == []

    # Chart re-render stamps a different updatedAt than live; still a controller stamp.
    rendered_ts = deepcopy(deploy)
    rendered_ts["metadata"].setdefault("annotations", {})["updatedAt"] = "render-time"
    live_ts = deepcopy(deploy)
    live_ts["metadata"].setdefault("annotations", {})["updatedAt"] = "live-time"
    assert _paths(rendered_ts, live_ts) == []



def test_shadow_offline_refuses_non_list_json(tmp_path):
    rpath = tmp_path / "rendered.json"
    lpath = tmp_path / "live.json"
    rpath.write_text("{}")
    lpath.write_text("[]")
    proc = subprocess.run(
        [sys.executable, "-m", "orbitd.shadow_offline", str(rpath), str(lpath)],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 2


def test_an_empty_env_value_matches_the_server_dropping_it():
    """EnvVar.value is omitempty: the API server stores `value: ""` as no value. The
    orbitd Deployment rendered ORBITD_APPLY_PAIRS="" and every plan after the apply
    still reported the Deployment changed (docs/V1.md §8)."""
    rendered = _deploy()
    rendered["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": "PAIRS", "value": ""}, {"name": "MODE", "value": "on"}]
    live = deepcopy(rendered)
    live["spec"]["template"]["spec"]["containers"][0]["env"][0] = {"name": "PAIRS"}
    assert _paths(rendered, live) == []
    # a real difference in a non-empty value is still drift
    live["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"] = "off"
    assert any(p.endswith("/env/1/value") for p in _paths(rendered, live))
    # and a live value where the render has an empty one is drift too
    live["spec"]["template"]["spec"]["containers"][0]["env"][0] = {"name": "PAIRS", "value": "x"}
    assert any(p.endswith("/env/0/value") for p in _paths(rendered, live))
