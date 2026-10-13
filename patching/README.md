# Linux Patching with Ansible

Ansible playbooks that patch RedHat-family (RHEL, Rocky, Alma, CentOS Stream, Fedora) and Debian-family (Debian, Ubuntu) servers in four phases:

1. **Vulnerability check**: list security advisories and all available updates.
2. **Pre-patch health check**: record the system state and check free disk space.
3. **Patch**: install updates and record which packages changed.
4. **Post-patch**: optionally reboot, then check that services and ports came back.

Each phase writes a plain-text report on every target server. When you run the full workflow, a host that fails a phase is dropped from the later phases. For example, a host that is short on disk space is not patched.

## Requirements

- **Control node:** `ansible-core` 2.13 or later. Only `ansible.builtin` modules are used, so no extra collections are needed.
- **Targets:** SSH access and `sudo` rights for the Ansible user, plus Python 3.
  - RedHat family: `dnf`. `dnf needs-restarting` (from `dnf-utils` / `yum-utils`) is used to detect whether a reboot is needed.
  - Debian family: `apt`.

## Quick start

1. Add your servers to `inventory/hosts.ini` under `[patch_targets]`:

   ```ini
   [patch_targets]
   web01.example.com
   db01.example.com ansible_host=10.0.0.21

   [patch_targets:vars]
   ansible_user=ansible
   ```

2. Check the settings in `inventory/group_vars/all.yml` (see [Configuration](#configuration)).

3. Run the full workflow from this directory:

   ```bash
   ansible-playbook site.yml
   ```

   If `sudo` asks for a password, add `-K`.

Before the reboot step, phase 4 stops and asks what to do (see [Phase 4](#phase-4--post-patch-reboot-and-sanity-check-04_postpatchyml)).

## Usage examples

```bash
ansible-playbook site.yml                          # all four phases
ansible-playbook site.yml --limit web01            # one host
ansible-playbook site.yml -e security_only=true    # security updates only
ansible-playbook site.yml -e reboot_confirm=no     # skip the reboot prompt, don't reboot
ansible-playbook site.yml -e reboot_confirm=yes    # reboot every patched server without asking
ansible-playbook site.yml -e reboot_confirm=required  # reboot only servers that need it

ansible-playbook 01_vuln_check.yml                 # report only, changes nothing
ansible-playbook 02_prepatch.yml                   # health check only
ansible-playbook site.yml --check                  # dry run (see note below)
```

> **Dry-run note:** `--check` skips the command-based tasks, so reports may be incomplete. To see what *would* be patched, run `01_vuln_check.yml` instead. It does not change the system, apart from refreshing the apt cache on Debian.

## Repository layout

```
patching/
├── ansible.cfg                    # Ansible settings (inventory path, sudo, forks, output format)
├── site.yml                       # Runs all four phases in order
├── 01_vuln_check.yml              # Phase 1: list applicable/security updates
├── 02_prepatch.yml                # Phase 2: health check + disk space gate
├── 03_patch.yml                   # Phase 3: apply updates
├── 04_postpatch.yml               # Phase 4: reboot prompt + sanity checks
├── inventory/
│   ├── hosts.ini                  # Target servers (group: patch_targets)
│   └── group_vars/all.yml         # All tunable settings
├── tasks/
│   └── check_reboot_required.yml  # Shared: sets `reboot_required` per host
└── templates/
    ├── vuln_report.j2             # Phase 1 report
    ├── prepatch_report.j2         # Phase 2 report
    ├── patch_report.j2            # Phase 3 report
    └── postpatch_report.j2        # Phase 4 report
```

## Phases

### Phase 1 – Vulnerability check (`01_vuln_check.yml`)

Read-only. Lists the updates that are available for each host.

- **RedHat:** `dnf updateinfo list --security` (security advisories) and `dnf check-update` (all updates).
- **Debian:** refreshes the apt cache and simulates `apt-get dist-upgrade`. Packages that come from a `*-security` repository count as security updates.

**Report:** `/var/log/vuln_check_<YYYYMMDD>.log`

### Phase 2 – Pre-patch health check (`02_prepatch.yml`)

Records the server's state before it changes:

- uptime and load, last boot time
- disk space and inodes, memory, CPU details and utilisation
- top 15 processes by CPU and by memory
- running services, listening ports, failed systemd units

It also saves the list of running services to `/var/lib/patching/services_before.txt`. Phase 4 compares against this list.

**Gate:** the host **fails** if any mount listed in `min_free_mb` has less free space than its threshold. Mounts that don't exist on a host are skipped.

**Report:** `/var/log/prepatch_check_<YYYYMMDD>.log`

### Phase 3 – Patch (`03_patch.yml`)

- **RedHat:** `dnf` upgrades all packages (`*`), or only security updates when `security_only: true`. Packages in `patch_exclude` are skipped.
- **Debian:** the packages in `patch_exclude` are put on hold, then the playbook runs `apt dist-upgrade`, or installs only the security packages when `security_only: true`. The holds are always released afterwards, even if the upgrade fails. `NEEDRESTART_MODE=l` stops Ubuntu's needrestart from restarting services in the middle of the run.

The playbook compares the installed package list before and after the upgrade to show exactly what changed, then checks whether a reboot is needed. If the upgrade fails, the report is still written with the error, and then the host is marked as failed.

**Report:** `/var/log/patching_<YYYYMMDD>.log`

### Phase 4 – Post-patch reboot and sanity check (`04_postpatch.yml`)

1. Shows which servers need a reboot and asks once for all of them:
   - `yes`: reboot all patched servers
   - `required`: reboot only the servers that need it
   - `no`: don't reboot; the sanity checks still run

   Pass `-e reboot_confirm=yes|required|no` to skip the prompt, e.g. for cron or CI.
2. Reboots the chosen servers and waits up to `reboot_timeout` seconds for them to come back.
3. Runs the sanity checks. The host **fails** if:
   - any service in `critical_services` is not running
   - any port in `sanity_ports` is not listening (each port is retried for about 60 s)
   - a service that was running at pre-patch is now stopped. Patterns in `services_ignore_missing` are ignored, and this check can be turned off with `fail_on_missing_services: false`.

The report also lists services that have started since pre-patch, failed systemd units, the running kernel, and whether a reboot is still needed.

**Report:** `/var/log/postpatch_check_<YYYYMMDD>.log`

### Shared task – `tasks/check_reboot_required.yml`

Sets the `reboot_required` fact for each host:

- **RedHat:** `dnf needs-restarting -r`. Any non-zero exit code counts as "reboot required", to be safe.
- **Debian:** checks whether `/var/run/reboot-required` exists.

## Configuration

All settings are in `inventory/group_vars/all.yml`. You can override any of them per host or per group in the inventory, or for a single run with `-e`.

| Variable | Default | Description |
|---|---|---|
| `log_dir` | `/var/log` | Directory on each target where reports are written. |
| `log_date` | today (`YYYYMMDD`) | Date suffix used in report file names. |
| `state_dir` | `/var/lib/patching` | Where the pre-patch services snapshot is stored. |
| `min_free_mb` | `/: 2048`, `/var: 2048`, `/boot: 200` | Minimum free MB per mount. If a mount is below its threshold, phase 2 fails for that host. |
| `security_only` | `false` | `true` = install only security updates. |
| `patch_exclude` | `[]` | Packages to leave alone. RedHat accepts globs (`kernel*`); Debian needs exact package names. |
| `reboot_timeout` | `1200` | Seconds to wait for a server to come back after a reboot. |
| `critical_services` | `ssh` (Debian) / `sshd` (RedHat) | Services that must be running after patching. The `.service` suffix is optional. |
| `sanity_ports` | `[]` | TCP ports that must be listening after patching, e.g. `[22, 80, 443]`. |
| `fail_on_missing_services` | `true` | Fail the host if a service that was running before patching is now stopped. |
| `services_ignore_missing` | see file | Regexes for on-demand or short-lived services that shouldn't be reported as missing. |
| `reboot_confirm` | *(unset → prompt)* | `yes` / `required` / `no`. Set it to skip the interactive reboot prompt. |

### Example: per-group overrides

```ini
# inventory/hosts.ini
[web]
web01.example.com
web02.example.com

[db]
db01.example.com

[patch_targets:children]
web
db
```

```yaml
# inventory/group_vars/web.yml
critical_services: [nginx, sshd]
sanity_ports: [22, 80, 443]

# inventory/group_vars/db.yml
critical_services: [postgresql, sshd]
sanity_ports: [22, 5432]
patch_exclude: ['postgresql*']
```

## Reports

Reports are written **on each target server**, not on the control node:

| Phase | File |
|---|---|
| 1 | `/var/log/vuln_check_<YYYYMMDD>.log` |
| 2 | `/var/log/prepatch_check_<YYYYMMDD>.log` |
| 3 | `/var/log/patching_<YYYYMMDD>.log` |
| 4 | `/var/log/postpatch_check_<YYYYMMDD>.log` |

To copy them to the control node:

```bash
ansible patch_targets -b -m fetch \
  -a "src=/var/log/patching_$(date +%Y%m%d).log dest=./reports/ flat=no"
```

A short summary for each host is also printed in the Ansible output at the end of every phase.

## Troubleshooting

- **Pre-patch fails on disk space:** free up space on the mount named in the error, or lower its value in `min_free_mb`, then run again.
- **Sanity check reports stopped services that don't matter:** add a regex for them to `services_ignore_missing`.
- **"Pre-patch snapshot not found" in the post-patch report:** phase 4 was run without phase 2. Run `02_prepatch.yml` first if you want the services comparison.
- **RedHat host always shows "reboot required":** install `dnf-utils` (or `yum-utils`) so that `dnf needs-restarting` is available.
