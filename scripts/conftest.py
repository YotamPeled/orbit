"""The verify stages export PYTHONPATH=scripts/verify so their python blocks import the
shared display rule (scripts/verify/shown.py). Tests that run a stage FRAGMENT under
`bash -c` never pass through that export, so the suite provides it once, here."""
import os
import pathlib

_VERIFY = str(pathlib.Path(__file__).resolve().parent / "verify")
os.environ["PYTHONPATH"] = _VERIFY + (":" + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")

import sys
if _VERIFY not in sys.path:
    sys.path.insert(0, _VERIFY)      # in-process loaders of the stage helpers (importlib in tests) need it too
