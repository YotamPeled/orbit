"""Area 3a of the whole-app sweep (credential plane and access): one test per repair.

Each test fails with its repair reverted: the principal store is serialised and writes
through a unique temp file; mint stages every namespace before placing any; a non-finite
sync timestamp is not a crash; Kube publishes its clients together."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import textwrap
import threading
import time
import types

import pytest

from test_secrets_mint import NAMESPACES, PLANT_B64, mint, opened, path_of, world

from orbitd.auth import Store
from orbitd.backends import Kube
from orbitd.roles import Role
from orbitd.secrets import EXIT_DIFF, EXIT_OK
from orbitd.source import _gitsync_mtime, _parse_synced


# ------------------------------------------------------------ auth.Store

def test_a_retire_is_not_undone_by_a_concurrent_mint(tmp_path):
    """Thread A retires `bob` and, between its load and its save, thread B records a
    token. Serialised, B waits for A: bob stays gone and the token is recorded.
    Unserialised, B's save writes back a snapshot that still carries bob."""
    store = Store(str(tmp_path / "principals.json"))
    store.add_principal("bob", Role.operator, 60)
    a_loaded = threading.Event()
    b_done = threading.Event()
    real_load = Store.load

    def slow_load(self):
        data = real_load(self)
        if threading.current_thread().name == "retire":
            a_loaded.set()
            b_done.wait(1.0)  # give B every chance to interleave
        return data

    Store.load = slow_load
    try:
        a = threading.Thread(target=store.remove_principal, args=("bob",), name="retire")
        a.start()
        assert a_loaded.wait(2.0)
        b = threading.Thread(target=store.record_token, args=("jti-1", "carol", Role.viewer, 1, 2),
                             name="mint")
        b.start()
        b.join(3.0)
        b_done.set()
        a.join(3.0)
    finally:
        Store.load = real_load
    data = store.load()
    assert "bob" not in data["principals"]
    assert "jti-1" in data["tokens"]
    assert store.check_secret("bob", "anything") is None


def test_concurrent_writers_leave_a_parseable_store_with_every_record(tmp_path):
    store = Store(str(tmp_path / "principals.json"))
    start = threading.Barrier(8)

    def writer(i):
        start.wait()
        for j in range(10):
            store.record_token(f"jti-{i}-{j}", "svc", Role.viewer, 1, 2)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)
    data = json.loads((tmp_path / "principals.json").read_text())
    assert len(data["tokens"]) == 80
    leftovers = [p for p in os.listdir(tmp_path) if ".tmp" in p]
    assert leftovers == []


def test_every_save_writes_through_its_own_temp_name(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "principals.json"))
    names = []
    real_replace = os.replace

    def spy(src, dst):
        names.append(os.path.basename(src))
        real_replace(src, dst)

    monkeypatch.setattr("orbitd.auth.os.replace", spy)
    store.save({"principals": {}, "tokens": {}})
    store.save({"principals": {}, "tokens": {}})
    assert len(names) == 2 and names[0] != names[1]
    assert f".tmp.{os.getpid()}" not in names[0]


def test_the_store_lock_holds_across_processes(tmp_path):
    """A second process (`orbitd bootstrap`) shares the file. The child takes the
    sidecar flock, then rewrites the store with its own principal while holding it;
    the parent's change must wait for the child, so both principals survive. Without
    flock the parent writes first and the child's replace drops it."""
    path = tmp_path / "principals.json"
    store = Store(str(path))
    store.add_principal("carol", Role.admin, 60)
    child = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import fcntl, json, os, sys, time
            fd = os.open({str(path) + ".lock"!r}, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = json.load(open({str(path)!r}))  # snapshot under the lock, then dally
            print("held", flush=True)
            time.sleep(0.5)
            data["principals"]["child"] = {{"role": "viewer", "secret_sha256": "x", "ttl": 1, "created": "c"}}
            tmp = {str(path)!r} + ".child"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, {str(path)!r})
            os.close(fd)
        """)],
        stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "held"
    store.add_principal("parent", Role.viewer, 60)
    assert child.wait(5.0) == 0
    names = set(store.load()["principals"])
    assert {"carol", "child", "parent"} <= names


# ------------------------------------------------------------ secrets.mint

def test_a_mid_run_sops_failure_places_no_file(tmp_path):
    instance, pub, fake = world(tmp_path)
    assert mint(instance, pub, fake) == EXIT_OK
    before = {ns: path_of(instance, ns).read_text() for ns in NAMESPACES}
    calls = {"encrypt": 0}
    real_run = fake.run

    def flaky(argv, **kw):
        if "encrypt" in argv:
            calls["encrypt"] += 1
            if calls["encrypt"] == 2:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return real_run(argv, **kw)

    fake.run = flaky
    assert mint(instance, pub, fake, value="plantval_second_gg77tt", rotate=True) == EXIT_DIFF
    fake.run = real_run
    after = {ns: path_of(instance, ns).read_text() for ns in NAMESPACES}
    assert after == before, "a partial rotation placed one namespace's file"
    for ns in NAMESPACES:
        doc = opened(fake, path_of(instance, ns))
        assert doc["spec"]["secretTemplates"][0]["data"]["token"] == PLANT_B64


def test_a_target_that_became_a_directory_places_nothing_and_leaves_no_temp(tmp_path):
    instance, pub, fake = world(tmp_path)
    assert mint(instance, pub, fake) == EXIT_OK
    first = path_of(instance, NAMESPACES[0]).read_text()
    second = path_of(instance, NAMESPACES[1])
    second.unlink()
    second.mkdir()
    assert mint(instance, pub, fake, value="plantval_second_gg77tt", rotate=True) == EXIT_DIFF
    assert path_of(instance, NAMESPACES[0]).read_text() == first, "the first namespace was rotated alone"
    assert second.is_dir()
    for ns in NAMESPACES:
        assert [p.name for p in path_of(instance, ns).parent.glob("*.tmp")] == []


def test_a_failing_store_save_leaves_no_temp(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "principals.json"))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("orbitd.auth.json.dump", boom)
    with pytest.raises(OSError):
        store.save({"principals": {}, "tokens": {}})
    assert [p.name for p in tmp_path.iterdir()] == []


# ------------------------------------------------------------ source

def test_a_non_finite_sync_timestamp_is_ignored(tmp_path):
    assert _parse_synced({"synced": "nan"}) is None
    assert _parse_synced({"synced": "inf"}) is None
    assert _parse_synced({"synced": float("nan")}) is None
    assert _parse_synced({"synced": "1700000000"}) == 1700000000.0
    (tmp_path / "git-sync.json").write_text('{"synced": "nan"}')
    mtime = _gitsync_mtime(tmp_path)
    assert mtime is not None and math.isfinite(mtime)


# ------------------------------------------------------------ backends.Kube

def test_kube_publishes_every_client_together(monkeypatch):
    """A thread that finds `core` built must find `apps` built as well."""
    seen = {}

    builds = []

    class Slow:
        def __init__(self, api_client):
            builds.append(1)
            time.sleep(0.2)

    class Quick:
        def __init__(self, api_client):
            pass

    def api_client(*a, **k):
        # the shape Kube binds its default request timeout to
        return types.SimpleNamespace(rest_client=types.SimpleNamespace(request=lambda *a, **k: None))

    fake_client = types.SimpleNamespace(CoreV1Api=Quick, AppsV1Api=Slow, CustomObjectsApi=Quick,
                                        Configuration=lambda: object(), ApiClient=api_client)
    fake_config = types.SimpleNamespace(new_client_from_config=lambda config_file: api_client())
    monkeypatch.setitem(sys.modules, "kubernetes", types.SimpleNamespace(client=fake_client, config=fake_config))
    monkeypatch.setitem(sys.modules, "kubernetes.client", fake_client)
    monkeypatch.setitem(sys.modules, "kubernetes.config", fake_config)
    kube = Kube(kubeconfig="/dev/null")

    def first():
        kube.core

    def second():
        time.sleep(0.05)
        seen["apps"] = kube.apps

    a = threading.Thread(target=first)
    b = threading.Thread(target=second)
    a.start(); b.start(); a.join(3.0); b.join(3.0)
    assert seen["apps"] is not None
    assert len(builds) == 1, "the second thread built its own client set"
