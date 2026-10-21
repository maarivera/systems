#!/usr/bin/env python3
"""Wait until hosts accept SSH connections, e.g. after Terraform creates a VM.

Hosts come from the first of these that is set:
  arguments     python3 wait_for_ssh.py 10.0.0.5 10.0.0.6
  HOSTS         HOSTS=10.0.0.5,10.0.0.6 python3 wait_for_ssh.py
  TF_IPS_FILE   TF_IPS_FILE=ips.json python3 wait_for_ssh.py
                (output of `terraform output -json ip_addresses`; IPv6 and loopback
                 addresses are ignored)

Settings: PORT (default 22), TIMEOUT in seconds for all hosts (default 300),
INTERVAL between attempts (default 5).
Exits 0 when every host answers, 1 on timeout, 2 when no hosts were given.
"""
import json
import os
import socket
import sys
import time


def addresses(value):
    """Flatten a Terraform output value into a list of strings."""
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [a for item in value for a in addresses(item)]
    return []


def target_hosts(argv):
    if argv:
        return argv
    if os.environ.get("HOSTS"):
        return [h.strip() for h in os.environ["HOSTS"].split(",") if h.strip()]
    path = os.environ.get("TF_IPS_FILE")
    if path:
        with open(path) as f:
            ips = addresses(json.load(f))
        return [ip for ip in dict.fromkeys(ips) if ":" not in ip and not ip.startswith("127.")]
    return []


def ssh_ready(host, port, timeout=5):
    """True once the host sends an SSH banner (an open port alone can mean sshd isn't up yet)."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(4).startswith(b"SSH-")
    except OSError:
        return False


def main(argv):
    hosts = target_hosts(argv)
    if not hosts:
        print("No hosts to wait for: pass them as arguments, HOSTS or TF_IPS_FILE", file=sys.stderr)
        return 2
    port = int(os.environ.get("PORT", "22"))
    timeout = float(os.environ.get("TIMEOUT", "300"))
    interval = float(os.environ.get("INTERVAL", "5"))

    deadline = time.monotonic() + timeout
    pending = list(hosts)
    print(f"Waiting up to {timeout:.0f}s for SSH on {', '.join(hosts)} (port {port})", flush=True)
    while True:
        for host in list(pending):
            if ssh_ready(host, port):
                print(f"ready    {host}", flush=True)
                pending.remove(host)
        if not pending:
            print("All hosts are accepting SSH")
            return 0
        if time.monotonic() >= deadline:
            print(f"timeout  {', '.join(pending)} did not answer within {timeout:.0f}s", file=sys.stderr)
            return 1
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
