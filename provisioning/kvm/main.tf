locals {
  # One entry per NIC. The MAC is derived from the VM name so cloud-init can
  # match each interface reliably, whatever the guest names it (eth0, enp1s0...).
  nics = [
    for i in range(var.nic_count) : {
      name    = "eth${i}"
      network = var.networks[min(i, length(var.networks) - 1)]
      ipv4    = try(var.ipv4_addresses[i], "")
      mac = format("52:54:00:%s:%s:%s",
        substr(md5("${var.vm_name}-${i}"), 0, 2),
        substr(md5("${var.vm_name}-${i}"), 2, 2),
        substr(md5("${var.vm_name}-${i}"), 4, 2),
      )
    }
  ]

  first_static = try([for n in local.nics : n.name if n.ipv4 != ""][0], null)

  user_data = "#cloud-config\n${yamlencode({
    hostname         = var.vm_name
    fqdn             = "${var.vm_name}.${var.domain}"
    manage_etc_hosts = true
    users = [{
      name                = var.ssh_user
      groups              = ["wheel", "sudo"]
      shell               = "/bin/bash"
      sudo                = "ALL=(ALL) NOPASSWD:ALL"
      ssh_authorized_keys = var.ssh_public_keys
    }]
    ssh_pwauth = false
    packages   = ["qemu-guest-agent"]
    runcmd     = [["systemctl", "enable", "--now", "qemu-guest-agent"]]
  })}"

  network_config = yamlencode({
    version = 2
    ethernets = {
      for n in local.nics : n.name => merge(
        {
          match    = { macaddress = n.mac }
          set-name = n.name
        },
        # Tuple indexing instead of ?: because the branches are different object types.
        [
          { dhcp4 = true },
          {
            dhcp4       = false
            addresses   = ["${n.ipv4}/${var.ipv4_netmask}"]
            nameservers = { addresses = var.dns_servers, search = [var.domain] }
          },
        ][n.ipv4 == "" ? 0 : 1],
        [
          {},
          { routes = [{ to = "default", via = var.ipv4_gateway }] },
        ][n.name == local.first_static && var.ipv4_gateway != null ? 1 : 0],
      )
    }
  })
}

resource "libvirt_volume" "base" {
  name   = "${var.vm_name}-base.qcow2"
  pool   = var.storage_pool
  source = var.base_image
  format = "qcow2"
}

resource "libvirt_volume" "root" {
  name           = "${var.vm_name}-root.qcow2"
  pool           = var.storage_pool
  base_volume_id = libvirt_volume.base.id
  size           = var.disk_size_gb * 1024 * 1024 * 1024
  format         = "qcow2"
}

resource "libvirt_cloudinit_disk" "init" {
  name           = "${var.vm_name}-cloudinit.iso"
  pool           = var.storage_pool
  user_data      = local.user_data
  network_config = local.network_config
}

resource "libvirt_domain" "server" {
  name       = var.vm_name
  vcpu       = var.cpu
  memory     = var.memory_mb
  cloudinit  = libvirt_cloudinit_disk.init.id
  qemu_agent = true
  autostart  = true

  cpu {
    mode = "host-passthrough"
  }

  dynamic "network_interface" {
    for_each = local.nics
    content {
      network_name = network_interface.value.network
      mac          = network_interface.value.mac
    }
  }

  disk {
    volume_id = libvirt_volume.root.id
  }

  console {
    type        = "pty"
    target_type = "serial"
    target_port = "0"
  }

  graphics {
    type        = "vnc"
    listen_type = "address"
  }

  lifecycle {
    precondition {
      condition     = length(var.ipv4_addresses) <= var.nic_count
      error_message = "ipv4_addresses has more entries than nic_count."
    }
  }
}
