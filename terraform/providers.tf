# Zugangsdaten ausschliesslich ueber Umgebungsvariablen aus secrets.env:
# VSPHERE_SERVER, VSPHERE_USER, VSPHERE_PASSWORD.
provider "vsphere" {
  # Zertifikatspruefung deaktiviert, symmetrisch zu VMWARE_VALIDATE_CERTS=false
  # auf der Ansible-Seite (AKI/SKI-Mismatch der VMCA, s. Kontext_WAB.md).
  allow_unverified_ssl = true
}
