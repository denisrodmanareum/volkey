#!/bin/zsh
set -euo pipefail

LOG_DIR="/Users/riot91naver.com/.openclaw/logs"
LOG_FILE="$LOG_DIR/watchdog.log"
PLIST="/Users/riot91naver.com/Library/LaunchAgents/ai.openclaw.gateway.plist"
LABEL="ai.openclaw.gateway"
PORT="18789"

mkdir -p "$LOG_DIR"

ts() {
  date '+%Y-%m-%d %H:%M:%S %Z'
}

log() {
  printf '[%s] %s\n' "$(ts)" "$1" >> "$LOG_FILE"
}

gateway_reachable() {
  if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    return 1
  fi
  if ! openclaw channels status >/tmp/openclaw-watchdog-status.txt 2>&1; then
    return 1
  fi
  grep -q "Gateway reachable" /tmp/openclaw-watchdog-status.txt
}

ensure_launchagent_loaded() {
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    return 0
  fi
  log "LaunchAgent not loaded; bootstrapping $LABEL"
  launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
  launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
}

restart_gateway() {
  log "Gateway unhealthy; reinstalling and restarting"
  openclaw gateway install >/tmp/openclaw-watchdog-install.txt 2>&1 || true
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
  launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
}

ensure_launchagent_loaded

if gateway_reachable; then
  log "Gateway healthy"
  exit 0
fi

log "Gateway check failed"
restart_gateway
sleep 4

if gateway_reachable; then
  log "Gateway recovered"
  exit 0
fi

log "Gateway still unhealthy after restart"
exit 1
