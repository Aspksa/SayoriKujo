"""Automatic project memory; deterministic provider, no external API calls."""
import json
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

import test_brain as base

brain = base.brain


class LearningTests(unittest.TestCase):
    setUp = base.BrainTests.setUp
    turn = base.BrainTests.turn

    def analyse(self, text, items=(), project=None, progress=(), chat=None):
        self.cloud.analysis = {"items": list(items), "project": project, "progress": list(progress)}
        chat, _, _ = self.turn(text, chat_id=chat)
        self.assertTrue(self.service.process_one())
        return chat

    def project(self, name, chat=None, quote="Осталось: добавить поиск."):
        text = f"Проект {name}. {quote}"
        chat = self.analyse(text, [{"quote": quote, "kind": "task", "scope": "project", "slot": "next.task"}],
                            {"name": name, "quote": f"Проект {name}."},
                            [{"kind": "todo", "quote": quote}], chat)
        row = next(p for p in self.service.state()["projects"] if p["name"] == name)
        return chat, row

    def test_project_detected_and_inherited_with_source_links(self):
        chat, project = self.project("Sakura")
        self.analyse("Решили: хранить данные в SQLite.", [], progress=[
            {"kind": "decision", "quote": "Решили: хранить данные в SQLite."}], chat=chat)
        row = self.service.state()["projects"][0]
        self.assertEqual(row["id"], project["id"])
        self.assertEqual({e["kind"] for e in row["entries"]}, {"decision", "todo"})
        source = self.service.store.messages(chat)[-2]
        self.assertEqual(source["project_context"]["status"], "inherited")
        self.assertEqual(source["id"], row["entries"][0]["message_id"])

    def test_all_progress_categories_completion_and_handoff(self):
        chat, project = self.project("Sakura")
        task_id = project["entries"][0]["id"]
        quotes = {"goal": "Цель: сделать помощницу.", "decision": "Решили: использовать SQLite.",
                  "done": "Сделано: добавлен поиск.", "stopped": "Остановились: на проверках."}
        self.analyse(" ".join(quotes.values()), [], progress=[
            {"kind": kind, "quote": quote, "resolves": [task_id] if kind == "done" else []}
            for kind, quote in quotes.items()], chat=chat)
        summary = self.service.get("messages", {"chat_id": [chat]})["summary"]
        self.assertEqual({e["kind"] for e in summary["project"]["entries"]}, set(quotes))
        self.assertEqual(summary["message_count"], 4)
        self.assertIn("Сделано", summary["last_user"]["content"])
        reopened = brain.BrainService(self.root, self.cloud.factory, start_worker=False)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.store.learning.chat_summary(chat), summary)
        self.cloud.analysis = {"items": []}
        self.turn("Продолжай с места остановки", chat)
        prompt = json.dumps(self.cloud.calls[-1], ensure_ascii=False)
        self.assertIn(quotes["stopped"], prompt)
        self.assertIn("Текущий проект", prompt)

    def test_identical_quotes_in_two_projects_do_not_deduplicate_or_conflict(self):
        first, a = self.project("Sakura")
        second, b = self.project("Kitsune")
        rows = self.service.store.memories()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(m["status"] == "active" for m in rows))
        self.assertNotEqual(rows[0]["digest"], rows[1]["digest"])
        for chat, project in ((first, a), (second, b)):
            retrieved = self.service.store.retrieve("Продолжай", chat)
            expected = self.service.get("memories", {"project_id": [project["id"]]})["memories"]
            self.assertEqual([m["id"] for m in retrieved], [m["id"] for m in expected])

    def test_query_selects_project_without_importing_previous_handoff(self):
        chat, a = self.project("Sakura", quote="Осталось: сделать красную тему.")
        _, b = self.project("Kitsune", quote="Осталось: сделать синюю тему.")
        rows = self.service.store.retrieve("Продолжай проект Kitsune", chat)
        self.assertEqual([r["content"] for r in rows], ["Осталось: сделать синюю тему."])
        context = self.service.store.learning.context(chat, "Проект Kitsune, что дальше?")
        self.assertEqual(context["project"]["name"], "Kitsune")
        self.assertIsNone(context["summary"])
        self.assertEqual(self.service.store.retrieve("Проект Novaya. Что дальше?", chat), [])

    def test_ambiguous_projects_do_not_pollute_global_memory(self):
        chat, _ = self.project("Sakura")
        self.project("Kitsune")
        text = "Sakura и Kitsune. Осталось: добавить календарь."
        self.analyse(text, [{"kind": "task", "quote": "Осталось: добавить календарь.", "scope": "project"}],
                     progress=[{"kind": "todo", "quote": "Осталось: добавить календарь."}], chat=chat)
        source = self.service.store.messages(chat)[-2]
        self.assertEqual(source["project_context"]["status"], "ambiguous")
        self.assertEqual(len(self.service.store.memories()), 2)
        self.assertEqual(self.service.store.retrieve("Продолжай", chat), [])
        self.assertIsNone(self.service.store.learning.context(chat, "Продолжай")["summary"]["last_user"])

    def test_profile_contains_repeated_global_preferences_only(self):
        text = "Мне нужны короткие ответы."
        item = {"kind": "preference", "quote": text, "scope": "global", "slot": "answer.length"}
        self.analyse(text, [item])
        self.analyse(text, [item])
        chat, _ = self.project("Sakura")
        self.analyse("В проекте нужен тёмный интерфейс.", [{"kind": "preference", "scope": "project",
                     "quote": "В проекте нужен тёмный интерфейс."}], chat=chat)
        profile = self.service.state()["profile"]
        self.assertEqual([m["content"] for m in profile], [text])
        self.assertEqual(profile[0]["mentions"], 2)
        self.assertIn(profile[0]["id"], [m["id"] for m in self.service.store.retrieve("Как отвечать?")])

    def test_explicit_correction_versions_and_restore_without_model_calls(self):
        text = "Мне нужен русский интерфейс."
        self.analyse(text, [{"kind": "preference", "slot": "interface.language", "quote": text}])
        old = self.service.store.memories()[0]
        updated = "Теперь мне нужен английский интерфейс."
        self.analyse(updated, [{"kind": "preference", "slot": "interface.language", "quote": updated, "replaces": old["id"]}])
        rows = self.service.store.memories()
        current = next(m for m in rows if m["status"] == "active")
        self.assertEqual(current["content"], updated)
        self.assertEqual(next(m for m in rows if m["id"] == old["id"])["status"], "superseded")
        history = self.service.get("versions", {"id": [current["id"]]})["versions"]
        self.assertEqual({v["snapshot"]["content"] for v in history}, {text, updated})
        calls = len(self.cloud.calls)
        version = next(v for v in history if v["snapshot"]["content"] == text)
        self.service.post("restore-version", {"id": version["id"]})
        self.assertEqual(len(self.cloud.calls), calls)
        self.assertEqual([m["content"] for m in self.service.state()["profile"]], [text])
        self.assertTrue(self.service.state()["profile"][0]["confirmed"])

    def test_restored_manual_text_does_not_resurrect_discarded_edit(self):
        identity = self.service.store.edit_memory({"content": "Мне нужны короткие ответы.", "kind": "preference"})
        original = self.service.store.learning.versions(identity)[0]
        self.service.store.edit_memory({"id": identity, "content": "Мне нужны подробные ответы.", "kind": "preference"})
        self.service.store.learning.restore(original["id"])
        self.analyse("Мне нужны подробные ответы.", [{"kind": "preference", "quote": "Мне нужны подробные ответы."}])
        self.assertEqual([r["content"] for r in self.service.store.memories()], ["Мне нужны короткие ответы."])

    def test_restore_resolves_unrelated_slot_conflict_in_same_scope(self):
        self.analyse("Мне нужен русский интерфейс.", [{"kind": "preference", "slot": "interface.language", "quote": "Мне нужен русский интерфейс."}])
        old = self.service.store.memories()[0]
        original = self.service.store.learning.versions(old["id"])[0]
        self.analyse("Мне нужен английский интерфейс.", [{"kind": "preference", "slot": "interface.language", "quote": "Мне нужен английский интерфейс."}])
        other = next(r for r in self.service.store.memories() if r["id"] != old["id"])
        self.service.store.edit_memory({"id": other["id"], "content": other["content"], "kind": other["kind"]})
        self.service.store.learning.restore(original["id"])
        self.assertEqual([r["id"] for r in self.service.state()["profile"]], [old["id"]])

    def test_project_core_is_scoped_and_restoration_restores_scope(self):
        chat, a = self.project("Sakura")
        identity = self.service.store.memories()[0]["id"]
        self.service.store.edit_memory({"id": identity, "content": "В этом проекте важна красная тема.", "tier": "core", "project_id": a["id"]})
        version = self.service.store.learning.versions(identity)[0]
        self.service.store.edit_memory({"id": identity, "content": "Общее правило оформления.", "project_id": None})
        self.service.store.learning.restore(version["id"])
        self.assertEqual(self.service.store.retrieve("Любой вопрос"), [])
        self.assertEqual([r["id"] for r in self.service.store.retrieve("Продолжай", chat)], [identity])

    def test_implicit_conflict_cannot_claim_explicit_replacement(self):
        self.analyse("Я выбираю русский язык.", [{"kind": "preference", "slot": "language", "quote": "Я выбираю русский язык."}])
        old = self.service.store.memories()[0]
        self.analyse("Я выбираю английский язык.", [{"kind": "preference", "slot": "language",
                     "quote": "Я выбираю английский язык.", "replaces": old["id"]}])
        self.assertEqual({m["status"] for m in self.service.store.memories()}, {"conflict"})
        self.assertEqual(self.service.state()["profile"], [])

    def test_correction_cannot_replace_another_project(self):
        chat, a = self.project("Sakura")
        _, b = self.project("Kitsune", quote="Осталось: добавить календарь.")
        target = self.service.store.memories(project_id=b["id"])[0]
        self.analyse("Теперь осталось: добавить музыку.", [{"kind": "task", "slot": "next.task", "scope": "project",
                     "quote": "Теперь осталось: добавить музыку.", "replaces": target["id"]}], chat=chat)
        other = self.service.store.memories(project_id=b["id"])[0]
        self.assertEqual(other["status"], "active")
        self.assertEqual({r["status"] for r in self.service.store.memories(project_id=a["id"])}, {"conflict"})

    def test_late_background_job_does_not_rewind_fact_or_project_progress(self):
        chat, project = self.project("Sakura")
        self.turn("Теперь осталось: добавить видео. Остановились: на видео.", chat)
        older = self.service.store.claim_job()
        self.turn("Теперь осталось: добавить музыку. Остановились: на музыке.", chat)
        newer = self.service.store.claim_job()
        old_id = self.service.store.memories()[0]["id"]
        def analysis(job, task, stopped, replaces):
            return brain.validate_analysis(json.dumps({"items": [{"kind": "task", "scope": "project", "slot": "next.task",
                "quote": task, "replaces": replaces}], "progress": [{"kind": "stopped", "quote": stopped}]}), job["content"])
        self.service.store.finish_job(newer, analysis(newer, "Теперь осталось: добавить музыку.", "Остановились: на музыке.", old_id))
        active = next(m for m in self.service.store.memories() if m["status"] == "active")
        self.service.store.finish_job(older, analysis(older, "Теперь осталось: добавить видео.", "Остановились: на видео.", active["id"]))
        self.assertEqual([m["content"] for m in self.service.store.memories() if m["status"] == "active"], ["Теперь осталось: добавить музыку."])
        board = self.service.state()["projects"][0]
        self.assertEqual([e["content"] for e in board["entries"] if e["kind"] == "stopped"], ["Остановились: на музыке."])

    def test_delayed_unnamed_message_keeps_earlier_project(self):
        chat, a = self.project("Sakura")
        self.turn("Осталось: добавить рисунки.", chat)
        older = self.service.store.claim_job()
        self.project("Kitsune", chat, quote="Осталось: добавить календарь.")
        analysis = brain.validate_analysis(json.dumps({"items": [{"kind": "task", "scope": "project",
                    "quote": "Осталось: добавить рисунки."}]}), older["content"])
        self.service.store.finish_job(older, analysis)
        source = next(m for m in self.service.store.messages(chat) if m["id"] == older["message_id"])
        self.assertEqual(source["project_context"]["project_id"], a["id"])
        self.assertEqual(self.service.store.learning.chat_summary(chat)["project"]["name"], "Kitsune")
        self.assertTrue(any("рисунки" in m["content"] for m in self.service.store.memories(project_id=a["id"])))

    def test_unassigned_project_scope_is_not_promoted_to_global(self):
        self.analyse("Осталось: добавить музыку.", [{"kind": "task", "scope": "project", "quote": "Осталось: добавить музыку."}])
        self.assertEqual(self.service.store.memories(), [])

    def test_manual_project_selection_and_invalid_project(self):
        chat, a = self.project("Sakura")
        _, b = self.project("Kitsune")
        self.service.post("set-project", {"chat_id": chat, "project_id": b["id"]})
        self.assertEqual(self.service.store.learning.chat_summary(chat)["project"]["id"], b["id"])
        with self.assertRaises(brain.BrainError):
            self.service.post("set-project", {"chat_id": chat, "project_id": "missing"})

    def test_deleted_sources_remove_progress_and_restore_unresolved_task(self):
        chat, a = self.project("Sakura")
        task_id = a["entries"][0]["id"]
        completed = self.analyse("Проект Sakura. Сделано: добавлен поиск.", [],
            {"name": "Sakura", "quote": "Проект Sakura."}, [{"kind": "done", "quote": "Сделано: добавлен поиск.", "resolves": [task_id]}])
        self.assertEqual([e["kind"] for e in self.service.state()["projects"][0]["entries"]], ["done"])
        self.service.store.delete_chat(completed)
        self.assertEqual([e["kind"] for e in self.service.state()["projects"][0]["entries"]], ["todo"])
        self.service.store.delete_chat(chat)
        self.assertEqual(self.service.state()["projects"], [])
        self.assertEqual(self.service.state()["exams"]["details"], [])

    def test_checks_run_automatically_and_report_real_misses(self):
        self.analyse("Мне нужны короткие ответы.", [{"kind": "preference", "quote": "Мне нужны короткие ответы.", "question": "Как отвечать?"}])
        checks = self.service.state()["exams"]
        self.assertEqual((checks["total"], checks["passed"]), (1, 1))
        calls = len(self.cloud.calls)
        with patch.object(self.service.store, "_retrieve", return_value=[]):
            checks = self.service.post("checks", {})
        self.assertEqual(checks["passed"], 0)
        self.assertFalse(checks["details"][0]["retrieved"])
        self.assertEqual(len(self.cloud.calls), calls)
        for _ in range(22):
            self.service.store.learning.run_checks()
        with self.service.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memory_check_runs").fetchone()[0], 20)

    def test_check_failure_does_not_undo_saved_analysis(self):
        with patch.object(self.service.store.learning, "run_checks", side_effect=sqlite3.OperationalError("busy")):
            self.analyse("Мне нужны короткие ответы.", [{"kind": "preference", "quote": "Мне нужны короткие ответы."}])
        self.assertEqual(len(self.service.store.memories()), 1)
        self.assertEqual(self.service.state()["jobs"][0]["status"], "done")
        self.assertIn("недоступна", self.service.state()["exams"]["error"])

    def test_validation_rejects_fabricated_progress_projects_secrets_and_wrong_types(self):
        text = "Проект Sakura. Цель: сделать помощницу."
        parsed = brain.validate_analysis(json.dumps({"items": [{"quote": text, "kind": []}],
            "project": {"name": "Invented", "quote": text}, "progress": [
                {"kind": "done", "quote": "Сделано: всё готово."}, {"kind": [], "quote": text},
                {"kind": "goal", "quote": "Цель: сделать помощницу."}]}), text)
        self.assertEqual(parsed["items"], [])
        self.assertIsNone(parsed["project"])
        self.assertEqual(len(parsed["progress"]), 1)
        parsed = brain.validate_analysis(json.dumps({"items": [], "project": {"name": "secret", "quote": "token=secretsecret"},
                    "progress": [{"kind": "goal", "quote": "token=secretsecret"}]}), "token=secretsecret")
        self.assertEqual(parsed["progress"], [])
        self.assertIsNone(parsed["project"])

    def test_summary_excludes_deleted_and_other_project_memories(self):
        chat, a = self.project("Sakura", quote="Осталось: добавить красную тему.")
        self.project("Kitsune", chat, quote="Осталось: добавить синюю тему.")
        summary = self.service.store.learning.chat_summary(chat)
        self.assertEqual([r["content"] for r in summary["memories"]], ["Осталось: добавить синюю тему."])
        identity = summary["memories"][0]["id"]
        self.service.store.delete_memory(identity)
        self.assertEqual(self.service.store.learning.chat_summary(chat)["memories"], [])
        self.assertEqual(self.service.store.learning.versions(identity), [])

    def test_legacy_upgrade_preserves_data_and_creates_backup_for_each_schema(self):
        self.turn()
        self.service.process_one()
        memory_id = self.service.store.memories()[0]["id"]
        key_before = (self.root / "secret.json").read_bytes()
        self.service.close()
        with closing(sqlite3.connect(self.service.store.path)) as db:
            legacy = {"settings", "chats", "messages", "memories", "sources", "memory_terms", "tombstones", "jobs"}
            for table in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                if table[0] not in legacy:
                    db.execute('DROP TABLE "' + table[0] + '"')
            db.execute("PRAGMA user_version=0")
        reopened = brain.BrainService(self.root, self.cloud.factory, start_worker=False)
        self.addCleanup(reopened.close)
        self.assertEqual((self.root / "secret.json").read_bytes(), key_before)
        self.assertEqual(reopened.store.memories()[0]["id"], memory_id)
        self.assertTrue(reopened.store.learning.versions(memory_id))
        backups = list((self.root / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 3)
        backups = list((self.root / "backups").glob("before-v2-*.sqlite3"))
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
        another = brain.BrainService(self.root, self.cloud.factory, start_worker=False)
        self.addCleanup(another.close)
        self.assertEqual(len(list((self.root / "backups").glob("*.sqlite3"))), 3)
        self.assertEqual(len(another.store.messages(another.store.overview()["chats"][0]["id"])), 2)


if __name__ == "__main__":
    unittest.main()
