#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
credential_file="${HOME}/.config/lts/capital-demo.env"
unit_dir="${HOME}/.config/systemd/user"

if [[ ! -s "${credential_file}" ]]; then
  printf 'Missing %s\nRun: %s/examples/scripts/configure_capital_demo.py\n' \
    "${credential_file}" "${repo_root}"
  exit 2
fi
install -Dm644 \
  "${repo_root}/examples/systemd/lts-capital-demo-observer.service" \
  "${unit_dir}/lts-capital-demo-observer.service"
install -Dm644 \
  "${repo_root}/examples/systemd/lts-capital-demo-observer.timer" \
  "${unit_dir}/lts-capital-demo-observer.timer"
systemctl --user daemon-reload
systemctl --user enable --now lts-capital-demo-observer.timer
systemctl --user start lts-capital-demo-observer.service
