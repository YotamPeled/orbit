output "server_ipv4" {
  description = "Public IPv4 of the node."
  value       = hcloud_server.prod.ipv4_address
}

output "server_ipv6" {
  description = "Public IPv6 network of the node."
  value       = hcloud_server.prod.ipv6_address
}

output "ssh" {
  description = "Copy-paste to reach the box."
  value       = "ssh -i ${var.ssh_private_key_path} root@${hcloud_server.prod.ipv4_address}"
}

output "kube_tunnel" {
  description = "The API server is firewalled off; kubectl goes through this tunnel."
  value       = "ssh -i ${var.ssh_private_key_path} -N -L 6443:127.0.0.1:6443 root@${hcloud_server.prod.ipv4_address}"
}
