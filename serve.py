#!/usr/bin/env python3
"""履歴HTMLを localhost で配り、ブラウザから在庫チェックを叩けるようにする。

file:// で開いた HTML からはコマンドを実行できない（ブラウザの制約）。
そのため、ここで小さなサーバーを立ててリンクの受け皿にしている。

  GET  /         いまの状態から HTML を作り直して返す
  POST /refresh  watcher.py --once を実行する（ボタンの実体）

127.0.0.1 にだけ束縛する。外部からは繋がらない。
"""

import http.server
import os
import subprocess
import sys
import threading
import urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

HOST = "127.0.0.1"
PORT = int(os.environ.get("GEOWATCH_PORT", "8787"))
PYTHON = sys.executable or "/usr/bin/python3"

# チェックは 10〜20 秒かかる。連打されても 1 本だけ走らせる。
_run_lock = threading.Lock()


def _render():
    """現在の状態から HTML を生成してその中身を返す。"""
    import watcher
    import report
    cfg = watcher.load_config() or {}
    state = watcher.load_json(watcher.LOCAL_STATE_PATH, {})
    report.write_report(cfg, state, watcher.load_history(), watcher.REPORT_PATH)
    with open(watcher.REPORT_PATH, "rb") as f:
        return f.read()


def _run_check():
    """watcher.py --once を実行する。二重起動はしない。"""
    if not _run_lock.acquire(blocking=False):
        return False, "すでに確認中です"
    try:
        p = subprocess.run([PYTHON, os.path.join(BASE_DIR, "watcher.py"), "--once"],
                           capture_output=True, timeout=180, cwd=BASE_DIR)
        if p.returncode != 0:
            return False, (p.stderr.decode("utf-8", "replace").strip()
                           or "終了コード %d" % p.returncode)
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "時間切れ（180秒）"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    finally:
        _run_lock.release()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "GeoStockWatcher"

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _same_origin(self):
        """他サイトのページから叩かれるのを防ぐ。

        実害は「在庫チェックが1回走る」程度だが、ブラウザで開いている
        別のページから勝手に叩けるのは筋が悪いので閉じておく。
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # curl など。ローカルからの直接実行は許す
        host = urllib.parse.urlparse(origin).hostname
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                self._send(200, _render(), "text/html; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                self._send(500, ("生成に失敗しました: %s" % exc).encode("utf-8"))
        else:
            self._send(404, b"not found")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/refresh":
            return self._send(404, b"not found")
        if not self._same_origin():
            return self._send(403, "この画面からのみ実行できます".encode("utf-8"))
        ok, err = _run_check()
        if ok:
            self._send(204)
        else:
            self._send(503, err.encode("utf-8"))

    def log_message(self, fmt, *args):
        # アクセスログは出さない。watcher.log と混ざると読みにくいだけなので。
        pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    srv = Server((HOST, PORT), Handler)
    print("履歴ページ: http://%s:%d/" % (HOST, PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
