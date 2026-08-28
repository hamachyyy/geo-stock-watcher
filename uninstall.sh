#!/bin/bash
# 自動チェックを停止して launchd から外す。
set -euo pipefail
LABEL="com.mori.geostockwatcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WEB_LABEL="com.mori.geostockwatcher.web"
WEB_PLIST="$HOME/Library/LaunchAgents/$WEB_LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$WEB_LABEL" 2>/dev/null || true
rm -f "$PLIST" "$WEB_PLIST"
echo "✅ 停止しました。config.json / state.json / logs は残しています。"
