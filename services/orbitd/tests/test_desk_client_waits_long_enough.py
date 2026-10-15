"""The desk client's read timeout outlasts the longest a tool may legitimately hold a
request: orbit_job_wait blocks up to its 120 s cap, and a 30 s read timeout made every
long wait a transport failure (docs/V1.md §8, found shipping checkers-lab)."""

import inspect

from orbitd import client
from orbitd.tools_viewer import job_wait


def test_the_read_timeout_outlasts_job_wait_s_cap():
    http = client._open_http({}, None, "https://orbit.example.invalid/mcp")
    try:
        cap = 120
        assert "le=120" in inspect.getsource(job_wait), "job_wait's cap moved; re-check this bound"
        assert http.timeout.read is not None and http.timeout.read > cap
        assert http.timeout.connect is not None and http.timeout.connect <= 30
    finally:
        import asyncio
        asyncio.run(http.aclose())
