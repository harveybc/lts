#!/usr/bin/env bash
set -euo pipefail

target="${HOME}/.config/lts/oanda-practice.env"
mkdir -p "$(dirname "${target}")"

read -r -p "OANDA REST-v20 Practice account ID: " account_id
read -r -s -p "OANDA REST-v20 Practice token: " access_token
printf '\n'

if [[ -z "${account_id}" || -z "${access_token}" ]]; then
  printf 'Account ID and token are both required.\n' >&2
  exit 2
fi

umask 077
temporary="${target}.tmp"
{
  printf 'OANDA_PRACTICE_ACCOUNT_ID=%q\n' "${account_id}"
  printf 'OANDA_PRACTICE_TOKEN=%q\n' "${access_token}"
} >"${temporary}"
chmod 600 "${temporary}"
mv "${temporary}" "${target}"
printf 'Stored OANDA Practice credentials at %s (mode 600).\n' "${target}"

unit_source="${HOME}/Documents/GitHub/lts/examples/systemd/lts-oanda-practice-observer.service"
unit_target="${HOME}/.config/systemd/user/lts-oanda-practice-observer.service"
if [[ -f "${unit_source}" ]]; then
  install -Dm644 "${unit_source}" "${unit_target}"
  systemctl --user daemon-reload
  systemctl --user enable --now lts-oanda-practice-observer.service
fi
