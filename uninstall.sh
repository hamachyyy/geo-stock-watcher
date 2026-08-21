#!/bin/bash
# 自動チェックを停止して launchd から外す。
set -euo pipefail
LABEL="com.mori.geostockwatcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "✅ 停止しました。config.json / state.json / logs は残しています。"
