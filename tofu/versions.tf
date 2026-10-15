terraform {
  required_version = ">= 1.9"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.51"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}

# Both providers read their credentials from the environment:
#   HCLOUD_TOKEN, CLOUDFLARE_API_TOKEN
# Sourced from ~/.config/<instance>-platform/env. Never passed as variables,
# so they can never land in a .tfvars file or in state by accident.
provider "hcloud" {}
provider "cloudflare" {}
