# --- Hypervisor ---------------------------------------------------------------

variable "libvirt_uri" {
  description = "libvirt connection URI. Use qemu+ssh://user@host/system for a remote KVM host."
  type        = string
  default     = "qemu:///system"
}

variable "storage_pool" {
  description = "libvirt storage pool for the VM disks."
  type        = string
  default     = "default"
}

variable "base_image" {
  description = "Cloud image (qcow2) to build the VM from: a URL or a path on the KVM host."
  type        = string
}

# --- Server sizing ------------------------------------------------------------

variable "vm_name" {
  description = "VM name, also used as the guest hostname."
  type        = string
}

variable "domain" {
  description = "DNS domain set in the guest."
  type        = string
  default     = "localdomain"
}

variable "cpu" {
  description = "Number of vCPUs."
  type        = number
  default     = 2

  validation {
    condition     = var.cpu >= 1 && var.cpu <= 128 && floor(var.cpu) == var.cpu
    error_message = "cpu must be a whole number between 1 and 128."
  }
}

variable "memory_mb" {
  description = "Memory in MB (e.g. 4096 = 4 GB)."
  type        = number
  default     = 4096

  validation {
    condition     = var.memory_mb >= 512 && floor(var.memory_mb) == var.memory_mb
    error_message = "memory_mb must be a whole number of at least 512."
  }
}

variable "disk_size_gb" {
  description = "Size of the OS disk in GB. Must be >= the cloud image's virtual size."
  type        = number
  default     = 20
}

# --- Networking ---------------------------------------------------------------

variable "nic_count" {
  description = "Number of network interfaces to attach."
  type        = number
  default     = 1

  validation {
    condition     = var.nic_count >= 1 && var.nic_count <= 16 && floor(var.nic_count) == var.nic_count
    error_message = "nic_count must be a whole number between 1 and 16."
  }
}

variable "networks" {
  description = <<-EOT
    libvirt networks for the NICs, in order. NIC n uses networks[n]; if the
    list is shorter than nic_count, the remaining NICs use the last entry.
  EOT
  type        = list(string)
  default     = ["default"]

  validation {
    condition     = length(var.networks) >= 1
    error_message = "networks must contain at least one libvirt network."
  }
}

variable "ipv4_addresses" {
  description = <<-EOT
    Static IPv4 address per NIC, in order. Leave empty (or set an entry to "")
    for DHCP on that NIC.
  EOT
  type        = list(string)
  default     = []
}

variable "ipv4_netmask" {
  description = "Prefix length used for every static address."
  type        = number
  default     = 24
}

variable "ipv4_gateway" {
  description = "Default gateway, set on the first NIC with a static address."
  type        = string
  default     = null
}

variable "dns_servers" {
  description = "DNS servers for NICs with a static address."
  type        = list(string)
  default     = []
}

# --- Access -------------------------------------------------------------------

variable "ssh_user" {
  description = "Admin user created by cloud-init (passwordless sudo)."
  type        = string
  default     = "admin"
}

variable "ssh_public_keys" {
  description = "SSH public keys authorised for ssh_user."
  type        = list(string)

  validation {
    condition     = length(var.ssh_public_keys) >= 1
    error_message = "Provide at least one SSH public key."
  }
}
