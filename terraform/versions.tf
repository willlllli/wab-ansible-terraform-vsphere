# Versionen fest pinnen: tatsaechlich installierte Staende (Laborprotokoll).
terraform {
  required_version = "= 1.16.3"

  required_providers {
    vsphere = {
      source  = "vmware/vsphere"
      version = "= 2.15.2"
    }
  }
}
