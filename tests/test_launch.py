import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sayori_server", ROOT / "server.py")
server_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server_module)
HTTP = build_opener(ProxyHandler({}))


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "Другой диск с пробелами" / "Саёри & Kujо!"
        shutil.copytree(ROOT, self.project, ignore=shutil.ignore_patterns("__pycache__", ".git"))

    def launch(self, command):
        process = subprocess.Popen(command, cwd=self.temp.name, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")

        def cleanup():
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    process.terminate()
                process.wait(timeout=10)
            process.stdout.close()

        self.addCleanup(cleanup)
        lines = queue.Queue()

        def read_output():
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        output = []
        for _ in range(30):
            line = lines.get(timeout=30)
            if line is None:
                self.fail("Launcher exited before readiness:\n" + "".join(output))
            output.append(line)
            match = re.search(r"http://127\.0\.0\.1:\d+/", line)
            if match:
                return match.group(0)
        self.fail("No startup address:\n" + "".join(output))

    def start_server(self):
        return self.launch([sys.executable, "-I", "-X", "utf8", "-u",
                            str(self.project / "server.py"), "--no-browser"])

    def test_unicode_path_unrelated_working_directory_and_routes(self):
        url = self.start_server()
        with HTTP.open(url, timeout=5) as response:
            self.assertIn("Саёри Кудзё", response.read().decode("utf-8"))
        with HTTP.open(url + "health", timeout=5) as response:
            self.assertEqual(json.load(response), {"app": "SayoriKujo", "status": "ready"})
        for path in ("server.py", "../README.md", "bootstrap/start.ps1"):
            with self.assertRaises(HTTPError) as error:
                HTTP.open(url + path, timeout=5)
            self.assertEqual(error.exception.code, 404)

    def test_parallel_instances_reserve_different_ports(self):
        # Keep several arbitrary ports occupied while both instances start.
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            first, second = self.start_server(), self.start_server()
            self.assertNotEqual(first, second)
            for url in (first, second):
                with HTTP.open(url + "health", timeout=5) as response:
                    self.assertEqual(response.status, 200)

    def test_foreign_host_rejected(self):
        url = self.start_server()
        with self.assertRaises(HTTPError) as error:
            HTTP.open(Request(url, headers={"Host": "example.org"}), timeout=5)
        self.assertEqual(error.exception.code, 403)

    @unittest.skipUnless(os.name == "nt", "Requires Windows cmd.exe and PowerShell 5.1")
    def test_actual_bat_from_unicode_path(self):
        bat = self.project / "SayoriKujo.bat"
        command = f'"{os.environ["COMSPEC"]}" /d /v:off /s /c ""{bat}""'
        url = self.launch(command)
        with HTTP.open(url + "health", timeout=5) as response:
            self.assertEqual(response.status, 200)


class BrowserTests(unittest.TestCase):
    def test_browser_opens_only_after_health_response(self):
        with server_module.LocalServer(("127.0.0.1", 0), server_module.Handler) as server:
            url = f"http://127.0.0.1:{server.server_port}/"
            stopped = threading.Event()
            opened = threading.Event()

            def browser_open(address, new):
                with HTTP.open(address + "health", timeout=5) as response:
                    self.assertEqual(json.load(response)["status"], "ready")
                opened.set()
                return True

            with patch.object(server_module.webbrowser, "open", side_effect=browser_open) as browser:
                worker = threading.Thread(target=server_module.open_browser_when_ready, args=(url, stopped))
                worker.start()
                # Listener exists, but requests are not yet handled.
                self.assertFalse(opened.wait(0.1))
                serving = threading.Thread(target=server.serve_forever)
                serving.start()
                try:
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
                    self.assertTrue(opened.is_set())
                    browser.assert_called_once_with(url, new=2)
                finally:
                    stopped.set()
                    server.shutdown()
                    serving.join(timeout=5)
                    worker.join(timeout=5)

    def test_stopped_server_does_not_open_browser(self):
        stopped = threading.Event()
        stopped.set()
        with patch.object(server_module.webbrowser, "open") as browser:
            server_module.open_browser_when_ready("http://127.0.0.1:1/", stopped)
            browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
