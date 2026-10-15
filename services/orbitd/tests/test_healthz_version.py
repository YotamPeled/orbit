"""/healthz reports the release the image was built from (docs/V1.md §8: it said 0.1.0
for every release, so it could not tell an operator which orbitd was running)."""
import orbitd
import pytest
from orbitd import build_version, UNRELEASED


def test_a_release_tag_is_reported():
    assert build_version({"ORBITD_VERSION": "v0.33.0"}) == "v0.33.0"
    assert build_version({"ORBITD_VERSION": "v1.0.0-rc.1"}) == "v1.0.0-rc.1"
    assert build_version({"ORBITD_VERSION": "main-3c92dd8"}) == "main-3c92dd8"


def test_no_build_version_says_unreleased_not_a_made_up_number():
    assert build_version({}) == UNRELEASED
    assert build_version({"ORBITD_VERSION": ""}) == UNRELEASED


def test_a_value_of_the_wrong_shape_is_never_repeated():
    for bad in ("0.1.0", "v1.2", "latest", "v1.0.0\nX-Injected: 1", "<script>", "v1.0.0 extra"):
        assert build_version({"ORBITD_VERSION": bad}) == UNRELEASED, bad


@pytest.mark.anyio
async def test_healthz_carries_the_build_version(http):
    async with http() as h:
        body = (await h.get("/healthz")).json()
    assert body["version"] == orbitd.__version__
    assert body["version"] != "0.1.0"
