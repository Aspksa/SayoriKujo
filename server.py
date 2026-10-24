"""Minimal local SayoriKujo server. Requires only Python's standard library."""

import argparse
import json
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


PAGE = """<!doctype html>
<html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SayoriKujo</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#faf7f3;
color:#382b2e;font:18px system-ui,sans-serif;text-align:center}
main{padding:32px}h1{font-weight:500;font-size:clamp(32px,7vw,56px);margin:0 0 12px}
p{color:#796b70;line-height:1.6}small{font-size:14px;color:#907f84}
</style><main><h1>Саёри Кудзё</h1><p>SayoriKujo запущена.</p>
<small>Локальный сервер работает.</small></main></html>""".encode("utf-8")


class LocalServer(ThreadingHTTPServer):
    # Windows must not share the chosen socket with another process.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server_version = "SayoriKujo"
    sys_version = ""

    def do_GET(self):
        self.respond()

    def do_HEAD(self):
        self.respond(head_only=True)

    def respond(self, head_only=False):
        port = self.server.server_port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        if path == "/":
            body, content_type = PAGE, "text/html; charset=utf-8"
        elif path == "/health":
            body = json.dumps({"app": "SayoriKujo", "status": "ready"}).encode()
            content_type = "application/json; charset=utf-8"
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def log_message(self, format, *args):
        # Avoid echoing untrusted URL text into the console.
        pass


def open_browser_when_ready(url, stopped):
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 15
    while not stopped.is_set() and time.monotonic() < deadline:
        try:
            with opener.open(url + "health", timeout=1) as response:
                ready = json.load(response) == {"app": "SayoriKujo", "status": "ready"}
            if ready:
                if stopped.is_set():
                    return
        except (OSError, ValueError):
            stopped.wait(0.1)
            continue
        if not ready:
            stopped.wait(0.1)
            continue
        try:
            if not webbrowser.open(url, new=2):
                print(f"[SayoriKujo] Откройте адрес в браузере вручную: {url}", flush=True)
        except Exception as error:
            print(f"[SayoriKujo] Не удалось открыть браузер: {error}. Откройте вручную: {url}", flush=True)
        return
    if not stopped.is_set():
        print("[SayoriKujo] Браузер не открыт: сервер не подтвердил готовность.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Локальный сервер SayoriKujo")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер")
    args = parser.parse_args()
    stopped = threading.Event()
    try:
        # Port 0 allocates and reserves a free port in one operation.
        with LocalServer(("127.0.0.1", 0), Handler) as server:
            url = f"http://127.0.0.1:{server.server_port}/"
            print(f"[SayoriKujo] Готово: {url}", flush=True)
            print("[SayoriKujo] Для остановки нажмите Ctrl+C или закройте это окно.", flush=True)
            if not args.no_browser:
                threading.Thread(target=open_browser_when_ready, args=(url, stopped), daemon=True).start()
            try:
                server.serve_forever(poll_interval=0.2)
            finally:
                stopped.set()
    except KeyboardInterrupt:
        print("\n[SayoriKujo] Сервер остановлен.", flush=True)
    except Exception as error:
        print(f"[SayoriKujo] Ошибка запуска: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
