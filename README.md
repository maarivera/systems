# systems

Practical automation for day-to-day Linux systems administration.

Each folder is a self-contained Ansible project for a real operational task. Clone the repo, point a project at your inventory, and run it against your environments: dev, staging, or production.

## Automations

| Project | What it does | Targets |
|---|---|---|
| [`patching/`](patching/) | End-to-end OS patching: vulnerability check → pre-patch health gate → patch → reboot prompt → post-patch sanity check. Each phase writes a report on every server. | RHEL family (dnf), Debian / Ubuntu (apt) |
| [`offboarding/`](offboarding/) | Event-Driven Ansible user offboarding: an HR termination webhook triggers removal of access from SSO, Entra ID, email, Teams, Azure and AWS. Dry run by default; writes a report per user. | Okta, Entra ID, Exchange Online, Teams, Azure RBAC, AWS IAM / Identity Center |
| [`packages/`](packages/) | Install packages read from a list file (`files/packages.txt` by default, overridable per group or per run). Writes a report of what was installed. | RHEL family (dnf), Debian / Ubuntu (apt) |
| [`automation-hub/`](automation-hub/) | Web UI to catalog these automations, chain them into workflows with approval steps, simulate runs and export them as bash runners. Python 3.9+ only. See its README for setup. | Runs on the control node |

See each project's own README for full details and configuration.

## Design principles

- **Check before changing.** Read-only checks run first, and a host that fails a gate (for example, low disk space) is dropped before anything is changed on it.
- **Nothing disruptive without confirmation.** Reboots ask first, unless you explicitly pass an answer for unattended runs.
- **Leave evidence.** Every run writes plain-text reports under `/var/log` on each server, so you can attach them to change tickets and audits.
- **Configure, don't edit.** Behaviour is controlled from `inventory/group_vars`, so the playbooks stay the same across environments.
- **No extra dependencies.** Only `ansible.builtin` modules are used, so no Galaxy collections need to be installed.
- **Run it whole or in parts.** Each phase is its own playbook, and `site.yml` runs them all in order.

## Requirements

- **Control node:** Linux, macOS, or WSL with `ansible-core` installed.
- **Target servers:** SSH access, a user with `sudo` rights, and Python 3.

## Getting started

```bash
git clone https://github.com/maarivera/systems.git
cd systems/patching

vi inventory/hosts.ini              # add your servers under [patch_targets]
vi inventory/group_vars/all.yml     # review thresholds, critical services, ports

ansible-playbook 01_vuln_check.yml  # read-only: what would be patched?
ansible-playbook site.yml -K        # full run (-K if sudo needs a password)
```

## Using it in real environments

The playbooks are built to be safe, but you are still patching live systems. A rollout that works well in practice:

1. **Test in non-production first**, on at least one server for each OS and version you run.
2. **Run the read-only phases ahead of the maintenance window.** Use `01_vuln_check.yml` to see what's pending and `02_prepatch.yml` to catch disk-space failures, so you can fix them before the window starts rather than during it.
3. **Take backups or snapshots first.** The playbooks do not snapshot VMs or back up data.
4. **Patch in waves** with `--limit`: non-prod → a canary host → the rest. The reboot prompt applies to every host in the run, so never put all members of a cluster or load-balanced pool in the same run.
5. **Define "healthy" for each server group.** Set `critical_services` and `sanity_ports` in group vars (e.g. `inventory/group_vars/web.yml`) so the post-patch check verifies your applications, not just the OS.
6. **Keep the reports.** Collect them from the servers for your change record (see [patching/README.md → Reports](patching/README.md#reports)).
7. **For unattended or scheduled runs**, pass the reboot decision up front so nothing waits on a prompt:

   ```bash
   ansible-playbook site.yml -e reboot_confirm=required   # or yes / no
   ```

### One inventory per environment

Keep each environment's servers and settings apart by copying the `inventory/` directory, including its `group_vars/`:

```
patching/
├── inventory/            # default (dev)
├── inventory-staging/
└── inventory-prod/
```

```bash
ansible-playbook -i inventory-prod/hosts.ini site.yml --limit web
```

## Adding a new automation

To keep the repo consistent, new projects should follow the same layout as `patching/`:

- A top-level folder with its own `ansible.cfg`, `inventory/`, `site.yml`, and `README.md`.
- Settings in `inventory/group_vars/all.yml`, with sensible defaults.
- Read-only checks before changes, and a clear pass/fail result for each host.
- Reports written on the target servers.
