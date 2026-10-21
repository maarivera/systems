# --- vCenter connection -------------------------------------------------------

variable "vsphere_server" {
  description = "vCenter hostname or IP."
  type        = string
}

variable "vsphere_user" {
  description = "vCenter user."
  type        = string
}

variable "vsphere_password" {
  description = "vCenter password. Prefer TF_VAR_vsphere_password over a tfvars file."
  type        = string
  sensitive   = true
}

variable "vsphere_allow_unverified_ssl" {
  description = "Skip TLS verification for vCenter (self-signed certs)."
  type        = bool
  default     = false
}

# --- Placement ----------------------------------------------------------------

variable "datacenter" {
  description = "vSphere datacenter name."
  type        = string
}

variable "cluster" {
  description = "Compute cluster the VM runs in."
  type        = string
}

variable "datastore" {
  description = "Datastore for the VM disks."
  type        = string
}

variable "folder" {
  description = "VM folder path (relative to the datacenter). Empty for the root."
  type        = string
  default     = ""
}

variable "template" {
  description = "Name of the VM template to clone (must have VMware Tools / open-vm-tools)."
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

variable "cores_per_socket" {
  description = "Cores per socket. cpu must be divisible by this."
  type        = number
  default     = 1

  validation {
    condition     = var.cores_per_socket >= 1 && floor(var.cores_per_socket) == var.cores_per_socket
    error_message = "cores_per_socket must be a whole number >= 1."
  }
}

variable "memory_mb" {
  description = "Memory in MB (e.g. 4096 = 4 GB). Must be a multiple of 4."
  type        = number
  default     = 4096

  validation {
    condition     = var.memory_mb >= 1024 && var.memory_mb % 4 == 0
    error_message = "memory_mb must be at least 1024 and a multiple of 4."
  }
}

variable "disk_size_gb" {
  description = "Size of the OS disk in GB. Must be >= the template's disk size."
  type        = number
  default     = 40
}

# --- Networking ---------------------------------------------------------------

variable "nic_count" {
  description = "Number of network interfaces to attach."
  type        = number
  default     = 1

  validation {
    condition     = var.nic_count >= 1 && var.nic_count <= 10 && floor(var.nic_count) == var.nic_count
    error_message = "nic_count must be a whole number between 1 and 10 (vSphere limit)."
  }
}

variable "networks" {
  description = <<-EOT
    Port groups for the NICs, in order. NIC n uses networks[n]; if the list is
    shorter than nic_count, the remaining NICs use the last entry. A single
    entry puts every NIC on the same port group.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.networks) >= 1
    error_message = "networks must contain at least one port group."
  }
}

variable "adapter_type" {
  description = "Virtual NIC type for all NICs."
  type        = string
  default     = "vmxnet3"

  validation {
    condition     = contains(["vmxnet3", "e1000e", "e1000"], var.adapter_type)
    error_message = "adapter_type must be vmxnet3, e1000e or e1000."
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
  description = "Default gateway (only used when at least one static address is set)."
  type        = string
  default     = null
}

variable "dns_servers" {
  description = "DNS servers set in the guest."
  type        = list(string)
  default     = []
}
