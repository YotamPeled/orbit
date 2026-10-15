# orbitd: the control surface. python:slim, one pip install, non-root; the signing
# key, the principal store and the revocation list are mounted, never baked in.
# Build from the repository root so the desk's ship scripts can be copied in:
#   docker buildx build -f services/orbitd/Dockerfile .
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssl ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# helm v3.19.0 from the official release tarball, sha256-pinned per architecture.
# No plugins. TARGETARCH is amd64/arm64 under buildx; fall back to dpkg otherwise.
ARG TARGETARCH
ENV HELM_VERSION=v3.19.0
RUN set -eux; \
    arch="${TARGETARCH}"; \
    if [ -z "$arch" ]; then arch="$(dpkg --print-architecture)"; fi; \
    case "$arch" in \
      amd64) sha=a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496 ;; \
      arm64) sha=440cf7add0aee27ebc93fada965523c1dc2e0ab340d4348da2215737fc0d76ad ;; \
      *) echo "unsupported helm architecture: $arch"; exit 1 ;; \
    esac; \
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${arch}.tar.gz" -o /tmp/helm.tgz; \
    echo "${sha}  /tmp/helm.tgz" | sha256sum -c -; \
    tar -xzf /tmp/helm.tgz -C /tmp; \
    install -m 755 "/tmp/linux-${arch}/helm" /usr/local/bin/helm; \
    rm -rf /tmp/helm.tgz "/tmp/linux-${arch}"; \
    helm version --short
# The container root is read-only; helm template still wants cache/config dirs.
ENV HELM_CACHE_HOME=/tmp/helm-cache \
    HELM_CONFIG_HOME=/tmp/helm-config \
    HELM_DATA_HOME=/tmp/helm-data

RUN useradd --uid 10001 --create-home orbit
# This build's copy of orbit-core, for the commit gate's checks (orbitd/checks): the
# vendored paths of core-manifest.txt, to compare an instance's copy against and to run
# the scaffolder and the generator from. Never the instance's own scripts.
COPY core-manifest.txt /opt/orbit-core/core-manifest.txt
COPY charts /opt/orbit-core/charts
COPY scripts /opt/orbit-core/scripts
COPY tofu /opt/orbit-core/tofu
COPY skills /opt/orbit-core/skills
COPY .claude/skills /opt/orbit-core/.claude/skills
COPY template /opt/orbit-core/template
ENV ORBIT_CORE_DIR=/opt/orbit-core
WORKDIR /app
COPY services/orbitd/pyproject.toml /app/
COPY services/orbitd/orbitd /app/orbitd
COPY scripts/ship.sh scripts/monitor-commit.sh scripts/features-commit.sh scripts/catalog-commit.sh scripts/lib-app-clone.sh \
     scripts/bump-and-push.sh scripts/bump-tag.py \
     scripts/github-app-token.py scripts/lib-instance.sh /app/scripts/
# yaml is the only third-party import the ship scripts need (github-app-token.py
# is stdlib + openssl; the rest of the image already carries git).
RUN chmod 755 /app/scripts/* \
    && pip install --no-cache-dir /app PyYAML

# The release this image is built from; /healthz reports it (orbitd/__init__.py).
#   docker buildx build -f services/orbitd/Dockerfile --build-arg ORBITD_VERSION=v0.33.0 .
ARG ORBITD_VERSION=
ENV ORBITD_VERSION=${ORBITD_VERSION}

USER 10001
ENV HOME=/home/orbit PORT=8080
EXPOSE 8080
ENTRYPOINT ["orbitd"]
CMD ["serve"]
