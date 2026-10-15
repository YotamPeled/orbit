"""orbitd: Orbit's control surface. See docs/ORBITD.md."""

import os
import re

# The release this image was built from. The Dockerfile bakes it into the image as
# ORBITD_VERSION (`--build-arg ORBITD_VERSION=v0.33.0`); /healthz and the MCP server info
# report it, so "which orbitd is running" is answered by the running process, not by
# reading a Deployment. A value of the wrong shape is never repeated: /healthz is public.
_VERSION_SHAPE = re.compile(r"v\d{1,4}\.\d{1,4}\.\d{1,4}(-[0-9A-Za-z.]{1,32})?|main-[0-9a-f]{7}")
UNRELEASED = "unreleased"


def build_version(env: "os._Environ[str] | dict[str, str]" = os.environ) -> str:
    value = env.get("ORBITD_VERSION", "")
    return value if _VERSION_SHAPE.fullmatch(value) else UNRELEASED


__version__ = build_version()
