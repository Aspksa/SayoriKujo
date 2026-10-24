"""Minimal local SayoriKujo server. Requires only Python's standard library."""

import argparse
import importlib.util
import html
import json
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from urllib.request import ProxyHandler, build_opener


# The launcher uses -I, so load this project's module by its absolute path.
_traffic_spec = importlib.util.spec_from_file_location(
    "sayori_traffic", Path(__file__).resolve().parent / "bootstrap" / "traffic.py")
traffic = importlib.util.module_from_spec(_traffic_spec)
_traffic_spec.loader.exec_module(traffic)
_diagnostics_spec = importlib.util.spec_from_file_location(
    "sayori_diagnostics", Path(__file__).resolve().parent / "bootstrap" / "diagnostics.py")
diagnostics = importlib.util.module_from_spec(_diagnostics_spec)
_diagnostics_spec.loader.exec_module(diagnostics)
_brain_spec = importlib.util.spec_from_file_location(
    "sayori_brain", Path(__file__).resolve().parent / "modules" / "brain" / "__init__.py",
    submodule_search_locations=[str(Path(__file__).resolve().parent / "modules" / "brain")])
brain_module = importlib.util.module_from_spec(_brain_spec)
sys.modules[_brain_spec.name] = brain_module
_brain_spec.loader.exec_module(brain_module)


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
<small>Локальный сервер работает.</small><p><a href="/brain">Мозги · чат и память</a></p><p><a href="/history">История сеансов</a></p></main></html>""".encode("utf-8")


def history_page(store):
    def escape(value):
        return html.escape(str(value), quote=True)

    def seconds(value):
        try:
            return f"{float(value):.3f} с"
        except (ValueError, TypeError):
            return "—"

    def byte_pair(values):
        if not isinstance(values, dict):
            return "нет данных"
        rx, tx = values.get("received_bytes"), values.get("sent_bytes")
        if not isinstance(rx, (int, float)) or not isinstance(tx, (int, float)):
            return "нет данных"
        result = f"↓ {traffic.size_text(rx)} / ↑ {traffic.size_text(tx)}"
        if values.get("complete") is False:
            result += " (неполно)"
        return result

    rows = []
    for record in store.records():
        stats = record.get("traffic")
        stats = stats if isinstance(stats, dict) else {}
        try:
            started = diagnostics.datetime.fromisoformat(record["started_at"]).astimezone().strftime("%d.%m.%Y %H:%M:%S")
        except ValueError:
            started = record["started_at"]
        duration = seconds(record.get("duration_seconds"))
        if not record.get("duration_is_final"):
            duration += " (последняя запись)"
        stage_items = []
        for stage in record.get("stages", []) or []:
            if isinstance(stage, dict):
                stage_items.append("<li>" + escape(stage.get("label", "Этап")) + " — "
                                   + escape(seconds(stage.get("seconds"))) + "; "
                                   + escape({"ok": "готово", "error": "ошибка", "skipped": "пропущено",
                                             "manual": "открыть вручную"}.get(stage.get("status"), "завершён")) + "</li>")
        cells = [started, diagnostics.STATUSES.get(record.get("status"), "Неизвестно"), duration,
                 seconds(record.get("startup_seconds")), byte_pair(stats.get("network_pc")),
                 byte_pair(stats.get("local_http"))]
        rows.append("<tr>" + "".join("<td>" + escape(cell) + "</td>" for cell in cells) + "</tr>"
                    + '<tr><td colspan="6"><details><summary>Этапы запуска · ' + escape(record["id"][:8])
                    + "</summary><ul>" + "".join(stage_items) + "</ul></details></td></tr>")
    body = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>История сеансов · SayoriKujo</title>
<style>body{background:#faf7f3;color:#382b2e;font:16px system-ui,sans-serif;margin:0}
main{max-width:1400px;margin:auto;padding:28px}a{color:#8b3847}nav{display:flex;gap:24px}
.table{overflow:auto}table{border-collapse:collapse;width:100%;background:#fff}
td,th{text-align:left;padding:14px;border-bottom:1px solid #e5dade;vertical-align:top;min-width:120px}
th{background:#eee4e7}details{font-size:14px;color:#796b70}p{line-height:1.6;color:#796b70}</style>
<main><nav><a href="/">Главная</a><a href="/history">Обновить</a></nav><h1>История сеансов</h1>
<p>Сеть ПК — все программы на физических адаптерах. HTTP — локальный обмен SayoriKujo.
Данные сохраняются каждые 10 секунд. Для прерванного сеанса показана последняя сохранённая длительность.</p>
<div class="table"><table><thead><tr><th>Начало</th><th>Состояние</th><th>Работа</th>
<th>Полный запуск</th><th>Сеть ПК</th><th>HTTP локально</th></tr></thead><tbody>"""
    body += "".join(rows) if rows else '<tr><td colspan="6">Сеансов пока нет или запись недоступна.</td></tr>'
    return (body + "</tbody></table></div></main></html>").encode("utf-8")


class LocalServer(ThreadingHTTPServer):
    # Windows must not share the chosen socket with another process.
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.traffic = traffic.HttpTraffic()
        self.session_store = diagnostics.SessionStore(Path(__file__).resolve().parent / "data")
        self.journal = None
        self.brain_service = None
        self.brain_lock = threading.Lock()
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(*args, **kwargs)

    def get_brain(self):
        with self.brain_lock:
            if self.brain_service is None:
                self.brain_service = brain_module.BrainService(Path(__file__).resolve().parent / "data" / "brain")
            return self.brain_service

    def server_close(self):
        if self.brain_service is not None:
            self.brain_service.close()
        super().server_close()

    def handle_error(self, request, client_address):
        if self.journal is not None:
            self.journal.error(sys.exc_info()[1])
        else:
            super().handle_error(request, client_address)

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

    def valid_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def json_response(self, status, result, head_only=False):
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def brain_api(self, path, payload=None, head_only=False):
        try:
            service = self.server.get_brain()
            route = path.removeprefix("/api/brain/")
            result = (service.post(route, payload) if payload is not None
                      else service.get(route, parse_qs(urlsplit(self.path).query)))
            if route == "state":
                result["csrf_token"] = self.server.csrf_token
            self.json_response(200, result, head_only)
        except brain_module.BrainError as error:
            self.json_response(error.status, {"error": str(error)}, head_only)
        except Exception:
            # Do not serialize request bodies, keys or provider exceptions into diagnostics.
            if self.server.journal is not None:
                self.server.journal.log("Модуль Мозги: внутренняя ошибка обработки запроса.", "ERROR")
            self.json_response(503, {"error": "Модуль Мозги временно недоступен. Проверьте доступ к папке data/brain."}, head_only)

    def do_POST(self):
        if not self.valid_host():
            self.json_response(403, {"error": "Недопустимый адрес сервера."})
            return
        origin = self.headers.get("Origin")
        if (origin is not None and origin != "http://" + self.headers["Host"]) or not secrets.compare_digest(
                self.headers.get("X-Sayori-Token", "").encode("utf-8"), self.server.csrf_token.encode("ascii")):
            self.json_response(403, {"error": "Обновите страницу: проверка локального запроса не пройдена."})
            return
        path = urlsplit(self.path).path
        if not path.startswith("/api/brain/"):
            self.json_response(404, {"error": "Действие не найдено."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 256 * 1024 or self.headers.get_content_type() != "application/json":
                raise ValueError()
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError()
        except (OSError, ValueError, UnicodeError):
            self.json_response(400, {"error": "Ожидается JSON размером до 256 КиБ."})
            return
        self.brain_api(path, payload)

    def respond(self, head_only=False):
        if not self.valid_host():
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        if path.startswith("/api/brain/"):
            self.brain_api(path, head_only=head_only)
            return
        if path == "/brain" or path in ("/brain/ui.js", "/brain/ui.css", "/brain/map.js", "/brain/map.css", "/brain/map-canvas.js"):
            name = "ui.html" if path == "/brain" else path.rsplit("/", 1)[1]
            body = (Path(__file__).resolve().parent / "modules" / "brain" / name).read_bytes()
            content_type = {"ui.html": "text/html; charset=utf-8", "ui.js": "text/javascript; charset=utf-8",
                            "ui.css": "text/css; charset=utf-8", "map.js": "text/javascript; charset=utf-8",
                            "map.css": "text/css; charset=utf-8", "map-canvas.js": "text/javascript; charset=utf-8"}[name]
        elif path == "/":
            body, content_type = PAGE, "text/html; charset=utf-8"
        elif path == "/health":
            body = json.dumps({"app": "SayoriKujo", "status": "ready"}).encode()
            content_type = "application/json; charset=utf-8"
        elif path == "/history":
            body, content_type = history_page(self.server.session_store), "text/html; charset=utf-8"
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
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
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
                return "manual"
        except Exception as error:
            report(f"[SayoriKujo] Не удалось открыть браузер: {error}. Откройте вручную: {url}")
            return "manual"
        return "ok"
    if not stopped.is_set():
        report("[SayoriKujo] Браузер не открыт: сервер не подтвердил готовность.")
        return "error"
    return "skipped"


def main():
    parser = argparse.ArgumentParser(description="Локальный сервер SayoriKujo")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер")
    parser.add_argument("--history", action="store_true", help="Показать историю сеансов без запуска сервера")
    args = parser.parse_args()
    store = diagnostics.SessionStore(Path(__file__).resolve().parent / "data")
    if args.history:
        store.recover_and_prune()
        print(diagnostics.history_text(store))
        return 0
    stopped = threading.Event()
    console = traffic.ConsoleOutput()
    journal = diagnostics.Session(store, warning=console.message)
    console.sink = journal.log
    monitor = None
    exit_code = 0
    previous_thread_hook = threading.excepthook

    def thread_error(args):
        journal.error(args.exc_value)
        console.message("[SayoriKujo] Ошибка фонового потока; подробности сохранены в журнале.")

    threading.excepthook = thread_error

    def complete_browser(url):
        started = time.monotonic()
        console.message("[5/5] Открытие браузера...")
        outcome = open_browser_when_ready(url, stopped, console.message)
        elapsed = time.monotonic() - started
        journal.stage("browser", "Открытие браузера", elapsed, outcome or "skipped")
        console.message(f"[5/5] Открытие браузера — {('готово' if outcome == 'ok' else 'открыть вручную')}, {elapsed:.3f} с.")
        total = journal.startup_complete()
        if total is not None:
            console.message(f"[SayoriKujo] Полный запуск: {total:.3f} с.")

    try:
        if not journal.data["stages"]:
            console.message("[1/5] Подготовка — прямой запуск Python.")
            journal.stage("preparation", "Подготовка", time.monotonic() - journal.started_clock)
            console.message("[2/5] Проверка Python — используется уже запущенный интерпретатор.")
            journal.stage("python", "Проверка Python", 0)
        console.message("[Журнал] Логи: data/logs · история: data/sessions · просмотр: /history")
        stage_start = time.monotonic()
        console.message("[3/5] Подготовка сервера...")
        # Port 0 allocates and reserves a free port in one operation.
        with LocalServer(("127.0.0.1", 0), Handler) as server:
            server.session_store, server.journal = store, journal
            url = f"http://127.0.0.1:{server.server_port}/"
            elapsed = time.monotonic() - stage_start
            journal.stage("server", "Подготовка сервера", elapsed)
            console.message(f"[3/5] Подготовка сервера — готово, {elapsed:.3f} с.")
            console.message(f"[SayoriKujo] Готово: {url}")
            console.message("[SayoriKujo] Для остановки нажмите Ctrl+C или закройте это окно.")
            stage_start = time.monotonic()
            console.message("[4/5] Монитор сети и история...")
            monitor = traffic.TrafficMonitor(server.traffic, console, on_sample=journal.checkpoint)
            monitor.start()
            elapsed = time.monotonic() - stage_start
            journal.stage("monitor", "Монитор сети и история", elapsed)
            console.message(f"[4/5] Монитор сети и история — готово, {elapsed:.3f} с.")
            if not args.no_browser:
                threading.Thread(target=complete_browser, args=(url,), daemon=True).start()
            else:
                journal.stage("browser", "Открытие браузера", 0, "skipped")
                console.message("[5/5] Открытие браузера — пропущено (--no-browser), 0.000 с.")
                total = journal.startup_complete()
                if total is not None:
                    console.message(f"[SayoriKujo] Полный запуск: {total:.3f} с.")
            try:
                server.serve_forever(poll_interval=0.2)
            finally:
                stopped.set()
    except KeyboardInterrupt:
        console.message("[SayoriKujo] Сервер остановлен.")
    except Exception as error:
        exit_code = 1
        if not any(stage.get("name") == "server" for stage in journal.data["stages"]):
            journal.stage("server", "Подготовка сервера", time.monotonic() - stage_start, "error")
        journal.error(error)
        console.message(f"[SayoriKujo] Ошибка запуска: {error}")
    finally:
        stopped.set()
        if monitor is not None:
            monitor.stop()
        console.message("[Журнал] Сеанс завершён.")
        journal.finish("failed" if exit_code else "stopped", exit_code,
                       monitor.snapshot() if monitor is not None else None)
        console.sink = None
        threading.excepthook = previous_thread_hook
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
