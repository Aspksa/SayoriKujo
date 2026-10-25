from datetime import datetime, timezone, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import test_launch as launch


diagnostics = launch.server_module.diagnostics


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = diagnostics.SessionStore(Path(self.temp.name) / "Сеансы")
        self.store.prepare()

    def record(self, days=0, status="stopped", **kwargs):
        identity = uuid.uuid4().hex
        data = {"id": identity, "started_at": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
                "status": status, "duration_seconds": 12, "last_seen_at": diagnostics.utc_now()}
        data.update(kwargs)
        diagnostics.atomic_json(self.store.sessions / (identity + ".json"), data)
        return data

    def test_retention_removes_old_logs_and_limits_completed_sessions(self):
        old = self.record(days=31)
        for suffix in (".log", ".log.1", ".log.2"):
            (self.store.logs / (old["id"] + suffix)).write_text("old", encoding="utf-8")
        for days in (3, 2, 1):
            self.record(days=days)
        self.store.recover_and_prune(max_sessions=2)
        self.assertEqual(len(self.store.records()), 2)
        self.assertFalse(list(self.store.logs.glob(old["id"] + "*")))

    def test_active_session_is_not_pruned_or_marked_interrupted(self):
        session = diagnostics.Session(self.store)
        self.addCleanup(session.finish)
        session.data["started_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        session.checkpoint(force=True)
        self.store.recover_and_prune(max_sessions=0)
        self.assertEqual(self.store.records()[0]["status"], "running")

    def test_abandoned_record_is_marked_interrupted_without_inventing_end_time(self):
        abandoned = self.record(status="running")
        self.store.recover_and_prune()
        record = self.store.records()[0]
        self.assertEqual(record["id"], abandoned["id"])
        self.assertEqual(record["status"], "interrupted")
        self.assertFalse(record["duration_is_final"])
        self.assertNotIn("ended_at", record)
        self.assertEqual(record["duration_seconds"], 12)

    def test_corrupt_record_does_not_break_other_history(self):
        good = self.record()
        (self.store.sessions / (uuid.uuid4().hex + ".json")).write_text("{broken", encoding="utf-8")
        self.store.recover_and_prune()
        self.assertEqual([row["id"] for row in self.store.records()], [good["id"]])

    def test_log_rotation_is_bounded_and_preserves_recent_messages(self):
        session = diagnostics.Session(self.store)
        self.addCleanup(session.finish)
        for index in range(5):
            session.log(str(index) + "x" * 150_000)
        logs = list(self.store.logs.glob(session.identity + ".log*"))
        self.assertEqual(len(logs), 3)
        self.assertTrue(all(path.stat().st_size <= 256 * 1024 for path in logs))
        self.assertIn("4" + "x" * 100, (self.store.logs / (session.identity + ".log")).read_text())

    def test_concurrent_stage_and_checkpoint_writes_are_complete_json(self):
        session = diagnostics.Session(self.store, checkpoint_seconds=0)
        self.addCleanup(session.finish)
        threads = [threading.Thread(target=session.stage, args=(f"step-{index}", "Проверка", 0.1))
                   for index in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        saved = self.store.records()[0]
        self.assertEqual({row["name"] for row in saved["stages"]}, {f"step-{i}" for i in range(10)})
        self.assertFalse(list(self.store.sessions.glob("*.tmp")))

    def test_boot_record_is_adopted_and_stage_timings_are_retained(self):
        record = self.record(status="starting", stages=[{"name": "python", "seconds": 0.25}])
        with patch.dict(os.environ, {"SAYORI_SESSION_ID": record["id"], "SAYORI_BOOT_SECONDS": "0.5"}):
            session = diagnostics.Session(self.store)
        self.addCleanup(session.finish)
        self.assertEqual(session.identity, record["id"])
        self.assertEqual(session.data["stages"][0]["seconds"], 0.25)
        self.assertGreaterEqual(session.startup_complete(), 0.5)

    def test_history_html_escapes_dynamic_stage_text(self):
        self.record(stages=[{"label": "<script>alert(1)</script>", "seconds": 1, "status": "ok"}])
        page = launch.server_module.history_page(self.store).decode("utf-8")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>", page)
        self.assertIn("нет данных", page)

    def test_storage_failure_does_not_raise_or_recurse_into_console(self):
        blocked = Path(self.temp.name) / "blocked"
        blocked.write_text("file", encoding="utf-8")
        output = io.StringIO()
        console = launch.server_module.traffic.ConsoleOutput(output)
        session = diagnostics.Session(diagnostics.SessionStore(blocked), warning=console.message)
        console.sink = session.log
        console.message("Работа продолжается")
        session.stage("test", "Проверка", 0)
        session.finish()
        self.assertFalse(session.enabled)
        self.assertIn("Запись недоступна", output.getvalue())
        self.assertIn("Работа продолжается", output.getvalue())

    def test_server_start_failure_has_traceback_log_and_failed_record(self):
        output = io.StringIO()
        with patch.object(launch.server_module.diagnostics, "SessionStore", return_value=self.store), \
                patch.object(launch.server_module, "LocalServer", side_effect=OSError("test bind failure")), \
                patch.object(launch.server_module.sys, "argv", ["server.py", "--no-browser"]), \
                patch.object(launch.server_module.sys, "stdout", output):
            self.assertEqual(launch.server_module.main(), 1)
        record = self.store.records()[0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["exit_code"], 1)
        self.assertTrue(any(row["name"] == "server" and row["status"] == "error" for row in record["stages"]))
        log = (self.store.logs / (record["id"] + ".log")).read_text(encoding="utf-8")
        self.assertIn("test bind failure", log)
        self.assertIn("[ERROR]", log)


class DiagnosticsLaunchTests(launch.LaunchHarness):
    def records(self):
        return diagnostics.SessionStore(self.project / "data").records()

    def test_normal_stop_saves_traffic_duration_and_all_five_stages(self):
        running = self.spawn(self.python_command(), new_console=True)
        url = running.url()
        self.assert_healthy(url)
        with launch.HTTP.open(url + "history", timeout=5) as response:
            self.assertIn("История запусков", response.read().decode("utf-8"))
        self.assertEqual(running.interrupt(), 0)
        record = self.records()[0]
        self.assertEqual(record["status"], "stopped")
        self.assertTrue(record["duration_is_final"])
        self.assertGreater(record["duration_seconds"], 0)
        self.assertEqual([row["name"] for row in record["stages"]],
                         ["preparation", "python", "server", "monitor", "browser"])
        self.assertEqual(record["stages"][-1]["status"], "skipped")
        self.assertGreater(record["traffic"]["local_http"]["received_bytes"], 0)
        self.assertGreater(record["traffic"]["local_http"]["sent_bytes"], 0)

    def test_abrupt_stop_is_recovered_by_history_command_without_new_session(self):
        running = self.spawn(self.python_command())
        url = running.url()
        self.assert_healthy(url)
        deadline = time.monotonic() + 16
        saved = None
        while time.monotonic() < deadline:
            saved = self.records()[0]
            if (saved.get("duration_seconds", 0) >= 8
                    and saved.get("traffic", {}).get("local_http", {}).get("received_bytes", 0) > 0):
                break
            threading.Event().wait(0.2)
        else:
            self.fail("Periodic checkpoint was not saved: " + repr(saved))
        running.close()
        self.assertEqual(self.records()[0]["status"], "running")
        command = self.python_command() + ["--history"]
        result = subprocess.run(command, cwd=self.temp.name, capture_output=True,
                                encoding="utf-8", timeout=20, check=True)
        self.assertNotIn("http://127.0.0.1:", result.stdout)
        self.assertIn("Прерван без завершения", result.stdout)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["status"], "interrupted")

    def test_parallel_sessions_keep_separate_records_and_logs(self):
        first = self.spawn(self.python_command(), new_console=True)
        second = self.spawn(self.python_command(), new_console=True)
        for running in (first, second):
            self.assert_healthy(running.url())
        self.assertEqual(len(self.records()), 2)
        for running in (first, second):
            self.assertEqual(running.interrupt(), 0)
        self.assertTrue(all(row["status"] == "stopped" for row in self.records()))
        self.assertEqual(len(list((self.project / "data" / "logs").glob("*.log"))), 2)

    def test_storage_blocked_still_serves_site_and_keeps_data_routes_private(self):
        (self.project / "data").write_text("blocked directory", encoding="utf-8")
        running = self.spawn(self.python_command(), new_console=True)
        url = running.url()
        self.assert_healthy(url)
        for path in ("data/sessions", "data/logs", "bootstrap/diagnostics.py"):
            with self.assertRaises(launch.HTTPError) as error:
                launch.HTTP.open(url + path, timeout=5)
            self.assertEqual(error.exception.code, 404)
        self.assertEqual(running.interrupt(), 0)

    @unittest.skipUnless(launch.WINDOWS, "Requires Windows BAT and PowerShell")
    def test_bat_records_python_failure_in_utf8_log_and_history(self):
        env = os.environ.copy()
        env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
        running = self.spawn(self.bat_command(), env=env)
        running.wait_for(lambda line: "Startup failed" in line)
        running.process.stdin.write("\n")
        running.process.stdin.flush()
        self.assertEqual(running.process.wait(timeout=10), 1)
        record = self.records()[0]
        self.assertEqual(record["status"], "failed", repr(record) + "\n" + "".join(running.output))
        self.assertEqual(record["stages"][-1]["name"], "python")
        self.assertEqual(record["stages"][-1]["status"], "error")
        log = (self.project / "data" / "logs" / (record["id"] + ".log")).read_text(encoding="utf-8")
        self.assertIn("Не найден рабочий Python", log)

    @unittest.skipUnless(launch.WINDOWS, "Requires Windows BAT and PowerShell")
    def test_bat_hands_boot_timings_to_the_same_python_session(self):
        running = self.spawn(self.bat_command())
        self.assert_healthy(running.url())
        running.wait_for(lambda line: "Полный запуск:" in line)
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual([row["name"] for row in records[0]["stages"]],
                         ["preparation", "python", "server", "monitor", "browser"])
        self.assertGreater(records[0]["startup_seconds"], 0)
        self.assertGreater(records[0]["stages"][1]["seconds"], 0)
        self.assertNotIn("Историю не удалось сохранить", "".join(running.output))


if __name__ == "__main__":
    unittest.main()
