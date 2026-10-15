.PHONY: sync check verify core-sync core-check init

# Chicken-and-egg breaker: a fresh instance repo has no scripts/ yet. Until the first
# core-sync lands the vendored copy, fetch core-sync.sh from a clone of the pinned core
# and run it from here. After that, the vendored scripts/core-sync.sh takes over.
define BOOTSTRAP_SYNC
	u=$$(sed -n 's/^url: //p' orbit-core.lock); r=$$(sed -n 's/^ref: //p' orbit-core.lock); \
	t=$$(mktemp -d); trap 'rm -rf "$$t"' EXIT; \
	git clone -q --depth 1 --branch "$$r" "$$u" "$$t/core" && bash "$$t/core/scripts/core-sync.sh"
endef

core-sync:     ## vendor orbit-core at the pin in orbit-core.lock (works on a bare template)
	@if [ -x scripts/core-sync.sh ]; then scripts/core-sync.sh; else $(BOOTSTRAP_SYNC); fi
core-check:    ## verify vendored files match the pin
	@if [ -x scripts/core-sync.sh ]; then scripts/core-sync.sh --check; \
	 else echo "nothing vendored yet — run 'make core-sync' first"; exit 1; fi

init: core-sync ## one-time scaffold: fill clusters/ placeholders from instance/config.yaml
	@python3 scripts/instance-init.py

sync:          ## catalog -> manifests, namespaces
	@python3 scripts/catalog-sync.py

# What is compared is the generated tree as it was when `make check` started against what
# the generator writes for the catalog now: committed or not. It used to be `git diff`
# after a sync, which fails on every change that is not committed yet (so "sync, check,
# commit", the documented order, always failed at check) and cannot see a new generated
# file at all, since git does not diff a file it does not track.
check:         ## fail if the generated files are not what the catalog generates
	@t=$$(mktemp -d); trap 'rm -rf "$$t"' EXIT; \
	for d in apps orbit; do mkdir -p "$$t/$$d"; [ -d "clusters/$$d" ] && cp -a "clusters/$$d/." "$$t/$$d/"; done; \
	python3 scripts/catalog-sync.py || exit 1; \
	stale=0; for d in apps orbit; do \
	  [ -d "clusters/$$d" ] || mkdir -p "clusters/$$d"; \
	  diff -rq "$$t/$$d" "clusters/$$d" || stale=1; \
	done; \
	if [ "$$stale" -ne 0 ]; then \
	  echo "ERROR: the generated files were stale (listed above). 'make sync' has just rewritten them: read the diff and commit it"; exit 1; \
	fi; \
	echo "catalog and manifests agree"

verify:        ## run every stage's verification, cumulatively
	@failed=0; for s in scripts/verify/stage*.sh; do echo "== $$s =="; bash "$$s" || failed=1; done; \
	if [ "$$failed" -ne 0 ]; then echo "verify: FAILED (at least one stage failed; see output above)"; \
	else echo "verify: all stages passed"; fi; \
	exit $$failed
