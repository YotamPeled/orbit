# Traefik's own CRDs, vendored

`apps/traefik.yaml` sets `helm.skipCrds: true` so applying the Traefik chart
cannot overwrite the Gateway API v1.6 bundle vendored in `config/gateway-api/` (the
chart's `crds/` dir carries an older copy of it, plus the Traefik Hub CRDs Orbit does
not run). That decision leaves Traefik's OWN CRDs uninstalled — and the chart still
renders a `traefik.io/v1alpha1 TLSStore` from its `tlsStore:` values, so on a FRESH
cluster the traefik Application never syncs at all:

    failed to discover server resources for group version traefik.io/v1alpha1:
    the server could not find the requested resource

(An instance whose cluster predates `skipCrds` has these CRDs from history and never
sees it. A scaffold onto a wiped node is where it surfaces.)

So the CRDs are vendored here instead — only the `traefik.io_*` files, taken verbatim
from the `crds/` directory of the exact chart version the traefik Application pins
(traefik 37.1.1 / Traefik v3.5.2), and applied by the wave-0 `traefik-crds` Application
in `apps/traefik.yaml`. Traefik CRD upgrades are therefore a deliberate platform change
made together with the chart bump, which is what the `skipCrds` comment already asks
for.

These files are the chart's, not Orbit's: no GENERATED banner, no instance string, no
edit. `make init` copies the `*.yaml` into `clusters/platform/crds/traefik/`; the directory source
ignores this README.

| | |
|---|---|
| Chart | traefik 37.1.1 (`https://traefik.github.io/charts`) |
| appVersion | v3.5.2 |
| Source | `traefik/crds/traefik.io_*.yaml` (ten files; Hub and Gateway API CRDs omitted) |

Refresh with:

```sh
helm pull traefik/traefik --version 37.1.1 --untar
cp traefik/crds/traefik.io_*.yaml \
  provisioning/clusters/platform/crds/traefik/
```

Then `cmp` each file against the pulled copy so "verbatim" is checkable rather than
trusted. On a chart bump, replace them wholesale and update the three rows above.
