# scripts

Small helper scripts that workflows chain between the Terraform and Ansible projects.

| File | What it does |
|---|---|
| `preflight.sh` | Checks that the tools a run needs (`ansible-playbook`, `terraform`, `python3`, `ssh`) are installed and that there is free disk space. Exits 1 if anything is missing. |
| `wait_for_ssh.py` | Waits until hosts send an SSH banner, for example right after Terraform creates a VM and before Ansible connects. Reads hosts from arguments, `HOSTS`, or a Terraform `ip_addresses` output (`TF_IPS_FILE`). |

## Usage

```bash
cd systems/scripts

bash preflight.sh
REQUIRED="ansible-playbook ssh" MIN_FREE_MB=500 bash preflight.sh

python3 wait_for_ssh.py 10.0.0.5 10.0.0.6
TIMEOUT=600 TF_IPS_FILE=../provisioning/kvm/ips.json python3 wait_for_ssh.py
```

Both use only bash and the Python standard library.
