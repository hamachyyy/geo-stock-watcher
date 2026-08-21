#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ゲオモバイル (UQ mobile 代理店) 端末ページの在庫復活ウォッチャー

在庫切れページには <section id="js-soldout"> が含まれ、在庫があるページには含まれない。
この差分を判定材料にして、売切 -> 在庫あり に変化した瞬間に ntfy.sh 経由で
スマホへプッシュ通知を送る。

サイトが Akamai のボット判定を行っているため、取得は Chrome 相当のヘッダを付けた
curl (HTTP/2) に委譲している。Python の urllib / requests は 403 になる。
"""

import argparse
import base64
import re
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOCAL_CONFIG_PATH = os.path.join(BASE_DIR, "config.local.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "watcher.log")

CURL = "/usr/bin/curl"

# Akamai を通過できる Chrome 相当のヘッダ一式。順序・組み合わせを変えると 403 に戻る。
BROWSER_HEADERS = [
    'sec-ch-ua: "Chromium";v="126", "Not)A;Brand";v="24", "Google Chrome";v="126"',
    "sec-ch-ua-mobile: ?0",
    'sec-ch-ua-platform: "macOS"',
    "Upgrade-Insecure-Requests: 1",
    "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Sec-Fetch-Site: none",
    "Sec-Fetch-Mode: navigate",
    "Sec-Fetch-User: ?1",
    "Sec-Fetch-Dest: document",
    "Accept-Language: ja,en-US;q=0.9,en;q=0.8",
]

SOLD_OUT_MARKERS = ('id="js-soldout"', "sold_title")
# 在庫あり・在庫切れどちらのページにも必ず存在する構造アンカー。
# これが無い応答は「判定不能」にして、取得失敗を在庫復活と誤解しないようにする。
DEFAULT_SENTINEL = 'id="js-bottom_section"'

IN_STOCK = "in_stock"
SOLD_OUT = "sold_out"
UNKNOWN = "unknown"


# --------------------------------------------------------------------------- utils

def now():
    return datetime.now()


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def log(msg, level="INFO"):
    line = "%s [%s] %s" % (iso(now()), level, msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _trim_log()
    except Exception:
        pass


def _trim_log(max_bytes=1024 * 1024, keep_lines=3000):
    try:
        if os.path.getsize(LOG_PATH) <= max_bytes:
            return
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines[-keep_lines:])
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def deep_merge(base, over):
    """over の値で base を上書きする（辞書は再帰的に）。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    """config.json を土台に config.local.json と環境変数を重ねる。

    公開リポジトリに ntfy のトピック名を置かずに済むよう、CI では環境変数
    NTFY_TOPIC から受け取る。ローカル実行では config.local.json（git 管理外）を使う。
    """
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        return None
    cfg = deep_merge(cfg, load_json(LOCAL_CONFIG_PATH, {}))
    env_ntfy = {}
    for env_key, cfg_key in (("NTFY_TOPIC", "topic"), ("NTFY_SERVER", "server"),
                             ("NTFY_TOKEN", "token")):
        val = os.environ.get(env_key, "").strip()
        if val:
            env_ntfy[cfg_key] = val
    if env_ntfy:
        cfg = deep_merge(cfg, {"ntfy": env_ntfy})
    return cfg


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- fetch

def fetch(url, timeout=30, retries=3):
    """curl でページを取得する。(html, error) を返す。"""
    last_err = None
    for attempt in range(1, retries + 1):
        cmd = [CURL, "-sS", "--compressed", "--http2",
               "--max-time", str(timeout),
               "-w", "\n__HTTP_STATUS__%{http_code}"]
        for h in BROWSER_HEADERS:
            cmd += ["-H", h]
        cmd.append(url)
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
            body = out.stdout.decode("utf-8", "replace")
            if "__HTTP_STATUS__" in body:
                body, status = body.rsplit("\n__HTTP_STATUS__", 1)
                status = status.strip()
            else:
                status = "000"
            if status == "200" and body:
                return body, None
            last_err = "HTTP %s" % status
        except subprocess.TimeoutExpired:
            last_err = "timeout"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        if attempt < retries:
            time.sleep(3 * attempt)
    return None, last_err


def classify(html, sentinel=DEFAULT_SENTINEL, title_contains=None):
    """ページ HTML から在庫状態を判定する。

    在庫切れは <section id="js-soldout"> の有無で判定する。ただし「無い＝在庫あり」は
    取得エラーでも成立してしまうため、正しい商品ページが最後まで取れていることを
    先に確認し、確認できなければ UNKNOWN を返して通知を抑止する。
    """
    if not html or len(html) < 5000:
        return UNKNOWN
    if "</html>" not in html:
        # 途中で切れた応答。在庫判定には使わない。
        return UNKNOWN
    if sentinel and sentinel not in html:
        # 想定外のページ（エラーページ・サイト改修など）。
        return UNKNOWN
    if title_contains:
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        if not m or title_contains not in m.group(1):
            # 別ページへリダイレクトされている可能性。
            return UNKNOWN
    for marker in SOLD_OUT_MARKERS:
        if marker in html:
            return SOLD_OUT
    return IN_STOCK


# --------------------------------------------------------------------------- notify

def _encode_header(value):
    """ntfy のヘッダは ASCII のみ。日本語は RFC 2047 で encoded-word にする。"""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return "=?UTF-8?B?%s?=" % b64


def notify_ntfy(cfg, title, message, click_url=None, priority="urgent",
                tags="rotating_light"):
    topic = (cfg.get("ntfy", {}) or {}).get("topic", "").strip()
    if not topic:
        log("ntfy topic が未設定のため通知できません", "ERROR")
        return False
    server = (cfg.get("ntfy", {}) or {}).get("server", "https://ntfy.sh").rstrip("/")
    url = "%s/%s" % (server, topic)

    cmd = [CURL, "-sS", "--max-time", "20", "-o", "/dev/null",
           "-w", "%{http_code}", "-X", "POST",
           "-H", "Title: " + _encode_header(title),
           "-H", "Priority: " + priority,
           "-H", "Tags: " + tags]
    if click_url:
        cmd += ["-H", "Click: " + click_url]
    token = (cfg.get("ntfy", {}) or {}).get("token", "").strip()
    if token:
        cmd += ["-H", "Authorization: Bearer " + token]
    cmd += ["--data-binary", message.encode("utf-8").decode("utf-8"), url]

    for attempt in range(1, 4):
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=30)
            code = out.stdout.decode().strip()
            if code.startswith("2"):
                log("ntfy 送信成功: %s" % title)
                return True
            log("ntfy 送信失敗 (HTTP %s) attempt=%d" % (code, attempt), "WARN")
        except Exception as e:  # noqa: BLE001
            log("ntfy 送信エラー: %s attempt=%d" % (e, attempt), "WARN")
        if attempt < 3:
            time.sleep(5 * attempt)

    if cfg.get("mac_fallback_notification", True):
        _notify_mac(title, message)
    return False


def _notify_mac(title, message):
    """ntfy が届かなかったときの保険。ローカル通知だけでも残す。"""
    try:
        safe_t = title.replace('"', "'")
        safe_m = message.replace('"', "'").replace("\n", " ")
        script = 'display notification "%s" with title "%s" sound name "Glass"' % (
            safe_m, safe_t)
        subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                       timeout=15)
        log("Mac ローカル通知にフォールバックしました", "WARN")
    except Exception as e:  # noqa: BLE001
        log("Mac 通知も失敗: %s" % e, "ERROR")


# --------------------------------------------------------------------------- core

def check_all(cfg, state, force_notify=False):
    targets = cfg.get("targets", [])
    tstate = state.setdefault("targets", {})
    renotify_hours = float(cfg.get("renotify_hours", 6))
    err_threshold = int(cfg.get("health_alert_after_errors", 6))
    health_cooldown = float(cfg.get("health_alert_cooldown_hours", 12))

    for t in targets:
        url = t["url"]
        name = t.get("name", url.rsplit("/", 1)[-1])
        sentinel = t.get("sentinel", DEFAULT_SENTINEL)
        title_contains = t.get("title_contains")

        # アクセスの間隔を少しばらけさせる
        time.sleep(random.uniform(0.5, 4.0))

        html, err = fetch(url)
        entry = tstate.setdefault(url, {
            "name": name, "status": UNKNOWN, "since": iso(now()),
            "last_checked": None, "last_notified": None, "consecutive_errors": 0,
        })
        entry["name"] = name

        if html is None:
            entry["consecutive_errors"] = entry.get("consecutive_errors", 0) + 1
            entry["last_checked"] = iso(now())
            log("%s: 取得失敗 (%s) 連続%d回目" % (name, err, entry["consecutive_errors"]),
                "WARN")
            continue

        status = classify(html, sentinel, title_contains)
        entry["last_checked"] = iso(now())

        if status == UNKNOWN:
            entry["consecutive_errors"] = entry.get("consecutive_errors", 0) + 1
            log("%s: 判定不能 (ページ構造が想定外) 連続%d回目"
                % (name, entry["consecutive_errors"]), "WARN")
            continue

        entry["consecutive_errors"] = 0
        prev = entry.get("status", UNKNOWN)

        if status != prev:
            entry["status"] = status
            entry["since"] = iso(now())
            log("%s: %s -> %s" % (name, prev, status))
            if status == IN_STOCK:
                _send_restock(cfg, entry, name, url)
            elif prev == IN_STOCK:
                notify_ntfy(cfg, "在庫切れに戻りました",
                            "%s は再び在庫切れになりました。\n%s" % (name, url),
                            click_url=url, priority="default", tags="heavy_minus_sign")
        else:
            log("%s: %s (変化なし)" % (name, status))
            if status == IN_STOCK:
                last = parse_iso(entry.get("last_notified"))
                due = last is None or (now() - last) >= timedelta(hours=renotify_hours)
                if due or force_notify:
                    _send_restock(cfg, entry, name, url, repeat=True)

    _maybe_health_alert(cfg, state, err_threshold, health_cooldown)
    state["last_run"] = iso(now())
    return state


def _send_restock(cfg, entry, name, url, repeat=False):
    title = "在庫復活" + ("（継続中）" if repeat else "！")
    msg = "%s が購入可能になっています。\n%s" % (name, url)
    notify_ntfy(cfg, "%s %s" % (title, name), msg, click_url=url,
                priority="urgent", tags="rotating_light,iphone")
    entry["last_notified"] = iso(now())


def _maybe_health_alert(cfg, state, threshold, cooldown_hours):
    broken = [e for e in state.get("targets", {}).values()
              if e.get("consecutive_errors", 0) >= threshold]
    if not broken:
        return
    last = parse_iso(state.get("last_health_alert"))
    if last is not None and (now() - last) < timedelta(hours=cooldown_hours):
        return
    names = "、".join(e.get("name", "?") for e in broken)
    notify_ntfy(cfg, "監視が止まっている可能性",
                "%s のページを %d 回連続で判定できませんでした。"
                "サイト構造の変更かネットワーク障害の可能性があります。" % (names, threshold),
                priority="high", tags="warning")
    state["last_health_alert"] = iso(now())


# --------------------------------------------------------------------------- cli

def cmd_status(cfg, state):
    print("=== ゲオモバイル在庫ウォッチャー ===")
    print("最終実行: %s" % state.get("last_run", "(まだ実行されていません)"))
    print("通知先  : %s/%s" % ((cfg.get("ntfy", {}) or {}).get("server", "https://ntfy.sh"),
                              (cfg.get("ntfy", {}) or {}).get("topic", "(未設定)")))
    print("")
    label = {IN_STOCK: "在庫あり", SOLD_OUT: "在庫切れ", UNKNOWN: "不明"}
    for url, e in (state.get("targets") or {}).items():
        print("%-14s %s  (この状態になった時刻: %s)" % (
            e.get("name", "?"), label.get(e.get("status"), "?"), e.get("since", "-")))
        print("   最終チェック: %s / 連続エラー: %d"
              % (e.get("last_checked", "-"), e.get("consecutive_errors", 0)))
        print("   %s" % url)
    return 0


def strip_volatile(state):
    """毎回変わる項目を落とした状態を返す。

    GitHub Actions では状態をリポジトリに書き戻して永続化する。最終チェック時刻まで
    含めると 5 分ごとにコミットが積み上がるため、CI では在庫状態そのものが変わった
    ときだけ差分が出るようにする。
    """
    out = {k: v for k, v in state.items() if k != "last_run"}
    targets = {}
    for url, e in (state.get("targets") or {}).items():
        targets[url] = {k: v for k, v in e.items() if k != "last_checked"}
    out["targets"] = targets
    return out


def main():
    ap = argparse.ArgumentParser(description="ゲオモバイル 在庫復活ウォッチャー")
    ap.add_argument("--once", action="store_true", help="1 回だけチェックする（既定動作）")
    ap.add_argument("--status", action="store_true", help="現在の状態を表示する")
    ap.add_argument("--test-notify", action="store_true", help="テスト通知を送る")
    ap.add_argument("--force-notify", action="store_true",
                    help="在庫ありなら再通知間隔を無視して通知する")
    ap.add_argument("--reset", action="store_true", help="保存済みの状態を消す")
    ap.add_argument("--durable-state", action="store_true",
                    help="状態から最終チェック時刻を省く（CI で差分を出さないため）")
    args = ap.parse_args()

    cfg = load_config()
    if cfg is None:
        print("config.json が見つかりません: %s" % CONFIG_PATH, file=sys.stderr)
        return 1
    if not (cfg.get("ntfy", {}) or {}).get("topic", "").strip():
        print("ntfy のトピックが未設定です。config.local.json か環境変数 NTFY_TOPIC "
              "で指定してください。", file=sys.stderr)
        return 2
    state = load_json(STATE_PATH, {})

    if args.reset:
        save_json(STATE_PATH, {})
        print("状態をリセットしました。")
        return 0

    if args.status:
        return cmd_status(cfg, state)

    if args.test_notify:
        ok = notify_ntfy(cfg, "テスト通知",
                         "ゲオモバイル在庫ウォッチャーは正常に動作しています。",
                         click_url=(cfg.get("targets") or [{}])[0].get("url"),
                         priority="default", tags="white_check_mark")
        print("送信成功" if ok else "送信失敗（logs/watcher.log を確認してください）")
        return 0 if ok else 1

    state = check_all(cfg, state, force_notify=args.force_notify)
    save_json(STATE_PATH, strip_volatile(state) if args.durable_state else state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
