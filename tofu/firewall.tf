# Hetzner firewalls run in their network, before the packet reaches the host.
# The node currently has none — everything is exposed.
#
# Deliberately absent: 6443 (the Kubernetes API). kubectl reaches the cluster
# through an SSH tunnel, so the control plane is never on the public internet.
# That is the single highest-value rule in this file.
resource "hcloud_firewall" "prod" {
  # The cloud-side name derives from the instance name — for the original instance this
  # reproduces the pre-extraction name exactly, so nothing is renamed in Hetzner.
  name = "${local.instance.instance.name}-platform-prod"

  rule {
    description = "SSH"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  rule {
    description = "HTTP (ACME http-01 + the 301 to https)"
    direction   = "in"
    protocol    = "tcp"
    port        = "80"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  rule {
    description = "HTTPS"
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  rule {
    description = "ICMP echo, so the box is diagnosable"
    direction   = "in"
    protocol    = "icmp"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }
}

# A separate attachment resource rather than `firewall_ids` on the server, so that
# attaching or detaching never touches the server resource itself.
resource "hcloud_firewall_attachment" "prod" {
  firewall_id = hcloud_firewall.prod.id
  server_ids  = [hcloud_server.prod.id]
}
