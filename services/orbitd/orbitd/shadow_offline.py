"""Offline shadow diff: production normalise + compare on two JSON lists."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .shadow import compare_objects

USAGE = "usage: python -m orbitd.shadow_offline <rendered.json> <live.json>"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        rendered = _load_list(args[0])
        live = _load_list(args[1])
    except (OSError, ValueError) as e:
        print(f"orbitd.shadow_offline: {e}", file=sys.stderr)
        return 2
    report = compare_objects(rendered, live)
    json.dump(report.model_dump(mode="json"), sys.stdout)
    sys.stdout.write("\n")
    return 0


def _load_list(path: str) -> list[object]:
    text = Path(path).read_text()
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"{path} is not a JSON list")
    return data


if __name__ == "__main__":
    raise SystemExit(main())
