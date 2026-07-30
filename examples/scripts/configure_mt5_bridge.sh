#!/usr/bin/env bash
set -euo pipefail

if [[ "$(hostname -s)" != "dragon" ]]; then
  printf 'The MT5 bridge must be configured on dragon.\n' >&2
  exit 2
fi

target="${HOME}/.config/lts/mt5-bridge.env"
environment_python="${HOME}/anaconda3/envs/trading-stack/bin/python"
repository="${HOME}/Documents/GitHub/lts"

if [[ ! -x "${environment_python}" ]]; then
  printf 'Missing trading-stack Python at %s.\n' "${environment_python}" >&2
  exit 2
fi

"${environment_python}" -m pip install \
  --requirement "${repository}/requirements-mt5-bridge.txt"
"${environment_python}" -m pip install --no-deps --editable "${repository}"

install -d -m 700 "${HOME}/.config/lts"
if [[ ! -s "${target}" ]]; then
  secret="$(openssl rand -hex 32)"
  umask 077
  printf 'LTS_MT5_BRIDGE_SECRET=%s\n' "${secret}" >"${target}"
fi
chmod 600 "${target}"

unit_source="${HOME}/Documents/GitHub/lts/examples/systemd/lts-mt5-bridge.service"
unit_target="${HOME}/.config/systemd/user/lts-mt5-bridge.service"
install -m 644 "${unit_source}" "${unit_target}"
install -m 644 \
  "${repository}/examples/systemd/lts-mt5-bridge-watchdog.service" \
  "${HOME}/.config/systemd/user/lts-mt5-bridge-watchdog.service"
install -m 644 \
  "${repository}/examples/systemd/lts-mt5-bridge-watchdog.timer" \
  "${HOME}/.config/systemd/user/lts-mt5-bridge-watchdog.timer"
systemctl --user daemon-reload
systemctl --user enable --now lts-mt5-bridge.service
systemctl --user enable --now lts-mt5-bridge-watchdog.timer

printf 'MT5 bridge secret is stored at %s (mode 600).\n' "${target}"
printf 'The value must be entered locally in the MT5 EA; do not paste it in chat.\n'
printf 'Bridge health: http://192.168.122.1:8766/health\n'
