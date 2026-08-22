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
import subprocess
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


def _git(args, cwd, timeout=25):
    """git を実行して標準出力を返す。失敗したら None。"""
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           timeout=timeout)
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def load_ci_history(base_dir, fetch=True):
    """GitHub Actions が書き戻した state.json のコミット履歴から遷移を復元する。

    CI は在庫状態が変わったときだけ state.json をコミットする。各版に入っている
    (機種, 状態, その状態になった時刻) の組を拾えば、Mac が寝ていた間に CI が
    捉えた変化も履歴に混ぜられる。
    """
    if not os.path.isdir(os.path.join(base_dir, ".git")):
        return []
    if fetch:
        # CI のコミットは手元に無いので取ってくる。オフラインでも落ちないよう戻り値は見ない。
        _git(["fetch", "--quiet", "origin", "main"], base_dir, timeout=25)
    ref = "origin/main" if _git(["rev-parse", "--verify", "--quiet",
                                 "origin/main"], base_dir) else "HEAD"
    out = _git(["log", "--format=%H", "--reverse", ref, "--", "state.json"], base_dir)
    if not out:
        return []

    events, seen = [], set()
    for sha in out.split():
        blob = _git(["show", "%s:state.json" % sha], base_dir)
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        for url, t in (data.get("targets") or {}).items():
            name, status, since = t.get("name"), t.get("status"), t.get("since")
            if not (name and status and since):
                continue
            key = (name, status, since)
            if key in seen:
                continue
            seen.add(key)
            events.append({"ts": since, "event": "transition", "name": name,
                           "url": url, "to": status, "source": "github"})
    return events


def merge_events(local_history, ci_events):
    """手元と CI の記録を時系列に並べ、状態が実際に変わった点だけ残す。

    同じ在庫復活を両方が別々の時刻で記録するため、機種ごとに時系列で見て
    直前と同じ状態の記録は捨てる。結果として最初に気づいた側の時刻が残る。
    """
    rows = []
    for r in local_history or []:
        if r.get("event") == "transition" and r.get("to"):
            rows.append({"ts": r.get("ts"), "name": r.get("name"), "url": r.get("url"),
                         "to": r.get("to"), "source": r.get("source", "mac")})
    rows += list(ci_events or [])

    per = {}
    for r in rows:
        ts = _parse(r.get("ts"))
        if ts is None:
            continue
        per.setdefault(r.get("name"), []).append((ts, r))

    merged = []
    for name, items in per.items():
        items.sort(key=lambda x: x[0])
        last = None
        for ts, r in items:
            if r.get("to") == last:
                continue          # 状態が変わっていない＝同じ変化の重複記録
            last = r.get("to")
            row = dict(r)
            row["_ts"] = ts
            merged.append(row)
    merged.sort(key=lambda r: r["_ts"])
    return merged


def build_windows(history, state, now=None):
    """遷移の並びから「在庫があった区間」を組み立てる。"""
    now = now or datetime.now()
    per_target = {}
    for row in history:
        ts = row.get("_ts") or _parse(row.get("ts"))
        if ts is None:
            continue
        per_target.setdefault(row.get("name", "?"), []).append((ts, row))

    windows = []
    for name, rows in per_target.items():
        rows.sort(key=lambda x: x[0])
        open_at, src = None, None
        for ts, row in rows:
            if row.get("to") == "in_stock":
                open_at, src = ts, row.get("source", "mac")
            elif open_at is not None:
                windows.append({"name": name, "start": open_at, "end": ts,
                                "ongoing": False, "source": src})
                open_at = None
        if open_at is not None:
            windows.append({"name": name, "start": open_at, "end": now,
                            "ongoing": True, "source": src})
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
.tl .src{font-size:11px;color:var(--muted);border:1px solid var(--line);
 border-radius:999px;padding:2px 9px}
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
    base_dir = os.path.dirname(os.path.abspath(path))
    targets = state.get("targets") or {}
    ci_events = load_ci_history(base_dir,
                                fetch=(cfg or {}).get("fetch_ci_history", True))
    events = merge_events(history, ci_events)
    windows = build_windows(events, state, now)
    parts = []

    parts.append('<div class="wrap">')
    parts.append("<h1>ゲオモバイル 在庫判定履歴</h1>")
    n_ci_new = len([x for x in events if x.get("source") == "github"])
    parts.append('<p class="sub">最終チェック %s ／ このページは 60 秒ごとに再読み込みされます<br>'
                 '手元の Mac の記録と GitHub Actions の記録（%d 件）を統合しています'
                 '（うち GitHub だけが捉えた変化 %d 件）</p>'
                 % (e(state.get("last_run") or "未実行"), len(ci_events), n_ci_new))

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
            src = {"github": "GitHub", "mac": "Mac"}.get(w.get("source"), "Mac")
            parts.append('<li><span class="who">%s</span>'
                         '<span class="when">%s</span>'
                         '<span class="src">%s が検知</span>%s</li>'
                         % (e(w["name"]), e(span), e(src), tail))
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

    parts.append('<p class="sub" style="margin-top:10px">'
                 '区間の開始時刻は「検知した時刻」です。監視が途切れていた間に復活していた'
                 '場合、実際の復活はこれより早い可能性があります。</p>')
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
