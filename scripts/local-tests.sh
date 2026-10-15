#!/usr/bin/env bash
# Every offline test in the repository, run on this machine in a Linux container.
#
#   scripts/local-tests.sh                 the whole run: syntax, the scripts suite, orbitd's suite
#   scripts/local-tests.sh scripts [pytest args]     only the scripts suite (or the files named)
#   scripts/local-tests.sh orbitd  [pytest args]     only orbitd's suite (paths relative to services/orbitd)
#
# There is no CI (owner's rule, docs/V1.md §2 and §10, 2026-10-06): nothing runs in GitHub
# Actions. This is what a change is held to before it is merged, and its tally is pasted
# in the pull request. It tests the working tree as it is, committed or not, on a copy
# with the repository's real history behind it (one test asks git what core ever deleted).
#
# The image is what the old CI job ran on: Ubuntu, Python, shellcheck, git, make, and sops, age,
# promtool and helm at the desk image's versions and checksums. It is built here and cached by
# Docker. The container gets an init process and may create user namespaces, which the
# node-bootstrap tests need for their fake node.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
# The repository's own hooks (.githooks: no personal string or secret is committed or
# pushed, no co-author trailer): running the tests once is enough to have them.
[ "$(git config core.hooksPath 2>/dev/null)" = .githooks ] || git config core.hooksPath .githooks
WHAT=${1:-all}; [ "$#" -gt 0 ] && shift
case $WHAT in all|scripts|orbitd) ;; *) echo "usage: scripts/local-tests.sh [all|scripts|orbitd] [pytest args]" >&2; exit 2 ;; esac

docker build -q -t orbit-tests:local -f - services/orbitd >/dev/null <<'DOCKERFILE'
FROM ubuntu:24.04
RUN export DEBIAN_FRONTEND=noninteractive && apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 python3-pip python3-venv iptables kmod shellcheck git openssh-client curl ca-certificates       util-linux uidmap procps make >/dev/null && rm -rf /var/lib/apt/lists/*  && mkdir -p /run/systemd /usr/lib/systemd
# sops, age, promtool, helm: the versions and checksums of the desk image (desk/checksums.txt)
RUN set -e; cd /tmp;     curl -fsSL -o sops https://github.com/getsops/sops/releases/download/v3.13.3/sops-v3.13.3.linux.amd64;     echo "e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b  sops" | sha256sum -c -; install -m 755 sops /usr/local/bin/sops;     curl -fsSL -o age.tgz https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz;     echo "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10  age.tgz" | sha256sum -c -;     tar -xzf age.tgz; install -m 755 age/age age/age-keygen /usr/local/bin/;     curl -fsSL -o prom.tgz https://github.com/prometheus/prometheus/releases/download/v3.13.2/prometheus-3.13.2.linux-amd64.tar.gz;     echo "0e8c4d46101bd025ea8265e377d2caabc57f488fc1be1c367f37db69ea41be6f  prom.tgz" | sha256sum -c -;     tar -xzf prom.tgz; install -m 755 prometheus-3.13.2.linux-amd64/promtool /usr/local/bin/; curl -fsSL -o helm.tgz https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz; echo "a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496  helm.tgz" | sha256sum -c -; tar -xzf helm.tgz; install -m 755 linux-amd64/helm /usr/local/bin/helm; rm -rf /tmp/*
COPY pyproject.toml /tmp/orbitd/pyproject.toml
RUN python3 -m pip install --quiet --no-cache-dir --break-system-packages pyyaml pytest pytest-xdist  && python3 -c "import tomllib;d=tomllib.load(open('/tmp/orbitd/pyproject.toml','rb'));print(chr(10).join(d['project']['dependencies']+d['project'].get('optional-dependencies',{}).get('test',[])))" > /tmp/req.txt  && python3 -m pip install --quiet --no-cache-dir --break-system-packages -r /tmp/req.txt
RUN git config --system --add safe.directory '*'
DOCKERFILE

# Executable bits live in the git index (a Windows mount shows none): hand the list over.
EXEC=$(git ls-files -s | awk '$1=="100755"{print $4}' | tr '\n' ' ')
GITDIR=$(cd "$(git rev-parse --git-common-dir)" && pwd)
win(){ if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }

MSYS_NO_PATHCONV=1 exec docker run --rm --init -u 1000:1000 \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  -e HOME=/tmp -e SHA="$(git rev-parse HEAD)" -e EXEC="$EXEC" -e WHAT="$WHAT" -e WORKERS="${LOCAL_TESTS_WORKERS:-1}" \
  -v "$(win "$ROOT"):/src:ro" -v "$(win "$GITDIR"):/main.git:ro" \
  orbit-tests:local bash -c '
    set -euo pipefail
    git config --global user.email tests@local; git config --global user.name tests
    git clone -q --no-checkout /main.git /tmp/work
    cd /tmp/work && git reset -q "$SHA" && git config core.fileMode false
    tar -C /src --exclude=./.git -cf - . | tar -xf -
    git add -A
    for f in $EXEC; do [ -e "$f" ] && git update-index --chmod=+x "$f"; done
    # one test at a time unless LOCAL_TESTS_WORKERS says otherwise: on this laptop parallel
    # workers were killed mid-run (the Docker VM memory), which reads as failures
    PAR=""; [ "${WORKERS:-1}" -gt 1 ] && PAR="-n $WORKERS --dist loadfile"
    rc=0
    if [ "$WHAT" = all ]; then
      echo "== syntax and the template Alertmanager wiring"
      for s in scripts/verify/stage*.sh scripts/*.sh; do bash -n "$s"; done
      python3 scripts/verify/am-routes.py app \
        template/provisioning/clusters/platform/apps/kube-prometheus-stack.yaml \
        --secrets template/provisioning/clusters/platform/config/monitoring/secrets.yaml --producers-optional
    fi
    if [ "$WHAT" = all ] || [ "$WHAT" = scripts ]; then
      echo "== scripts suite"
      if [ "$#" -gt 0 ] && [ "$WHAT" = scripts ]; then python3 -m pytest -p no:cacheprovider "$@" || rc=1
      else
        # one pytest process per file: the whole suite in one process was killed for memory
        # at 96% on this laptop, which leaves no tally at all
        p=0; f=0; bad=""
        for file in $(find scripts -name "test_*.py" | sort); do
          out=$(python3 -m pytest "$file" -q $PAR -p no:cacheprovider 2>&1) || { bad="$bad $file"; echo "$out" | tail -40; }
          last=$(echo "$out" | tail -1)
          n=$(echo "$last" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+" || true); p=$((p + ${n:-0}))
          n=$(echo "$last" | grep -oE "[0-9]+ failed" | grep -oE "[0-9]+" || true); f=$((f + ${n:-0}))
        done
        echo "scripts suite: $p passed, $f failed"
        [ -z "$bad" ] || { echo "files with a failure or an error:$bad"; rc=1; }
      fi
    fi
    if [ "$WHAT" = all ] || [ "$WHAT" = orbitd ]; then
      echo "== orbitd suite"
      cd services/orbitd
      if [ "$#" -gt 0 ] && [ "$WHAT" = orbitd ]; then python3 -m pytest -p no:cacheprovider "$@" || rc=1
      else python3 -m pytest -q $PAR -p no:cacheprovider || rc=1; fi
    fi
    [ "$rc" = 0 ] && echo "local-tests: all passed" || echo "local-tests: FAILED"
    exit "$rc"' -- "$@"
