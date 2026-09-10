terraform {
  required_providers {
    vsphere = {
      source  = "vmware/vsphere"
      version = "2.15.2"
    }
  }
}

provider "vsphere" {
  vsphere_server       = var.vsphere_server
  user                 = var.vsphere_user
  password             = var.vsphere_password
  allow_unverified_ssl = true
}

