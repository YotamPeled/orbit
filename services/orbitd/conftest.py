"""Root conftest: make `python3 -m pytest` mean the same thing on a machine whose
interpreter cannot import the pinned dependencies.

The verify line is `python3 -m pip install -q -e . ; python3 -m pytest -q`. On a
distribution whose python ships without pip, or refuses installs into the system
interpreter (PEP 668), the first half fails silently and the second half would fail on
`import mcp` for a reason that has nothing to do with the code. So: if the SDK is not
importable, build `.venv` beside this file from `pyproject.toml` (pins included), and
re-exec this same pytest invocation under it. The exit code is the venv run's. Nothing
happens when the dependencies are already present.
"""

import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
VENV = HERE / ".venv"


def _venv_python() -> pathlib.Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _deps_missing() -> bool:
    try:
        import anyio  # noqa: F401
        import httpx2  # noqa: F401
        import mcp  # noqa: F401
    except ImportError:
        return True
    return False


def pytest_configure(config) -> None:
    """Runs before collection, so `tests/conftest.py` (which imports the SDK) is never
    loaded here. Capture is suspended first: pytest has already redirected fd 1 and 2
    to its own buffers, and an exec'd process inheriting them would run silently."""
    if not _deps_missing():
        return
    if os.environ.get("ORBITD_BOOTSTRAPPED"):
        raise SystemExit("orbitd tests: dependencies still missing after bootstrap; see .venv")
    capman = config.pluginmanager.getplugin("capturemanager")
    if capman is not None:
        capman.suspend_global_capture(in_=True)
    py = _venv_python()
    if not py.exists():
        print(f"orbitd tests: {sys.executable} cannot import mcp; building {VENV}", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "-q", "-e", f"{HERE}[test]"], check=True)
    sys.stdout.flush()
    sys.stderr.flush()
    env = {**os.environ, "ORBITD_BOOTSTRAPPED": "1"}
    os.execve(str(py), [str(py), "-m", "pytest", *sys.argv[1:]], env)
