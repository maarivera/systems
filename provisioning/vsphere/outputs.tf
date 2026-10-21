output "vm_id" {
  description = "Managed object ID of the VM."
  value       = vsphere_virtual_machine.server.moid
}

output "default_ip_address" {
  description = "Primary IP reported by VMware Tools."
  value       = vsphere_virtual_machine.server.default_ip_address
}

output "ip_addresses" {
  description = "All guest IPs reported by VMware Tools."
  value       = vsphere_virtual_machine.server.guest_ip_addresses
}

output "nics" {
  description = "Port group and MAC address of each NIC, in order."
  value = [
    for i, nic in vsphere_virtual_machine.server.network_interface : {
      network = local.nics[i].network
      mac     = nic.mac_address
    }
  ]
}
