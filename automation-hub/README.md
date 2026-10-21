# automation-hub

A web UI for the automations in this repository. Store each playbook, Terraform module, rulebook or script in a catalog, design workflows that chain them together (with approval steps), simulate a run, then export the workflow as a bash runner to execute it for real.

The hub is a single Python server (`server.py`) with a SQLite database. It uses the standard library only, so there is nothing to `pip install`.

## Requirements

- **Python 3.9 or newer** (`python3 --version`).
- **Linux, macOS, or WSL 2.** `runner.sh` needs `bash`.
- **Optional tools for syntax checks.** The hub validates files with whichever of these are on the `PATH`, and falls back to built-in checks for the rest:

  | Tool | Used for |
  |---|---|
  | `ansible-core` | `ansible-playbook --syntax-check`, `ansible-inventory` |
  | `ansible-rulebook` | Event-Driven Ansible rulebooks (`offboarding/`) |
  | `terraform` | `terraform fmt`, and `terraform validate` once a module has been initialised |
  | `shellcheck` | Lint for shell scripts, on top of `bash -n` |

  Testing a workflow in the visualizer lists the tools found under **Tools on this machine**.

The hub never connects to hosts or cloud APIs. Checks are read-only, and workflow runs inside the hub are simulations.

## 1. Start the hub

From the repository root:

```bash
cd automation-hub
./runner.sh
```

The hub starts on <http://localhost:8080/> and opens it in your browser. Leave the terminal open; press **Ctrl+C** to stop it.

| Command | What it does |
|---|---|
| `./runner.sh` | Start on port 8080 |
| `./runner.sh 9000` | Start on another port |
| `./runner.sh stop` | Stop a hub running in another terminal |
| `./runner.sh reset-password <user>` | Set a new password for a user (asks for it) |
| `NO_OPEN=1 ./runner.sh` | Don't open a browser |
| `HOST=0.0.0.0 ./runner.sh` | Listen on every interface (see [Access from other machines](#access-from-other-machines)) |
| `VERBOSE=1 ./runner.sh` | Log every request |
| `DATA=/srv/hub ./runner.sh` | Keep the database somewhere other than `automation-hub/data/` |

Running `./runner.sh` again restarts a hub that is already running. If the port is used by another program, it tells you so; pick another port.

**On WSL 2** the hub listens on all interfaces automatically, because Windows can only reach WSL servers that do. It opens the page in your Windows browser with `wslview` or `explorer.exe`.

## 2. Create the admin account

The first time you open the hub it has no users, so it asks you to create one. That first account is an **admin**. Passwords need at least 8 characters.

## 3. Load the automations

The catalog comes pre-filled with this repository's projects (`patching/`, `packages/`, `offboarding/`, `provisioning/`, `scripts/`), but their files are not in the database yet.

1. Go to the **Catalog** page.
2. Click **Sync all from repo**. The hub copies each project's text files from the repository into the database, and keeps a version history for every file.

Secrets and state are never imported: `terraform.tfvars`, `*.auto.tfvars`, `*.tfstate`, `.env` and `crash.log` are skipped, as are `.terraform/`, `.git/` and `reports/` folders. Run **Sync all from repo** again whenever you change files in the repository.

To get full Terraform validation, initialise each module once:

```bash
cd provisioning/kvm && terraform init
cd ../vsphere && terraform init
```

## 4. Add users

On the **Users** page, an admin can add people with one of three roles:

| Role | Can |
|---|---|
| **Admin** | Everything: users, catalog, stored files, inventories, backups, run history, workflows, and approving any approval step |
| **Approver** | Build and launch workflows, and approve or reject approval steps in runs launched by someone else |
| **Workflow user** | Build, test and launch workflows; sees the catalog and inventories but can't change them |

An approver can't approve a run they launched, so a workflow with an approval step needs at least two people who can approve (approvers or admins).

## 5. Add inventories

On the **Inventories** page, upload the hosts your workflows target: an Ansible INI or YAML inventory, a CSV, JSON, or a plain list of hosts. They are stored as Ansible INI. You can then pick hosts on each workflow step in the visualizer.

## 6. Build and run a workflow

1. Open the **visualizer** and add nodes from **START**. Drag from a node's right edge onto another node to link them, and choose **on success**, **on failure** or **always** for each link.
2. Pick the hosts for each step, or take them from an upstream Terraform node's `ip_addresses` output.
3. Click **Launch** to simulate a run and check the paths. Approval steps wait for an approver to decide on the **Approvals** page.
4. Click **Export → Bash runner** and run the script from the repository root:

   ```bash
   bash workflow.sh
   ```

## Data and backups

Everything lives in `data/hub.db` (git-ignored): the catalog, stored files and their history, workflows, runs, inventories, users and sessions.

- **Back up** with **Back up hub (JSON)** in the UI (admins only), and restore with **Restore from backup**.
- Or stop the hub and copy the `data/` folder.

Passwords are stored as PBKDF2-SHA256 hashes and only a hash of each session token is kept, so a copy of the database can't be used to sign in. It still contains your playbooks and inventories, so keep backups private.

## Access from other machines

By default the hub listens on `127.0.0.1` only (all interfaces on WSL 2, where it is still reachable only from that Windows machine). To let other people reach it:

```bash
HOST=0.0.0.0 ./runner.sh
```

Then open `http://<server-ip>:8080/`. Only do this on a trusted network: the hub serves plain HTTP, so passwords cross the network unencrypted. For anything wider, put it behind a reverse proxy that terminates TLS. The hub accepts requests addressed to an IP address or `localhost` only, so the proxy must forward with one of those as the `Host` header.

## Running without runner.sh

To run it under systemd or another service manager, start the server directly:

```bash
python3 server.py --host 127.0.0.1 --port 8080 --db data/hub.db --repo ..
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Address to listen on |
| `--port` | `8080` | Port to listen on |
| `--db` | `automation-hub/data/hub.db` | SQLite database file (created if missing) |
| `--repo` | the repository root | Where **Sync all from repo** reads project files |
| `--verbose` | off | Log every request |
| `--reset-password USER` | | Set a new password for `USER`, then exit |

A minimal systemd unit:

```ini
[Unit]
Description=Automation Hub
After=network.target

[Service]
User=automation
WorkingDirectory=/opt/systems/automation-hub
ExecStart=/usr/bin/python3 server.py --host 127.0.0.1 --port 8080 --db data/hub.db --repo ..
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `python3 3.9 or newer is required` | Install a newer Python and make sure `python3` points at it |
| `Port 8080 is already in use by another program` | `./runner.sh 9000` |
| The page says *Browser storage only* | You opened `index.html` as a file. Start `./runner.sh` and use `http://localhost:8080/` |
| Locked out of the admin account | On the hub machine: `./runner.sh reset-password <user>` (this also re-enables a disabled account) |
| `Too many failed sign-ins` | Wait the number of seconds shown; 5 failures within 5 minutes trigger a short lockout |
| `Unknown host` (403) | You reached the hub by a host name. Use `localhost` or the IP address |
| Playbook check says a collection is missing | `ansible-galaxy collection install -r requirements.yml` in that project |
| Terraform check says *Skipped: run terraform init* | Run `terraform init` in that module's folder |
