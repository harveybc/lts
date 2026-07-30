#!/usr/bin/env bash
set -euo pipefail

if [[ "$(hostname -s)" != "dragon" ]]; then
  printf 'The selected MT5 VM host is dragon.\n' >&2
  exit 2
fi
if [[ $# -ne 1 ]]; then
  printf 'Usage: %s /absolute/path/to/Windows11.iso\n' "$0" >&2
  exit 2
fi

iso="$(realpath "$1")"
if [[ ! -r "${iso}" ]]; then
  printf 'Windows ISO is not readable: %s\n' "${iso}" >&2
  exit 2
fi
if ! id -nG | tr ' ' '\n' | grep -qx libvirt; then
  printf 'Log out and back in so the libvirt group is active.\n' >&2
  exit 2
fi
if virsh dominfo lts-mt5-paper >/dev/null 2>&1; then
  printf 'VM lts-mt5-paper already exists; refusing to duplicate it.\n' >&2
  exit 2
fi

image="${HOME}/VirtualMachines/lts-mt5-paper.qcow2"
qemu-img create -f qcow2 "${image}" 100G

os_variant="win11"
if ! osinfo-query os short-id | awk '{print $1}' | grep -qx win11; then
  os_variant="win10"
fi

virt-install \
  --name lts-mt5-paper \
  --description "OANDA MT5 Paper execution adapter; no portfolio ownership" \
  --memory 8192 \
  --vcpus 4,sockets=1,cores=4,threads=1 \
  --cpu host-passthrough \
  --os-variant "${os_variant}" \
  --boot uefi \
  --tpm backend.type=emulator,backend.version=2.0,model=tpm-crb \
  --disk "path=${image},format=qcow2,bus=sata,cache=none,discard=unmap" \
  --cdrom "${iso}" \
  --network network=default,model=e1000e \
  --graphics spice \
  --video qxl \
  --sound ich9 \
  --channel spicevmc \
  --noautoconsole

printf '\nVM lts-mt5-paper created and started.\n'
printf 'Open virt-manager on dragon to complete Windows installation.\n'
