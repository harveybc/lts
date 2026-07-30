#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  printf 'Run this script with sudo on Dragon.\n' >&2
  exit 2
fi

if [[ "$(hostname -s)" != "dragon" ]]; then
  printf 'Refusing to configure remote login on %s; expected dragon.\n' \
    "$(hostname -s)" >&2
  exit 2
fi

for command in grdctl openssl ufw; do
  if ! command -v "${command}" >/dev/null 2>&1; then
    printf 'Required command is missing: %s\n' "${command}" >&2
    exit 1
  fi
done

rdp_port=3389
service_user=gnome-remote-desktop
service_group=gnome-remote-desktop
certificate_dir="/var/lib/${service_user}/.local/share/gnome-remote-desktop/certificates"
certificate_path="${certificate_dir}/dragon-rdp.crt"
key_path="${certificate_dir}/dragon-rdp.key"

install -d -m 700 -o "${service_user}" -g "${service_group}" \
  "${certificate_dir}"

if [[ ! -s "${certificate_path}" || ! -s "${key_path}" ]]; then
  openssl req \
    -new \
    -newkey rsa:3072 \
    -x509 \
    -sha256 \
    -nodes \
    -days 825 \
    -subj "/CN=dragon" \
    -keyout "${key_path}" \
    -out "${certificate_path}"
fi

chown "${service_user}:${service_group}" "${certificate_path}" "${key_path}"
chmod 644 "${certificate_path}"
chmod 600 "${key_path}"

grdctl --system rdp set-port "${rdp_port}"
grdctl --system rdp disable-port-negotiation
grdctl --system rdp set-auth-methods credentials
grdctl --system rdp set-tls-cert "${certificate_path}"
grdctl --system rdp set-tls-key "${key_path}"
grdctl --system rdp disable-view-only

printf '\nSet dedicated RDP credentials for Dragon.\n'
printf 'Do not reuse a broker, GitHub, email, or system password.\n'
grdctl --system rdp set-credentials
grdctl --system rdp enable

ufw allow in on wlp0s20f3 from 192.168.1.0/24 \
  to any port "${rdp_port}" proto tcp comment 'Dragon RDP local LAN'
ufw allow in on tailscale0 from 100.64.0.0/10 \
  to any port "${rdp_port}" proto tcp comment 'Dragon RDP Tailscale'

systemctl enable --now gnome-remote-desktop.service

printf '\nDragon remote login configuration:\n'
grdctl --system status
printf '\nFirewall rules for TCP %s:\n' "${rdp_port}"
ufw status numbered | grep -E "(${rdp_port}|Status:)" || true
printf '\nUse 192.168.1.235 from the local LAN or 100.110.215.85 via Tailscale.\n'
