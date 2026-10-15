terraform {
  required_version = ">= 1.5"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 6.0"
    }
  }
}

provider "oci" {
  # ~/.oci/config の DEFAULT プロファイルをそのまま使う
  region = var.region
}
