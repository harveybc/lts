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

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  qemu-kvm \
  qemu-utils \
  libvirt-daemon-system \
  libvirt-clients \
  virtinst \
  virt-manager \
  ovmf \
  swtpm \
  swtpm-tools \
  dnsmasq-base \
  spice-client-gtk

usermod -aG libvirt,kvm "${target_user}"
systemctl enable --now libvirtd.service

if ! virsh net-info default >/dev/null 2>&1; then
  printf 'The libvirt default network was not created by the package.\n' >&2
  exit 1
fi
virsh net-autostart default
if ! virsh net-info default | grep -q 'Active:.*yes'; then
  virsh net-start default
fi

install -d -o "${target_user}" -g "${target_user}" \
  "/home/${target_user}/VirtualMachines/iso"

printf '\nKVM/libvirt host configuration complete.\n'
printf 'Log out and back in once so group membership takes effect.\n'
virt-host-validate qemu || true
