# provisioning/kvm

Terraform that builds a Linux server on a KVM host through libvirt, from a qcow2 cloud image configured by cloud-init. CPU, memory and the number of NICs are variables.

## Files

| File | Purpose |
|---|---|
| `versions.tf` | Terraform / provider versions and the libvirt provider |
| `variables.tf` | All inputs, with validation |
| `main.tf` | Base image, root disk, cloud-init disk (user + network config) and the VM |
| `outputs.tf` | VM UUID, IP addresses, and the name, network and MAC of each NIC |
| `terraform.tfvars.example` | Example values; copy to `terraform.tfvars` |

## Usage

```bash
cd systems/provisioning/kvm
cp terraform.tfvars.example terraform.tfvars
vi terraform.tfvars

terraform init
terraform plan
terraform apply

# Override sizing for one run
terraform apply -var cpu=8 -var memory_mb=16384 -var nic_count=3

ssh admin@<ip from the ip_addresses output>
```

## Sizing and NIC variables

| Variable | Default | Meaning |
|---|---|---|
| `cpu` | `2` | vCPUs (1–128) |
| `memory_mb` | `4096` | Memory in MB, at least 512 |
| `disk_size_gb` | `20` | Root disk; must be at least the cloud image's virtual size |
| `nic_count` | `1` | Number of NICs (1–16) |
| `networks` | `["default"]` | libvirt networks, in NIC order. If shorter than `nic_count`, extra NICs use the last entry |
| `ipv4_addresses` | `[]` | Static IP per NIC; `""` or missing entry = DHCP |
| `ipv4_netmask` / `ipv4_gateway` / `dns_servers` | `24` / none / `[]` | Settings for static NICs; the gateway goes on the first static NIC |
| `ssh_user` / `ssh_public_keys` | `admin` / — | Admin user created by cloud-init, key-only SSH, passwordless sudo |

Each NIC gets a MAC derived from the VM name, and the guest renames it `eth0`, `eth1`, … to match, so the static/DHCP settings always land on the right interface.

CPU, memory and NIC changes are applied by libvirt redefining the domain; the VM may restart. cloud-init only runs on first boot, so changing the IP or user settings later needs a rebuild (`terraform apply -replace=libvirt_domain.server`).

## Requirements

- Terraform 1.5+
- A KVM host with libvirt, the storage pool and the networks already defined (`virsh pool-list`, `virsh net-list`)
- `mkisofs` / `genisoimage` on the machine running Terraform (used to build the cloud-init ISO)
- The user running Terraform in the `libvirt` group, or root, or SSH access for a `qemu+ssh://` URI

## Troubleshooting

- **`Permission denied` on the disk when the VM starts** (Ubuntu/Debian hosts with AppArmor): set `security_driver = "none"` in `/etc/libvirt/qemu.conf` and restart `libvirtd`, or put the pool under `/var/lib/libvirt/images`.
- **`ip_addresses` is empty**: the QEMU guest agent is installed by cloud-init on first boot; run `terraform refresh` a minute after apply.
