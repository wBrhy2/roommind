#!/usr/bin/env bash
# Disposable Home Assistant test instance for the RoomMind plugin — runs as your
# user, no sudo, no systemd, no sim harness. Real onboarding + real integration
# flow; the repo's custom_components/roommind is mounted into its config.
#
#   dev/sim/local/ha-test.sh up      [port]   start (default 8123)
#   dev/sim/local/ha-test.sh status           pid + HTTP check
#   dev/sim/local/ha-test.sh logs    [-f]     HA log
#   dev/sim/local/ha-test.sh down             stop
#   dev/sim/local/ha-test.sh reset            stop + delete all instance data
#
# The configuration.yaml (demo entities + tariff helpers) is only written when
# missing; delete it (or use reset) to re-seed.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
DATA="${ROOMMIND_HA_TEST_HOME:-$HOME/.roommind-ha-test}"
CONF="$DATA/config"
HA_VERSION="${ROOMMIND_HA_TEST_VERSION:-2026.10.0}"
PY="$HOME/.roommind-sim/venvs/ha-$HA_VERSION/bin/python"
HOST="${ROOMMIND_HA_TEST_HOST:-127.0.0.1}"
PIDFILE="$DATA/ha.pid"
LOG="$DATA/ha.log"

need_python() {
  [[ -x "$PY" ]] || {
    echo "HA $HA_VERSION venv missing: $PY" >&2
    echo "create it once with:  dev/sim/bin/sim venv add $HA_VERSION" >&2
    exit 1
  }
}

write_config() {
  [[ -f "$CONF/configuration.yaml" ]] && return 0
  mkdir -p "$CONF"
  cat > "$CONF/configuration.yaml" <<'YAML'
# Seeded by dev/sim/local/ha-test.sh — RoomMind plugin test bed.
default_config:

# Fake house: climate.*, weather.*, calendar.*, sensors — bind rooms to these.
demo:

# Tariff sources for Settings -> Time-of-Use control:
input_number:
  strompreis:
    name: Strompreis
    min: 0.0
    max: 0.4
    step: 0.001
    initial: 0.22

input_select:
  tarifperiode:
    name: Tarifperiode
    options: ["On-Peak", "Mid-Peak", "Off-Peak", "Ultra-Low Overnight"]
    initial: "On-Peak"

input_datetime:
  naechster_wechsel:
    name: Naechster Tarifwechsel
    has_date: true
    has_time: true
YAML
  : > "$CONF/secrets.yaml"
}

mount_plugin() {
  mkdir -p "$CONF/custom_components"
  ln -sfn "$REPO/custom_components/roommind" "$CONF/custom_components/roommind"
}

running_pid() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid; pid="$(cat "$PIDFILE")"
  kill -0 "$pid" 2>/dev/null && echo "$pid" || return 1
}

cmd="${1:-up}"; shift || true
case "$cmd" in
  up)
    PORT="${1:-${ROOMMIND_HA_TEST_PORT:-8123}}"
    need_python
    if pid="$(running_pid)"; then
      echo "already running (pid $pid): http://$HOST:$PORT"
      exit 0
    fi
    write_config
    mount_plugin
    mkdir -p "$DATA"
    echo "starting HA $HA_VERSION on http://$HOST:$PORT (log: $LOG) ..."
    setsid nohup "$PY" -m homeassistant --config "$CONF" --host "$HOST" --port "$PORT" \
      >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 90); do
      code="$(curl -s -o /dev/null -w '%{http_code}' "http://$HOST:$PORT/" 2>/dev/null || true)"
      [[ "$code" == "200" ]] && break
      running_pid > /dev/null || { echo "HA exited early, last log lines:"; tail -n 15 "$LOG"; exit 1; }
      sleep 2
    done
    cat <<EOF

  http://$HOST:$PORT   — first visit: complete onboarding (create the owner
  account, pick location/time zone), then:

  1. Settings -> Devices & Services -> Add Integration -> "RoomMind"
     (custom component, already mounted; no HACS needed)
  2. The RoomMind sidebar panel appears; add rooms bound to the demo
     climate/sensor entities in configuration.yaml.
  3. Settings -> Time-of-Use control: enable, then bind
       cost sensor    input_number.strompreis   (thresholds 0.20/0.12/0.06)
       period sensor  input_select.tarifperiode
     — the demo calendar.* entities cover the calendar source too.
  4. Per-room: Time-of-Use mode (setback / setback + boost), coast rules.

  Toggle the tariff live from the HA UI (developer tools -> input helpers)
  or:  curl the state machines — see README.md.
  Stop with:  dev/sim/local/ha-test.sh down
EOF
    ;;
  down)
    if pid="$(running_pid)"; then
      kill "$pid" && echo "stopped (pid $pid)"
      rm -f "$PIDFILE"
    else
      echo "not running"; rm -f "$PIDFILE"
    fi
    ;;
  status)
    PORT="${ROOMMIND_HA_TEST_PORT:-8123}"
    if pid="$(running_pid)"; then
      code="$(curl -s -o /dev/null -w '%{http_code}' "http://$HOST:$PORT/" || true)"
      echo "running (pid $pid), http://$HOST:$PORT -> $code"
    else
      echo "not running"
    fi
    ;;
  logs)
    exec tail -n 80 "$@" "$LOG"
    ;;
  reset)
    "$0" down || true
    rm -rf "$DATA"
    echo "deleted $DATA"
    ;;
  *)
    echo "usage: ha-test.sh [up|down|status|logs [-f]|reset] [port]" >&2
    exit 2
    ;;
esac
