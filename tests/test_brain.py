from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

import test_launch as launch


brain = launch.server_module.brain_module
provider = __import__('sayori_brain.provider', fromlist=['CloudClient'])
KEY = "test-private-key-1234567890"


class FakeCloud:
    def __init__(self):
        self.calls = []
        self.items = None
        self.bad_json = False
        self.analysis = None

    def factory(self, endpoint, key):
        self.endpoint, self.key = endpoint, key
        return self

    def models(self):
        self.calls.append("models")
        return [{"id": "chat-model", "chat_candidate": True}]

    def complete(self, model, messages):
        self.calls.append({"model": model, "messages": messages})
        if messages[0]["content"] == brain.EXTRACT:
            if self.bad_json:
                return "not json", {}
            text = json.loads(messages[-1]["content"])["user_message"]
            if self.analysis is not None:
                value = self.analysis(json.loads(messages[-1]["content"])) if callable(self.analysis) else self.analysis
                return json.dumps(value, ensure_ascii=False), {}
            items = self.items if self.items is not None else ([] if text.rstrip().endswith('?') else [
                {"quote": text, "kind": "preference", "slot": "interface.language", "note": "Предпочтение интерфейса"}])
            return json.dumps({"items": items}, ensure_ascii=False), {}
        return "Ответ получен.", {"total_tokens": 10}


class BrainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sayori-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Саёри & память"
        self.cloud = FakeCloud()
        self.service = brain.BrainService(self.root, self.cloud.factory, start_worker=False)
        self.addCleanup(self.service.close)
        self.service.settings({"api_key": KEY, "model": "chat-model", "memory_model": "memory-model"})

    def turn(self, text="Мне нужен русский интерфейс.", chat_id=None, request_id=None):
        chat_id = chat_id or self.service.store.create_chat()
        request_id = request_id or uuid.uuid4().hex
        result = self.service.chat({"chat_id": chat_id, "text": text, "request_id": request_id})
        return chat_id, request_id, result

    def test_chat_analysis_memory_and_retrieval_survive_restart(self):
        first, _, _ = self.turn()
        self.assertTrue(self.service.process_one())
        memory = self.service.store.memories()[0]
        self.assertEqual(memory["content"], "Мне нужен русский интерфейс.")
        self.assertEqual(memory["sources"][0]["chat_id"], first)
        reopened = brain.BrainService(self.root, self.cloud.factory, start_worker=False)
        self.addCleanup(reopened.close)
        chat_id = reopened.store.create_chat()
        result = reopened.chat({"chat_id": chat_id, "text": "Какой интерфейс мне нужен?", "request_id": uuid.uuid4().hex})
        self.assertEqual(result["context"]["memories"], 1)
        self.assertIn("Мне нужен русский интерфейс.", json.dumps(self.cloud.calls[-1], ensure_ascii=False))

    def test_no_external_calls_on_configuration_or_state_and_auto_off(self):
        self.service.state()
        self.service.settings({"auto_memory": False})
        self.assertEqual(self.cloud.calls, [])
        self.turn()
        self.assertFalse(self.service.process_one())
        self.assertEqual(len(self.cloud.calls), 1)

    def test_duplicate_request_does_not_charge_second_completion(self):
        chat, request, first = self.turn()
        self.turn(chat_id=chat, request_id=request)
        self.assertEqual(len(self.cloud.calls), 1)
        self.assertEqual(len(self.service.store.messages(chat)), 2)

    def test_read_failure_after_saved_reply_does_not_turn_success_into_failure(self):
        chat_id = self.service.store.create_chat()
        request_id = uuid.uuid4().hex
        payload = {"chat_id": chat_id, "text": "Мне нужен русский интерфейс.", "request_id": request_id}
        original = self.service.store.messages
        reads = 0

        def read_once_then_fail(identity):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise brain.BrainError("Временная ошибка чтения", 503)
            return original(identity)

        with patch.object(self.service.store, "messages", side_effect=read_once_then_fail):
            with self.assertRaises(brain.BrainError):
                self.service.chat(payload)
        saved = original(chat_id)
        self.assertEqual([row["status"] for row in saved], ["done", "done"])
        retry = self.service.chat(payload)
        self.assertTrue(retry["repeated"])
        self.assertEqual(len(retry["messages"]), 2)
        self.assertEqual(len(self.cloud.calls), 1)

    def test_message_reports_memory_job_progress_and_exact_sources(self):
        chat_id, _, _ = self.turn()
        source = self.service.store.messages(chat_id)[0]
        self.assertEqual(source["memory_job"]["status"], "pending")
        self.assertEqual(self.service.state()["analysis"]["pending"], 1)
        job = self.service.store.claim_job()
        self.assertEqual(self.service.store.messages(chat_id)[0]["memory_job"]["status"], "processing")
        self.service.store.finish_job(job, [{"quote": job["content"], "kind": "preference"}])
        source = self.service.store.messages(chat_id)[0]
        self.assertEqual(source["memory_job"]["status"], "done")
        self.assertEqual(source["memory_job"]["added"], 1)
        self.assertEqual(self.service.state()["analysis"]["done"], 1)
        unrelated = self.service.store.edit_memory({"content": "Отдельное ручное воспоминание"})
        rows = self.service.get("memories", {"message_id": [source["id"]]})["memories"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sources"][0]["id"], source["id"])
        self.assertNotEqual(rows[0]["id"], unrelated)
        selected = self.service.get("memories", {"ids": [unrelated]})["memories"]
        self.assertEqual([row["id"] for row in selected], [unrelated])

    def test_failed_memory_analysis_is_visible_on_its_message(self):
        chat_id, _, _ = self.turn()
        self.cloud.bad_json = True
        self.service.process_one()
        user = self.service.store.messages(chat_id)[0]
        self.assertEqual(user["memory_job"]["status"], "failed")
        self.assertIn("JSON", user["memory_job"]["error"])

    def test_source_filters_reject_invalid_or_excessive_ids(self):
        for query in ({"message_id": ["invalid"]}, {"ids": ["' OR 1=1"]},
                      {"ids": [",".join([uuid.uuid4().hex for _ in range(21)])]}):
            with self.subTest(query=query), self.assertRaises(brain.BrainError) as error:
                self.service.get("memories", query)
            self.assertEqual(error.exception.status, 400)

    def test_only_grounded_user_quotes_are_stored(self):
        self.cloud.items = [
            {"quote": "Пользователь любит зелёный.", "kind": "fact"},
            {"quote": "Мне нужен русский интерфейс.", "kind": "preference"},
        ]
        self.turn()
        self.service.process_one()
        self.assertEqual([r["content"] for r in self.service.store.memories()], ["Мне нужен русский интерфейс."])

    def test_repeated_fact_has_one_memory_and_two_provenance_links(self):
        self.turn()
        self.service.process_one()
        self.turn()
        self.service.process_one()
        rows = self.service.store.memories()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["sources"]), 2)

    def test_question_is_not_saved_as_fact_even_if_extractor_suggests_it(self):
        question = "Какой интерфейс мне нужен?"
        self.cloud.items = [{"quote": question, "kind": "fact"}]
        self.turn(question)
        self.service.process_one()
        self.assertEqual(self.service.store.memories(), [])

    def test_delete_memory_prevents_resurrection_on_reanalysis(self):
        chat, _, _ = self.turn()
        self.service.process_one()
        self.service.store.delete_memory(self.service.store.memories()[0]["id"])
        user_id = self.service.store.messages(chat)[0]["id"]
        self.service.store.enqueue(user_id)
        self.service.process_one()
        self.assertEqual(self.service.store.memories(), [])

    def test_delete_chat_cancels_processing_and_removes_unconfirmed_facts(self):
        chat, _, _ = self.turn()
        job = self.service.store.claim_job()
        self.service.store.delete_chat(chat)
        self.assertEqual(self.service.store.finish_job(job, [{"quote": job["content"], "kind": "fact"}]), 0)
        self.assertEqual(self.service.store.overview()["jobs"], [])
        self.assertEqual(self.service.store.memories(), [])

    def test_key_revision_change_cancels_stale_analysis_result(self):
        self.turn()
        job = self.service.store.claim_job()
        self.service.settings({"delete_key": True})
        self.assertEqual(self.service.store.finish_job(job, [{"quote": job["content"], "kind": "fact"}]), 0)
        self.assertEqual(self.service.store.memories(), [])
        self.assertEqual(self.service.state()["jobs"][0]["status"], "cancelled")

    def test_invalid_analysis_is_visible_and_does_not_destroy_chat(self):
        chat, _, _ = self.turn()
        self.cloud.bad_json = True
        self.service.process_one()
        self.assertEqual(self.service.state()["jobs"][0]["status"], "failed")
        self.assertEqual(len(self.service.store.messages(chat)), 2)
        self.assertEqual(self.service.store.memories(), [])

    def test_possible_conflicts_are_excluded_until_user_resolves_them(self):
        self.turn()
        self.service.process_one()
        self.turn("Мне нужен английский интерфейс.")
        self.service.process_one()
        rows = self.service.store.memories()
        self.assertTrue(all(row["status"] == "conflict" for row in rows))
        self.assertEqual(self.service.store.retrieve("интерфейс"), [])
        chosen = rows[0]
        self.service.store.edit_memory({"id": chosen["id"], "content": chosen["content"], "kind": chosen["kind"], "tier": "core"})
        self.assertEqual(len(self.service.store.retrieve("Любая новая тема")), 1)

    def test_archive_excluded_and_core_included_without_keyword_match(self):
        self.service.store.edit_memory({"content": "Обращаться ко мне по имени Aspksa", "tier": "core"})
        self.service.store.edit_memory({"content": "Старый проект интерфейса", "tier": "archive"})
        self.assertEqual(len(self.service.store.retrieve("интерфейс")), 1)
        self.assertEqual(len(self.service.store.memories("старый")), 1)

    def test_expired_analysis_is_not_automatically_reissued(self):
        self.turn()
        self.service.store.claim_job()
        with self.service.store.connection(write=True) as db:
            db.execute("UPDATE jobs SET started=?", (time.time() - 500,))
        before = len(self.cloud.calls)
        self.assertFalse(self.service.process_one())
        self.assertEqual(len(self.cloud.calls), before)
        self.assertEqual(self.service.state()["jobs"][0]["status"], "failed")

    def test_two_instances_cannot_claim_the_same_memory_job(self):
        self.turn()
        other = brain.BrainService(self.root, self.cloud.factory, start_worker=False)
        self.addCleanup(other.close)
        self.assertIsNotNone(self.service.store.claim_job())
        self.assertIsNone(other.store.claim_job())

    def test_key_is_not_returned_by_state_or_saved_in_database(self):
        self.assertNotIn(KEY, json.dumps(self.service.state()))
        self.assertNotIn(KEY.encode(), self.service.store.path.read_bytes())
        self.assertEqual(self.service.keys.get(), KEY)
        if os.name == "nt":
            self.assertNotIn(KEY, self.service.keys.path.read_text())
        else:
            self.assertEqual(self.service.keys.path.stat().st_mode & 0o777, 0o600)

    def test_known_key_in_chat_is_rejected_before_persistence_or_api_call(self):
        with self.assertRaises(brain.BrainError):
            self.turn("Мой ключ: " + KEY)
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.service.state()["growth"]["messages"], 0)


class TransportTests(unittest.TestCase):
    def test_minimal_payload_and_text_model_filter(self):
        class Response(io.BytesIO):
            pass
        class Opener:
            def __init__(self):
                self.requests = []
            def open(self, request, timeout):
                self.requests.append(request)
                result = ({"data": [{"id": "BAAI/bge-m3"}, {"id": "some/chat-model"}, {"id": "GigaChat/SaluteSpeech"}]}
                          if request.full_url.endswith('/models') else {"choices": [{"message": {"content": "Работает"}}]})
                return Response(json.dumps(result).encode())
        opener = Opener()
        client = provider.CloudClient(provider.ENDPOINTS[0], KEY, opener)
        rows = client.models()
        self.assertEqual([r["id"] for r in rows if r["chat_candidate"]], ["some/chat-model"])
        self.assertEqual(client.complete("some/chat-model", [{"role": "user", "content": "Привет"}])[0], "Работает")
        payload = json.loads(opener.requests[-1].data)
        self.assertEqual(set(payload), {"model", "messages", "stream"})
        self.assertFalse(payload["stream"])

    def test_secret_is_redacted_from_provider_error(self):
        class Opener:
            def open(self, request, timeout):
                error = json.dumps({"error": {"message": "Bad " + KEY}}).encode()
                raise HTTPError(request.full_url, 400, "Bad request", {}, io.BytesIO(error))
        with self.assertRaises(brain.BrainError) as error:
            provider.CloudClient(provider.ENDPOINTS[0], KEY, Opener()).models()
        self.assertNotIn(KEY, str(error.exception))

    def test_key_cannot_be_sent_to_arbitrary_endpoint_or_redirect(self):
        with self.assertRaises(brain.BrainError):
            provider.CloudClient("https://example.org/v1", KEY)
        with self.assertRaises(brain.BrainError):
            provider._NoRedirect().redirect_request(None, None, None, None, None, None)


class BrainHttpTests(unittest.TestCase):
    @contextmanager
    def live_server(self):
        with tempfile.TemporaryDirectory() as folder:
            cloud = FakeCloud()
            with launch.server_module.LocalServer(("127.0.0.1", 0), launch.server_module.Handler) as server:
                server.brain_service = brain.BrainService(Path(folder), cloud.factory, start_worker=False)
                worker = threading.Thread(target=server.serve_forever)
                worker.start()
                try:
                    yield server, cloud, f"http://127.0.0.1:{server.server_port}/"
                finally:
                    server.shutdown()
                    worker.join(timeout=5)

    def test_csrf_and_origin_protect_key_settings(self):
        with self.live_server() as (server, cloud, url):
            with launch.HTTP.open(url + 'api/brain/state', timeout=5) as response:
                state = json.load(response)
            for token, origin in (("", url.rstrip('/')), (state['csrf_token'], 'https://example.org')):
                request = Request(url + 'api/brain/settings', data=json.dumps({'api_key':KEY}).encode(), headers={
                    'Content-Type':'application/json','X-Sayori-Token':token,'Origin':origin})
                with self.assertRaises(HTTPError) as error:
                    launch.HTTP.open(request, timeout=5)
                self.assertEqual(error.exception.code,403)
            request = Request(url + 'api/brain/settings', data=json.dumps({'api_key':KEY,'model':'chat-model'}).encode(),headers={
                'Content-Type':'application/json','X-Sayori-Token':state['csrf_token'],'Origin':url.rstrip('/')})
            with launch.HTTP.open(request,timeout=5) as response:
                saved=json.load(response)
            self.assertTrue(saved['has_key'])
            self.assertNotIn(KEY,json.dumps(saved))
            self.assertEqual(cloud.calls,[])

    def test_brain_assets_load_but_data_and_python_sources_remain_private(self):
        with self.live_server() as (server,cloud,url):
            for path in ('brain','brain/ui.js','brain/ui.css','brain/map.js','brain/map.css','brain/map-canvas.js'):
                with launch.HTTP.open(url+path,timeout=5) as response:
                    self.assertEqual(response.status,200)
                    self.assertIn("script-src 'self'",response.headers['Content-Security-Policy'])
            for path in ('data/brain/secret.json','data/brain/brain.sqlite3','modules/brain/provider.py','modules/brain/growth.py','modules/brain/map_data.py','data/brain/backups/before-v3.sqlite3','brain/../provider.py'):
                with self.assertRaises(HTTPError) as error:
                    launch.HTTP.open(url+path,timeout=5)
                self.assertEqual(error.exception.code,404)


if __name__ == '__main__':
    unittest.main()
