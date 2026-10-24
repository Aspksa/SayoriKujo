"""Project-aware memory, version history, profiles, handoffs and local retrieval checks."""

from contextlib import closing
import json
import hashlib
import re
import sqlite3
import uuid

from .provider import BrainError
from .store import now, normalized, fingerprint, terms


PROGRESS = {"goal", "decision", "done", "todo", "stopped"}
CORRECTION = re.compile(r"\b(?:теперь|вместо|исправ\w*|замен\w*|отныне|больше\s+не)\b", re.I)


class Learning:
    def __init__(self, store):
        self.store = store
        self.check_error = None

    @staticmethod
    def digest(content, scope="global", project_id=None):
        if scope == "global":
            return fingerprint(content)
        return hashlib.sha256(("project\0" + str(project_id) + "\0" + normalized(content)).encode()).hexdigest()

    def migrate(self, backup):
        with self.store.connection() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] >= 2:
                return
        if backup:
            folder = self.store.root / "backups"
            folder.mkdir(exist_ok=True)
            destination = folder / ("before-v2-" + uuid.uuid4().hex + ".sqlite3")
            with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(destination)) as target:
                source.backup(target)
        schema = [
            """CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE,origin TEXT NOT NULL DEFAULT 'auto',created TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS project_sources(project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
                message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,PRIMARY KEY(project_id,message_id))""",
            """CREATE TABLE IF NOT EXISTS chat_projects(chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
                project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,source_created TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS analysis_contexts(message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,status TEXT NOT NULL,reason TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS memory_scopes(memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                scope TEXT NOT NULL DEFAULT 'global')""",
            """CREATE TABLE IF NOT EXISTS memory_projects(memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,PRIMARY KEY(memory_id,project_id))""",
            """CREATE TABLE IF NOT EXISTS project_entries(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,kind TEXT NOT NULL,content TEXT NOT NULL,
                digest TEXT NOT NULL,created TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',
                resolved_by TEXT REFERENCES project_entries(id) ON DELETE SET NULL,UNIQUE(project_id,message_id,kind,digest))""",
            "CREATE INDEX IF NOT EXISTS project_entries_lookup ON project_entries(project_id,created)",
            """CREATE TABLE IF NOT EXISTS memory_series(memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                series_id TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS memory_versions(id TEXT PRIMARY KEY,memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                snapshot TEXT NOT NULL,created TEXT NOT NULL,reason TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS chat_summaries(chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
                data TEXT NOT NULL,updated TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS memory_checks(id TEXT PRIMARY KEY,memory_id TEXT NOT NULL UNIQUE REFERENCES memories(id) ON DELETE CASCADE,
                question TEXT NOT NULL,project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,created TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS memory_check_runs(id TEXT PRIMARY KEY,created TEXT NOT NULL,total INTEGER NOT NULL,
                passed INTEGER NOT NULL,details TEXT NOT NULL)""",
        ]
        with self.store.connection(write=True) as db:
            if db.execute("PRAGMA user_version").fetchone()[0] >= 2:
                return
            for statement in schema:
                db.execute(statement)
            for row in db.execute("SELECT * FROM memories").fetchall():
                self.ensure_memory(db, row["id"])
                self.version(db, row["id"], "Начальная версия при обновлении")
                self.check_case(db, row["id"])
            for row in db.execute("SELECT id FROM chats").fetchall():
                self.refresh_summary(db, row[0])
            db.execute("PRAGMA user_version=2")

    def ensure_memory(self, db, identity):
        db.execute("INSERT OR IGNORE INTO memory_scopes VALUES(?,'global')", (identity,))
        db.execute("INSERT OR IGNORE INTO memory_series VALUES(?,?)", (identity, identity))

    def snapshot(self, db, identity):
        row = db.execute("SELECT * FROM memories WHERE id=?", (identity,)).fetchone()
        if not row:
            return None
        self.ensure_memory(db, identity)
        result = dict(row)
        result["scope"] = db.execute("SELECT scope FROM memory_scopes WHERE memory_id=?", (identity,)).fetchone()[0]
        result["projects"] = [r[0] for r in db.execute("SELECT project_id FROM memory_projects WHERE memory_id=?", (identity,))]
        return result

    def version(self, db, identity, reason):
        snapshot = self.snapshot(db, identity)
        if snapshot is not None:
            db.execute("INSERT INTO memory_versions VALUES(?,?,?,?,?)", (
                uuid.uuid4().hex, identity, json.dumps(snapshot, ensure_ascii=False), now(), reason))

    def matching_projects(self, db, text):
        normalized_text = " " + normalized(text) + " "
        return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY name")
                if " " + row["normalized_name"] + " " in normalized_text]

    def focus(self, db, chat_id, query=""):
        matches = self.matching_projects(db, query)
        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) > 1:
            return None
        # A new explicitly named project must not inherit another project's facts.
        if re.search(r'\b(?i:проект(?:е|а|ом)?)\s+[«"“]?[A-ZА-ЯЁ][\w.-]{1,79}', query):
            return None
        row = db.execute("SELECT project_id FROM chat_projects WHERE chat_id=?", (chat_id,)).fetchone() if chat_id else None
        return row[0] if row else None

    def scope_matches(self, db, memory_id, project_id):
        scope = db.execute("SELECT scope FROM memory_scopes WHERE memory_id=?", (memory_id,)).fetchone()
        if not scope or scope[0] == "global":
            return True
        return bool(project_id and db.execute("SELECT 1 FROM memory_projects WHERE memory_id=? AND project_id=?",
                                              (memory_id, project_id)).fetchone())

    def same_scope(self, db, identity, scope, project_id):
        row = db.execute("SELECT scope FROM memory_scopes WHERE memory_id=?", (identity,)).fetchone()
        current = row[0] if row else "global"
        if scope == "global":
            return current == "global"
        return current == "project" and bool(db.execute(
            "SELECT 1 FROM memory_projects WHERE memory_id=? AND project_id=?", (identity, project_id)).fetchone())

    def choose_project(self, db, job, analysis):
        source = db.execute("SELECT created FROM messages WHERE id=?", (job["message_id"],)).fetchone()
        proposal = analysis.get("project")
        known = self.matching_projects(db, job["content"])
        identities = {row["id"] for row in known}
        project_id = None
        if proposal:
            name, quote = proposal["name"], proposal["quote"]
            normalized_name = normalized(name)
            prior = db.execute("SELECT id FROM projects WHERE normalized_name=?", (normalized_name,)).fetchone()
            if prior:
                identities.add(prior[0])
            elif not known:
                project_id = uuid.uuid4().hex
                db.execute("INSERT INTO projects(id,name,normalized_name,created) VALUES(?,?,?,?)",
                           (project_id, name, normalized_name, now()))
                identities.add(project_id)
        if len(identities) > 1:
            status, reason = "ambiguous", "Упомянуто несколько проектов; сведения не привязаны автоматически."
            project_id = None
        elif identities:
            project_id = next(iter(identities))
            status, reason = "recognized", "Проект указан в сообщении."
            db.execute("INSERT OR IGNORE INTO project_sources VALUES(?,?)", (project_id, job["message_id"]))
        else:
            # A delayed job inherits the context at its message time, never a later switch.
            previous = db.execute("""SELECT a.project_id,m.created FROM analysis_contexts a
                JOIN messages m ON m.id=a.message_id WHERE m.chat_id=? AND m.created<=?
                ORDER BY m.created DESC,m.rowid DESC LIMIT 1""", (job["chat_id"], source[0])).fetchone()
            selected = db.execute("SELECT * FROM chat_projects WHERE chat_id=?", (job["chat_id"],)).fetchone()
            project_id = previous["project_id"] if previous else None
            if selected and selected["source_created"] <= source[0] and (not previous or selected["source_created"] >= previous["created"]):
                project_id = selected["project_id"]
            status, reason = (("inherited", "Продолжение текущего проекта.") if project_id else
                              ("unassigned", "Явный проект не определён."))
        db.execute("INSERT OR REPLACE INTO analysis_contexts VALUES(?,?,?,?)", (job["message_id"], project_id, status, reason))
        if project_id or status == "ambiguous":
            db.execute("""INSERT INTO chat_projects VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                       project_id=excluded.project_id,source_created=excluded.source_created
                       WHERE excluded.source_created>=chat_projects.source_created""",
                       (job["chat_id"], project_id, source[0]))
        return project_id, status

    def basis_time(self, db, row):
        if row["origin"] == "manual":
            return row["updated"]
        source = db.execute("SELECT MAX(m.created) FROM sources s JOIN messages m ON m.id=s.message_id WHERE s.memory_id=?", (row["id"],)).fetchone()[0]
        return source or row["created"]

    def apply_analysis(self, db, job, analysis):
        project_id, context_status = self.choose_project(db, job, analysis)
        source_created = db.execute("SELECT created FROM messages WHERE id=?", (job["message_id"],)).fetchone()[0]
        added = 0
        for item in analysis["items"]:
            quote = item["quote"]
            if context_status == "ambiguous" and not item.get("scope") and item["kind"] != "preference":
                continue
            scope = item.get("scope") or ("global" if item["kind"] == "preference" else "project" if project_id else "global")
            if self.matching_projects(db, quote):
                scope = "project"
            if scope == "project" and not project_id:
                continue
            digest = self.digest(quote, scope, project_id)
            if db.execute("SELECT 1 FROM tombstones WHERE digest=?", (digest,)).fetchone():
                continue
            slot = item.get("slot", "")
            prior = db.execute("SELECT * FROM memories WHERE digest=?", (digest,)).fetchone()
            identity = prior["id"] if prior else uuid.uuid4().hex
            target = db.execute("SELECT * FROM memories WHERE id=?", (item.get("replaces", ""),)).fetchone()
            valid_update = bool(target and target["id"] != identity and target["status"] == "active"
                                and target["kind"] == item["kind"] and slot and target["slot"] == slot
                                and CORRECTION.search(quote) and self.same_scope(db, target["id"], scope, project_id)
                                and source_created >= self.basis_time(db, target))
            conflicts = [row for row in db.execute("SELECT * FROM memories WHERE slot=? AND kind=? AND id!=? AND status IN ('active','conflict')",
                         (slot, item["kind"], identity)) if slot and self.same_scope(db, row["id"], scope, project_id)]
            older = bool(conflicts and any(source_created < self.basis_time(db, row) for row in conflicts))
            state = "active" if valid_update or not conflicts else "superseded" if older else "conflict"
            if valid_update:
                self.ensure_memory(db, target["id"])
                self.version(db, target["id"], "До явного исправления пользователем")
                db.execute("UPDATE memories SET status='superseded',updated=? WHERE id=?", (now(), target["id"]))
            elif conflicts and not older and not prior:
                for row in conflicts:
                    if row["origin"] == "auto":
                        self.version(db, row["id"], "До обнаружения возможного противоречия")
                        db.execute("UPDATE memories SET status='conflict',updated=? WHERE id=?", (now(), row["id"]))
            if not prior:
                db.execute("""INSERT INTO memories(id,content,digest,kind,tier,slot,note,origin,status,created,updated)
                           VALUES(?,?,?,?,?,?,?,'auto',?,?,?)""", (identity, quote, digest, item["kind"],
                           target["tier"] if valid_update else "long", slot, item.get("note", ""), state, now(), now()))
                self.store._index(db, identity, quote)
                self.ensure_memory(db, identity)
                db.execute("UPDATE memory_scopes SET scope=? WHERE memory_id=?", (scope, identity))
                added += 1
            elif valid_update:
                self.version(db, identity, "До повторного выбора прежнего факта")
                db.execute("UPDATE memories SET status='active',updated=? WHERE id=?", (now(), identity))
            if project_id and scope == "project":
                db.execute("INSERT OR IGNORE INTO memory_projects VALUES(?,?)", (identity, project_id))
            if valid_update:
                series = db.execute("SELECT series_id FROM memory_series WHERE memory_id=?", (target["id"],)).fetchone()[0]
                db.execute("UPDATE memory_series SET series_id=? WHERE memory_id=?", (series, identity))
            db.execute("INSERT OR IGNORE INTO sources VALUES(?,?)", (identity, job["message_id"]))
            if not prior or valid_update:
                self.version(db, identity, "Явное обновление факта" if valid_update else "Из сообщения пользователя")
            self.check_case(db, identity, item.get("question", ""), project_id if scope == "project" else None)
        if project_id:
            self.progress(db, project_id, job, analysis.get("progress", []), source_created)
        self.refresh_summary(db, job["chat_id"])
        self.store.growth.sync_analysis(db, job, analysis)
        return added

    def progress(self, db, project_id, job, entries, created):
        for entry in entries:
            identity = uuid.uuid4().hex
            kind, content = entry["kind"], entry["quote"]
            cursor = db.execute("INSERT OR IGNORE INTO project_entries(id,project_id,message_id,kind,content,digest,created) VALUES(?,?,?,?,?,?,?)",
                                (identity, project_id, job["message_id"], kind, content, fingerprint(content), created))
            if not cursor.rowcount:
                continue
            if kind in ("goal", "stopped"):
                previous = db.execute("SELECT * FROM project_entries WHERE project_id=? AND kind=? AND id!=? AND status='active' ORDER BY created DESC LIMIT 1",
                                      (project_id, kind, identity)).fetchone()
                if previous and previous["created"] > created:
                    db.execute("UPDATE project_entries SET status='superseded',resolved_by=? WHERE id=?", (previous["id"], identity))
                elif previous:
                    db.execute("UPDATE project_entries SET status='superseded',resolved_by=? WHERE id=?", (identity, previous["id"]))
            if kind == "done":
                for target in entry.get("resolves", []):
                    db.execute("UPDATE project_entries SET status='resolved',resolved_by=? WHERE id=? AND project_id=? AND kind='todo' AND created<=?",
                               (identity, target, project_id, created))

    def board(self, db, project_id):
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return None
        entries = [dict(item) for item in db.execute("""SELECT e.*,m.chat_id FROM project_entries e JOIN messages m ON m.id=e.message_id
                   WHERE e.project_id=? ORDER BY e.created DESC,e.rowid DESC""", (project_id,))]
        seen, filtered = set(), []
        for entry in entries:
            key = (entry["kind"], entry["digest"])
            if key not in seen:
                seen.add(key)
                if entry['status'] == 'active' or entry['resolved_by'] is None:
                    filtered.append(entry)
        return {**dict(row), "entries": filtered[:50]}

    def projects(self):
        with self.store.connection() as db:
            return [self.board(db, row[0]) for row in db.execute("SELECT id FROM projects ORDER BY created DESC")]

    def profile(self):
        with self.store.connection() as db:
            rows = db.execute("""SELECT m.*,(SELECT COUNT(*) FROM sources s WHERE s.memory_id=m.id) AS mentions
                       FROM memories m LEFT JOIN memory_scopes sc ON sc.memory_id=m.id
                       WHERE m.kind='preference' AND m.status='active' AND m.tier!='archive' AND COALESCE(sc.scope,'global')='global'
                       ORDER BY m.confirmed DESC,mentions DESC,m.updated DESC""").fetchall()
            return [dict(row) for row in rows]

    def refresh_summary(self, db, chat_id):
        messages = db.execute("SELECT id,role FROM messages WHERE chat_id=? AND status='done' ORDER BY created DESC,rowid DESC", (chat_id,)).fetchall()
        data = {"last_user": next((row[0] for row in messages if row[1] == "user"), None),
                "last_assistant": next((row[0] for row in messages if row[1] == "assistant"), None), "message_count": len(messages)}
        db.execute("INSERT INTO chat_summaries VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET data=excluded.data,updated=excluded.updated",
                   (chat_id, json.dumps(data), now()))

    def summary(self, db, chat_id):
        row = db.execute("SELECT * FROM chat_summaries WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row["data"])
        result = {"updated": row["updated"], "message_count": data["message_count"]}
        for key in ("last_user", "last_assistant"):
            message = db.execute("SELECT id,content,created FROM messages WHERE id=?", (data.get(key),)).fetchone()
            result[key] = {**dict(message), "content": message["content"][:700]} if message else None
            if message and key == 'last_user':
                context = db.execute("SELECT project_id,status FROM analysis_contexts WHERE message_id=?", (message['id'],)).fetchone()
                result[key]['project_id'] = context['project_id'] if context else None
                result[key]['context_status'] = context['status'] if context else 'pending'
        project_id = self.focus(db, chat_id)
        result["project"] = self.board(db, project_id) if project_id else None
        records = db.execute("""SELECT DISTINCT m.id,m.content FROM memories m
            JOIN sources s ON s.memory_id=m.id JOIN messages msg ON msg.id=s.message_id
            WHERE msg.chat_id=? AND m.status='active' AND m.tier!='archive' ORDER BY m.updated DESC""", (chat_id,)).fetchall()
        result["memories"] = [dict(memory) for memory in records if self.scope_matches(db, memory['id'], project_id)
            and not self.store.growth.task_completed(db, memory['id'])][:8]
        return result

    def chat_summary(self, chat_id):
        with self.store.connection() as db:
            return self.summary(db, chat_id)

    def analysis_context(self, chat_id, text):
        with self.store.connection() as db:
            focus = self.focus(db, chat_id, text)
            project = self.board(db, focus) if focus else None
            records = self.store._retrieve(db, text, chat_id)
            for record in records:
                row = db.execute("SELECT kind,slot FROM memories WHERE id=?", (record["id"],)).fetchone()
                record.update(dict(row))
            return {"known_projects": [row[0] for row in db.execute("SELECT name FROM projects ORDER BY created DESC LIMIT 50")],
                    "current_project": project, "existing_facts": records}

    def context(self, chat_id, text):
        with self.store.connection() as db:
            focus = self.focus(db, chat_id, text)
            project = self.board(db, focus) if focus else None
            if project:
                project = {"id": project["id"], "name": project["name"], "entries": [{"id": entry["id"], "kind": entry["kind"], "quote": entry["content"],
                           "source_message": entry["message_id"]} for entry in project["entries"][:8]]}
            summary = self.summary(db, chat_id)
            # Project switching does not import the previous project's handoff.
            if summary and summary.get("project") and summary["project"]["id"] != focus:
                summary = None
            compact = None
            if summary:
                last_user = summary['last_user']
                if last_user and (last_user.get('project_id') != focus or last_user.get('context_status') == 'ambiguous'):
                    last_user = None
                compact = {"last_user": last_user, "memories": summary["memories"][:4]}
            return {"project": project, "summary": compact}

    def check_case(self, db, identity, question="", project_id=None):
        row = db.execute("SELECT content,kind FROM memories WHERE id=?", (identity,)).fetchone()
        if not row:
            return
        if not question:
            question = "Что я предпочитаю?" if row["kind"] == "preference" else "Что известно: " + row["content"][:180]
        if project_id:
            name = db.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
            if name:
                question = "Проект " + name[0] + ". " + question
        db.execute("""INSERT INTO memory_checks VALUES(?,?,?,?,?) ON CONFLICT(memory_id)
                   DO UPDATE SET question=excluded.question,project_id=excluded.project_id""",
                   (uuid.uuid4().hex, identity, question[:400], project_id, now()))

    def run_checks(self):
        with self.store.connection() as db:
            closed_filter = ' AND NOT ('+self.store.growth.closed_task_sql+')' if hasattr(self.store,'growth') else ''
            cases = db.execute("""SELECT c.*,m.content,m.origin FROM memory_checks c JOIN memories m ON m.id=c.memory_id
                    WHERE m.status='active' AND m.tier!='archive'"""+closed_filter+' ORDER BY c.created DESC LIMIT 50').fetchall()
            details = []
            for case in cases:
                retrieved = self.store._retrieve(db, case["question"])
                direct = {row["id"] for row in retrieved}
                equivalents = self.store.growth.equivalents(db, case['memory_id']) if hasattr(self.store, 'growth') else {case['memory_id']}
                found = bool(equivalents & direct)
                grounded = case["origin"] == "manual" or bool(db.execute("""SELECT 1 FROM sources s JOIN messages m ON m.id=s.message_id
                           WHERE s.memory_id=? AND instr(m.content,?)>0""", (case["memory_id"], case["content"])).fetchone())
                details.append({"case_id": case["id"], "memory_id": case["memory_id"], "retrieved": found,
                                "grounded": grounded, "passed": found and grounded, 'grouped': found and case['memory_id'] not in direct})
        with self.store.connection(write=True) as db:
            db.execute("INSERT INTO memory_check_runs VALUES(?,?,?,?,?)", (uuid.uuid4().hex, now(), len(details),
                       sum(item["passed"] for item in details), json.dumps(details)))
            db.execute("DELETE FROM memory_check_runs WHERE id NOT IN (SELECT id FROM memory_check_runs ORDER BY created DESC LIMIT 20)")
        self.check_error = None
        return self.checks()

    def safe_checks(self):
        try:
            return self.run_checks()
        except (BrainError, sqlite3.Error, OSError):
            self.check_error = "Локальная проверка временно недоступна. Данные сохранены; проверку можно повторить."
            return None

    def checks(self):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM memory_check_runs ORDER BY created DESC LIMIT 1").fetchone()
            if not row:
                return {"total": 0, "passed": 0, "details": [], "created": None, "error": self.check_error}
            result = dict(row)
            result['error'] = self.check_error
            result["details"] = []
            for detail in json.loads(row["details"]):
                case = db.execute("SELECT question FROM memory_checks WHERE id=?", (detail["case_id"],)).fetchone()
                if case:
                    result["details"].append({**detail, "question": case[0]})
            return result

    def versions(self, identity):
        with self.store.connection() as db:
            series = db.execute("SELECT series_id FROM memory_series WHERE memory_id=?", (identity,)).fetchone()
            if not series:
                return []
            return [{**dict(row), "snapshot": json.loads(row["snapshot"])} for row in db.execute("""SELECT v.* FROM memory_versions v
                       JOIN memory_series s ON s.memory_id=v.memory_id WHERE s.series_id=? ORDER BY v.created DESC,v.rowid DESC""", (series[0],))]

    def restore(self, version_id):
        with self.store.connection(write=True) as db:
            version = db.execute("SELECT * FROM memory_versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                raise BrainError("Версия памяти не найдена.", 404)
            snapshot = json.loads(version["snapshot"])
            identity = version["memory_id"]
            restored_project = (snapshot.get("projects") or [None])[0]
            if snapshot.get("scope") == "project" and not db.execute("SELECT 1 FROM projects WHERE id=?", (restored_project,)).fetchone():
                raise BrainError("Проект этой версии недоступен. Выберите проект при ручном редактировании.", 409)
            digest = self.digest(snapshot["content"], snapshot.get("scope", "global"), restored_project)
            if db.execute("SELECT 1 FROM memories WHERE digest=? AND id!=?", (digest, identity)).fetchone():
                raise BrainError("Этот текст уже сохранён другой записью. Выберите её актуальной вручную.", 409)
            current = db.execute("SELECT digest FROM memories WHERE id=?", (identity,)).fetchone()
            if current[0] != digest:
                db.execute("INSERT OR IGNORE INTO tombstones VALUES(?)", (current[0],))
            self.version(db, identity, "До восстановления версии")
            series = db.execute("SELECT series_id FROM memory_series WHERE memory_id=?", (identity,)).fetchone()[0]
            if snapshot.get("slot"):
                others = db.execute("SELECT id FROM memories WHERE slot=? AND kind=? AND id!=? AND status IN ('active','conflict')",
                                    (snapshot["slot"], snapshot["kind"], identity)).fetchall()
                for other in others:
                    if self.same_scope(db, other[0], snapshot.get("scope", "global"), restored_project):
                        self.version(db, other[0], "До восстановления другого актуального факта")
                        db.execute("UPDATE memories SET status='superseded',updated=? WHERE id=?", (now(), other[0]))
            db.execute("UPDATE memories SET status='superseded' WHERE id IN (SELECT memory_id FROM memory_series WHERE series_id=?)", (series,))
            db.execute("""UPDATE memories SET content=?,digest=?,kind=?,tier=?,slot=?,note='',origin='manual',confirmed=1,
                       status='active',updated=? WHERE id=?""", (snapshot["content"], digest, snapshot["kind"], snapshot["tier"],
                       snapshot.get("slot", ""), now(), identity))
            db.execute("UPDATE memory_scopes SET scope=? WHERE memory_id=?", (snapshot.get("scope", "global"), identity))
            db.execute("DELETE FROM memory_projects WHERE memory_id=?", (identity,))
            for project_id in snapshot.get("projects", []):
                db.execute("INSERT OR IGNORE INTO memory_projects SELECT ?,id FROM projects WHERE id=?", (identity, project_id))
            db.execute("DELETE FROM tombstones WHERE digest=?", (digest,))
            self.store._index(db, identity, snapshot["content"])
            self.version(db, identity, "Восстановлено пользователем")
            self.check_case(db, identity, project_id=(snapshot.get("projects") or [None])[0])
            self.store.growth.memory_changed(db, identity, 'memory_restored')
        self.safe_checks()
        self.store.growth.safe_consolidate(force=True)
        return identity

    def set_project(self, chat_id, project_id):
        with self.store.connection(write=True) as db:
            if not db.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
                raise BrainError("Разговор не найден.", 404)
            if project_id and not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise BrainError("Проект не найден.", 404)
            db.execute("INSERT INTO chat_projects VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET project_id=excluded.project_id,source_created=excluded.source_created",
                       (chat_id, project_id, now()))
            self.refresh_summary(db, chat_id)

    def cleanup(self, db):
        for project in db.execute("SELECT id FROM projects WHERE origin='auto'").fetchall():
            identity = project[0]
            referenced = (db.execute("SELECT 1 FROM project_sources WHERE project_id=?", (identity,)).fetchone()
                          or db.execute("SELECT 1 FROM project_entries WHERE project_id=?", (identity,)).fetchone()
                          or db.execute("SELECT 1 FROM memory_projects WHERE project_id=?", (identity,)).fetchone()
                          or db.execute("SELECT 1 FROM memory_versions WHERE snapshot LIKE ?", ('%' + identity + '%',)).fetchone())
            if not referenced:
                db.execute("DELETE FROM projects WHERE id=?", (identity,))
