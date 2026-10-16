# Alle Objektreferenzen, Hardwarewerte und Timeouts stammen aus inventory.yaml
# (gemeinsame Quelle fuer Terraform, Ansible und die Python-Skripte).
# Hier bleiben nur die Werte, die je Aufruf gesetzt werden.

# Parallelitaetsstufe p; mit dem Design N = p zugleich die VM-Zahl der Welle.
# Je Aufruf setzen: -var vm_count=<p> (auch bei terraform destroy).
variable "vm_count" {
  type = number
  validation {
    condition     = contains([1, 2, 4, 8, 16, 32], var.vm_count)
    error_message = "vm_count muss eine Stufe der Messreihe sein: 1, 2, 4, 8, 16 oder 32."
  }
}

variable "inventory_path" {
  type    = string
  default = null # null = ../inventory.yaml relativ zu diesem Verzeichnis
}
