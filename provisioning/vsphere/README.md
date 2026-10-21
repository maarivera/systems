# provisioning

Terraform that clones a Linux server from a vSphere template. CPU, memory and the number of NICs are variables, so one configuration covers every server size.

## Files

| File | Purpose |
|---|---|
| `versions.tf` | Terraform / provider versions and the vSphere provider |
| `variables.tf` | All inputs, with validation |
| `main.tf` | Lookups (datacenter, cluster, datastore, template, port groups) and the VM |
| `outputs.tf` | VM ID, IP addresses, and the port group + MAC of each NIC |
| `terraform.tfvars.example` | Example values; copy to `terraform.tfvars` |

## Usage

```bash
cd systems/provisioning/vsphere
cp terraform.tfvars.example terraform.tfvars
vi terraform.tfvars
export TF_VAR_vsphere_password='...'

terraform init
terraform plan
terraform apply

# Override sizing for one run
terraform apply -var cpu=8 -var memory_mb=16384 -var nic_count=3
```

## Sizing and NIC variables

| Variable | Default | Meaning |
|---|---|---|
| `cpu` | `2` | vCPUs (1–128) |
| `cores_per_socket` | `1` | `cpu` must be divisible by this |
| `memory_mb` | `4096` | Memory in MB, multiple of 4, at least 1024 |
| `disk_size_gb` | `40` | OS disk; never shrinks below the template's disk |
| `nic_count` | `1` | Number of NICs (1–10) |
| `networks` | — | Port groups, in NIC order. If shorter than `nic_count`, extra NICs use the last entry |
| `adapter_type` | `vmxnet3` | `vmxnet3`, `e1000e` or `e1000` |
| `ipv4_addresses` | `[]` | Static IP per NIC; `""` or missing entry = DHCP |
| `ipv4_netmask` / `ipv4_gateway` / `dns_servers` | `24` / none / `[]` | Guest network settings |

CPU and memory hot-add are enabled, so increasing `cpu` or `memory_mb` later is applied without recreating the VM (the guest OS must support hot-add). Changing `nic_count` adds or removes NICs in place.

## Requirements

- Terraform 1.5+
- A vSphere template with VMware Tools / open-vm-tools (needed for guest customization)
- A vCenter account allowed to clone VMs, assign networks and customize guests
