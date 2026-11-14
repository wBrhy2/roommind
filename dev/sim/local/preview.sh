#!/usr/bin/env bash
# Time-of-Use preview for the local dev machine.
#
#   ./preview.sh [name]    bring the preview instance up (default name: tou)
#   ./preview.sh down      stop it again
#
# Brings up a throwaway Home Assistant (dev/sim) with the RoomMind code of this
# checkout, TOU enabled and a tariff timeline, caught up to real time. The
# instance stays reachable on the LAN without a password (simulated devices
# only) and stops by itself after 8 h.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
SIM="$REPO/dev/sim/bin/sim"
NAME="${1:-tou}"

if [[ "$NAME" == "down" ]]; then
  shift || true
  exec "$SIM" down "${1:-tou}" --rm
fi

"$SIM" up "$NAME" --scenario "$HERE/tou-preview.yaml" --catch-up --ttl 8h

port=$(python3 -c "import json,pathlib,sys; print(json.loads((pathlib.Path.home()/'.roommind-sim/instances'/'$NAME'/'meta.json').read_text())['port'])" 2>/dev/null || true)
cat <<EOF

  Panel (TOU is enabled and already shaped by the tariff):
    http://127.0.0.1:${port:-<port>}/roommind/settings    Settings -> Time-of-Use control (sources, shaping, coast rules)
    http://127.0.0.1:${port:-<port>}/roommind/room/wohnzimmer   per-room section (setback + boost)
    http://127.0.0.1:${port:-<port>}/roommind            rooms + tariff sensors

  Clock: live tier = peak (coast rule active), driven by the input helpers.
  Stop with:  $0 down
EOF
