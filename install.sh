#!/bin/bash
# 在庫ウォッチャーを launchd に登録して、5 分おきの自動チェックを始める。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.mori.geostockwatcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WEB_LABEL="com.mori.geostockwatcher.web"
WEB_PLIST="$HOME/Library/LaunchAgents/$WEB_LABEL.plist"
INTERVAL="${1:-300}"   # 秒。既定は 5 分
PORT="${2:-8787}"      # 履歴ページを配るポート

# トピックは config.local.json / 環境変数側にあるので、watcher の設定読み込みを使う
TOPIC=$(cd "$DIR" && /usr/bin/python3 -c 'import watcher; print(watcher.load_config()["ntfy"]["topic"] or "(未設定)")')

mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$DIR/watcher.py</string>
        <string>--once</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$DIR/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/logs/launchd.err.log</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# 履歴ページを localhost で配る常駐サーバー。
# file:// で開いた HTML からはコマンドを実行できないので、
# 「いま確認する」ボタンの受け皿としてこれが要る。
cat > "$WEB_PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$WEB_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$DIR/serve.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>GEOWATCH_PORT</key>
        <string>$PORT</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$DIR/logs/serve.out.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/logs/serve.err.log</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$(id -u)/$WEB_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$WEB_PLIST"

echo ""
echo "✅ 登録しました（${INTERVAL} 秒おきにチェックします）"
echo ""
echo "スマホ側の設定："
echo "  1. App Store で「ntfy」アプリを入れる"
echo "  2. アプリで「+」→ Topic に次を入力して購読する"
echo ""
echo "        $TOPIC"
echo ""
echo "  3. ターミナルで ./watcher.py --test-notify を実行し、届くか確認する"
echo ""
echo "履歴ページ： http://127.0.0.1:$PORT/"
echo "  「いま確認する」ボタンでその場で再チェックできます。"
echo ""
