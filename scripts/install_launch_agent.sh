#!/bin/bash
# Install (or reinstall) the PyVNC server as a LaunchAgent that starts at login.
# Usage: scripts/install_launch_agent.sh [port]        Uninstall: scripts/install_launch_agent.sh --uninstall
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.pyvnc.server.plist"
CONFIG="$HOME/.pyvnc"
PASSWORD_FILE="$CONFIG/password"
LOG="$CONFIG/server.log"
PORT="${1:-5900}"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Uninstalled. (Password and certificate are kept in $CONFIG)"
    exit 0
fi

PYTHON="$PROJECT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "Create the virtualenv first:  python3 -m venv .venv && .venv/bin/pip install -r requirements-server.txt"
    exit 1
fi

mkdir -p "$CONFIG" "$HOME/Library/LaunchAgents"
chmod 700 "$CONFIG"
if [[ ! -s "$PASSWORD_FILE" ]]; then
    read -r -s -p "Choose a VNC password (min 8 chars): " PW; echo
    read -r -s -p "Repeat password: " PW2; echo
    [[ "$PW" == "$PW2" ]] || { echo "Passwords do not match"; exit 1; }
    [[ ${#PW} -ge 8 ]] || { echo "Password too short"; exit 1; }
    (umask 077; printf '%s' "$PW" > "$PASSWORD_FILE")
fi

sed -e "s#__PYTHON__#$PYTHON#g" -e "s#__PROJECT__#$PROJECT#g" -e "s#__PASSWORD_FILE__#$PASSWORD_FILE#g" \
    -e "s#__PORT__#$PORT#g" -e "s#__LOG__#$LOG#g" \
    "$PROJECT/scripts/com.pyvnc.server.plist.template" > "$PLIST"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed and started. Log: $LOG"
echo "Certificate fingerprint is printed in the log; grant Screen Recording + Accessibility to:"
echo "    $(readlink -f "$PYTHON" 2>/dev/null || echo "$PYTHON")"
