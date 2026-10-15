# orbit-intake: the agent plane's event router (AGENT-PLANE.md, "Triggers are first class").
#
# Standard library only — no pip install, no Node, nothing but the interpreter and one
# file. That is a security property rather than minimalism for its own sake: this process
# holds three intake credentials plus every warm agent's caller bearer, and it can create
# Jobs in the namespace that mounts the org's subscription token. A dependency here is a
# dependency with all of that in reach.
#
# The base is pinned BY DIGEST, so the image CI publishes is the image CI tested — a
# floating `python:3.13-slim` tag means the tested and published artifacts are only
# probably the same.
#
# Refresh procedure: bump the digest, rebuild, and re-run services/intake/smoke.sh before
# merging.

# python:3.13-slim
FROM python@sha256:7ce4b6dfe35e55397b7cda544f8a13f191b7ae28dc5aad71fe664dbc9bc2623f

# Non-root, same uid convention as every other platform workload. Nothing is written at
# runtime — the routing config is a read-only ConfigMap mount — so no writable home is
# needed beyond what the base image already gives uid 10001.
RUN useradd --uid 10001 --create-home orbit
USER 10001
ENV HOME=/home/orbit

# Where the generated routing ConfigMap is projected. The file is READ ONCE at import and
# a defect in it is a startup crash, never a warning: see app.py `_boot_config`.
ENV INTAKE_ROUTES_FILE=/etc/orbit/intake/routes.json

COPY app.py /app/app.py
EXPOSE 8080
ENTRYPOINT ["python3", "-u", "/app/app.py"]
