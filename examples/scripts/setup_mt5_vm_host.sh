#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Run this script with sudo.\n' >&2
  exit 2
fi

target_user="${SUDO_USER:-harveybc}"
if [[ "$(hostname -s)" != "dragon" ]]; then
  printf 'Refusing to configure %s; the selected MT5 VM host is dragon.\n' \
    "$(hostname -s)" >&2
  exit 2
fi
target_home="$(getent passwd "${target_user}" | cut -d: -f6)"
if [[ -z "${target_home}" || ! -d "${target_home}" ]]; then
  printf 'Could not resolve the home directory for %s.\n' "${target_user}" >&2
  exit 2
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  qemu-system-x86 \
  qemu-utils \
  libvirt-daemon-system \
  libvirt-clients \
  virtinst \
  virt-manager \
  libosinfo-bin \
  ovmf \
  swtpm \
  swtpm-tools \
  dnsmasq-base \
  spice-client-gtk

usermod -aG libvirt,kvm "${target_user}"
if systemctl list-unit-files libvirtd.service --no-legend 2>/dev/null \
    | grep -q '^libvirtd.service'; then
  systemctl enable --now libvirtd.service
else
  systemctl enable --now virtqemud.socket virtnetworkd.socket
fi

if ! virsh net-info default >/dev/null 2>&1; then
  default_network="/usr/share/libvirt/networks/default.xml"
  if [[ ! -r "${default_network}" ]]; then
    printf 'Missing libvirt default network definition: %s\n' \
      "${default_network}" >&2
    exit 1
  fi
  virsh net-define "${default_network}"
fi
virsh net-autostart default
if ! virsh net-info default | grep -q 'Active:.*yes'; then
  virsh net-start default
fi

install -d -o "${target_user}" -g "${target_user}" \
  "${target_home}/VirtualMachines/iso"

printf '\nKVM/libvirt host configuration complete.\n'
printf 'Log out and back in once so group membership takes effect.\n'
virt-host-validate qemu || true
