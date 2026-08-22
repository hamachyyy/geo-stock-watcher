#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判定履歴を 1 枚の HTML にまとめる。

watcher.py がチェックのたびに呼び出して history.html を作り直す。
外部ファイルを読み込まない自己完結の HTML なので、ダブルクリックで開ける。
"""

import html
import json
import os
import re
from datetime import datetime

TS_FMT = "%Y-%m-%d %H:%M:%S"
LABEL = {"in_stock": "在庫あり", "sold_out": "在庫切れ", "unknown": "判定不能"}


def _parse(ts):
    try:
        return datetime.strptime(ts, TS_FMT)
    except (ValueError, TypeError):
        return None


def _fmt_dur(seconds):
    """秒数を「2日3時間」「45分」のような読みやすい形にする。"""
    seconds = int(max(0, seconds))
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return "%d日%d時間" % (d, h)
    if h:
        return "%d時間%d分" % (h, m)
    if m:
        return "%d分" % m
    return "%d秒" % seconds


def build_windows(history, state, now=None):
    """遷移の並びから「在庫があった区間」を組み立てる。"""
    now = now or datetime.now()
    per_target = {}
    for row in history:
        if row.get("event") != "transition":
            continue
        ts = _parse(row.get("ts"))
        if ts is None:
            continue
        per_target.setdefault(row.get("name", "?"), []).append((ts, row))

    windows = []
    for name, rows in per_target.items():
        rows.sort(key=lambda x: x[0])
        open_at = None
        for ts, row in rows:
            if row.get("to") == "in_stock":
                open_at = ts
            elif open_at is not None:
                windows.append({"name": name, "start": open_at, "end": ts,
                                "ongoing": False})
                open_at = None
        if open_at is not None:
            windows.append({"name": name, "start": open_at, "end": now,
                            "ongoing": True})
    windows.sort(key=lambda w: w["start"], reverse=True)
    return windows


def read_recent_log(log_path, limit=120):
    """最近のチェック結果をログから拾う。"""
    rows = []
    pat = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[(\w+)\] (.+)$")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except IOError:
        return rows
    for line in lines[-limit * 3:]:
        m = pat.match(line.strip())
        if not m:
            continue
        ts, level, msg = m.groups()
        status = None
        for key, lab in LABEL.items():
            if (": %s (変化なし)" % key) in msg or msg.endswith(": " + key):
                status = key
        if "-> in_stock" in msg:
            status = "in_stock"
        elif "-> sold_out" in msg:
            status = "sold_out"
        # 画面に出す文言なので in_stock などの内部表記を日本語に置き換える
        shown = msg
        for key, lab in LABEL.items():
            shown = shown.replace(key, lab)
        shown = shown.replace(" -> ", " → ")
        rows.append({"ts": ts, "level": level, "msg": shown, "status": status})
    return rows[-limit:][::-1]


CSS = """
:root{
  --bg:#f5f6f8; --card:#ffffff; --fg:#14161a; --muted:#697086; --line:#e3e6ec;
  --ok:#0f9b63; --ok-bg:#e6f6ee; --out:#8a91a6; --out-bg:#eef0f4;
  --warn:#c2731a; --warn-bg:#fdf2e2; --accent:#2f6df6;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --bg:#101319; --card:#181c24; --fg:#e8eaf0; --muted:#98a0b4; --line:#272d39;
    --ok:#43d39a; --ok-bg:#13312a; --out:#7b8397; --out-bg:#1e232d;
    --warn:#e0a758; --warn-bg:#33270f; --accent:#6f9bff;
  }
}
*{box-sizing:border-box}
body{margin:0;padding:28px 20px 60px;background:var(--bg);color:var(--fg);
 font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
 line-height:1.65;-webkit-font-smoothing:antialiased}
.wrap{max-width:920px;margin:0 auto}
h1{font-size:21px;margin:0 0 4px;letter-spacing:.01em}
h2{font-size:15px;margin:34px 0 12px;color:var(--muted);font-weight:600;
 letter-spacing:.04em}
.sub{color:var(--muted);font-size:13px;margin:0 0 24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.card .name{font-size:14px;color:var(--muted);margin-bottom:8px}
.badge{display:inline-block;padding:5px 12px;border-radius:999px;font-size:15px;
 font-weight:700;letter-spacing:.02em}
.b-in{background:var(--ok-bg);color:var(--ok)}
.b-out{background:var(--out-bg);color:var(--out)}
.b-unk{background:var(--warn-bg);color:var(--warn)}
.meta{font-size:12px;color:var(--muted);margin-top:10px}
.meta a{color:var(--accent);text-decoration:none}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:14px 16px}
.stat .v{font-size:24px;font-weight:700;letter-spacing:-.01em}
.stat .k{font-size:12px;color:var(--muted);margin-top:2px}
.tl{list-style:none;margin:0;padding:0}
.tl li{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:13px 16px;margin-bottom:9px;display:flex;flex-wrap:wrap;gap:4px 14px;
 align-items:baseline}
.tl .when{font-variant-numeric:tabular-nums;font-size:13px;color:var(--muted)}
.tl .who{font-weight:650;font-size:14px}
.tl .dur{margin-left:auto;font-size:13px;color:var(--muted)}
.live{color:var(--ok);font-weight:700}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;background:var(--card);
 border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:13px}
th,td{text-align:left;padding:8px 14px;border-bottom:1px solid var(--line);
 white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:12px}
tr:last-child td{border-bottom:none}
td.ts{font-variant-numeric:tabular-nums;color:var(--muted)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px}
.d-in{background:var(--ok)} .d-out{background:var(--out)} .d-unk{background:var(--warn)}
.empty{color:var(--muted);font-size:13px;background:var(--card);
 border:1px dashed var(--line);border-radius:12px;padding:18px;text-align:center}
footer{margin-top:36px;color:var(--muted);font-size:12px;line-height:1.9}
"""


def write_report(cfg, state, history, path, log_path=None, now=None):
    now = now or datetime.now()
    if log_path is None:
        log_path = os.path.join(os.path.dirname(os.path.abspath(path)),
                                "logs", "watcher.log")
    e = html.escape
    targets = state.get("targets") or {}
    windows = build_windows(history, state, now)
    parts = []

    parts.append('<div class="wrap">')
    parts.append("<h1>ゲオモバイル 在庫判定履歴</h1>")
    parts.append('<p class="sub">最終チェック %s ／ このページは 60 秒ごとに再読み込みされます</p>'
                 % e(state.get("last_run") or "未実行"))

    # 現在の状態
    parts.append('<div class="cards">')
    if not targets:
        parts.append('<div class="empty">まだ一度もチェックしていません。</div>')
    for url, t in targets.items():
        st = t.get("status", "unknown")
        cls = {"in_stock": "b-in", "sold_out": "b-out"}.get(st, "b-unk")
        since = _parse(t.get("since"))
        dur = _fmt_dur((now - since).total_seconds()) if since else "-"
        parts.append('<div class="card"><div class="name">%s</div>'
                     '<span class="badge %s">%s</span>'
                     '<div class="meta">この状態が %s 継続中<br>'
                     '最終チェック %s ／ 連続エラー %d 回<br>'
                     '<a href="%s" target="_blank" rel="noopener">商品ページを開く</a>'
                     '</div></div>'
                     % (e(t.get("name", "?")), cls, e(LABEL.get(st, st)), e(dur),
                        e(t.get("last_checked") or "-"),
                        t.get("consecutive_errors", 0), e(url)))
    parts.append("</div>")

    # 統計
    done = [w for w in windows if not w["ongoing"]]
    total = sum((w["end"] - w["start"]).total_seconds() for w in windows)
    avg = (sum((w["end"] - w["start"]).total_seconds() for w in done) / len(done)
           if done else 0)
    shortest = min((w["end"] - w["start"]).total_seconds() for w in done) if done else 0
    parts.append("<h2>これまでの傾向</h2>")
    parts.append('<div class="stats">')
    for v, k in ((str(len(windows)), "在庫復活を捉えた回数"),
                 (_fmt_dur(total), "在庫があった合計時間"),
                 (_fmt_dur(avg) if done else "—", "1回あたりの平均在庫時間"),
                 (_fmt_dur(shortest) if done else "—", "最短の在庫時間")):
        parts.append('<div class="stat"><div class="v">%s</div><div class="k">%s</div></div>'
                     % (e(v), e(k)))
    parts.append("</div>")

    # タイムライン
    parts.append("<h2>在庫があった区間</h2>")
    if not windows:
        parts.append('<div class="empty">まだ在庫復活を捉えていません。</div>')
    else:
        parts.append('<ul class="tl">')
        for w in windows:
            dur = _fmt_dur((w["end"] - w["start"]).total_seconds())
            if w["ongoing"]:
                tail = '<span class="dur"><span class="live">● 在庫あり</span> ' \
                       '／ %s 経過</span>' % e(dur)
                span = "%s 〜 現在" % w["start"].strftime("%m/%d %H:%M")
            else:
                tail = '<span class="dur">%s で売り切れ</span>' % e(dur)
                span = "%s 〜 %s" % (w["start"].strftime("%m/%d %H:%M"),
                                    w["end"].strftime("%m/%d %H:%M"))
            parts.append('<li><span class="who">%s</span>'
                         '<span class="when">%s</span>%s</li>'
                         % (e(w["name"]), e(span), tail))
        parts.append("</ul>")

    # 最近のチェック
    rows = read_recent_log(log_path)
    parts.append("<h2>最近のチェック</h2>")
    if not rows:
        parts.append('<div class="empty">ログがまだありません。</div>')
    else:
        parts.append('<div class="scroll"><table><thead><tr>'
                     "<th>時刻</th><th>結果</th></tr></thead><tbody>")
        for r in rows:
            d = {"in_stock": "d-in", "sold_out": "d-out"}.get(r["status"], "d-unk")
            parts.append('<tr><td class="ts">%s</td><td>'
                         '<span class="dot %s"></span>%s</td></tr>'
                         % (e(r["ts"]), d, e(r["msg"])))
        parts.append("</tbody></table></div>")

    parts.append("<footer>在庫切れページにだけ現れる <code>&lt;section id=\"js-soldout\"&gt;</code> "
                 "の有無で判定しています。<br>"
                 "取得できなかった場合や想定外のページだった場合は「判定不能」として扱い、"
                 "通知は送りません。<br>"
                 "このファイルは watcher.py が実行されるたびに作り直されます。</footer>")
    parts.append("</div>")

    doc = ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           '<meta http-equiv="refresh" content="60">'
           "<title>在庫判定履歴</title><style>%s</style></head><body>%s</body></html>"
           % (CSS, "".join(parts)))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(doc)
    os.replace(tmp, path)
    return path
