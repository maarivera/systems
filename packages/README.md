# packages

Install a list of packages on target servers. The list lives in a plain text file, so changing what gets installed never means editing the playbook.

## Files

| File | Purpose |
|---|---|
| `install_packages.yml` | The playbook |
| `files/packages.txt` | Default package list, one per line (`#` comments and blank lines ignored) |
| `inventory/hosts.ini` | Target servers, in the `[package_targets]` group |
| `inventory/group_vars/all.yml` | `packages_file`, `package_state`, report location |

## Usage

```bash
cd systems/packages
vi inventory/hosts.ini                # add servers under [package_targets]
vi files/packages.txt                 # list the packages

ansible-playbook install_packages.yml --check    # dry run
ansible-playbook install_packages.yml -K         # install (-K if sudo needs a password)

# Use a different list for one run
ansible-playbook install_packages.yml -e packages_file=/path/to/web_packages.txt
```

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `packages_file` | `files/packages.txt` | Package list on the control node. Set it in `group_vars/<group>.yml` to give each group its own list. |
| `package_state` | `present` | `present` installs missing packages only; `latest` also upgrades them. |
| `log_dir` | `/var/log` | Where the report is written on each server. |

## Targets

RHEL family (dnf) and Debian / Ubuntu (apt). Package names must exist in the target OS's repositories. If they differ between distros, use one list file per OS group.

## Report

Each run writes `<log_dir>/package_install_<YYYYMMDD>.log` on every server. It lists the requested packages and every package that was installed or upgraded, including dependencies.
