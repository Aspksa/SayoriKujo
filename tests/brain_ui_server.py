"""Synthetic provider for browser QA; never contacts Cloud.ru."""
import json
from pathlib import Path
import tempfile

import test_brain as tests


class ProjectCloud(tests.FakeCloud):
    def complete(self, model, messages):
        if messages[0]["content"] == tests.brain.EXTRACT:
            payload = json.loads(messages[-1]["content"])
            text = payload["user_message"]
            if text.startswith("Проект Sakura."):
                self.analysis = {"items": [{"quote": "Осталось: добавить поиск.", "kind": "task", "scope": "project"}],
                    "project": {"name": "Sakura", "quote": "Проект Sakura."}, "progress": [
                        {"kind": "goal", "quote": "Цель: сделать помощницу."},
                        {"kind": "todo", "quote": "Осталось: добавить поиск."},
                        {"kind": "stopped", "quote": "Остановились: на памяти."}]}
            elif text.startswith("Сделано:"):
                entries = (payload["context"].get("current_project") or {}).get("entries", [])
                self.analysis = {"items": [], "progress": [
                    {"kind": "done", "quote": "Сделано: добавлен поиск.", "resolves": [e["id"] for e in entries if e["kind"] == "todo"]},
                    {"kind": "stopped", "quote": "Остановились: на проверках."}]}
            else:
                self.analysis = None
        return super().complete(model, messages)


with tempfile.TemporaryDirectory() as folder:
    cloud = ProjectCloud()
    with tests.launch.server_module.LocalServer(("127.0.0.1", 0), tests.launch.server_module.Handler) as server:
        server.brain_service = tests.brain.BrainService(Path(folder), cloud.factory, start_worker=True)
        print(f"http://127.0.0.1:{server.server_port}/brain", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
