locals {
  inv = yamldecode(file(coalesce(var.inventory_path, "${path.module}/../inventory.yaml")))

  name_prefix = local.inv.naming.terraform_prefix # z. B. "wab-tf-"
  hw          = local.inv.hardware
  t           = local.inv.timeouts_min # Einheit MINUTEN; Ansible rechnet x 60

  # id -> Spec-Name. format/tonumber faengt ab, dass yamldecode ungequotetes 01
  # als Zahl liefert; Schluessel sind immer zweistellige Zeichenketten.
  slots = { for s in local.inv.slots : format("%02d", tonumber(s.id)) => s.spec }

  # Die ersten vm_count Slots in aufsteigender Reihenfolge
  selected = {
    for id in slice(sort(keys(local.slots)), 0, min(var.vm_count, length(local.slots))) :
    id => local.slots[id]
  }

  # Timeouts muessen ganze Minuten > 0 sein (0 schaltet beim Provider Waiter ab)
  timeouts_valid = alltrue([
    for k in ["clone", "customization", "guest_net", "shutdown"] :
    can(tonumber(local.t[k])) && try(tonumber(local.t[k]) > 0 && floor(tonumber(local.t[k])) == tonumber(local.t[k]), false)
  ])
}

data "vsphere_datacenter" "dc" {
  name = local.inv.datacenter
}

# Kein Cluster im Labor: Host direkt, Ressourcen aus seinem Root-Resource-Pool
data "vsphere_host" "host" {
  name          = local.inv.esxi_hostname
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_datastore" "ds" {
  name          = local.inv.datastore
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_network" "net" {
  name          = local.inv.network.portgroup
  datacenter_id = data.vsphere_datacenter.dc.id
}

# Pfad relativ zum VM-Ordner des Datacenters: <Template-Ordner>/<Template-Name>
data "vsphere_virtual_machine" "template" {
  name          = "${local.inv.tf_template_path}/${local.inv.template_name}"
  datacenter_id = data.vsphere_datacenter.dc.id
}

# Benannte Slot-Specs, nur referenziert (Netzwerk und Hostname kommen aus der Spec)
data "vsphere_guest_os_customization" "slot" {
  for_each = local.selected
  name     = each.value
}

resource "vsphere_virtual_machine" "vm" {
  for_each = local.selected

  name             = "${local.name_prefix}${each.key}"
  resource_pool_id = data.vsphere_host.host.resource_pool_id
  host_system_id   = data.vsphere_host.host.id
  datastore_id     = data.vsphere_datastore.ds.id
  folder           = local.inv.tf_vcenter_folder

  num_cpus = local.hw.num_cpus
  memory   = local.hw.memory_mb

  # Vom Template uebernommen, damit der Klon dem Template entspricht
  guest_id  	 = data.vsphere_virtual_machine.template.guest_id
  firmware  	 = data.vsphere_virtual_machine.template.firmware
  scsi_type 	 = data.vsphere_virtual_machine.template.scsi_type
  enable_logging = true

  extra_config = { "ethernet0.pciSlotNumber" = "192" }
  
  network_interface {
    network_id   = data.vsphere_network.net.id
    adapter_type = data.vsphere_virtual_machine.template.network_interface_types[0]
  }

  disk {
    label            = "disk0"
    size             = local.hw.disk_size_gb
    thin_provisioned = local.hw.disk_thin
    eagerly_scrub    = false
  }

  clone {
    template_uuid = data.vsphere_virtual_machine.template.id
    linked_clone  = false # Full Clone, explizit
    timeout       = local.t.clone

    customization_spec {
      id      = data.vsphere_guest_os_customization.slot[each.key].id
      timeout = local.t.customization
    }
  }

  # Messpunkt B: Ende = Customization abgeschlossen und IP ueber Tools gemeldet.
  # routable = false: nur auf eine gemeldete Adresse warten, nicht auf eine
  # zum Gateway passende - gleiches Ereignis wie wait_for_ip_address in Ansible.
  wait_for_guest_net_timeout  = local.t.guest_net
  wait_for_guest_net_routable = false
  wait_for_guest_ip_timeout   = 0 # alternativer IP-Waiter explizit aus

  # Reset (terraform destroy): geordnet ueber VMware Tools, danach hart
  shutdown_wait_timeout = local.t.shutdown
  force_power_off       = true

  lifecycle {
    precondition {
      condition     = local.timeouts_valid
      error_message = "timeouts_min in inventory.yaml: clone, customization, guest_net und shutdown muessen ganze Minuten > 0 sein."
    }
    precondition {
      condition     = length(local.slots) >= var.vm_count
      error_message = "inventory.yaml enthaelt weniger Slots als vm_count."
    }
    precondition {
      condition     = data.vsphere_virtual_machine.template.disks[0].size == local.hw.disk_size_gb
      error_message = "hardware.disk_size_gb weicht von der Template-Disk ab; ein Klon wuerde vergroessert."
    }
    precondition {
      condition     = data.vsphere_virtual_machine.template.disks[0].thin_provisioned == local.hw.disk_thin
      error_message = "hardware.disk_thin passt nicht zur Template-Disk."
    }
  }
}
