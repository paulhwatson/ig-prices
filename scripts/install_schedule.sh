#!/bin/bash
# Install (or remove) the launchd agent that runs the hourly update.
#
# launchd rather than cron: cron jobs on macOS run without Full Disk Access and
# die silently when the machine sleeps, whereas a launchd agent with
# StartCalendarInterval catches up after a wake.
#
# Two jobs, doing different work:
#   install         weekly backfill - deepens history using the fresh allowance
#   install-stream  the live candle collector - fills forward, costs nothing
#
#   ./scripts/install_schedule.sh install
#   ./scripts/install_schedule.sh install-stream
#   ./scripts/install_schedule.sh uninstall
#   ./scripts/install_schedule.sh uninstall-stream
#   ./scripts/install_schedule.sh status
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.paulwatson.prices.weekly"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STREAM_LABEL="com.paulwatson.prices.stream"
STREAM_PLIST="$HOME/Library/LaunchAgents/$STREAM_LABEL.plist"

write_plist() {
    mkdir -p "$HOME/Library/LaunchAgents" "$REPO_ROOT/logs"
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$REPO_ROOT/scripts/run_backfill.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <!-- Weekly, not hourly: IG's historical allowance is a weekly pool,
             and the live stream already fills forward for free. The only work
             left for a scheduled fetch is deepening the past, which there is
             exactly one allowance-worth of per week. Monday 06:10. -->
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>10</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>$REPO_ROOT</string>
    <key>StandardOutPath</key>
    <string>$REPO_ROOT/logs/backfill.out.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO_ROOT/logs/backfill.err.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLIST_EOF
}

write_stream_plist() {
    mkdir -p "$HOME/Library/LaunchAgents" "$REPO_ROOT/logs"
    # KeepAlive, not StartCalendarInterval: this one is a long-lived
    # subscription, not a job. If the socket drops, launchd restarts it.
    cat > "$STREAM_PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$STREAM_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$REPO_ROOT/scripts/run_stream.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_ROOT</string>
    <key>StandardOutPath</key>
    <string>$REPO_ROOT/logs/stream.out.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO_ROOT/logs/stream.err.log</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
PLIST_EOF
}

case "${1:-}" in
    install)
        write_plist
        launchctl unload "$PLIST" 2>/dev/null || true
        launchctl load "$PLIST"
        echo "Installed $LABEL - backfills weekly, Monday 06:10."
        echo "Logs: $REPO_ROOT/logs/prices.log (backfill.{out,err}.log are launchd's"
        echo "      own capture and are truncated on every run)"
        ;;
    install-stream)
        write_stream_plist
        launchctl unload "$STREAM_PLIST" 2>/dev/null || true
        launchctl load "$STREAM_PLIST"
        echo "Installed $STREAM_LABEL - keeps symbols current, restarted if it drops."
        echo "Logs: $REPO_ROOT/logs/prices.log (stream.{out,err}.log are launchd's"
        echo "      own capture and are truncated on every restart)"
        ;;
    uninstall-stream)
        launchctl unload "$STREAM_PLIST" 2>/dev/null || true
        rm -f "$STREAM_PLIST"
        echo "Removed $STREAM_LABEL."
        ;;
    uninstall)
        launchctl unload "$PLIST" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Removed $LABEL."
        ;;
    status)
        launchctl list | grep -E "$LABEL|$STREAM_LABEL" || echo "nothing loaded."
        ;;
    *)
        echo "usage: $0 {install|uninstall|install-stream|uninstall-stream|status}" >&2
        exit 2
        ;;
esac
