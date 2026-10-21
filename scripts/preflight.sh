#!/usr/bin/env bash
# Control-node preflight: check that the tools a workflow needs are installed and print
# their versions. Exits 1 if anything is missing, so a workflow stops before changing anything.
#
#   bash preflight.sh
#   REQUIRED="ansible-playbook terraform" bash preflight.sh
#   MIN_FREE_MB=500 bash preflight.sh     # free space needed in the current folder
set -uo pipefail

REQUIRED="${REQUIRED:-ansible-playbook terraform python3 ssh}"
MIN_FREE_MB="${MIN_FREE_MB:-200}"
problems=0

version_of() {
  case "$1" in
    terraform) terraform version 2>/dev/null | head -1 ;;
    ssh) ssh -V 2>&1 | head -1 ;;
    *) "$1" --version 2>&1 | head -1 ;;
  esac
}

for cmd in $REQUIRED; do
  if command -v "$cmd" >/dev/null 2>&1; then
    printf 'ok       %-18s %s\n' "$cmd" "$(version_of "$cmd")"
  else
    printf 'MISSING  %s\n' "$cmd"
    problems=$((problems + 1))
  fi
done

free_mb=$(df -Pm . | awk 'NR == 2 {print $4}')
if (( free_mb < MIN_FREE_MB )); then
  printf 'LOW      disk space: %s MB free here, %s MB needed\n' "$free_mb" "$MIN_FREE_MB"
  problems=$((problems + 1))
else
  printf 'ok       %-18s %s MB free\n' "disk space" "$free_mb"
fi

if [[ -z "${SSH_AUTH_SOCK:-}" ]] && ! compgen -G "$HOME/.ssh/id_*" >/dev/null; then
  echo 'WARN     no SSH agent and no key in ~/.ssh; Ansible may not be able to log in'
fi

if (( problems > 0 )); then
  echo "Preflight failed: $problems problem(s)"
  exit 1
fi
echo "Preflight passed"
