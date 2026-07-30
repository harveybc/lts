#!/usr/bin/env bash
set -euo pipefail

destination="${ALPACA_PAPER_ENV_FILE:-${HOME}/.config/lts/alpaca-paper.env}"

printf 'Alpaca Paper API Key ID: '
IFS= read -r api_key
printf 'Alpaca Paper Secret Key: '
IFS= read -rs api_secret
printf '\n'

if [[ -z "${api_key}" || -z "${api_secret}" ]]; then
  unset api_key api_secret
  printf 'Error: both values are required.\n' >&2
  exit 1
fi

install -d -m 700 "$(dirname "${destination}")"
temporary_file="$(mktemp "${destination}.tmp.XXXXXX")"
trap 'rm -f -- "${temporary_file}"' EXIT

umask 077
{
  printf 'ALPACA_PAPER_API_KEY_ID=%q\n' "${api_key}"
  printf 'ALPACA_PAPER_API_SECRET_KEY=%q\n' "${api_secret}"
  printf 'ALPACA_PAPER_BASE_URL=%q\n' 'https://paper-api.alpaca.markets'
} >"${temporary_file}"

chmod 600 "${temporary_file}"
mv -f -- "${temporary_file}" "${destination}"
trap - EXIT
unset api_key api_secret

printf 'Alpaca Paper credentials stored securely in %s\n' "${destination}"
