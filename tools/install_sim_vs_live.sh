#!/usr/bin/env bash
# Install the WO2 sim-vs-live oneshot service + fleet timer (user units).
#
# Usage:   tools/install_sim_vs_live.sh omega|dragon
#
# Ships configuration + units only. Read-only against brokers and
# runners; the collector writes solely to ~/.local/state/lts. Run BY THE
# OWNER/OPERATOR — this script is never executed by the authoring agent.
set -euo pipefail

MACHINE="${1:?usage: install_sim_vs_live.sh omega|dragon}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
CONF_DIR="${HOME}/.config/lts"

case "${MACHINE}" in
  omega)  CONFIG_SRC="${REPO}/examples/configs/sim_vs_live_window_v1.json" ;;
  dragon) CONFIG_SRC="${REPO}/examples/configs/sim_vs_live_window_dragon_v1.json" ;;
  *) echo "unknown machine '${MACHINE}' (omega|dragon)" >&2; exit 2 ;;
esac

mkdir -p "${UNIT_DIR}" "${CONF_DIR}" "${HOME}/.local/state/lts/sim-vs-live/reports"

# Config: never silently overwrite an operator-tuned file.
if [ -e "${CONF_DIR}/sim-vs-live.json" ]; then
  echo "keeping existing ${CONF_DIR}/sim-vs-live.json (new template: ${CONFIG_SRC})"
else
  cp "${CONFIG_SRC}" "${CONF_DIR}/sim-vs-live.json"
  echo "installed config ${CONF_DIR}/sim-vs-live.json"
fi

cp "${REPO}/examples/systemd/lts-sim-vs-live.service" "${UNIT_DIR}/"
cp "${REPO}/examples/systemd/lts-sim-vs-live.timer" "${UNIT_DIR}/"
echo "installed units into ${UNIT_DIR}"

systemctl --user daemon-reload
systemctl --user enable --now lts-sim-vs-live.timer
systemctl --user list-timers lts-sim-vs-live.timer --no-pager || true
echo "done: lts-sim-vs-live.timer active (oneshot collect + rolling report)"
