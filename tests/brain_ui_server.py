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
            if text.startswith('Большая ветка Голос.'):
                self.analysis={'items':[],'development':{'branch':{'name':'Голос','quote':'Большая ветка Голос.'},
                    'entries':[{'kind':'question','quote':quote} for quote in text.splitlines()[1:11]]}}
            elif text.startswith('Ветка Голос.'):
                self.analysis = {'items':[{'kind':'preference','scope':'global','slot':'voice.language','quote':'Хочу русскую озвучку.'}],
                    'development':{'branch':{'name':'Голос','quote':'Ветка Голос.'},'entries':[
                        {'kind':'question','quote':'Какой тембр выбрать?'},
                        {'kind':'option','quote':'Вариант: низкий голос.'},
                        {'kind':'option','quote':'Вариант: мягкий голос.'}]}}
            elif text == 'Выбираю мягкий голос.':
                entries=(payload.get('development_context') or {}).get('entries',[])
                self.analysis={'items':[],'development':{'entries':[{'kind':'choice','quote':text,
                    'targets':[e['id'] for e in entries if e['kind']=='question' or e['quote']=='Вариант: мягкий голос.']}]}}
            elif text.startswith('Я предпочитаю короткие ответы'):
                self.analysis={'items':[{'kind':'preference','scope':'global','quote':text,'slot':''}]}
            elif text.startswith("Проект Sakura."):
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
        server.session_store=tests.launch.server_module.diagnostics.SessionStore(Path(folder)/'launch-fixture')
        for status in ('stopped','failed','interrupted'):
            journal=tests.launch.server_module.diagnostics.Session(server.session_store)
            journal.stage('python','Проверка Python',.18)
            journal.stage('server','Подготовка локального сервера',.24,'error' if status=='failed' else 'ok')
            journal.startup_complete()
            journal.finish(status=status,snapshot={'network_pc':{'received_bytes':2048000,'sent_bytes':512000,'complete':True},'local_http':{'received_bytes':20480,'sent_bytes':40960,'complete':True}})
        server.brain_service = tests.brain.BrainService(Path(folder), cloud.factory, start_worker=True)
        print(f"http://127.0.0.1:{server.server_port}/brain", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
