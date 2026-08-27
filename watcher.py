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
# CI はリポジトリに書き戻して状態を保つ。手元の Mac は別ファイルを使う。
# 同じファイルを両方が書くと、CI の書き戻しと手元のコミットが毎回衝突するため。
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOCAL_STATE_PATH = os.path.join(BASE_DIR, "state.local.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "watcher.log")
HISTORY_PATH = os.path.join(BASE_DIR, "history.jsonl")
REPORT_PATH = os.path.join(BASE_DIR, "history.html")

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


def record_history(event):
    """状態が変わった出来事を history.jsonl に追記する。

    watcher.log は肥大を防ぐため古い行を捨てるが、在庫の変化そのものは
    後から振り返りたいので別ファイルに残す。変化時しか書かないため増え方は緩やか。
    """
    try:
        event = dict(event)
        event.setdefault("ts", iso(now()))
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        log("履歴の記録に失敗: %s" % e, "WARN")


def load_history(limit=None):
    """history.jsonl を古い順に読む。"""
    rows = []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except IOError:
        return []
    return rows[-limit:] if limit else rows


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

OUTLET_MIN_PRODUCTS = 15   # 実測 22 機種。これを下回ったら構造が壊れたとみなす
OUTLET_VARIANT_RANGE = (15, 60)  # 実測 29 バリアント。この範囲外なら解析失敗とみなす
OUTLET_MASS_CHANGE_RATIO = 0.7   # 既知の在庫切れのうち何割が同時に化けたら疑うか
OUTLET_MASS_CHANGE_MIN = 10      # ↑の判定に使う最低サンプル数


def _parse_outlet_reuse_section(content, base_name):
    """訳アリ品（状態C）区画: 容量ごとに <dd> が並び、それぞれに在庫状態がある。"""
    items = []
    dl_m = re.search(r'<dl class="price_box_problem">(.*?)</dl>', content, re.S)
    if not dl_m:
        return items
    for dd in re.findall(r'<dd>(.*?)</dd>', dl_m.group(1), re.S):
        dev_m = re.search(r'name="i_device"\s+value="(\d+)"', dd)
        if not dev_m:
            continue
        cap_m = re.search(r'<div class="production_strage">\s*<div>([^<]*)</div>', dd)
        capacity = (cap_m.group(1).strip() if cap_m else "")
        strage_m = re.search(r'name="i_strage"\s+value="(\w+)"', dd)
        key = "i_device=%s&i_strage=%s" % (dev_m.group(1),
                                            strage_m.group(1) if strage_m else "")
        label = (("%s %s（アウトレット）" % (base_name, capacity)).strip()
                 if capacity and capacity not in base_name
                 else "%s（アウトレット）" % base_name)
        status = SOLD_OUT if "-soldout" in dd else IN_STOCK
        items.append({"key": key, "label": label, "status": status})
    return items


def _parse_outlet_new_unused_section(content, base_name):
    """中古未使用品区画: 容量分けが無く、商品ごとに在庫状態が 1 つだけある。"""
    dev_m = re.search(r'name="i_device"\s+value="(\d+)"', content)
    if not dev_m:
        return []
    key = "i_device=%s&i_strage=" % dev_m.group(1)
    label = "%s（中古未使用品）" % base_name
    status = SOLD_OUT if "-soldout" in content else IN_STOCK
    return [{"key": key, "label": label, "status": status}]


def parse_outlet_listing(html):
    """アウトレットページ（訳アリ品／中古未使用品）を機種・容量ごとの在庫状態に分解する。

    このページには性質の異なる 2 種類の商品区画が同居している。
    どちらも `<section class="item_wrapper">...</section>` で 1 機種ぶんを表すが、

    - 訳アリ品（状態C）: 容量ごとに `<dd>` が並び、`<dl class="price_box_problem">`
      の中に容量分の在庫状態がある（1 機種で複数バリアント）
    - 中古未使用品: 容量分けが無く、商品ごとに `apply-btn` が 1 つだけある

    区画がどちらのタブに属すかではなく、区画の中身（`price_box_problem` の有無）で
    判定する。タブの HTML 構造が変わってもこちらは変わりにくいため。

    返り値は (items, ok)。items は {"key","label","status"} のリスト。
    ok が False のときはページ構造が想定と大きく違うということなので、
    呼び出し側は在庫状態を更新せず「判定不能」として扱う。
    """
    if not html or len(html) < 20000 or "</html>" not in html:
        return [], False

    sections = re.findall(r'<section class="item_wrapper">(.*?)</section>', html, re.S)
    if len(sections) < OUTLET_MIN_PRODUCTS:
        return [], False

    items = []
    for content in sections:
        h3_m = re.search(r'<h3>.*?<br\s*/?>\s*([^<]+?)\s*</h3>', content, re.S)
        if not h3_m:
            continue
        base_name = h3_m.group(1).strip()
        if 'price_box_problem' in content:
            items += _parse_outlet_reuse_section(content, base_name)
        elif 'apply-btn' in content:
            items += _parse_outlet_new_unused_section(content, base_name)

    if not (OUTLET_VARIANT_RANGE[0] <= len(items) <= OUTLET_VARIANT_RANGE[1]):
        return [], False
    return items, True


def _outlet_mass_change_guard(tstate, url, items):
    """既知の在庫切れの大半が同時に在庫ありへ変わっていないかを確かめる。

    アウトレット（訳アリ品）は数量限定の中古在庫で、多数の機種が同時に
    再入荷することは通常考えにくい。`-soldout` のクラス名が変わるなど
    サイト改修でマーカーを見失うと、全バリアントが一斉に「在庫あり」に
    見えてしまう（実際に検証で再現した）。それを在庫復活と誤解して
    一斉通知するのを防ぐためのガード。
    """
    known_sold_out = 0
    flipped = 0
    for it in items:
        prev = tstate.get("%s#%s" % (url, it["key"]), {}).get("status")
        if prev == SOLD_OUT:
            known_sold_out += 1
            if it["status"] == IN_STOCK:
                flipped += 1
    if (known_sold_out >= OUTLET_MASS_CHANGE_MIN
            and flipped / known_sold_out >= OUTLET_MASS_CHANGE_RATIO):
        return False, flipped, known_sold_out
    return True, flipped, known_sold_out


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

def _update_entry(cfg, state, key, name, url, status, err, renotify_hours,
                  force_notify, quiet=False):
    """1 対象ぶんの判定結果を状態に反映し、必要なら通知する。

    status が None なら取得失敗（err にエラー内容）。UNKNOWN ならページ構造が
    想定外。それ以外なら実際の在庫状態。quiet=True のときは「変化なし」の
    ログ行だけ抑える（アウトレットのように対象数が多い場合に使う）。
    """
    tstate = state.setdefault("targets", {})
    entry = tstate.setdefault(key, {
        "name": name, "status": UNKNOWN, "since": iso(now()),
        "last_checked": None, "last_notified": None, "consecutive_errors": 0,
    })
    entry["name"] = name

    if status is None:
        entry["consecutive_errors"] = entry.get("consecutive_errors", 0) + 1
        entry["last_checked"] = iso(now())
        entry["last_error"] = err
        log("%s: 取得失敗 (%s) 連続%d回目" % (name, err, entry["consecutive_errors"]),
            "WARN")
        return

    entry["last_checked"] = iso(now())

    if status == UNKNOWN:
        entry["consecutive_errors"] = entry.get("consecutive_errors", 0) + 1
        entry["last_error"] = "判定不能（ページ構造が想定外）"
        log("%s: 判定不能 (ページ構造が想定外) 連続%d回目"
            % (name, entry["consecutive_errors"]), "WARN")
        return

    entry["consecutive_errors"] = 0
    entry["last_error"] = None
    prev = entry.get("status", UNKNOWN)

    if status != prev:
        entry["status"] = status
        entry["since"] = iso(now())
        log("%s: %s -> %s" % (name, prev, status))
        record_history({"event": "transition", "name": name, "url": url,
                        "from": prev, "to": status})
        if status == IN_STOCK:
            _send_restock(cfg, entry, name, url)
        elif prev == IN_STOCK:
            notify_ntfy(cfg, "在庫切れに戻りました",
                        "%s は再び在庫切れになりました。\n%s" % (name, url),
                        click_url=url, priority="default", tags="heavy_minus_sign")
    else:
        if not quiet:
            log("%s: %s (変化なし)" % (name, status))
        if status == IN_STOCK:
            last = parse_iso(entry.get("last_notified"))
            due = last is None or (now() - last) >= timedelta(hours=renotify_hours)
            if due or force_notify:
                _send_restock(cfg, entry, name, url, repeat=True)


def _check_product_page(cfg, state, t, renotify_hours, force_notify):
    url = t["url"]
    name = t.get("name", url.rsplit("/", 1)[-1])
    sentinel = t.get("sentinel", DEFAULT_SENTINEL)
    title_contains = t.get("title_contains")

    html, err = fetch(url)
    if html is None:
        _update_entry(cfg, state, url, name, url, None, err, renotify_hours,
                      force_notify)
        return

    status = classify(html, sentinel, title_contains)
    _update_entry(cfg, state, url, name, url, status, None, renotify_hours,
                  force_notify)


def _check_outlet_listing(cfg, state, t, renotify_hours, force_notify):
    url = t["url"]
    name = t.get("name", "アウトレット")
    meta_key = url  # ページ全体の取得・解析失敗を追跡する代表キー
    tstate = state.setdefault("targets", {})

    html, err = fetch(url)
    if html is None:
        _update_entry(cfg, state, meta_key, name, url, None, err, renotify_hours,
                      force_notify)
        return

    items, ok = parse_outlet_listing(html)
    if not ok:
        _update_entry(cfg, state, meta_key, name, url, UNKNOWN, None,
                      renotify_hours, force_notify)
        log("%s: 一覧の解析に失敗（見つかった商品数が想定と違う）" % name, "WARN")
        return

    sane, flipped, known = _outlet_mass_change_guard(tstate, url, items)
    if not sane:
        log("%s: 既知の在庫切れ %d 件中 %d 件が同時に在庫ありへ変化。"
            "サイト構造の変化を疑い、今回は更新を保留" % (name, known, flipped), "WARN")
        _update_entry(cfg, state, meta_key, name, url, UNKNOWN, None,
                      renotify_hours, force_notify)
        return

    # ページ自体は正常に読めたので、代表キーに残っていた失敗記録は消す
    tstate.pop(meta_key, None)

    for item in items:
        key = "%s#%s" % (url, item["key"])
        _update_entry(cfg, state, key, item["label"], url, item["status"], None,
                      renotify_hours, force_notify, quiet=True)

    in_stock_n = sum(1 for it in items if it["status"] == IN_STOCK)
    log("%s: %d件中%d件が在庫あり" % (name, len(items), in_stock_n))


def check_all(cfg, state, force_notify=False):
    targets = cfg.get("targets", [])
    renotify_hours = float(cfg.get("renotify_hours", 6))
    err_threshold = int(cfg.get("health_alert_after_errors", 6))
    health_cooldown = float(cfg.get("health_alert_cooldown_hours", 12))

    for t in targets:
        # アクセスの間隔を少しばらけさせる
        time.sleep(random.uniform(0.5, 4.0))

        if t.get("type") == "outlet_listing":
            _check_outlet_listing(cfg, state, t, renotify_hours, force_notify)
        else:
            _check_product_page(cfg, state, t, renotify_hours, force_notify)

    _maybe_health_alert(cfg, state, err_threshold, health_cooldown)

    # 1 つでも正常に取れていれば「監視プロセスは生きている」とみなす。
    # 個別ページの不調は _maybe_health_alert の担当なので、ここでは分ける。
    healthy = any(e.get("consecutive_errors", 0) == 0
                  for e in state.get("targets", {}).values())
    ping_healthcheck(cfg, healthy)

    if not os.environ.get("GITHUB_ACTIONS"):
        # CI が自分の停止を検知することはできないので、手元からだけ見張る
        _maybe_ci_stall_alert(cfg, state,
                              float(cfg.get("ci_stall_alert_after_hours", 6)),
                              float(cfg.get("ci_stall_alert_cooldown_hours", 12)))

    state["last_run"] = iso(now())
    return state


def _send_restock(cfg, entry, name, url, repeat=False):
    title = "在庫復活" + ("（継続中）" if repeat else "！")
    msg = "%s が購入可能になっています。\n%s" % (name, url)
    notify_ntfy(cfg, "%s %s" % (title, name), msg, click_url=url,
                priority="urgent", tags="rotating_light,iphone")
    entry["last_notified"] = iso(now())


def ping_healthcheck(cfg, healthy):
    """外部の死活監視サービス（healthchecks.io など）に「生きている」と伝える。

    この仕組みが必要な理由: 監視する側が死んだとき、自分で「死にました」と
    通知することはできない。定期的な ping が途切れたことを外部に気づかせて、
    向こうから警告してもらうしかない。2026-08-27 に GitHub Actions が
    23 時間発火せず、こちらは 12 時間まったく気づけなかった。

    URL 未設定なら何もしない（設定した瞬間から有効になる）。
    """
    url = (os.environ.get("HEALTHCHECK_URL", "").strip()
           or (cfg.get("healthcheck_url") or "").strip())
    if not url:
        return
    if not healthy:
        url = url.rstrip("/") + "/fail"
    try:
        subprocess.run([CURL, "-sS", "-o", "/dev/null", "--max-time", "15",
                        "--retry", "2", url],
                       capture_output=True, timeout=45)
    except Exception as e:  # noqa: BLE001
        # ping が飛ばなくても在庫監視自体は続ける。失敗が続けば向こうが警告する。
        log("死活 ping 失敗: %s" % e, "WARN")


def _maybe_ci_stall_alert(cfg, state, max_idle_hours, cooldown_hours):
    """GitHub Actions が動かなくなっていないか、手元から見張る。

    GitHub のスケジュールは黙って止まる。CI 自身にこれを検知させることは
    できないので、Mac 側から実行記録を見にいく。Mac は 85% 眠っているが、
    数時間に一度起きれば足りる用途なのでこれで意味がある。
    """
    try:
        import report
        runs = report.load_ci_runs(BASE_DIR, limit=10)
    except Exception as e:  # noqa: BLE001
        log("CI 実行記録の取得に失敗: %s" % e, "WARN")
        return
    if not runs:
        return
    if any(r.get("status") == "in_progress" for r in runs):
        state.pop("ci_stall_alert_at", None)
        return

    stamps = [t for t in ((r.get("end") or r.get("ts")) for r in runs) if t]
    if not stamps:
        return
    latest = max(stamps)
    idle_h = (now() - latest).total_seconds() / 3600.0
    if idle_h < max_idle_hours:
        state.pop("ci_stall_alert_at", None)
        return

    last = parse_iso(state.get("ci_stall_alert_at"))
    if last is not None and (now() - last) < timedelta(hours=cooldown_hours):
        return
    log("GitHub Actions が %.1f 時間止まっています" % idle_h, "WARN")
    notify_ntfy(cfg, "GitHub 側の監視が止まっています",
                "最後の実行から %.1f 時間が経過しています（通常は常時稼働）。\n"
                "この間、在庫復活を見逃している可能性があります。\n"
                "手動で再開してください: gh workflow run watch.yml" % idle_h,
                priority="high", tags="warning")
    state["ci_stall_alert_at"] = iso(now())
    record_history({"event": "ci_stall_alert", "idle_hours": round(idle_h, 1)})


def _maybe_health_alert(cfg, state, threshold, cooldown_hours):
    broken = [e for e in state.get("targets", {}).values()
              if e.get("consecutive_errors", 0) >= threshold]
    if not broken:
        return
    last = parse_iso(state.get("last_health_alert"))
    if last is not None and (now() - last) < timedelta(hours=cooldown_hours):
        return
    names = "、".join(e.get("name", "?") for e in broken)
    # どこで・何回・何が起きているかを本文に出す。ログを見に行かなくても
    # 通知だけで「サイト側の障害か・こちらの不具合か」の見当がつくようにするため。
    detail = "\n".join(
        "%s: %s が%d回連続"
        % (e.get("name", "?"), e.get("last_error") or "エラー",
           e.get("consecutive_errors", 0))
        for e in broken)
    notify_ntfy(cfg, "監視が止まっている可能性",
                "%s\n\nサイト構造の変更かネットワーク障害の可能性があります。" % detail,
                priority="high", tags="warning")
    state["last_health_alert"] = iso(now())
    record_history({"event": "health_alert", "name": names,
                    "consecutive_errors": threshold})


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
    state_path = STATE_PATH if args.durable_state else LOCAL_STATE_PATH
    state = load_json(state_path, {})
    if not state and state_path == LOCAL_STATE_PATH:
        # 初回は CI 側の記録を引き継いで、いきなり「変化」と誤認しないようにする
        state = load_json(STATE_PATH, {})

    if args.reset:
        save_json(state_path, {})
        print("状態をリセットしました。")
        return 0

    if args.status:
        return cmd_status(cfg, state)  # ローカルの記録を表示する

    if args.test_notify:
        ok = notify_ntfy(cfg, "テスト通知",
                         "ゲオモバイル在庫ウォッチャーは正常に動作しています。",
                         click_url=(cfg.get("targets") or [{}])[0].get("url"),
                         priority="default", tags="white_check_mark")
        print("送信成功" if ok else "送信失敗（logs/watcher.log を確認してください）")
        return 0 if ok else 1

    state = check_all(cfg, state, force_notify=args.force_notify)
    save_json(state_path, strip_volatile(state) if args.durable_state else state)
    if not args.durable_state and cfg.get("write_html_report", True):
        try:
            import report
            report.write_report(cfg, state, load_history(), REPORT_PATH)
        except Exception as e:  # noqa: BLE001
            log("履歴HTMLの生成に失敗: %s" % e, "WARN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
