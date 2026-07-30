#!/usr/bin/env bash
set -euo pipefail

host="${IBKR_PAPER_HOST:-127.0.0.1}"
port="${IBKR_PAPER_PORT:-7497}"

if ! timeout 2 bash -c "exec 3<>/dev/tcp/${host}/${port}" 2>/dev/null; then
  printf '{"status":"waiting_for_tws","host":"%s","port":%s}\n' "${host}" "${port}"
  exit 0
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec "${repo_root}/examples/scripts/run_ibkr_paper_preflight.sh"
