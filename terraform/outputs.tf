# Fuer Wrapper und validate_vms.py (--prefix wab-tf --slots ...)
output "slot_ids" {
  value = sort(keys(vsphere_virtual_machine.vm))
}

output "vm_names" {
  value = sort([for vm in vsphere_virtual_machine.vm : vm.name])
}
