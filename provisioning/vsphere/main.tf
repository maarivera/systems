data "vsphere_datacenter" "dc" {
  name = var.datacenter
}

data "vsphere_compute_cluster" "cluster" {
  name          = var.cluster
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_datastore" "ds" {
  name          = var.datastore
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_virtual_machine" "template" {
  name          = var.template
  datacenter_id = data.vsphere_datacenter.dc.id
}

data "vsphere_network" "net" {
  for_each      = toset(var.networks)
  name          = each.value
  datacenter_id = data.vsphere_datacenter.dc.id
}

locals {
  # One entry per NIC: which port group it lands on and its address ("" = DHCP).
  nics = [
    for i in range(var.nic_count) : {
      network = var.networks[min(i, length(var.networks) - 1)]
      ipv4    = try(var.ipv4_addresses[i], "")
    }
  ]

  any_static = anytrue([for n in local.nics : n.ipv4 != ""])
}

resource "vsphere_virtual_machine" "server" {
  name             = var.vm_name
  folder           = var.folder != "" ? var.folder : null
  resource_pool_id = data.vsphere_compute_cluster.cluster.resource_pool_id
  datastore_id     = data.vsphere_datastore.ds.id

  num_cpus               = var.cpu
  num_cores_per_socket   = var.cores_per_socket
  memory                 = var.memory_mb
  cpu_hot_add_enabled    = true
  memory_hot_add_enabled = true

  guest_id  = data.vsphere_virtual_machine.template.guest_id
  scsi_type = data.vsphere_virtual_machine.template.scsi_type
  firmware  = data.vsphere_virtual_machine.template.firmware

  dynamic "network_interface" {
    for_each = local.nics
    content {
      network_id   = data.vsphere_network.net[network_interface.value.network].id
      adapter_type = var.adapter_type
    }
  }

  disk {
    label            = "disk0"
    size             = max(var.disk_size_gb, data.vsphere_virtual_machine.template.disks[0].size)
    thin_provisioned = data.vsphere_virtual_machine.template.disks[0].thin_provisioned
  }

  clone {
    template_uuid = data.vsphere_virtual_machine.template.id

    customize {
      linux_options {
        host_name = var.vm_name
        domain    = var.domain
      }

      # One block per NIC, in the same order as network_interface above.
      # Empty blocks mean DHCP.
      dynamic "network_interface" {
        for_each = local.nics
        content {
          ipv4_address = network_interface.value.ipv4 != "" ? network_interface.value.ipv4 : null
          ipv4_netmask = network_interface.value.ipv4 != "" ? var.ipv4_netmask : null
        }
      }

      ipv4_gateway    = local.any_static ? var.ipv4_gateway : null
      dns_server_list = var.dns_servers
      dns_suffix_list = [var.domain]
    }
  }

  lifecycle {
    precondition {
      condition     = var.cpu % var.cores_per_socket == 0
      error_message = "cpu (${var.cpu}) must be divisible by cores_per_socket (${var.cores_per_socket})."
    }
    precondition {
      condition     = length(var.ipv4_addresses) <= var.nic_count
      error_message = "ipv4_addresses has more entries than nic_count."
    }
  }
}
