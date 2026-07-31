#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
unit_dir="${HOME}/.config/systemd/user"

install -Dm644 \
  "${repo_root}/examples/systemd/lts-multi-venue-shadow.service" \
  "${unit_dir}/lts-multi-venue-shadow.service"
install -Dm644 \
  "${repo_root}/examples/systemd/lts-multi-venue-shadow.timer" \
  "${unit_dir}/lts-multi-venue-shadow.timer"
systemctl --user daemon-reload
systemctl --user enable --now lts-multi-venue-shadow.timer
systemctl --user start lts-multi-venue-shadow.service
