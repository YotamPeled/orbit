"""Shadow render-and-diff: desired objects versus what is live, persisted daily.

`diff` never writes to the cluster. Live reads go through the kube dynamic client with
`_request_timeout`. A daily runner in `serve` diffs every catalog pair; `streak_days`
counts consecutive empty reports ending today.

Compare names these classes rather than reporting them as `changed`:

- generated: Secret data a chart mints at render time. A Secret whose keys are
  TLS-shaped (`tls.crt`+`tls.key`, or `ca.crt`+`ca.key`) is generated when that
  data differs from live; other generated Secrets are named on the Application
  as `orbit.local/generated-secrets`. The applier creates them when missing and
  never overwrites.
- hook_derived: pod-template annotations whose key starts with `checksum/`
  (Helm chart idiom; subtree convention on the annotations map). The applier
  strips them before apply.
- Helm `toYaml` null idiom: a ConfigMap value that parses as YAML on both sides
  treats a null key as absent; a value that does not parse compares as a string.
- kubernetes DefaultStorageClass admission controller: any live
  PersistentVolumeClaim `/spec/storageClassName` is accepted only when the
  render omits the field.
- CloudNativePG barman-cloud plugin operator stamp: Secret `/data/SIDECAR_IMAGE`
  is ignored at that exact path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel, Field

from .audit import guarded_thread
from .backends import KUBE_TIMEOUT, BackendError, BackendUnconfigured
from .render import render as render_desired
from .state import AppState
from .tools_viewer._util import memory_bytes
from .tools_viewer.common import env_names, project_entries

log = logging.getLogger("orbitd.shadow")

FIRST_DELAY_SECONDS = 5 * 60
INTERVAL_SECONDS = 24 * 3600
LABEL_INSTANCE = "app.kubernetes.io/instance"
LABEL_PROJECT = "orbit.local/project"
LABEL_ENV = "orbit.local/env"
LABEL_GENERATED_SECRETS = "orbit.local/generated-secrets"
MONITORING_NS = "monitoring"
# Helm chart idiom: checksum/* on the pod template is derived from skipped hooks.
_CHECKSUM_PREFIX = "checksum/"
_TLS_KEY_PAIRS = (
    frozenset({"tls.crt", "tls.key"}),
    frozenset({"ca.crt", "ca.key"}),
)
_TLS_DATA_KEYS = frozenset({"tls.crt", "tls.key", "ca.crt", "ca.key"})
# CloudNativePG barman-cloud plugin operator writes SIDECAR_IMAGE into the
# plugin Secret; the chart placeholder is not desired state. Kept off _IGNORE
# because tenant Secrets do not carry this key.
_SIDECAR_IMAGE_PATH = "/data/SIDECAR_IMAGE"
# kubernetes DefaultStorageClass admission controller: any live value is
# accepted only when the render omits the field.
_ANY_VALUE = object()
_UNPARSED = object()

# Exact annotation/label keys controllers stamp; dropped before compare. One key each.
LABEL_ARGOCD_INSTANCE = "argocd.argoproj.io/instance"
_APPLIER_EXACT = {
    "argocd.argoproj.io/tracking-id",  # ArgoCD application controller
    LABEL_ARGOCD_INSTANCE,  # ArgoCD application controller
    "kubectl.kubernetes.io/last-applied-configuration",  # kubectl client
    "deployment.kubernetes.io/revision",  # deployment controller
}
_DROP_META = {
    "resourceVersion", "uid", "creationTimestamp", "generation",
    "managedFields", "selfLink", "resource_version", "creation_timestamp",
}
# Cluster-scoped kinds: the apiserver rejects metadata.namespace on these
# (REST mapper's Namespaced=false). Stamping destination.namespace would make
# live lookup look in a namespace they never occupy.
_CLUSTER_SCOPED = {
    "APIService",
    "CSIDriver",
    "CSINode",
    "CertificateSigningRequest",
    "ClusterExternalSecret",
    "ClusterIssuer",
    "ClusterRole",
    "ClusterRoleBinding",
    "ClusterSecretStore",
    "ComponentStatus",
    "CustomResourceDefinition",
    "FlowSchema",
    "IngressClass",
    "MutatingWebhookConfiguration",
    "Namespace",
    "Node",
    "PersistentVolume",
    "PriorityClass",
    "PriorityLevelConfiguration",
    "RuntimeClass",
    "StorageClass",
    "ValidatingAdmissionPolicy",
    "ValidatingAdmissionPolicyBinding",
    "ValidatingWebhookConfiguration",
    "VolumeAttachment",
    "VolumeSnapshotClass",
}

# Kinds the chart and the generator render; extras are listed from these in the dest ns.
# Tenant path only: orbitd-readonly does not grant list on StatefulSet, DaemonSet
# or PodDisruptionBudget. Those three live on PLATFORM_LIVE_GVKS (desk kubeconfig).
LIVE_GVKS: tuple[tuple[str, str], ...] = (
    ("v1", "Service"),
    ("v1", "ServiceAccount"),
    ("v1", "PersistentVolumeClaim"),
    ("v1", "ConfigMap"),
    ("v1", "ResourceQuota"),
    ("apps/v1", "Deployment"),
    ("batch/v1", "CronJob"),
    ("batch/v1", "Job"),
    ("rbac.authorization.k8s.io/v1", "Role"),
    ("rbac.authorization.k8s.io/v1", "RoleBinding"),
    ("networking.k8s.io/v1", "Ingress"),
    ("networking.k8s.io/v1", "NetworkPolicy"),
    ("traefik.io/v1alpha1", "Middleware"),
    ("monitoring.coreos.com/v1", "ServiceMonitor"),
    ("monitoring.coreos.com/v1", "Probe"),
    ("postgresql.cnpg.io/v1", "ScheduledBackup"),
    ("barmancloud.cnpg.io/v1", "ObjectStore"),
    ("postgresql.cnpg.io/v1", "Cluster"),
    ("opentelemetry.io/v1alpha1", "Instrumentation"),
    ("cert-manager.io/v1", "Certificate"),
)
PLATFORM_LIVE_GVKS: tuple[tuple[str, str], ...] = LIVE_GVKS + (
    ("apps/v1", "StatefulSet"),
    ("apps/v1", "DaemonSet"),
    ("policy/v1", "PodDisruptionBudget"),
)

_MEM_SUFFIX = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:Ei|Pi|Ti|Gi|Mi|Ki|E|P|T|G|M|K|k)$"
)
_CPU_MILLI = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)m$")

# Live-only fields equal the render only at these (apiVersion, kind, pointer, value)
# rows. `*` is one path segment (list index or map key). Nothing matches by field
# name alone; ConfigMap `data` is not in this table.
_ROLLING = {
    "type": "RollingUpdate",
    "rollingUpdate": {"maxUnavailable": "25%", "maxSurge": "25%"},
}
# DaemonSet rollingUpdate defaults differ from Deployment (maxUnavailable=1, maxSurge=0)
# pkg/apis/apps/v1/defaults.go SetDefaults_DaemonSet.
_DS_ROLLING = {
    "type": "RollingUpdate",
    "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0},
}


def _webhook_defaults(av: str, kind: str) -> tuple[tuple[str, str, str, Any], ...]:
    rows: list[tuple[str, str, str, Any]] = [
        # admissionregistration.k8s.io v1 defaults (pkg/apis/admissionregistration/v1/defaults.go):
        # matchPolicy Equivalent, timeoutSeconds 10, rules.scope '*', service.port 443.
        (av, kind, "/webhooks/*/matchPolicy", "Equivalent"),
        (av, kind, "/webhooks/*/timeoutSeconds", 10),
        (av, kind, "/webhooks/*/rules/*/scope", "*"),
        (av, kind, "/webhooks/*/clientConfig/service/port", 443),
    ]
    if kind == "MutatingWebhookConfiguration":
        rows.append((av, kind, "/webhooks/*/reinvocationPolicy", "Never"))
    return tuple(rows)


# One list so container and initContainer default/quantity rows cannot diverge.
_POD_CONTAINER_KEYS: tuple[str, ...] = ("containers", "initContainers")


def _container_defaults(av: str, kind: str, prefix: str) -> tuple[tuple[str, str, str, Any], ...]:
    return (
        (av, kind, f"{prefix}/imagePullPolicy", "IfNotPresent"),
        (av, kind, f"{prefix}/terminationMessagePath", "/dev/termination-log"),
        (av, kind, f"{prefix}/terminationMessagePolicy", "File"),
        (av, kind, f"{prefix}/securityContext", {}),
        (av, kind, f"{prefix}/ports/*/protocol", "TCP"),
        # Pod env fieldRef.apiVersion: kubelet/apiserver default "v1"
        # (pkg/apis/core/v1/defaults.go SetDefaults_ObjectFieldSelector).
        (av, kind, f"{prefix}/env/*/valueFrom/fieldRef/apiVersion", "v1"),
        # EnvVar.value is `omitempty` (api/core/v1/types.go): a rendered `value: ""`
        # is stored and served back as no value at all, so it can never match live
        # and read as permanent drift (docs/V1.md §8, ORBITD_APPLY_PAIRS="").
        (av, kind, f"{prefix}/env/*/value", ""),
        (av, kind, f"{prefix}/livenessProbe/httpGet/scheme", "HTTP"),
        (av, kind, f"{prefix}/livenessProbe/httpGet/httpHeaders", []),
        (av, kind, f"{prefix}/livenessProbe/successThreshold", 1),
        (av, kind, f"{prefix}/livenessProbe/timeoutSeconds", 1),
        (av, kind, f"{prefix}/livenessProbe/failureThreshold", 3),
        (av, kind, f"{prefix}/livenessProbe/periodSeconds", 10),
        (av, kind, f"{prefix}/livenessProbe/initialDelaySeconds", 0),
        (av, kind, f"{prefix}/readinessProbe/failureThreshold", 3),
        (av, kind, f"{prefix}/readinessProbe/httpGet/scheme", "HTTP"),
        (av, kind, f"{prefix}/readinessProbe/httpGet/httpHeaders", []),
        (av, kind, f"{prefix}/readinessProbe/successThreshold", 1),
        (av, kind, f"{prefix}/readinessProbe/timeoutSeconds", 1),
        (av, kind, f"{prefix}/readinessProbe/periodSeconds", 10),
        (av, kind, f"{prefix}/readinessProbe/initialDelaySeconds", 0),
        (av, kind, f"{prefix}/startupProbe/httpGet/scheme", "HTTP"),
        (av, kind, f"{prefix}/startupProbe/httpGet/httpHeaders", []),
        (av, kind, f"{prefix}/startupProbe/successThreshold", 1),
        (av, kind, f"{prefix}/startupProbe/timeoutSeconds", 1),
        (av, kind, f"{prefix}/startupProbe/failureThreshold", 3),
        (av, kind, f"{prefix}/startupProbe/periodSeconds", 10),
        (av, kind, f"{prefix}/startupProbe/initialDelaySeconds", 0),
    )


def _pod_defaults(av: str, kind: str, spec: str) -> tuple[tuple[str, str, str, Any], ...]:
    container_rows = tuple(
        row
        for key in _POD_CONTAINER_KEYS
        for row in _container_defaults(av, kind, f"{spec}/{key}/*")
    )
    return (
        (av, kind, f"{spec}/dnsPolicy", "ClusterFirst"),
        (av, kind, f"{spec}/schedulerName", "default-scheduler"),
        (av, kind, f"{spec}/securityContext", {}),
        (av, kind, f"{spec}/terminationGracePeriodSeconds", 30),
        (av, kind, f"{spec}/restartPolicy", "Always"),
        (av, kind, f"{spec}/hostNetwork", False),
        (av, kind, f"{spec}/hostIPC", False),
        (av, kind, f"{spec}/initContainers", []),
        (av, kind, f"{spec}/volumes/*/configMap/defaultMode", 420),
        (av, kind, f"{spec}/volumes/*/secret/defaultMode", 420),
        (av, kind, f"{spec}/volumes/*/projected/defaultMode", 420),
        # HostPathVolumeSource.type omitempty default is the empty string
        # (api/core/v1/types.go).
        (av, kind, f"{spec}/volumes/*/hostPath/type", ""),
        *container_rows,
    )


def _cluster_defaults() -> tuple[tuple[str, str, str, Any], ...]:
    av, kind = "postgresql.cnpg.io/v1", "Cluster"
    parameters = (
        ("archive_mode", "on"),
        ("archive_timeout", "5min"),
        ("dynamic_shared_memory_type", "posix"),
        ("full_page_writes", "on"),
        ("log_destination", "csvlog"),
        ("log_directory", "/controller/log"),
        ("log_filename", "postgres"),
        ("log_rotation_age", "0"),
        ("log_rotation_size", "0"),
        ("log_truncate_on_rotation", "false"),
        ("logging_collector", "on"),
        ("max_parallel_workers", "32"),
        ("max_replication_slots", "32"),
        ("max_worker_processes", "32"),
        ("shared_memory_type", "mmap"),
        ("shared_preload_libraries", ""),
        ("ssl_max_protocol_version", "TLSv1.3"),
        ("ssl_min_protocol_version", "TLSv1.3"),
        ("wal_keep_size", "512MB"),
        ("wal_level", "logical"),
        ("wal_log_hints", "on"),
        ("wal_receiver_timeout", "5s"),
        ("wal_sender_timeout", "5s"),
    )
    return (
        (av, kind, "/spec/affinity/podAntiAffinityType", "preferred"),
        (av, kind, "/spec/bootstrap/initdb/encoding", "UTF8"),
        (av, kind, "/spec/bootstrap/initdb/localeCType", "C"),
        (av, kind, "/spec/bootstrap/initdb/localeCollate", "C"),
        (av, kind, "/spec/enablePDB", True),
        (av, kind, "/spec/failoverDelay", 0),
        (av, kind, "/spec/logLevel", "info"),
        (av, kind, "/spec/maxSyncReplicas", 0),
        (av, kind, "/spec/minSyncReplicas", 0),
        (av, kind, "/spec/monitoring/customQueriesConfigMap",
         [{"key": "queries", "name": "cnpg-default-monitoring"}]),
        (av, kind, "/spec/monitoring/disableDefaultQueries", False),
        (av, kind, "/spec/monitoring/enablePodMonitor", False),
        (av, kind, "/spec/postgresGID", 26),
        (av, kind, "/spec/postgresUID", 26),
        (av, kind, "/spec/postgresql/syncReplicaElectionConstraint/enabled", False),
        (av, kind, "/spec/primaryUpdateMethod", "restart"),
        (av, kind, "/spec/probes/liveness/isolationCheck/connectionTimeout", 1000),
        (av, kind, "/spec/probes/liveness/isolationCheck/enabled", True),
        (av, kind, "/spec/probes/liveness/isolationCheck/requestTimeout", 1000),
        (av, kind, "/spec/replicationSlots/highAvailability/enabled", True),
        (av, kind, "/spec/replicationSlots/highAvailability/slotPrefix", "_cnpg_"),
        (av, kind, "/spec/replicationSlots/synchronizeReplicas/enabled", True),
        (av, kind, "/spec/replicationSlots/updateInterval", 30),
        (av, kind, "/spec/smartShutdownTimeout", 180),
        (av, kind, "/spec/startDelay", 3600),
        (av, kind, "/spec/stopDelay", 1800),
        (av, kind, "/spec/storage/resizeInUseVolumes", True),
        (av, kind, "/spec/switchoverDelay", 3600),
        *(
            (av, kind, f"/spec/postgresql/parameters/{name}", value)
            for name, value in parameters
        ),
    )


def _pod_quantity(av: str, kind: str, spec: str) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (av, kind, f"{spec}/{key}/*/resources/{bucket}/*")
        for key in _POD_CONTAINER_KEYS
        for bucket in ("limits", "requests")
    )


_DEFAULTS: tuple[tuple[str, str, str, Any], ...] = (
    *_pod_defaults("apps/v1", "Deployment", "/spec/template/spec"),
    ("apps/v1", "Deployment", "/spec/template/spec/restartPolicy", "Always"),
    ("apps/v1", "Deployment", "/spec/progressDeadlineSeconds", 600),
    ("apps/v1", "Deployment", "/spec/revisionHistoryLimit", 10),
    ("apps/v1", "Deployment", "/spec/minReadySeconds", 0),
    ("apps/v1", "Deployment", "/spec/strategy", _ROLLING),
    ("apps/v1", "Deployment", "/spec/strategy/rollingUpdate/maxSurge", "25%"),
    ("apps/v1", "Deployment", "/spec/strategy/rollingUpdate/maxUnavailable", "25%"),
    *_pod_defaults("apps/v1", "StatefulSet", "/spec/template/spec"),
    ("apps/v1", "StatefulSet", "/spec/revisionHistoryLimit", 10),
    # apps/v1 StatefulSet: podManagementPolicy OrderedReady, PVC retention Retain
    # (pkg/apis/apps/v1/defaults.go SetDefaults_StatefulSet).
    ("apps/v1", "StatefulSet", "/spec/podManagementPolicy", "OrderedReady"),
    ("apps/v1", "StatefulSet",
     "/spec/persistentVolumeClaimRetentionPolicy/whenDeleted", "Retain"),
    ("apps/v1", "StatefulSet",
     "/spec/persistentVolumeClaimRetentionPolicy/whenScaled", "Retain"),
    ("apps/v1", "StatefulSet", "/spec/updateStrategy/type", "RollingUpdate"),
    ("apps/v1", "StatefulSet", "/spec/volumeClaimTemplates/*/spec/volumeMode",
     "Filesystem"),
    *_pod_defaults("apps/v1", "DaemonSet", "/spec/template/spec"),
    ("apps/v1", "DaemonSet", "/spec/revisionHistoryLimit", 10),
    ("apps/v1", "DaemonSet", "/spec/updateStrategy", _DS_ROLLING),
    ("apps/v1", "DaemonSet", "/spec/updateStrategy/type", "RollingUpdate"),
    ("apps/v1", "DaemonSet", "/spec/updateStrategy/rollingUpdate/maxSurge", 0),
    ("apps/v1", "DaemonSet", "/spec/updateStrategy/rollingUpdate/maxUnavailable", 1),
    *_pod_defaults(
        "batch/v1", "CronJob", "/spec/jobTemplate/spec/template/spec",
    ),
    ("batch/v1", "CronJob", "/spec/concurrencyPolicy", "Allow"),
    ("batch/v1", "CronJob", "/spec/successfulJobsHistoryLimit", 3),
    ("batch/v1", "CronJob", "/spec/failedJobsHistoryLimit", 1),
    ("batch/v1", "CronJob", "/spec/suspend", False),
    ("batch/v1", "CronJob", "/spec/jobTemplate/spec/backoffLimit", 6),
    ("batch/v1", "CronJob", "/spec/jobTemplate/spec/completions", 1),
    ("batch/v1", "CronJob", "/spec/jobTemplate/spec/parallelism", 1),
    ("batch/v1", "CronJob", "/spec/jobTemplate/spec/completionMode", "NonIndexed"),
    *_pod_defaults("batch/v1", "Job", "/spec/template/spec"),
    ("batch/v1", "Job", "/spec/backoffLimit", 6),
    ("batch/v1", "Job", "/spec/completions", 1),
    ("batch/v1", "Job", "/spec/parallelism", 1),
    ("batch/v1", "Job", "/spec/completionMode", "NonIndexed"),
    ("batch/v1", "Job", "/spec/manualSelector", False),
    ("batch/v1", "Job", "/spec/podReplacementPolicy", "TerminatingOrFailed"),
    ("batch/v1", "Job", "/spec/suspend", False),
    ("v1", "Service", "/spec/type", "ClusterIP"),
    ("v1", "Service", "/spec/sessionAffinity", "None"),
    ("v1", "Service", "/spec/ports/*/protocol", "TCP"),
    # LoadBalancer Service: allocateLoadBalancerNodePorts defaults true
    # (pkg/apis/core/v1/defaults.go SetDefaults_Service).
    ("v1", "Service", "/spec/allocateLoadBalancerNodePorts", True),
    ("v1", "Service", "/spec/publishNotReadyAddresses", False),
    ("v1", "Secret", "/type", "Opaque"),
    ("v1", "PersistentVolumeClaim", "/spec/volumeMode", "Filesystem"),
    # kubernetes DefaultStorageClass admission controller fills
    # spec.storageClassName when the author omits it; any live value is
    # accepted only when the render omits the field.
    ("v1", "PersistentVolumeClaim", "/spec/storageClassName", _ANY_VALUE),
    # scheduling.k8s.io/v1 PriorityClass: globalDefault false, preemptionPolicy
    # PreemptLowerPriority (pkg/apis/scheduling/v1/defaults.go).
    ("scheduling.k8s.io/v1", "PriorityClass", "/globalDefault", False),
    ("scheduling.k8s.io/v1", "PriorityClass", "/preemptionPolicy",
     "PreemptLowerPriority"),
    # apiextensions conversion.strategy defaults to None
    # (pkg/apis/apiextensions/v1/defaults.go SetDefaults_CustomResourceDefinitionSpec).
    ("apiextensions.k8s.io/v1", "CustomResourceDefinition",
     "/spec/conversion/strategy", "None"),
    *_webhook_defaults(
        "admissionregistration.k8s.io/v1", "ValidatingWebhookConfiguration",
    ),
    *_webhook_defaults(
        "admissionregistration.k8s.io/v1", "MutatingWebhookConfiguration",
    ),
    # ValidatingAdmissionPolicy matchPolicy Equivalent, resourceRules.scope '*'
    # (pkg/apis/admissionregistration/v1/defaults.go).
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy",
     "/spec/matchConstraints/matchPolicy", "Equivalent"),
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy",
     "/spec/matchConstraints/resourceRules/*/scope", "*"),
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding",
     "/spec/matchResources/matchPolicy", "Equivalent"),
    ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding",
     "/spec/matchResources/resourceRules/*/scope", "*"),
    # ExternalSecret CRD schema default mergePolicy=Replace (live CRD
    # spec.versions[].schema.openAPIV3Schema.properties.spec.properties.target.
    # properties.template.properties.mergePolicy.default).
    ("external-secrets.io/v1", "ExternalSecret",
     "/spec/target/template/mergePolicy", "Replace"),
    # ClusterSecretStore CRD schema default expandSecretReferences=true (live CRD
    # spec.provider.infisical.secretsScope.expandSecretReferences.default).
    ("external-secrets.io/v1", "ClusterSecretStore",
     "/spec/provider/infisical/secretsScope/expandSecretReferences", True),
    # Password CRD schema default encoding=raw (live CRD
    # passwords.generators.external-secrets.io spec.encoding.default).
    ("generators.external-secrets.io/v1alpha1", "Password",
     "/spec/encoding", "raw"),
    ("networking.k8s.io/v1", "NetworkPolicy", "/spec/ingress/*/ports/*/protocol", "TCP"),
    ("networking.k8s.io/v1", "NetworkPolicy", "/spec/egress/*/ports/*/protocol", "TCP"),
    # Empty ingress/egress vs omitted is the same deny-all when policyTypes lists them.
    ("networking.k8s.io/v1", "NetworkPolicy", "/spec/ingress", []),
    ("networking.k8s.io/v1", "NetworkPolicy", "/spec/egress", []),
    *_cluster_defaults(),
    ("barmancloud.cnpg.io/v1", "ObjectStore",
     "/spec/instanceSidecarConfiguration/logLevel", "info"),
    ("barmancloud.cnpg.io/v1", "ObjectStore",
     "/spec/instanceSidecarConfiguration/retentionPolicyIntervalSeconds", 1800),
    ("v1", "PersistentVolumeClaim", "/metadata/finalizers",
     ["kubernetes.io/pvc-protection"]),
    ("external-secrets.io/v1", "ExternalSecret", "/metadata/finalizers",
     ["externalsecrets.external-secrets.io/externalsecret-cleanup"]),
    ("v1", "Namespace", "/spec/finalizers", ["kubernetes"]),
    # prometheus-operator Probe CRD defaults prober.path to the blackbox /probe.
    ("monitoring.coreos.com/v1", "Probe", "/spec/prober/path", "/probe"),
)

_OBJECT_IGNORE = (
    "/status",
    "/metadata/uid",
    "/metadata/resourceVersion",
    "/metadata/resource_version",
    "/metadata/generation",
    "/metadata/creationTimestamp",
    "/metadata/creation_timestamp",
    "/metadata/managedFields",
    "/metadata/selfLink",
)
_SERVICE_IGNORE = (
    "/spec/clusterIP",
    "/spec/clusterIPs",
    "/spec/ipFamilies",
    "/spec/ipFamilyPolicy",
    "/spec/internalTrafficPolicy",
    "/spec/ports/*/nodePort",  # API server allocates NodePort
    "/spec/healthCheckNodePort",  # API server allocates health-check NodePort
    # Controllers stamp Service finalizers (LoadBalancer cleanup, CNPG plugin).
    "/metadata/finalizers",
)
_PVC_IGNORE = (
    "/spec/volumeName",  # persistent volume binder; live-only binding
    "/metadata/annotations/pv.kubernetes.io~1bind-completed",  # persistent volume binder
    "/metadata/annotations/pv.kubernetes.io~1bound-by-controller",  # persistent volume binder
    "/metadata/annotations/volume.beta.kubernetes.io~1storage-provisioner",  # kubelet/external provisioner
    "/metadata/annotations/volume.kubernetes.io~1selected-node",  # persistent volume binder
    "/metadata/annotations/volume.kubernetes.io~1storage-provisioner",  # kubelet/external provisioner
)
_PODSPEC_PATHS = {
    "Deployment": "/spec/template/spec",
    "StatefulSet": "/spec/template/spec",
    "DaemonSet": "/spec/template/spec",
    "Job": "/spec/template/spec",
    "CronJob": "/spec/jobTemplate/spec/template/spec",
}
_JOB_LABEL_KEYS = (
    "controller-uid",
    "job-name",
    "batch.kubernetes.io~1controller-uid",
    "batch.kubernetes.io~1job-name",
)
# Render-time / restart stamps. The platform applier strips these before apply
# so a re-render cannot roll a pod whose compare already ignores the path.
RENDER_STAMP_ANNOTATIONS: tuple[str, ...] = (
    "updatedAt",
    "kubectl.kubernetes.io/restartedAt",
)
_RENDER_STAMP_WORKLOADS: tuple[tuple[str, str], ...] = (
    ("apps/v1", "Deployment"),
    ("apps/v1", "StatefulSet"),
    ("apps/v1", "DaemonSet"),
)


def _stamp_annotation_pointer(prefix: str, key: str) -> str:
    return f"{prefix}/{key.replace('~', '~0').replace('/', '~1')}"


def _ignore_rows() -> tuple[tuple[str, str, str], ...]:
    rows: list[tuple[str, str, str]] = []
    kinds = list(PLATFORM_LIVE_GVKS) + [
        ("v1", "Namespace"),
        ("v1", "Secret"),
        ("apiextensions.k8s.io/v1", "CustomResourceDefinition"),
        ("admissionregistration.k8s.io/v1", "ValidatingWebhookConfiguration"),
        ("admissionregistration.k8s.io/v1", "MutatingWebhookConfiguration"),
        ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicy"),
        ("admissionregistration.k8s.io/v1", "ValidatingAdmissionPolicyBinding"),
        ("rbac.authorization.k8s.io/v1", "ClusterRole"),
        ("rbac.authorization.k8s.io/v1", "ClusterRoleBinding"),
        ("scheduling.k8s.io/v1", "PriorityClass"),
        ("cert-manager.io/v1", "ClusterIssuer"),
        ("cert-manager.io/v1", "Issuer"),
        ("gateway.networking.k8s.io/v1", "Gateway"),
        ("external-secrets.io/v1", "ClusterSecretStore"),
        ("monitoring.coreos.com/v1", "Prometheus"),
        ("monitoring.coreos.com/v1", "Alertmanager"),
        ("monitoring.coreos.com/v1", "PrometheusRule"),
        ("generators.external-secrets.io/v1alpha1", "Password"),
    ]
    for av, kind in kinds:
        for pointer in _OBJECT_IGNORE:
            rows.append((av, kind, pointer))
    for pointer in _SERVICE_IGNORE:
        rows.append(("v1", "Service", pointer))
    for pointer in _PVC_IGNORE:
        rows.append(("v1", "PersistentVolumeClaim", pointer))
    # controller-gen (sigs.k8s.io/controller-tools) stamps this annotation when
    # generating CRDs; the apiserver keeps the live annotation on apply when the
    # chart omits it (annotation-key merge, not a chart value).
    rows.append((
        "apiextensions.k8s.io/v1", "CustomResourceDefinition",
        "/metadata/annotations/controller-gen.kubebuilder.io~1version",
    ))
    # cert-manager cainjector writes webhooks[].clientConfig.caBundle when the
    # object carries cert-manager.io/inject-ca-from or
    # cert-manager.io/inject-ca-from-secret (cainjector controller, not the chart).
    rows.append((
        "admissionregistration.k8s.io/v1", "ValidatingWebhookConfiguration",
        "/webhooks/*/clientConfig/caBundle",
    ))
    rows.append((
        "admissionregistration.k8s.io/v1", "MutatingWebhookConfiguration",
        "/webhooks/*/clientConfig/caBundle",
    ))
    # cert-manager cainjector also fills CRD conversion webhook caBundle
    # (cert-manager.io/inject-ca-from on the CRD).
    rows.append((
        "apiextensions.k8s.io/v1", "CustomResourceDefinition",
        "/spec/conversion/webhook/clientConfig/caBundle",
    ))
    # DaemonSet controller stamps deprecated.daemonset.template.generation.
    rows.append((
        "apps/v1", "DaemonSet",
        "/metadata/annotations/deprecated.daemonset.template.generation",
    ))
    # prometheus-operator validating webhook stamps prometheus-operator-validated.
    rows.append((
        "monitoring.coreos.com/v1", "PrometheusRule",
        "/metadata/annotations/prometheus-operator-validated",
    ))
    # ESO force-sync annotation is a controller/CLI stamp, not chart desired state.
    rows.append((
        "external-secrets.io/v1", "ExternalSecret",
        "/metadata/annotations/force-sync",
    ))
    rows.append((
        "external-secrets.io/v1", "ClusterSecretStore",
        "/metadata/annotations/force-sync",
    ))
    # Render-time / restart stamps named in RENDER_STAMP_ANNOTATIONS: kubectl
    # rollout restart writes restartedAt on the pod template; charts also stamp
    # updatedAt on Deployment object metadata.
    for av, kind in _RENDER_STAMP_WORKLOADS:
        for stamp in RENDER_STAMP_ANNOTATIONS:
            rows.append((
                av, kind,
                _stamp_annotation_pointer("/spec/template/metadata/annotations", stamp),
            ))
        if kind == "Deployment" and "updatedAt" in RENDER_STAMP_ANNOTATIONS:
            rows.append((
                av, kind,
                _stamp_annotation_pointer("/metadata/annotations", "updatedAt"),
            ))
    # StatefulSet controller fills volumeClaimTemplates[].status.
    rows.append((
        "apps/v1", "StatefulSet", "/spec/volumeClaimTemplates/*/status",
    ))
    for prefix in (
        "/metadata/labels",
        "/spec/selector/matchLabels",
        "/spec/template/metadata/labels",
    ):
        for key in _JOB_LABEL_KEYS:
            rows.append(("batch/v1", "Job", f"{prefix}/{key}"))
    for prefix in (
        "/metadata/labels",
        "/spec/jobTemplate/spec/selector/matchLabels",
        "/spec/jobTemplate/spec/template/metadata/labels",
    ):
        for key in _JOB_LABEL_KEYS:
            rows.append(("batch/v1", "CronJob", f"{prefix}/{key}"))
    # API server Canonicalize (pkg/registry/core/namespace/strategy.go): the
    # label is server-set, immutable and always equals metadata.name; ignoring
    # it hides nothing an author can express.
    rows.append(("v1", "Namespace", "/metadata/labels/kubernetes.io~1metadata.name"))
    # OpenTelemetry operator defaulting webhook: stamps default images as
    # annotations and fills spec.<lang>.image when the author omits it. Values
    # follow the operator version, not the catalog.
    for lang in (
        "apache-httpd", "dotnet", "go", "java", "nginx", "nodejs", "python",
    ):
        rows.append((
            "opentelemetry.io/v1alpha1", "Instrumentation",
            "/metadata/annotations/instrumentation.opentelemetry.io"
            f"~1default-auto-instrumentation-{lang}-image",
        ))
    for pointer in (
        "/spec/python/image",
        "/spec/dotnet/image",
        "/spec/java/image",
        "/spec/nodejs/image",
        # Unused-language specs the webhook injects in full (same shape as /status).
        "/spec/apacheHttpd",
        "/spec/go",
        "/spec/nginx",
    ):
        rows.append(("opentelemetry.io/v1alpha1", "Instrumentation", pointer))
    return tuple(rows)


_IGNORE: tuple[tuple[str, str, str], ...] = _ignore_rows()

_QUANTITY: tuple[tuple[str, str, str], ...] = (
    *_pod_quantity("apps/v1", "Deployment", "/spec/template/spec"),
    *_pod_quantity("apps/v1", "StatefulSet", "/spec/template/spec"),
    *_pod_quantity("apps/v1", "DaemonSet", "/spec/template/spec"),
    *_pod_quantity(
        "batch/v1", "CronJob", "/spec/jobTemplate/spec/template/spec",
    ),
    *_pod_quantity("batch/v1", "Job", "/spec/template/spec"),
    ("v1", "ResourceQuota", "/spec/hard/*"),
    ("v1", "PersistentVolumeClaim", "/spec/resources/requests/storage"),
    ("postgresql.cnpg.io/v1", "Cluster", "/spec/resources/requests/*"),
    ("postgresql.cnpg.io/v1", "Cluster", "/spec/resources/limits/*"),
    ("postgresql.cnpg.io/v1", "Cluster", "/spec/storage/size"),
    ("barmancloud.cnpg.io/v1", "ObjectStore",
     "/spec/instanceSidecarConfiguration/resources/requests/*"),
    ("barmancloud.cnpg.io/v1", "ObjectStore",
     "/spec/instanceSidecarConfiguration/resources/limits/*"),
    ("opentelemetry.io/v1alpha1", "Instrumentation",
     "/spec/python/resourceRequirements/requests/*"),
    ("opentelemetry.io/v1alpha1", "Instrumentation",
     "/spec/python/resourceRequirements/limits/*"),
    ("opentelemetry.io/v1alpha1", "Instrumentation",
     "/spec/dotnet/resourceRequirements/requests/*"),
    ("opentelemetry.io/v1alpha1", "Instrumentation",
     "/spec/dotnet/resourceRequirements/limits/*"),
    ("opentelemetry.io/v1alpha1", "Instrumentation",
     "/spec/nodejs/resourceRequirements/requests/*"),
    ("opentelemetry.io/v1alpha1", "Instrumentation",
     "/spec/nodejs/resourceRequirements/limits/*"),
    ("opentelemetry.io/v1alpha1", "Instrumentation",
     "/spec/java/resources/requests/*"),
    ("opentelemetry.io/v1alpha1", "Instrumentation",
     "/spec/java/resources/limits/*"),
)

_pair_mu = threading.Lock()
_pair_slots: dict[tuple[str, str], "_PairSlot"] = {}


DIFF_VALUE_LIMIT = 200


class ObjectRef(BaseModel):
    kind: str
    namespace: str = ""
    name: str


class PathDiff(BaseModel):
    path: str
    rendered: str
    live: str


class ChangedRef(BaseModel):
    kind: str
    namespace: str = ""
    name: str
    paths: list[str] = Field(default_factory=list)
    diffs: list[PathDiff] = Field(default_factory=list)


class OwnedRef(BaseModel):
    kind: str
    namespace: str = ""
    name: str
    owner: ObjectRef


class GeneratedRef(BaseModel):
    kind: str
    namespace: str = ""
    name: str
    keys: list[str] = Field(default_factory=list)


class ShadowReport(BaseModel):
    project: str
    env: str
    empty: bool
    rendered: list[ObjectRef] = Field(default_factory=list)
    live_matched: list[ObjectRef] = Field(default_factory=list)
    missing: list[ObjectRef] = Field(default_factory=list)
    extra: list[ObjectRef] = Field(default_factory=list)
    owned: list[OwnedRef] = Field(default_factory=list)
    hooks: list[ObjectRef] = Field(default_factory=list)
    generated: list[GeneratedRef] = Field(default_factory=list)
    changed: list[ChangedRef] = Field(default_factory=list)
    render_sha: str = ""
    streak_days: int = 0
    path: str | None = None
    report_path: str | None = None
    date: str | None = None
    error: str | None = None


def state_dir_of(state: AppState) -> Path:
    if state.cfg.state_dir:
        return Path(state.cfg.state_dir)
    return Path(state.cfg.signing_key_path).resolve().parent


# The catalog is untrusted input: a pair's ids build a report directory and a label
# selector, so only a catalog id (the same grammar the tools apply to their arguments)
# is accepted here; anything else is skipped with a logged reason.
PAIR_ID_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?")


def _pair_id_ok(value: Any) -> bool:
    return isinstance(value, str) and PAIR_ID_RE.fullmatch(value) is not None


def pair_ok(project: Any, env: Any) -> bool:
    """True when both ids are catalog ids: the only pairs diff() accepts."""
    return _pair_id_ok(project) and _pair_id_ok(env)


def renders_pair(entry: dict[str, Any], env: str) -> bool:
    """The generator's own rule, shared by the reconcile loop and `orbit onboard status`:
    a project hosted elsewhere or managed elsewhere renders nothing; a non-prod
    environment renders only with a truthy `enabled`."""
    if entry.get("hosted", "self") != "self" or entry.get("managed_elsewhere"):
        return False
    if env == "prod":
        return True
    environments = entry.get("environments")
    spec = environments.get(env) if isinstance(environments, dict) else None
    return bool(spec.get("enabled")) if isinstance(spec, dict) else False


def catalog_pairs(state: AppState, *, rendered: bool = False) -> list[tuple[str, str]]:
    """Every declared (project, env) whose ids are catalog ids; with `rendered`, only the
    pairs the generator renders (see renders_pair)."""
    pairs: list[tuple[str, str]] = []
    for entry in project_entries(state):
        pid = entry.get("id")
        if not pid:
            continue
        if not _pair_id_ok(pid):
            log.warning("shadow: skipping a catalog project whose id is not a catalog id")
            continue
        for env in env_names(entry):
            if not _pair_id_ok(env):
                log.warning("shadow: skipping an environment of %s whose name is not a catalog id", pid)
                continue
            if rendered and not renders_pair(entry, env):
                continue
            pairs.append((pid, env))
    return pairs


class _PairSlot:
    def __init__(self) -> None:
        self.gate = threading.Lock()
        self.event = threading.Event()
        self.busy = False
        self.report: ShadowReport | None = None
        self.error: BaseException | None = None
        self.gen = 0


def _slot_for(project: str, env: str) -> _PairSlot:
    key = (project, env)
    with _pair_mu:
        slot = _pair_slots.get(key)
        if slot is None:
            slot = _PairSlot()
            _pair_slots[key] = slot
        return slot


def diff(state: AppState, project: str, env: str, *,
         clock: Any | None = None) -> ShadowReport:
    """Render, compare to live, persist today's report, fill streak_days.

    One in-flight run per project/env: an overlapping call waits and returns
    that run's report rather than rendering twice.
    """
    if not pair_ok(project, env):
        # before any live read: the ids build a label selector and a report path
        raise ValueError("shadow diff needs catalog ids for project and env")
    slot = _slot_for(project, env)
    with slot.gate:
        if slot.busy:
            gen = slot.gen
            event = slot.event
            waiting = True
        else:
            waiting = False
            slot.busy = True
            slot.event = threading.Event()
            slot.gen += 1
            gen = slot.gen
            slot.report = None
            slot.error = None
    if waiting:
        event.wait()
        with slot.gate:
            if slot.gen == gen:
                if slot.error is not None:
                    raise slot.error
                if slot.report is not None:
                    return slot.report
        return diff(state, project, env, clock=clock)
    try:
        report = _diff_unlocked(state, project, env, clock=clock)
        with slot.gate:
            if slot.gen == gen:
                slot.report = report
        return report
    except BaseException as e:
        with slot.gate:
            if slot.gen == gen:
                slot.error = e
        raise
    finally:
        with slot.gate:
            if slot.gen == gen:
                slot.busy = False
                slot.event.set()


def _diff_unlocked(state: AppState, project: str, env: str, *,
                   clock: Any | None = None) -> ShadowReport:
    objects, sha = render_desired(state, project, env)
    live_objs = read_live(state, objects, project, env)
    report = compare_objects(
        objects, live_objs, project=project, env=env, render_sha=sha,
    )
    report.date = _utc_today(clock).isoformat()
    stored = _write_report(state, report, clock=clock)
    report.path = str(stored)
    report.report_path = str(stored)
    report.streak_days = streak_days(state, project, env, clock=clock)
    return report


def read_live(state: AppState, rendered: list[dict[str, Any]],
              project: str, env: str = "", *,
              kinds: tuple[tuple[str, str], ...] = LIVE_GVKS) -> list[dict[str, Any]]:
    """Live objects for the rendered set plus labelled extras per namespace. No writes."""
    return list(_live_index(state, rendered, project, env, kinds=kinds).values())


def compare_objects(
    rendered: list[dict[str, Any]],
    live: list[dict[str, Any]],
    *,
    project: str = "",
    env: str = "",
    render_sha: str = "",
    generated_secret_names: set[str] | frozenset[str] | None = None,
) -> ShadowReport:
    """Normalise and diff two object lists. No kube, no instance dir, no persist."""
    rendered = [deepcopy(obj) for obj in rendered if isinstance(obj, dict)]
    live = [deepcopy(obj) for obj in live if isinstance(obj, dict)]
    named_generated = frozenset(generated_secret_names or ())
    dest_ns = _dest_namespace(rendered) or _dest_namespace(live)
    if dest_ns:
        rendered = [_apply_namespace(obj, dest_ns) for obj in rendered]
    live_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    live_by_kn: dict[tuple[str, str], dict[str, Any]] = {}
    for obj in live:
        live_by_key[_obj_key(obj)] = obj
        live_by_kn.setdefault(_kind_name(obj), obj)
    rendered_by_kn = {_kind_name(obj): obj for obj in rendered}
    instance_vals = _instance_values(rendered)
    if project:
        instance_vals.add(project)
    rendered_refs: list[ObjectRef] = []
    matched: list[ObjectRef] = []
    missing: list[ObjectRef] = []
    changed: list[ChangedRef] = []
    generated: list[GeneratedRef] = []
    for obj in rendered:
        ref = _ref(obj)
        rendered_refs.append(ref)
        key = _obj_key(obj)
        live_obj = live_by_key.pop(key, None)
        if live_obj is None:
            live_obj = live_by_kn.get(_kind_name(obj))
            if live_obj is not None:
                live_by_key.pop(_obj_key(live_obj), None)
        if live_obj is None:
            missing.append(ref)
            continue
        matched.append(ref)
        diffs = _changed_diffs(
            _normalise_live(obj), _normalise_live(live_obj), kind=ref.kind,
            api_version=str(obj.get("apiVersion") or live_obj.get("apiVersion") or ""),
        )
        gen_keys = _generated_data_keys(obj, live_obj, diffs, named_generated)
        if gen_keys:
            generated.append(GeneratedRef(
                kind=ref.kind, namespace=ref.namespace, name=ref.name,
                keys=gen_keys,
            ))
            drop = set(gen_keys)
            diffs = [d for d in diffs if _data_key_from_path(d.path) not in drop]
        if diffs:
            changed.append(ChangedRef(
                kind=ref.kind, namespace=ref.namespace, name=ref.name,
                paths=[d.path for d in diffs], diffs=diffs,
            ))
    extra: list[ObjectRef] = []
    owned: list[OwnedRef] = []
    hooks: list[ObjectRef] = []
    rendered_list = list(rendered)
    for _, obj in sorted(live_by_key.items()):
        ref = _ref(obj)
        if _is_helm_hook(obj):
            hooks.append(ref)
            continue
        owner = _owner_if_rendered(obj, rendered_by_kn)
        if owner is None:
            owner = _sts_volume_claim_owner(obj, rendered_list)
        if owner is not None:
            owned.append(OwnedRef(
                kind=ref.kind, namespace=ref.namespace, name=ref.name, owner=owner,
            ))
            continue
        if _is_labelled_extra(
            obj, instance_vals=instance_vals, project=project, env=env,
        ):
            extra.append(ref)
    if not render_sha:
        payload = json.dumps(rendered, sort_keys=True, separators=(",", ":"), default=str)
        render_sha = hashlib.sha256(payload.encode()).hexdigest()
    return ShadowReport(
        project=project, env=env,
        empty=not missing and not extra and not changed,
        rendered=rendered_refs, live_matched=matched,
        missing=missing, extra=extra, owned=owned, hooks=hooks,
        generated=generated, changed=changed,
        render_sha=render_sha,
    )


def streak_days(state: AppState, project: str, env: str, *,
                clock: Any | None = None) -> int:
    """Consecutive daily reports with empty=true ending today. Zero if today is missing or not empty."""
    today = _utc_today(clock)
    directory = _report_dir(state, project, env)
    n = 0
    day = today
    while True:
        path = directory / f"{day.isoformat()}.json"
        if not path.is_file():
            break
        try:
            body = json.loads(path.read_text())
        except (OSError, ValueError):
            break
        if not isinstance(body, dict) or body.get("empty") is not True:
            break
        n += 1
        day = day - timedelta(days=1)
    return n


def _write_report(state: AppState, report: ShadowReport, *, clock: Any | None) -> Path:
    directory = _report_dir(state, project=report.project, env=report.env)
    directory.mkdir(parents=True, exist_ok=True)
    day = _utc_today(clock).isoformat()
    if not report.date:
        report.date = day
    path = directory / f"{day}.json"
    payload = report.model_dump(mode="json")
    payload.pop("path", None)
    payload.pop("report_path", None)
    body = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(directory),
        prefix=f"{path.name}.", suffix=".tmp", delete=False,
    ) as tmp:
        tmp.write(body)
        tmp_name = tmp.name
    os.replace(tmp_name, path)
    return path


def _report_dir(state: AppState, project: str, env: str) -> Path:
    if not (_pair_id_ok(project) and _pair_id_ok(env)):
        raise ValueError("shadow report path needs catalog ids for project and env")
    return state_dir_of(state) / "shadow" / f"{project}-{env}"


def _utc_today(clock: Any | None):
    if clock is not None and hasattr(clock, "now"):
        return datetime.fromtimestamp(float(clock.now()), tz=timezone.utc).date()
    return datetime.now(timezone.utc).date()


def _ref(obj: dict[str, Any]) -> ObjectRef:
    meta = obj.get("metadata") or {}
    return ObjectRef(
        kind=str(obj.get("kind") or ""),
        namespace=str(meta.get("namespace") or ""),
        name=str(meta.get("name") or ""),
    )


def _obj_key(obj: dict[str, Any]) -> tuple[str, str, str]:
    meta = obj.get("metadata") or {}
    return (
        str(obj.get("kind") or ""),
        str(meta.get("namespace") or ""),
        str(meta.get("name") or ""),
    )


def _kind_name(obj: dict[str, Any]) -> tuple[str, str]:
    meta = obj.get("metadata") or {}
    return (str(obj.get("kind") or ""), str(meta.get("name") or ""))


def _apply_namespace(obj: dict[str, Any], dest_ns: str) -> dict[str, Any]:
    if obj.get("kind") in _CLUSTER_SCOPED or not dest_ns:
        return obj
    meta = obj.setdefault("metadata", {})
    if not meta.get("namespace"):
        meta["namespace"] = dest_ns
    return obj


def _instance_values(objects: list[dict[str, Any]]) -> set[str]:
    values: set[str] = set()
    for obj in objects:
        labels = (obj.get("metadata") or {}).get("labels") or {}
        value = labels.get(LABEL_INSTANCE)
        if value:
            values.add(str(value))
    return values


def _owner_if_rendered(
    obj: dict[str, Any],
    rendered_by_kn: dict[tuple[str, str], dict[str, Any]],
) -> ObjectRef | None:
    for ref in (obj.get("metadata") or {}).get("ownerReferences") or []:
        owner = rendered_by_kn.get((str(ref.get("kind") or ""), str(ref.get("name") or "")))
        if owner is not None:
            return _ref(owner)
    return None


def _is_helm_hook(obj: dict[str, Any]) -> bool:
    ann = (obj.get("metadata") or {}).get("annotations") or {}
    return bool(ann.get("helm.sh/hook"))


def _sts_volume_claim_owner(
    obj: dict[str, Any], rendered: list[dict[str, Any]],
) -> ObjectRef | None:
    """PVC from a StatefulSet volumeClaimTemplate: name <template>-<sts>-<n>."""
    if str(obj.get("kind") or "") != "PersistentVolumeClaim":
        return None
    if (obj.get("metadata") or {}).get("ownerReferences"):
        return None
    name = str((obj.get("metadata") or {}).get("name") or "")
    ns = str((obj.get("metadata") or {}).get("namespace") or "")
    labels = (obj.get("metadata") or {}).get("labels") or {}
    for sts in rendered:
        if str(sts.get("kind") or "") != "StatefulSet":
            continue
        sts_meta = sts.get("metadata") or {}
        if str(sts_meta.get("namespace") or "") != ns:
            continue
        sts_name = str(sts_meta.get("name") or "")
        spec = sts.get("spec") or {}
        selector = (spec.get("selector") or {}).get("matchLabels") or {}
        if selector and any(labels.get(k) != v for k, v in selector.items()):
            continue
        for tmpl in spec.get("volumeClaimTemplates") or []:
            tmpl_name = str(((tmpl.get("metadata") or {}).get("name") or ""))
            if not tmpl_name:
                continue
            prefix = f"{tmpl_name}-{sts_name}-"
            if name.startswith(prefix) and name[len(prefix):].isdigit():
                return _ref(sts)
    return None


def _live_index(state: AppState, rendered: list[dict[str, Any]],
                project: str, env: str = "", *,
                kinds: tuple[tuple[str, str], ...] = LIVE_GVKS) -> dict[tuple[str, str, str], dict[str, Any]]:
    dyn = state.backends.kube.dynamic
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for obj in rendered:
        live = _get_live(dyn, obj)
        if live is not None:
            ref = _ref(_as_dict(live))
            found[(ref.kind, ref.namespace, ref.name)] = _as_dict(live)
    for dest_ns in _dest_namespaces(rendered):
        for selector in _extra_selectors(dest_ns, project, env):
            for api_version, kind in _extra_gvks(rendered, kinds=kinds):
                for item in _list_live(dyn, api_version, kind, dest_ns, selector):
                    body = _as_dict(item)
                    ref = _ref(body)
                    key = (ref.kind, ref.namespace, ref.name)
                    found.setdefault(key, body)
    return found


def _extra_gvks(rendered: list[dict[str, Any]], *,
                kinds: tuple[tuple[str, str], ...] = LIVE_GVKS) -> tuple[tuple[str, str], ...]:
    """`kinds` for the extra scan, plus each rendered object's own GVK."""
    ordered: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pair in kinds:
        if pair[1] in _CLUSTER_SCOPED:
            continue
        if pair not in seen:
            seen.add(pair)
            ordered.append(pair)
    for obj in rendered:
        kind = str(obj.get("kind") or "")
        if not kind or kind in _CLUSTER_SCOPED:
            continue
        pair = (str(obj.get("apiVersion") or "v1"), kind)
        if pair not in seen:
            seen.add(pair)
            ordered.append(pair)
    return tuple(ordered)


def _dest_namespaces(rendered: list[dict[str, Any]]) -> list[str]:
    """Unique namespaces of namespaced objects, in first-seen order."""
    seen: list[str] = []
    for obj in rendered:
        if obj.get("kind") in _CLUSTER_SCOPED:
            continue
        ns = str((obj.get("metadata") or {}).get("namespace") or "")
        if ns and ns not in seen:
            seen.append(ns)
    return seen


def _dest_namespace(rendered: list[dict[str, Any]]) -> str:
    """Tenant destination namespace for filling missing metadata.namespace.

    Prefers a non-monitoring namespace so a dashboard/probe in `monitoring` cannot
    stamp the tenant objects. Falls back to the only namespace, else empty.
    """
    nss = _dest_namespaces(rendered)
    tenants = [n for n in nss if n != MONITORING_NS]
    if tenants:
        return tenants[0]
    return nss[0] if nss else ""


def _extras_selector(namespace: str, project: str, env: str) -> str:
    return _extra_selectors(namespace, project, env)[0]


def _extra_selectors(namespace: str, project: str, env: str) -> list[str]:
    if namespace == MONITORING_NS:
        return [f"{LABEL_PROJECT}={project},{LABEL_ENV}={env}"]
    out = [f"{LABEL_INSTANCE}={project}"]
    if project:
        out.append(f"{LABEL_ARGOCD_INSTANCE}={project}")
    return out


def _is_labelled_extra(
    obj: dict[str, Any], *, instance_vals: set[str], project: str, env: str,
) -> bool:
    labels = (obj.get("metadata") or {}).get("labels") or {}
    ns = str((obj.get("metadata") or {}).get("namespace") or "")
    if ns == MONITORING_NS:
        return (
            bool(project) and bool(env)
            and str(labels.get(LABEL_PROJECT) or "") == project
            and str(labels.get(LABEL_ENV) or "") == env
        )
    inst = labels.get(LABEL_INSTANCE)
    if instance_vals and inst in instance_vals:
        return True
    argocd_inst = str(labels.get(LABEL_ARGOCD_INSTANCE) or "")
    return bool(project) and argocd_inst == project


def _get_live(dyn: Any, obj: dict[str, Any]) -> Any | None:
    api_version = obj.get("apiVersion") or "v1"
    kind = obj.get("kind") or ""
    meta = obj.get("metadata") or {}
    name = meta.get("name") or ""
    namespace = meta.get("namespace") or ""
    try:
        resource = dyn.resources.get(api_version=api_version, kind=kind)
        kwargs: dict[str, Any] = {"name": name, "_request_timeout": KUBE_TIMEOUT}
        if namespace and kind not in _CLUSTER_SCOPED:
            kwargs["namespace"] = namespace
        return resource.get(**kwargs)
    except Exception as e:
        if _is_not_found(e):
            return None
        raise


def _list_live(dyn: Any, api_version: str, kind: str, namespace: str,
               label_selector: str) -> list[Any]:
    try:
        resource = dyn.resources.get(api_version=api_version, kind=kind)
        body = resource.get(
            namespace=namespace, label_selector=label_selector,
            _request_timeout=KUBE_TIMEOUT,
        )
    except Exception as e:
        if _is_not_found(e):
            return []
        raise
    if body is None:
        return []
    if isinstance(body, dict):
        return list(body.get("items") or [])
    items = getattr(body, "items", None)
    return list(items) if items else []


def _is_not_found(exc: BaseException) -> bool:
    name = exc.__class__.__name__
    if name in {"NotFoundError", "ResourceNotFoundError"}:
        return True
    return getattr(exc, "status", None) == 404


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            return data
    data = getattr(obj, "_data", None)
    return data if isinstance(data, dict) else {}


def _normalise_live(obj: Any) -> Any:
    if isinstance(obj, dict) and obj.get("kind") == "Secret":
        obj = _canonicalise_secret(obj)
    return _normalise(obj)


def _canonicalise_secret(obj: dict[str, Any]) -> dict[str, Any]:
    """API-server Secret behaviour: stringData merges into data; SA-token data is server-filled."""
    obj = dict(obj)
    meta = obj.get("metadata") or {}
    ann = (meta.get("annotations") or {}) if isinstance(meta, dict) else {}
    sa = (
        obj.get("type") == "kubernetes.io/service-account-token"
        or "kubernetes.io/service-account.name" in ann
    )
    string_data = obj.pop("stringData", None)
    if sa:
        obj.pop("data", None)
        if isinstance(meta, dict):
            meta = dict(meta)
            annotations = dict(ann)
            annotations.pop("kubernetes.io/service-account.uid", None)
            labels = dict(meta.get("labels") or {})
            labels.pop("kubernetes.io/legacy-token-last-used", None)
            if annotations:
                meta["annotations"] = annotations
            else:
                meta.pop("annotations", None)
            if labels:
                meta["labels"] = labels
            else:
                meta.pop("labels", None)
            obj["metadata"] = meta
        return obj
    if isinstance(string_data, dict):
        data = dict(obj.get("data") or {})
        for key, value in string_data.items():
            if key not in data:
                data[key] = base64.b64encode(str(value).encode()).decode()
        if data:
            obj["data"] = data
    return obj


def _normalise(obj: Any, *, in_metadata: bool = False) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            nv = _normalise(value, in_metadata=(in_metadata or key == "metadata"))
            if key == "metadata" and isinstance(nv, dict):
                nv = _strip_meta_stamps(nv)
            if isinstance(nv, dict) and not nv:
                continue
            out[key] = nv
        return out
    if isinstance(obj, list):
        return [_normalise(item, in_metadata=in_metadata) for item in obj]
    return obj


def _strip_meta_stamps(meta: dict[str, Any]) -> dict[str, Any]:
    out = dict(meta)
    for key in list(out):
        if key in _DROP_META:
            out.pop(key, None)
    ann = dict(out.get("annotations") or {})
    for key in list(ann):
        if key in _APPLIER_EXACT:
            ann.pop(key, None)
    if ann:
        out["annotations"] = ann
    else:
        out.pop("annotations", None)
    labels = dict(out.get("labels") or {})
    for key in list(labels):
        if key in _APPLIER_EXACT:
            labels.pop(key, None)
    if labels:
        out["labels"] = labels
    else:
        out.pop("labels", None)
    return out


def _encode_diff(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)[:DIFF_VALUE_LIMIT]


def _fingerprint(value: Any) -> str:
    """A short digest standing in for a Secret value in a report: drift is still named
    (the digests differ) and nothing that was in `.data` is ever written out."""
    body = json.dumps(value, sort_keys=True, default=str).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()[:16]


def _path_diff(path: str, rendered: Any, live: Any, *, kind: str = "") -> PathDiff:
    if kind == "Secret":
        # a Secret's values are compared, never reported: the report is persisted, returned
        # by a tool and printed by the offline runner
        # (a Kubernetes object is a mapping on both sides, so a diff is always keyed:
        # there is no root-level diff that could carry a whole Secret body)
        top = (path or "").strip("/").split("/", 1)[0]
        if top in ("data", "stringData"):
            return PathDiff(path=path or "/", rendered=_fingerprint(rendered), live=_fingerprint(live))
    return PathDiff(
        path=path or "/", rendered=_encode_diff(rendered), live=_encode_diff(live),
    )


def _service_target_port_default(
    key: str, lval: Any, rendered: dict[str, Any], live: dict[str, Any], *,
    path: str, kind: str,
) -> bool:
    """ServicePort.targetPort defaults to the port number (core/v1 SetDefaults_ServicePort)."""
    if kind != "Service" or key != "targetPort":
        return False
    segs = _pointer_segments(path)
    if segs[:2] != ["spec", "ports"]:
        return False
    port = rendered.get("port") if isinstance(rendered, dict) else None
    if port is None and isinstance(live, dict):
        port = live.get("port")
    return lval == port


def _service_account_alias(key: str, lval: Any, rendered: dict[str, Any],
                           live: dict[str, Any], *, path: str,
                           kind: str) -> bool:
    if key != "serviceAccount" or not isinstance(lval, str):
        return False
    if _PODSPEC_PATHS.get(kind) != path:
        return False
    name = live.get("serviceAccountName")
    if name is None:
        name = rendered.get("serviceAccountName")
    return name is not None and lval == name


def _changed_diffs(rendered: Any, live: Any, path: str = "", *,
                   kind: str = "", api_version: str = "") -> list[PathDiff]:
    if isinstance(rendered, dict) or isinstance(live, dict):
        if rendered is None:
            rendered = {}
        if live is None:
            live = {}
        if not isinstance(rendered, dict) or not isinstance(live, dict):
            return [_path_diff(path or "/", rendered, live, kind=kind)]
        out: list[PathDiff] = []
        for key in sorted(set(rendered) | set(live), key=str):
            child = f"{path}/{_escape(str(key))}"
            # Helm chart idiom: checksum/* on the pod template is derived from
            # templates that may be hooks we skip. The applier strips them before apply.
            if _is_hook_derived_checksum(path, key):
                continue
            # /status is controller-owned even when a chart copies a placeholder in.
            if key == "status" and _ignored(api_version, kind, child):
                continue
            # Annotation stamps in the ignore table (restartedAt, updatedAt, …)
            # differ by value when a chart re-renders the timestamp; still live-only.
            if _ignored(api_version, kind, child) and "/annotations/" in child:
                continue
            # CNPG barman-cloud plugin operator writes SIDECAR_IMAGE even when
            # the chart renders a placeholder at the same path.
            if kind == "Secret" and child == _SIDECAR_IMAGE_PATH:
                continue
            if key not in rendered and _ignored(api_version, kind, child):
                continue
            if key in rendered and key not in live:
                rval = rendered[key]
                if rval is None:
                    continue
                if isinstance(rval, dict) and not rval:
                    continue
                # A map the live object does not carry at all: what counts is what it
                # would hold after the keys this comparison ignores. A pod template
                # whose only annotation is a Helm `checksum/*` is the case: the applier
                # strips that key before apply, so a freshly applied object has no
                # annotations map and the render still does (seen on the test box: tempo
                # read as changed right after it was applied).
                if isinstance(rval, dict) and not _changed_diffs(
                        rval, {}, child, kind=kind, api_version=api_version):
                    continue
                found, allowed = _default_at(api_version, kind, child)
                if found and rval == allowed:
                    continue
                out.append(_path_diff(child, rval, None, kind=kind))
                continue
            if key not in rendered and key in live:
                lval = live[key]
                found, allowed = _default_at(api_version, kind, child)
                if found and (allowed is _ANY_VALUE or lval == allowed):
                    continue
                if _service_account_alias(
                    key, lval, rendered, live, path=path, kind=kind,
                ):
                    continue
                if _service_target_port_default(
                    key, lval, rendered, live, path=path, kind=kind,
                ):
                    continue
                if isinstance(lval, dict):
                    out.extend(_changed_diffs(
                        {}, lval, child, kind=kind, api_version=api_version,
                    ))
                    continue
                out.append(_path_diff(child, None, lval, kind=kind))
                continue
            out.extend(_changed_diffs(
                rendered[key], live[key], child, kind=kind, api_version=api_version,
            ))
        return out
    if isinstance(rendered, list) or isinstance(live, list):
        if not isinstance(rendered, list) or not isinstance(live, list):
            return [_path_diff(path or "/", rendered, live, kind=kind)]
        if len(live) != len(rendered):
            return [_path_diff(path or "/", rendered, live, kind=kind)]
        out: list[PathDiff] = []
        for i, rval in enumerate(rendered):
            out.extend(_changed_diffs(
                rval, live[i], f"{path}/{i}", kind=kind, api_version=api_version,
            ))
        return out
    if _values_equal(rendered, live, path, api_version=api_version, kind=kind):
        return []
    return [_path_diff(path or "/", rendered, live, kind=kind)]


def _changed_paths(rendered: Any, live: Any, path: str = "", *,
                   kind: str = "", api_version: str = "") -> list[str]:
    return [d.path for d in _changed_diffs(
        rendered, live, path, kind=kind, api_version=api_version,
    )]


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _pointer_segments(path: str) -> list[str]:
    if not path or path == "/":
        return []
    return [_unescape(p) for p in path.lstrip("/").split("/")]


def _seg_match(pattern: list[str], segments: list[str]) -> bool:
    return len(pattern) == len(segments) and all(
        p == "*" or p == s for p, s in zip(pattern, segments)
    )


def _path_match(pattern: str, pointer: str, *, subtree: bool = False) -> bool:
    pat = _pointer_segments(pattern)
    got = _pointer_segments(pointer)
    if subtree:
        if len(got) < len(pat):
            return False
        return _seg_match(pat, got[:len(pat)])
    return _seg_match(pat, got)


def _ignored(api_version: str, kind: str, pointer: str) -> bool:
    # server-managed fields (status, uid, resourceVersion, managedFields, …) are ignored
    # for EVERY kind: an allowlist of kinds made any kind the instance renders but the
    # table does not enumerate (an ExternalSecret, say) permanent drift, with its
    # controller's status messages copied into the report
    if any(_path_match(pattern, pointer, subtree=True) for pattern in _OBJECT_IGNORE):
        return True
    return any(
        av == api_version and k == kind and _path_match(pattern, pointer, subtree=True)
        for av, k, pattern in _IGNORE
    )


def _default_at(api_version: str, kind: str, pointer: str) -> tuple[bool, Any]:
    for av, k, pattern, value in _DEFAULTS:
        if av == api_version and k == kind and _path_match(pattern, pointer):
            return True, value
    return False, None


def _quantity_at(api_version: str, kind: str, pointer: str) -> bool:
    return any(
        av == api_version and k == kind and _path_match(pattern, pointer)
        for av, k, pattern in _QUANTITY
    )


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _values_equal(left: Any, right: Any, path: str = "", *,
                  api_version: str = "", kind: str = "") -> bool:
    if left == right:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return left == right
    if kind == "ConfigMap" and _configmap_yaml_equal(left, right, path):
        return True
    if not _quantity_at(api_version, kind, path):
        return False
    if _is_num(left) and _is_num(right):
        return float(left) == float(right)
    if _looks_memory(left) or _looks_memory(right):
        return memory_bytes(left) == memory_bytes(right)
    if _looks_cpu(left) or _looks_cpu(right):
        return _cpu_millis(left) == _cpu_millis(right)
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return False


def _looks_memory(value: Any) -> bool:
    return isinstance(value, str) and bool(_MEM_SUFFIX.match(value.strip()))


def _looks_cpu(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(_CPU_MILLI.match(text))


def _cpu_millis(value: Any) -> int:
    if _is_num(value):
        return int(round(float(value) * 1000))
    text = str(value).strip()
    if text.endswith("m") and _CPU_MILLI.match(text):
        return int(round(float(text[:-1])))
    return int(round(float(text) * 1000))


def _is_hook_derived_checksum(parent_path: str, key: Any) -> bool:
    """Helm chart idiom: checksum/* on the pod-template annotations map."""
    if not isinstance(key, str) or not key.startswith(_CHECKSUM_PREFIX):
        return False
    segs = _pointer_segments(parent_path)
    return segs[-3:] == ["template", "metadata", "annotations"]


def _data_key_from_path(path: str) -> str | None:
    segs = _pointer_segments(path)
    if len(segs) == 2 and segs[0] == "data":
        return segs[1]
    return None


def _secret_data_keys(obj: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("data", "stringData"):
        body = obj.get(field) or {}
        if isinstance(body, dict):
            keys.update(str(k) for k in body)
    return keys


def _is_tls_shaped(keys: set[str]) -> bool:
    return any(pair <= keys for pair in _TLS_KEY_PAIRS)


def _generated_data_keys(
    rendered: dict[str, Any],
    live: dict[str, Any],
    diffs: list[PathDiff],
    named: frozenset[str],
) -> list[str]:
    if str(rendered.get("kind") or "") != "Secret":
        return []
    data_keys = [k for d in diffs if (k := _data_key_from_path(d.path)) is not None]
    if not data_keys:
        return []
    name = str((rendered.get("metadata") or {}).get("name") or "")
    if name in named:
        return sorted(set(data_keys))
    all_keys = _secret_data_keys(rendered) | _secret_data_keys(live)
    if _is_tls_shaped(all_keys):
        return sorted({k for k in data_keys if k in _TLS_DATA_KEYS})
    return []


def _try_parse_yaml(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError):
        # live ConfigMap data is cluster content; a document nested past the
        # interpreter's limit is compared as text, not raised out of the diff
        return _UNPARSED


def _yaml_null_equal(left: Any, right: Any) -> bool:
    """Helm toYaml null idiom: a null key equals an absent key."""
    if left is None and right is None:
        return True
    if left is None:
        return isinstance(right, dict) and _yaml_null_equal({}, right)
    if right is None:
        return isinstance(left, dict) and _yaml_null_equal(left, {})
    if isinstance(left, dict) or isinstance(right, dict):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        for key in set(left) | set(right):
            if not _yaml_null_equal(left.get(key, None), right.get(key, None)):
                return False
        return True
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list):
            return False
        if len(left) != len(right):
            return False
        return all(_yaml_null_equal(a, b) for a, b in zip(left, right))
    return left == right


def _configmap_yaml_equal(left: Any, right: Any, path: str) -> bool:
    segs = _pointer_segments(path)
    if segs[:1] != ["data"] or len(segs) != 2:
        return False
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    parsed_left = _try_parse_yaml(left)
    parsed_right = _try_parse_yaml(right)
    if parsed_left is _UNPARSED or parsed_right is _UNPARSED:
        return False
    # Scalars stay string compare: "1" and "001" both parse as YAML 1, which
    # must remain a change. Nested documents (Helm toYaml) are dict or list.
    if not isinstance(parsed_left, (dict, list)) or not isinstance(parsed_right, (dict, list)):
        return False
    return _yaml_null_equal(parsed_left, parsed_right)


STOP_TIMEOUT_S = 10.0


class DailyRunner:
    """First diff `first_delay` after start, then every `interval`. Clock is injectable."""

    def __init__(self, state: AppState, *, clock: Any | None = None,
                 first_delay: float = FIRST_DELAY_SECONDS,
                 interval: float = INTERVAL_SECONDS,
                 stop_event: threading.Event | None = None,
                 differ: Callable[..., ShadowReport] | None = None) -> None:
        self.state = state
        self.clock = clock
        self.first_delay = first_delay
        self.interval = interval
        self.stop_event = stop_event or threading.Event()
        self.differ = differ or diff
        self.runs: list[Any] = []
        self.thread: threading.Thread | None = None

    def _sleep(self, seconds: float) -> None:
        if self.stop_event.is_set():
            return
        if self.clock is not None and hasattr(self.clock, "sleep"):
            self.clock.sleep(seconds)
            return
        self.stop_event.wait(timeout=seconds)

    def loop(self) -> None:
        self._sleep(self.first_delay)
        while not self.stop_event.is_set():
            self.run_once()
            if self.stop_event.is_set():
                break
            self._sleep(self.interval)

    def run_once(self) -> list[ShadowReport]:
        reports: list[ShadowReport] = []
        try:
            pairs = catalog_pairs(self.state)
        except (BackendUnconfigured, BackendError) as e:
            log.warning("shadow catalog: %s", e)
            self.runs.append(_utc_today(self.clock).isoformat())
            return reports
        for project, env in pairs:
            if self.stop_event.is_set():
                break  # the pair in progress finished; no other starts after a stop
            try:
                reports.append(self.differ(self.state, project, env, clock=self.clock))
            except Exception:
                log.exception("shadow diff %s/%s failed", project, env)
                failed = ShadowReport(
                    project=project, env=env, empty=False,
                    error="diff failed",
                    date=_utc_today(self.clock).isoformat(),
                )
                try:
                    stored = _write_report(self.state, failed, clock=self.clock)
                    failed.path = str(stored)
                    failed.report_path = str(stored)
                except Exception:
                    log.exception("shadow persist %s/%s failed", project, env)
                reports.append(failed)
        self.runs.append(_utc_today(self.clock).isoformat())
        return reports

    def start(self) -> DailyRunner:
        self.thread = guarded_thread(
            target=self.loop, name="orbitd-shadow", daemon=True,
        )
        self.thread.start()
        return self

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        """Signal the loop and wait for its thread; the pair being diffed finishes, no
        other starts. False when the thread outlived `timeout`. Idempotent."""
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        alive = thread is not None and thread.is_alive()
        if alive:
            log.warning("shadow loop still running %ss after stop; abandoning the thread", timeout)
        return not alive


def start_daily_runner(state: AppState, **kwargs: Any) -> DailyRunner:
    return DailyRunner(state, **kwargs).start()


__all__ = [
    "FIRST_DELAY_SECONDS", "INTERVAL_SECONDS", "LIVE_GVKS", "PLATFORM_LIVE_GVKS",
    "DIFF_VALUE_LIMIT",
    "LABEL_GENERATED_SECRETS", "RENDER_STAMP_ANNOTATIONS",
    "ObjectRef", "PathDiff", "ChangedRef", "OwnedRef", "GeneratedRef",
    "ShadowReport",
    "state_dir_of", "catalog_pairs", "read_live", "compare_objects", "diff",
    "streak_days", "DailyRunner", "start_daily_runner",
]
