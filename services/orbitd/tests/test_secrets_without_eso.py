"""orbit secrets verify on a cluster that no longer serves ExternalSecret.

Once the sops handover is done the External Secrets Operator and its CRDs are deleted.
verify still lists ExternalSecret targets to report a Secret with no file, and it must read
"the kind is not served" as "there are none", not fail the whole run."""

from __future__ import annotations

from kubernetes.dynamic.exceptions import ResourceNotFoundError

import test_secrets as ts
from orbitd.secrets import ES_KIND, EXIT_OK, export_secrets, verify_secrets


def _unserve_external_secrets(kube, monkeypatch) -> None:
    kube.objects[:] = [o for o in kube.objects if o.get("kind") != ES_KIND]
    resources = kube.dynamic.resources
    real_get = resources.get

    def get(api_version=None, kind=None, **kwargs):
        if kind == ES_KIND:
            raise ResourceNotFoundError(f"No matches found for kind {kind!r}")
        return real_get(api_version=api_version, kind=kind, **kwargs)

    monkeypatch.setattr(resources, "get", get)
    assert kube.dynamic.resources is resources, "the fake must hand out one resources object"


def test_verify_runs_after_the_externalsecret_api_is_gone(tmp_path, monkeypatch, capsys):
    instance, kube, pub, fake = ts._world(tmp_path)
    ts._age_key(tmp_path, monkeypatch)
    assert export_secrets(instance, kube, recipient_file=str(pub), sops_run=fake.run) == EXIT_OK
    capsys.readouterr()
    _unserve_external_secrets(kube, monkeypatch)
    rc = verify_secrets(instance, kube, sops_run=fake.run)
    out, err = capsys.readouterr()
    assert "cannot discover" not in err
    assert rc == EXIT_OK, err
    assert "proj-dev/app-config match" in out
    assert "no-file" not in out
