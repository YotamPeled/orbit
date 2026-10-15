# The node. Created by tofu for a fresh org; an ADOPTED node (one that existed
# before tofu) is imported by an instance-side tofu/import.tf carrying an import
# block for hcloud_server.prod — import is a no-op once in state, and a fresh org
# simply has no such file.
#
# The instance config decides size and place (nodes[0].serverType/location);
# defaults preserve the reference instance's adopted cx43/fsn1.

resource "hcloud_server" "prod" {
  name        = var.server_name
  server_type = try(local.instance.nodes[0].serverType, "cx43")
  location    = try(local.instance.nodes[0].location, "fsn1")
  image       = "ubuntu-26.04"
  backups     = var.enable_backups

  # Injected at creation so a FRESH box is reachable at first boot. For an adopted
  # node this attribute is in ignore_changes (Hetzner only injects at creation and
  # the provider treats it as replace-on-change), so listing it here cannot touch
  # a running server.
  ssh_keys = [hcloud_ssh_key.operator.name]

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  lifecycle {
    # Deliberate-act guard: destroying a node must never be a side effect. A
    # throwaway instance is torn down via the API + `tofu state rm`, or by
    # removing this line first — either way, on purpose.
    prevent_destroy = true

    # Every one of these is replace-on-change in the hcloud provider. Ignoring them
    # means an edit here can never silently trade a running node for a new one.
    ignore_changes = [ssh_keys, user_data, image, location, datacenter]
  }
}

resource "hcloud_ssh_key" "operator" {
  name       = var.ssh_key_name
  public_key = trimspace(file(pathexpand(var.ssh_public_key_path)))
}
