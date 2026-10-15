# Instance values arrive from the instance repo's tofu/instance.auto.tfvars — core
# carries no defaults for them. Tokens come from the environment, never from files.

variable "server_id" {
  description = "ID of the existing Hetzner server, adopted via the import block in server.tf."
  type        = string
}

variable "server_name" {
  description = "Must match the existing server's name, or OpenTofu will try to rename it. Mirrors instance/config.yaml nodes[0].name (a variable default cannot reference a local, so it is supplied via tfvars)."
  type        = string
}

variable "ssh_key_name" {
  description = "Cloud-side name of the registered SSH key (renaming replaces the key)."
  type        = string
}

variable "ssh_public_key_path" {
  description = "Public half of the keypair used to reach the node."
  type        = string
}

# The PRIVATE half, and it never leaves the workstation: it is interpolated into the
# copy-paste strings in outputs.tf and into nothing tofu sends anywhere. It carried one
# operator's personal key filename until 2026-09-07, which made every scaffolded org's
# `tofu output ssh` a command only that operator could run. The default is the ssh-keygen
# default; scripts/lib-instance.sh exposes the same pointer to the shell as ORBIT_SSH_KEY
# (from `$ORBIT_CONFIG_DIR/ssh-key-path` when the org keeps a dedicated key), so set this
# to the same path in tofu/instance.auto.tfvars if that is not where the key lives.
variable "ssh_private_key_path" {
  description = "Private half of the keypair, used only to build the ssh/tunnel output strings."
  type        = string
  default     = "~/.ssh/id_ed25519"
}

variable "enable_backups" {
  description = "Hetzner automated backups. Adds 20% to the server price."
  type        = bool
  default     = true
}

# Zone IDs are not secrets, but they are instance data: supplied per zone via tfvars.
variable "zone_ids" {
  description = "Cloudflare zone id per managed DNS zone, keyed by zone name."
  type        = map(string)
}
