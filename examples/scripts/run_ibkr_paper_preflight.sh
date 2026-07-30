#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
default_python="${HOME}/anaconda3/envs/trading-stack/bin/python"
if [[ ! -x "${default_python}" ]]; then
  default_python="python"
fi

cd "${repo_root}"
exec "${PYTHON_BIN:-${default_python}}" -m app.ibkr_paper_cli \
  --config examples/configs/ibkr_paper_execution_lab_v1.json \
  preflight
