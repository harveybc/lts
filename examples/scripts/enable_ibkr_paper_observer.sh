#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
unit_dir="${HOME}/.config/systemd/user"

install -Dm644 \
  "${repo_root}/examples/systemd/lts-ibkr-paper-observer.service" \
  "${unit_dir}/lts-ibkr-paper-observer.service"
install -Dm644 \
  "${repo_root}/examples/systemd/lts-ibkr-paper-observer.timer" \
  "${unit_dir}/lts-ibkr-paper-observer.timer"
systemctl --user daemon-reload
systemctl --user enable --now lts-ibkr-paper-observer.timer

if timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/7497" 2>/dev/null; then
  systemctl --user start lts-ibkr-paper-observer.service
  printf 'IBKR TWS Paper is online at 127.0.0.1:7497; preflight started.\n'
else
  printf '%s\n' \
    'Observer enabled; waiting for TWS Paper at 127.0.0.1:7497.' \
    'In TWS: API > Settings > Enable ActiveX and Socket Clients,' \
    'keep Read-Only API enabled, and use socket port 7497.'
fi
