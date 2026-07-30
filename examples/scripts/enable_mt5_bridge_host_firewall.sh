#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Run this script with sudo on dragon.\n' >&2
  exit 2
fi

if [[ "$(hostname -s)" != "dragon" ]]; then
  printf 'The MT5 bridge firewall policy belongs on dragon.\n' >&2
  exit 2
fi

add_rule_once() {
  local marker="$1"
  shift
  if ! ufw status | grep -Fq "${marker}"; then
    ufw "$@" comment "${marker}"
  fi
}

add_rule_once \
  "lts-mt5-bridge-vm" \
  allow in on virbr0 from 192.168.122.0/24 to any port 8766 proto tcp
add_rule_once \
  "lts-mt5-bridge-tailscale" \
  allow in on tailscale0 from 100.64.0.0/10 to any port 8766 proto tcp
add_rule_once \
  "lts-mt5-bridge-deny-other" \
  deny in to any port 8766 proto tcp

ufw status verbose
