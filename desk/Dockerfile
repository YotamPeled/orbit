# orbit-desk: every command-line tool the desk needs, in one Linux image, so the operator
# machine needs only Docker (Windows, macOS or Linux alike). scripts/desk.sh runs it with
# the instance checkout, the operator's credentials directory and kubeconfig mounted, and
# the entrypoint opens the ssh tunnel to the node's API (the API port is firewalled; the
# tunnel is the only way in, see docs/ONBOARDING.md §4).
#
# Built on the operator machine like every other image (docs/V1.md §2):
#   docker buildx build -f desk/Dockerfile -t ghcr.io/<owner>/orbit-desk:<tag> .
# (from the repository root: the image carries the `orbit` desk CLI from services/orbitd)
# Versions are pinned and every download is checked against checksums.txt (the upstream
# release checksums; age publishes none, so its line is the hash of the first download).
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git openssh-client curl ca-certificates openssl make jq dnsutils unzip \
    && rm -rf /var/lib/apt/lists/*

ARG KUBECTL_VERSION=v1.36.4
ARG HELM_VERSION=v3.19.0
ARG SOPS_VERSION=v3.13.3
ARG AGE_VERSION=v1.3.2
ARG PROM_VERSION=3.13.2
ARG TOFU_VERSION=1.13.1

COPY desk/checksums.txt /tmp/checksums.txt
RUN set -eux; cd /tmp; \
    fetch() { curl -fsSL "$1" -o "$2"; grep " $2\$" /tmp/checksums.txt | sha256sum -c -; }; \
    fetch "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" kubectl; \
    install -m 755 kubectl /usr/local/bin/kubectl; \
    fetch "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" helm.tgz; \
    tar -xzf helm.tgz; install -m 755 linux-amd64/helm /usr/local/bin/helm; \
    fetch "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.amd64" sops; \
    install -m 755 sops /usr/local/bin/sops; \
    fetch "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-amd64.tar.gz" age.tgz; \
    tar -xzf age.tgz; install -m 755 age/age age/age-keygen /usr/local/bin/; \
    fetch "https://github.com/prometheus/prometheus/releases/download/v${PROM_VERSION}/prometheus-${PROM_VERSION}.linux-amd64.tar.gz" prom.tgz; \
    tar -xzf prom.tgz; install -m 755 "prometheus-${PROM_VERSION}.linux-amd64/promtool" /usr/local/bin/promtool; \
    fetch "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip" tofu.zip; \
    unzip -q tofu.zip tofu; install -m 755 tofu /usr/local/bin/tofu; \
    rm -rf /tmp/*; \
    kubectl version --client >/dev/null; helm version --short; sops --version | head -1; \
    age --version; promtool --version | head -1; tofu version | head -1

# The `orbit` desk CLI (platform plan/apply/retire, secrets, onboard, login/call) is the
# orbitd package itself.
COPY services/orbitd/pyproject.toml /opt/orbitd/
COPY services/orbitd/orbitd /opt/orbitd/orbitd
RUN pip install --no-cache-dir /opt/orbitd PyYAML && orbit --help >/dev/null

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
# The release this desk was built from (docker build --build-arg ORBITD_VERSION=vX.Y.Z):
# `orbit check` refuses a checkout pinned to another core, by this name.
ARG ORBITD_VERSION=
ENV ORBITD_VERSION=${ORBITD_VERSION}

ENV HELM_CACHE_HOME=/tmp/helm-cache HELM_CONFIG_HOME=/tmp/helm-config HELM_DATA_HOME=/tmp/helm-data
COPY desk/entrypoint.sh /usr/local/bin/desk-entrypoint
RUN chmod 755 /usr/local/bin/desk-entrypoint
WORKDIR /instance
ENTRYPOINT ["desk-entrypoint"]
CMD ["bash"]
