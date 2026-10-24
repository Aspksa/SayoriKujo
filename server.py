"""Minimal local SayoriKujo server. Requires only Python's standard library."""

import argparse
import importlib.util
import json
from pathlib import Path
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


# The launcher uses -I, so load this project's module by its absolute path.
_traffic_spec = importlib.util.spec_from_file_location(
    "sayori_traffic", Path(__file__).resolve().parent / "bootstrap" / "traffic.py")
traffic = importlib.util.module_from_spec(_traffic_spec)
_traffic_spec.loader.exec_module(traffic)


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

    def __init__(self, *args, **kwargs):
        self.traffic = traffic.HttpTraffic()
        super().__init__(*args, **kwargs)

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

    def setup(self):
        super().setup()
        self.rfile = traffic.MeteredStream(self.rfile, self.server.traffic, "rx")
        self.wfile = traffic.MeteredStream(self.wfile, self.server.traffic, "tx")

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


def open_browser_when_ready(url, stopped, report=None):
    report = report if report is not None else lambda text: print(text, flush=True)
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
                report(f"[SayoriKujo] Откройте адрес в браузере вручную: {url}")
        except Exception as error:
            report(f"[SayoriKujo] Не удалось открыть браузер: {error}. Откройте вручную: {url}")
        return
    if not stopped.is_set():
        report("[SayoriKujo] Браузер не открыт: сервер не подтвердил готовность.")


def main():
    parser = argparse.ArgumentParser(description="Локальный сервер SayoriKujo")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер")
    args = parser.parse_args()
    stopped = threading.Event()
    console = traffic.ConsoleOutput()
    monitor = None
    try:
        # Port 0 allocates and reserves a free port in one operation.
        with LocalServer(("127.0.0.1", 0), Handler) as server:
            url = f"http://127.0.0.1:{server.server_port}/"
            console.message(f"[SayoriKujo] Готово: {url}")
            console.message("[SayoriKujo] Для остановки нажмите Ctrl+C или закройте это окно.")
            monitor = traffic.TrafficMonitor(server.traffic, console)
            monitor.start()
            if not args.no_browser:
                threading.Thread(target=open_browser_when_ready,
                                 args=(url, stopped, console.message), daemon=True).start()
            try:
                server.serve_forever(poll_interval=0.2)
            finally:
                stopped.set()
    except KeyboardInterrupt:
        console.message("[SayoriKujo] Сервер остановлен.")
    except Exception as error:
        console.message(f"[SayoriKujo] Ошибка запуска: {error}")
        return 1
    finally:
        stopped.set()
        if monitor is not None:
            monitor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
