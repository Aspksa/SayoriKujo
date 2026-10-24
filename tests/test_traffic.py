import io
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import Mock

from test_launch import HTTP, WINDOWS, LaunchHarness, server_module


traffic = server_module.traffic


class AccountingTests(unittest.TestCase):
    def test_session_baseline_rates_and_64_bit_counters(self):
        session = traffic.AdapterSession()
        baseline = 1 << 45
        session.update({1: (baseline, baseline)}, 10)
        self.assertEqual((session.received, session.sent, session.rx_rate), (0, 0, 0))
        session.update({1: (baseline + 1_000_000, baseline + 500_000)}, 12)
        self.assertEqual((session.received, session.sent), (1_000_000, 500_000))
        monitor = traffic.TrafficMonitor(traffic.HttpTraffic(), traffic.ConsoleOutput(io.StringIO()))
        monitor.session = session
        self.assertIn("↓4.00 ↑2.00 Мбит/с", monitor.status_text())

    def test_adapter_reset_and_new_adapter_do_not_add_lifetime_traffic(self):
        session = traffic.AdapterSession()
        session.update({1: (1000, 2000)}, 0)
        session.update({1: (2000, 3000), 2: (50_000_000, 60_000_000)}, 2)
        self.assertEqual((session.received, session.sent), (1000, 1000))
        session.update({1: (10, 5), 2: (50_001_000, 60_002_000)}, 4)
        self.assertEqual((session.received, session.sent), (2000, 3000))
        self.assertTrue(session.incomplete)
        session.update({2: (50_002_000, 60_003_000)}, 6)
        session.update({1: (1_000_000, 2_000_000), 2: (50_003_000, 60_004_000)}, 8)
        self.assertEqual((session.received, session.sent), (4000, 5000))

    def test_missing_statistics_are_not_reported_as_zero_and_recovery_is_safe(self):
        output = io.StringIO()
        provider = Mock()
        provider.read.side_effect = [
            {1: (100, 200)}, {1: (1100, 2200)}, OSError("temporarily unavailable"),
            {1: (5_000_000, 6_000_000)}, {1: (5_000_100, 6_000_200)},
        ]
        monitor = traffic.TrafficMonitor(traffic.HttpTraffic(), traffic.ConsoleOutput(output), provider)
        monitor.sample()
        monitor.sample()
        monitor.sample()
        self.assertIn("нет данных", monitor.status_text())
        self.assertEqual((monitor.session.received, monitor.session.sent), (1000, 2000))
        monitor.sample()
        self.assertEqual((monitor.session.received, monitor.session.sent), (1000, 2000))
        monitor.stop()
        self.assertIn("учёт неполный", output.getvalue())
        self.assertEqual((monitor.session.received, monitor.session.sent), (1100, 2200))

    def test_unavailable_counters_still_allow_local_traffic_and_stop_cleanly(self):
        provider = Mock()
        sampled = threading.Event()

        def unavailable():
            sampled.set()
            raise OSError("access denied")

        provider.read.side_effect = unavailable
        output = io.StringIO()
        local = traffic.HttpTraffic()
        monitor = traffic.TrafficMonitor(local, traffic.ConsoleOutput(output), provider, interval=0.01)
        monitor.start()
        sampled.clear()
        try:
            self.assertTrue(sampled.wait(2))
            local.add("rx", 100)
            local.add("tx", 200)
        finally:
            monitor.stop()
        monitor.stop()
        self.assertFalse(monitor.thread.is_alive())
        self.assertIn("нет данных; расход не подменяется нулём", output.getvalue())
        self.assertIn("всего 300 Б", output.getvalue())
        self.assertEqual(output.getvalue().count("HTTP SayoriKujo локально:"), 1)

    def test_console_status_does_not_overwrite_error_messages(self):
        output = io.StringIO()
        output.isatty = lambda: True
        console = traffic.ConsoleOutput(output)
        console.status("Статистика")
        console.message("Ошибка: браузер не открылся")
        self.assertTrue(output.getvalue().endswith("Ошибка: браузер не открылся\n"))
        self.assertEqual(console.width, 0)

    def test_binary_byte_units(self):
        self.assertEqual(traffic.size_text(0), "0 Б")
        self.assertEqual(traffic.size_text(1024), "1.0 КиБ")
        self.assertEqual(traffic.size_text(1024 ** 3), "1.0 ГиБ")


class HttpAccountingTests(unittest.TestCase):
    @contextmanager
    def live_server(self):
        with server_module.LocalServer(("127.0.0.1", 0), server_module.Handler) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                yield server
            finally:
                server.shutdown()
                worker.join(timeout=5)

    def test_http_bytes_match_actual_requests_and_responses_including_errors(self):
        with self.live_server() as server:
            received = sent = 0
            for method, path in (("GET", "/"), ("HEAD", "/"), ("GET", "/missing")):
                request = (f"{method} {path} HTTP/1.0\r\n"
                           f"Host: 127.0.0.1:{server.server_port}\r\n\r\n").encode()
                with socket.create_connection(server.server_address, timeout=5) as connection:
                    connection.sendall(request)
                    response = bytearray()
                    while data := connection.recv(8192):
                        response.extend(data)
                received += len(request)
                sent += len(response)
            self.assertEqual(server.traffic.snapshot(), (received, sent))

    def test_loopback_requests_do_not_change_adapter_session_totals(self):
        with self.live_server() as server:
            provider = Mock()
            provider.read.return_value = {1: (100_000, 200_000)}
            output = io.StringIO()
            monitor = traffic.TrafficMonitor(server.traffic, traffic.ConsoleOutput(output), provider)
            monitor.sample()
            url = f"http://127.0.0.1:{server.server_port}/"
            for _ in range(3):
                with HTTP.open(url, timeout=5) as response:
                    response.read()
            monitor.sample()
            self.assertEqual((monitor.session.received, monitor.session.sent), (0, 0))
            rx, tx = server.traffic.snapshot()
            self.assertGreater(rx, 0)
            self.assertGreater(tx, len(server_module.PAGE) * 3)

    @unittest.skipUnless(WINDOWS, "Requires the native Windows interface API")
    def test_windows_native_counters_match_powershell_adapter_statistics(self):
        provider = traffic.WindowsInterfaces()
        before = provider.read()
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.check_output([
            str(powershell), "-NoProfile", "-Command",
            "$ErrorActionPreference='Stop'; @(Get-NetAdapter -Physical | "
            "Get-NetAdapterStatistics | Select-Object ReceivedBytes,SentBytes) | ConvertTo-Json -Compress",
        ], text=True, timeout=30)
        after = provider.read()
        rows = json.loads(result)
        rows = [rows] if isinstance(rows, dict) else rows
        self.assertTrue(rows, "CI must have a physical adapter for this integration test")
        for index, field in ((0, "ReceivedBytes"), (1, "SentBytes")):
            measured = sum(row[field] for row in rows)
            self.assertLessEqual(sum(c[index] for c in before.values()), measured)
            self.assertLessEqual(measured, sum(c[index] for c in after.values()))


class TrafficLauncherTests(LaunchHarness):
    def test_monitor_is_visible_in_launcher_and_summary_appears_on_ctrl_c(self):
        running = self.spawn(self.python_command(), new_console=True)
        url = running.url()
        running.wait_for(lambda line: "HTTP SayoriKujo" in line)
        self.assert_healthy(url)
        self.assertEqual(running.interrupt(), 0)
        running.reader.join(timeout=5)
        remaining = []
        while not running.lines.empty():
            line = running.lines.get_nowait()
            if line is not None:
                remaining.append(line)
        output = "".join(running.output + remaining)
        self.assertIn("Сеть ПК за сеанс:", output)
        self.assertIn("HTTP SayoriKujo локально:", output)


if __name__ == "__main__":
    unittest.main()
