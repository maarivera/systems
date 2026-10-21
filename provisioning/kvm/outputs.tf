output "vm_id" {
  description = "libvirt domain UUID."
  value       = libvirt_domain.server.id
}

output "ip_addresses" {
  description = "Guest IPs reported by the QEMU guest agent (empty until the agent is running)."
  value       = flatten(libvirt_domain.server.network_interface[*].addresses)
}

output "nics" {
  description = "Guest name, libvirt network and MAC address of each NIC, in order."
  value = [
    for n in local.nics : {
      name    = n.name
      network = n.network
      mac     = n.mac
    }
  ]
}
