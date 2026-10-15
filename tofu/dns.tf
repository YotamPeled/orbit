# Phase 1 DNS is strictly additive. The apex records for the primary and tenant zones
# still point at Firebase and still serve live traffic — they get repointed at their
# own cutovers (Phase 3 and Phase 6), each with DNS as the rollback.
#
# The one record we add now is a wildcard. Every future subdomain — argocd, grafana,
# and one per project — resolves the moment its Ingress exists, with no
# further DNS change and nothing to forget. Routing is then decided entirely by
# Traefik from the service catalog, which is the point of the catalog.

# The instance config is the one place org identity lives; every other file in this
# module reads it rather than carrying its own copy of the domain/zone/node values.
locals {
  instance = yamldecode(file("${path.module}/../instance/config.yaml"))
  # The primary domain may itself be a subdomain of an owned zone (a test org at
  # orbtest.example.org inside zone example.org). The zone is the dnsZones entry
  # the primary sits inside — never assumed to equal the primary.
  primary_zone = [for z in local.instance.domains.dnsZones
  : z if endswith(local.instance.domains.primary, z)][0]
  dev_zone = [for z in local.instance.domains.dnsZones
  : z if endswith(local.instance.domains.dev, z)][0]
}

resource "cloudflare_dns_record" "wildcard_primary" {
  zone_id = var.zone_ids[local.primary_zone]
  name    = "*.${local.instance.domains.primary}"
  type    = "A"
  content = hcloud_server.prod.ipv4_address
  ttl     = 300
  comment = "managed by opentofu (orbit)"

  # Grey cloud for now. cert-manager issues real certificates via DNS-01, so
  # Traefik terminates TLS itself and Cloudflare's proxy would only add a second
  # hop and a second cert to reason about. Turned on selectively at cutover, where
  # DDoS absorption in front of a public tenant actually earns its keep.
  proxied = false
}

# Non-prod environments live under the dev domain, one label deeper than the primary
# wildcard reaches (*.example.org does not match x.dev.example.org — DNS wildcards
# span a single label). Same additive, grey-cloud reasoning as above.
resource "cloudflare_dns_record" "wildcard_dev" {
  zone_id = var.zone_ids[local.dev_zone]
  name    = "*.${local.instance.domains.dev}"
  type    = "A"
  content = hcloud_server.prod.ipv4_address
  ttl     = 300
  comment = "managed by opentofu (orbit)"
  proxied = false
}
