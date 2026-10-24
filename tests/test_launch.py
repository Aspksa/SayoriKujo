import importlib.util
import io
import errno
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import venv
from contextlib import contextmanager, redirect_stdout
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sayori_server", ROOT / "server.py")
server_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server_module)
HTTP = build_opener(ProxyHandler({}))
WINDOWS = os.name == "nt"


class RunningProcess:
    def __init__(self, command, cwd, env=None, new_console=False):
        flags = subprocess.CREATE_NEW_CONSOLE if WINDOWS and new_console else 0
        self.process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", creationflags=flags,
        )
        self.lines = queue.Queue()
        self.output = []
        self.reader = threading.Thread(target=self.read_output, daemon=True)
        self.reader.start()

    def read_output(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def wait_for(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                raise AssertionError("Process exited early:\n" + "".join(self.output))
            self.output.append(line)
            result = predicate(line)
            if result:
                return result
        raise AssertionError("Timed out waiting for output:\n" + "".join(self.output))

    def url(self):
        return self.wait_for(lambda line: (match.group(0) if
                            (match := re.search(r"http://127\.0\.0\.1:\d+/", line)) else None))

    def close(self):
        if self.process.poll() is None:
            if WINDOWS:
                subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"),
                                "/F", "/T", "/PID", str(self.process.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.process.terminate()
            self.process.wait(timeout=10)
        self.reader.join(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()

    def interrupt(self):
        if WINDOWS:
            # The target has its own console. Attach a helper to that console only,
            # so Ctrl+C cannot interrupt the test runner or other CI processes.
            helper = """
import ctypes, sys
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.FreeConsole()
if not k.AttachConsole(int(sys.argv[1])):
    raise ctypes.WinError(ctypes.get_last_error())
try:
    k.SetConsoleCtrlHandler(None, True)
    if not k.GenerateConsoleCtrlEvent(0, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    k.OpenProcess.restype = wintypes.HANDLE
    handle = k.OpenProcess(0x00100000, False, int(sys.argv[1]))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        if k.WaitForSingleObject(handle, 5000) != 0:
            raise RuntimeError('Server did not exit after Ctrl+C')
    finally:
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle(handle)
finally:
    k.FreeConsole()
"""
            subprocess.run([sys.executable, "-I", "-c", helper, str(self.process.pid)],
                           check=True, timeout=10, capture_output=True, text=True)
        else:
            self.process.send_signal(signal.SIGINT)
        return self.process.wait(timeout=10)


class LaunchHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "Папка с пробелами" / "Саёри (Кудзё) & Kujо!"
        shutil.copytree(ROOT, self.project, ignore=shutil.ignore_patterns("__pycache__", ".git"))

    def spawn(self, command, **kwargs):
        running = RunningProcess(command, cwd=self.temp.name, **kwargs)
        self.addCleanup(running.close)
        return running

    def python_command(self):
        return [sys.executable, "-I", "-X", "utf8", "-u",
                str(self.project / "server.py"), "--no-browser"]

    def bat_command(self):
        bat = self.project / "SayoriKujo.bat"
        return f'"{os.environ["COMSPEC"]}" /d /v:off /s /c ""{bat}""'

    def start_server(self):
        return self.spawn(self.python_command()).url()

    def assert_healthy(self, url):
        with HTTP.open(url + "health", timeout=5) as response:
            self.assertEqual(json.load(response), {"app": "SayoriKujo", "status": "ready"})

    def assert_port_released(self, url):
        address = ("127.0.0.1", int(url.rstrip("/").rsplit(":", 1)[1]))
        with socket.socket() as connection:
            connection.settimeout(1)
            self.assertIn(connection.connect_ex(address),
                          (errno.ECONNREFUSED, getattr(errno, "WSAECONNREFUSED", 10061)))
        # TCP TIME_WAIT can delay rebinding after HTTP, even though the listener
        # and its process have exited. A refused connection verifies no orphan listener.


class LaunchTests(LaunchHarness):
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
        # Keep an arbitrary port occupied while both instances start.
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
        self.assert_healthy(self.spawn(self.bat_command()).url())

    def test_ctrl_c_exits_cleanly_and_releases_port(self):
        running = self.spawn(self.python_command(), new_console=True)
        url = running.url()
        self.assert_healthy(url)
        self.assertEqual(running.interrupt(), 0)
        self.assert_port_released(url)

    def test_project_can_be_moved_and_restarted(self):
        command = self.bat_command() if WINDOWS else self.python_command()
        first = self.spawn(command)
        first_url = first.url()
        self.assert_healthy(first_url)
        first.close()
        self.assert_port_released(first_url)
        destination = Path(self.temp.name) / "Новая папка (после переноса)!" / "Саёри & Кудзё"
        destination.parent.mkdir()
        shutil.move(str(self.project), destination)
        self.project = destination
        command = self.bat_command() if WINDOWS else self.python_command()
        self.assert_healthy(self.spawn(command).url())


@unittest.skipUnless(WINDOWS, "Requires Windows cmd.exe and PowerShell 5.1")
class WindowsFailureTests(LaunchHarness):
    def isolated_environment(self):
        env = os.environ.copy()
        # Exclude both Python and the system py.exe launcher from discovery.
        env["PATH"] = str(Path(env["SystemRoot"]) / "System32")
        return env

    def assert_failure_keeps_window(self, expected):
        running = self.spawn(self.bat_command(), env=self.isolated_environment())
        running.wait_for(lambda line: "Startup failed" in line)
        output = "".join(running.output)
        self.assertIn(expected, output)
        self.assertNotRegex(output, r"http://127\.0\.0\.1:\d+/")
        # stdin remains open, like a user who has not pressed a key at pause.
        with self.assertRaises(subprocess.TimeoutExpired):
            running.process.wait(timeout=0.3)
        running.process.stdin.write("\n")
        running.process.stdin.flush()
        self.assertEqual(running.process.wait(timeout=10), 1)

    def test_missing_python_has_clear_error_and_waits(self):
        self.assert_failure_keeps_window("Не найден рабочий Python 3.10+")

    def test_old_python_rejected_and_window_waits(self):
        old_python = os.environ.get("SAYORI_TEST_OLD_PYTHON")
        if not old_python:
            if os.environ.get("GITHUB_ACTIONS"):
                self.fail("CI must supply a real Python 3.9 interpreter")
            self.skipTest("Set SAYORI_TEST_OLD_PYTHON to a Python 3.9 executable")
        version = subprocess.check_output([old_python, "-I", "-c",
                                           "import sys;print(sys.version_info[:2])"], text=True).strip()
        self.assertEqual(version, "(3, 9)")
        subprocess.run([old_python, "-I", "-m", "venv", "--without-pip",
                        str(self.project / ".venv")], check=True, timeout=30)
        self.assert_failure_keeps_window("Не найден рабочий Python 3.10+")

    def test_broken_first_python_falls_back_to_venv(self):
        runtime = self.project / "runtime"
        runtime.mkdir()
        (runtime / "python.exe").write_bytes(b"This is a damaged executable")
        venv.EnvBuilder(with_pip=False).create(self.project / ".venv")
        running = self.spawn(self.bat_command(), env=self.isolated_environment())
        self.assert_healthy(running.url())
        self.assertIn(str(self.project / ".venv" / "Scripts" / "python.exe"), "".join(running.output))

    def test_missing_server_file_is_named_and_window_waits(self):
        (self.project / "server.py").unlink()
        self.assert_failure_keeps_window("Не найден server.py")

    def test_missing_bootstrap_file_is_named_and_window_waits(self):
        (self.project / "bootstrap" / "start.ps1").unlink()
        self.assert_failure_keeps_window("Missing required file: bootstrap\\start.ps1")

    def test_bat_on_another_drive_with_different_working_drive(self):
        subst = str(Path(os.environ["SystemRoot"]) / "System32/subst.exe")
        free_drive = next((f"{letter}:" for letter in "ZYXWVUTSRQPONMLKJIHGFED"
                           if not Path(f"{letter}:\\").exists()), None)
        self.assertIsNotNone(free_drive, "Need one unused drive letter for the test")
        subprocess.run([subst, free_drive, self.temp.name], check=True, timeout=10)
        self.addCleanup(lambda: subprocess.run([subst, free_drive, "/D"], check=True, timeout=10))
        self.project = Path(free_drive + "\\") / self.project.relative_to(self.temp.name)
        self.assertNotEqual(self.project.drive.lower(), Path(self.temp.name).drive.lower())
        self.assert_healthy(self.spawn(self.bat_command()).url())


class BrowserTests(unittest.TestCase):
    @contextmanager
    def live_server(self):
        with server_module.LocalServer(("127.0.0.1", 0), server_module.Handler) as server:
            serving = threading.Thread(target=server.serve_forever)
            serving.start()
            try:
                yield f"http://127.0.0.1:{server.server_port}/"
            finally:
                server.shutdown()
                serving.join(timeout=5)

    def test_missing_browser_prints_address_and_server_stays_alive(self):
        with self.live_server() as url:
            output = io.StringIO()
            with patch.object(server_module.webbrowser, "open", return_value=False) as browser:
                with redirect_stdout(output):
                    server_module.open_browser_when_ready(url, threading.Event())
                browser.assert_called_once_with(url, new=2)
            self.assertIn(url, output.getvalue())
            self.assertIn("вручную", output.getvalue())
            with HTTP.open(url + "health", timeout=5) as response:
                self.assertEqual(json.load(response)["status"], "ready")

    def test_browser_exception_prints_address_without_retrying(self):
        with self.live_server() as url:
            output = io.StringIO()
            with patch.object(server_module.webbrowser, "open", side_effect=OSError("No browser")) as browser:
                with redirect_stdout(output):
                    server_module.open_browser_when_ready(url, threading.Event())
                browser.assert_called_once_with(url, new=2)
            self.assertIn(url, output.getvalue())
            self.assertIn("No browser", output.getvalue())
            with HTTP.open(url, timeout=5) as response:
                self.assertEqual(response.status, 200)

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
