#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
credential_file="${ALPACA_PAPER_ENV_FILE:-${HOME}/.config/lts/alpaca-paper.env}"

if [[ ! -r "${credential_file}" ]]; then
  printf 'Missing Alpaca Paper credential file: %s\n' "${credential_file}" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${credential_file}"
set +a

cd "${repo_root}"
default_python="${HOME}/anaconda3/envs/trading-stack/bin/python"
if [[ ! -x "${default_python}" ]]; then
  default_python="python"
fi
exec "${PYTHON_BIN:-${default_python}}" -m app.alpaca_paper_cli \
  --config examples/configs/alpaca_paper_execution_lab_v1.json \
  preflight
