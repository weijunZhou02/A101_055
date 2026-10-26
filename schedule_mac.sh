#!/bin/bash
# Run demo_trade.py every day on this Mac with launchd, at 10:05, 11:05 and
# 12:05 local time. Only the first run after 00:00 UTC trades (00:00 UTC is
# 11:00 in Sydney during daylight saving, 10:00 otherwise); the others see the
# lists are done and exit. If the Mac is asleep at those times, launchd runs
# the job when it wakes. caffeinate keeps the Mac awake while a run is going.
#
#   ./schedule_mac.sh            install or reinstall the schedule
#   ./schedule_mac.sh --status   is it loaded, how often it ran, last exit code
#   ./schedule_mac.sh --remove   remove the schedule
#
# Output goes to demo/demo_trade.log. PYTHON=/path/to/python3 picks the
# interpreter (default: python3 on your PATH; it needs numpy and pandas).
set -euo pipefail

LABEL=local.a101-055.demo-trade
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
  --remove)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed the $LABEL schedule."
    exit 0 ;;
  --status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|runs =|last exit code" \
      || echo "Not installed."
    exit 0 ;;
  "") ;;
  *)
    echo "usage: $0 [--status | --remove]" >&2
    exit 1 ;;
esac

PYTHON="${PYTHON:-$(command -v python3)}"
if ! "$PYTHON" -c "import numpy, pandas" 2>/dev/null; then
  echo "ERROR: $PYTHON cannot import numpy and pandas. Run: pip install -r requirements.txt" >&2
  echo "       or choose another interpreter: PYTHON=/path/to/python3 $0" >&2
  exit 1
fi
if ! grep -qE '^BINANCE_DEMO_API_KEY=.+' "$DIR/.env" 2>/dev/null; then
  echo "ERROR: no demo API key in $DIR/.env. Run: python3 demo_trade.py --set-keys" >&2
  exit 1
fi

mkdir -p "$DIR/demo" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>$PYTHON</string>
    <string>demo_trade.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>5</integer></dict>
    <dict><key>Hour</key><integer>11</integer><key>Minute</key><integer>5</integer></dict>
    <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>5</integer></dict>
  </array>
  <key>StandardOutPath</key>
  <string>$DIR/demo/demo_trade.log</string>
  <key>StandardErrorPath</key>
  <string>$DIR/demo/demo_trade.log</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && sleep 1 || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed: demo_trade.py runs daily at 10:05, 11:05 and 12:05 local time."
echo "  Python: $PYTHON"
echo "  Log:    $DIR/demo/demo_trade.log"
echo "  Check:  $0 --status    Remove: $0 --remove"
