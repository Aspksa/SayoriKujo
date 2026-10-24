"""Local transactional chats, memory, provenance and extraction queue."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import time
import uuid

from .provider import BrainError, ENDPOINTS


KINDS = {"fact", "preference", "project", "task"}
TIERS = {"long", "core", "archive"}


def now():
    return datetime.now(timezone.utc).isoformat()


def normalized(text):
    return " ".join(re.findall(r"[\w]+", text.casefold(), flags=re.UNICODE))


def fingerprint(text):
    return hashlib.sha256(normalized(text).encode()).hexdigest()


def terms(text):
    words = re.findall(r"[a-zа-яё0-9]+", text.casefold())
    return {word[:-2] if len(word) > 6 else word for word in words if len(word) >= 3}


DEFAULTS = {"endpoint": ENDPOINTS[0], "model": "", "memory_model": "", "auto_memory": True,
            "revision": 0, "models": []}


class BrainStore:
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "brain.sqlite3"
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, title TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    role TEXT NOT NULL, content TEXT NOT NULL, created TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'done', request_id TEXT UNIQUE, error TEXT,
                    model TEXT, used_memory TEXT NOT NULL DEFAULT '[]');
                CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, created);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, content TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL, tier TEXT NOT NULL, slot TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active', created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources (
                    memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                    message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
                    PRIMARY KEY(memory_id, message_id));
                CREATE TABLE IF NOT EXISTS memory_terms (
                    memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE, term TEXT NOT NULL,
                    PRIMARY KEY(memory_id, term));
                CREATE INDEX IF NOT EXISTS terms_lookup ON memory_terms(term);
                CREATE TABLE IF NOT EXISTS tombstones (digest TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, created TEXT NOT NULL, started REAL, claim TEXT,
                    revision INTEGER NOT NULL, endpoint TEXT NOT NULL, model TEXT NOT NULL,
                    manual INTEGER NOT NULL DEFAULT 0, error TEXT, added INTEGER NOT NULL DEFAULT 0);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES(1, ?)", (json.dumps(DEFAULTS),))

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except sqlite3.OperationalError as error:
            db.rollback()
            raise BrainError("Хранилище памяти недоступно или занято. Повторите действие.", 503) from error
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def config(self, db=None):
        if db is None:
            with self.connection() as connection:
                return self.config(connection)
        return {**DEFAULTS, **json.loads(db.execute("SELECT data FROM settings WHERE id=1").fetchone()[0])}

    def configure(self, changes, secret_action=None):
        with self.connection(write=True) as db:
            config = self.config(db)
            for name in ("endpoint", "model", "memory_model", "auto_memory"):
                if name in changes:
                    config[name] = changes[name]
            if config["endpoint"] not in ENDPOINTS:
                raise BrainError("Выберите адрес API Cloud.ru из списка.")
            if not isinstance(config["auto_memory"], bool):
                raise BrainError("Некорректный режим автоматической памяти.")
            for field in ("model", "memory_model"):
                if not isinstance(config[field], str) or len(config[field]) > 250:
                    raise BrainError("Некорректное название модели.")
                config[field] = config[field].strip()
            if changes.get("endpoint", config["endpoint"]) != self.config(db)["endpoint"] or secret_action is not None:
                config["revision"] += 1
                config["models"] = []
            if secret_action is not None:
                secret_action()
            if not config["auto_memory"]:
                db.execute("UPDATE jobs SET status='cancelled', error=? WHERE status='pending' AND manual=0",
                           ("Автопамять выключена.",))
            db.execute("UPDATE settings SET data=? WHERE id=1", (json.dumps(config, ensure_ascii=False),))

    def cache_models(self, rows, revision):
        with self.connection(write=True) as db:
            config = self.config(db)
            if config["revision"] == revision:
                config["models"] = rows
                db.execute("UPDATE settings SET data=? WHERE id=1", (json.dumps(config, ensure_ascii=False),))

    def create_chat(self):
        identity = uuid.uuid4().hex
        with self.connection(write=True) as db:
            db.execute("INSERT INTO chats VALUES(?,?,?)", (identity, "Новый разговор", now()))
        return identity

    def messages(self, identity):
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM chats WHERE id=?", (identity,)).fetchone():
                raise BrainError("Разговор не найден.", 404)
            result = []
            for row in db.execute("""SELECT m.*, j.id AS job_id, j.status AS job_status,
                       j.error AS job_error, j.added AS job_added FROM messages m
                       LEFT JOIN jobs j ON j.message_id=m.id WHERE m.chat_id=? ORDER BY m.created,m.rowid""", (identity,)):
                record = dict(row)
                record["used_memory"] = json.loads(record["used_memory"])
                job_id = record.pop("job_id")
                status, error, added = (record.pop(field) for field in ("job_status", "job_error", "job_added"))
                record["memory_job"] = {"id": job_id, "status": status, "error": error, "added": added} if job_id else None
                result.append(record)
            return result

    def begin_turn(self, chat_id, text, request_id):
        with self.connection(write=True) as db:
            previous = db.execute("SELECT * FROM messages WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if previous["chat_id"] != chat_id or previous["content"] != text:
                    raise BrainError("Идентификатор запроса уже использован.", 409)
                if previous["status"] == "pending":
                    raise BrainError("Этот запрос уже выполняется.", 409)
                if previous["status"] == "error":
                    raise BrainError(previous["error"] or "Запрос завершился ошибкой.", 502)
                return previous["id"], False
            if not db.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
                raise BrainError("Разговор удалён или не найден.", 404)
            pending = db.execute("SELECT * FROM messages WHERE chat_id=? AND status='pending'", (chat_id,)).fetchall()
            for row in pending:
                if time.time() - datetime.fromisoformat(row["created"]).timestamp() < 120:
                    raise BrainError("Дождитесь ответа в этом разговоре.", 409)
                db.execute("UPDATE messages SET status='error', error=? WHERE id=?",
                           ("Предыдущий запрос был прерван. Отправьте сообщение повторно.", row["id"]))
            identity = uuid.uuid4().hex
            db.execute("INSERT INTO messages(id,chat_id,role,content,created,status,request_id) VALUES(?,?,?,?,?,'pending',?)",
                       (identity, chat_id, "user", text, now(), request_id))
            db.execute("UPDATE chats SET title=? WHERE id=? AND title='Новый разговор'", (text[:65], chat_id))
            return identity, True

    def fail_turn(self, identity, error):
        with self.connection(write=True) as db:
            db.execute("UPDATE messages SET status='error',error=? WHERE id=? AND status='pending'", (error, identity))

    def finish_turn(self, identity, answer, model, memories, config):
        with self.connection(write=True) as db:
            source = db.execute("SELECT * FROM messages WHERE id=?", (identity,)).fetchone()
            if not source:
                raise BrainError("Разговор удалён во время запроса; ответ не сохранён.", 409)
            if source["status"] != "pending":
                raise BrainError("Этот запрос уже завершён или был прерван.", 409)
            db.execute("UPDATE messages SET status='done' WHERE id=?", (identity,))
            db.execute("INSERT INTO messages(id,chat_id,role,content,created,model,used_memory) VALUES(?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, source["chat_id"], "assistant", answer, now(), model, json.dumps(memories)))
            if config["auto_memory"]:
                self._enqueue(db, identity, config, manual=False)

    def _enqueue(self, db, message_id, config, manual):
        row = db.execute("SELECT role FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row or row["role"] != "user":
            raise BrainError("Сообщение пользователя не найдено.", 404)
        existing = db.execute("SELECT status FROM jobs WHERE message_id=?", (message_id,)).fetchone()
        if existing and existing[0] in ("pending", "processing"):
            return
        db.execute("""INSERT INTO jobs(id,message_id,status,created,revision,endpoint,model,manual)
                   VALUES(?,?,'pending',?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET
                   status='pending', created=excluded.created, revision=excluded.revision, endpoint=excluded.endpoint,
                   model=excluded.model, manual=excluded.manual, error=NULL, added=0, claim=NULL, started=NULL""",
                   (uuid.uuid4().hex, message_id, now(), config["revision"], config["endpoint"],
                    config["memory_model"] or config["model"], int(manual)))

    def enqueue(self, message_id):
        with self.connection(write=True) as db:
            self._enqueue(db, message_id, self.config(db), manual=True)

    def claim_job(self):
        with self.connection(write=True) as db:
            db.execute("UPDATE jobs SET status='failed', error=? WHERE status='processing' AND started<?",
                       ("Анализ был прерван. Повторите его вручную, чтобы избежать повторных платных запросов.", time.time() - 150))
            config = self.config(db)
            rows = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created").fetchall()
            for row in rows:
                if row["revision"] != config["revision"] or (not config["auto_memory"] and not row["manual"]):
                    db.execute("UPDATE jobs SET status='cancelled',error=? WHERE id=?",
                               ("Настройки изменились. При необходимости повторите анализ.", row["id"]))
                    continue
                claim = uuid.uuid4().hex
                db.execute("UPDATE jobs SET status='processing',started=?,claim=? WHERE id=?", (time.time(), claim, row["id"]))
                source = db.execute("SELECT content,chat_id FROM messages WHERE id=?", (row["message_id"],)).fetchone()
                return {**dict(row), "claim": claim, "content": source["content"], "chat_id": source["chat_id"]}
        return None

    def fail_job(self, job, error):
        with self.connection(write=True) as db:
            db.execute("UPDATE jobs SET status='failed',error=? WHERE id=? AND claim=? AND status='processing'",
                       (error, job["id"], job["claim"]))

    def _index(self, db, identity, content):
        db.execute("DELETE FROM memory_terms WHERE memory_id=?", (identity,))
        db.executemany("INSERT INTO memory_terms VALUES(?,?)", ((identity, term) for term in terms(content)))

    def finish_job(self, job, items):
        added = 0
        with self.connection(write=True) as db:
            current = db.execute("SELECT * FROM jobs WHERE id=? AND claim=? AND status='processing'",
                                 (job["id"], job["claim"])).fetchone()
            if not current:
                return 0
            config = self.config(db)
            if config["revision"] != job["revision"] or (not config["auto_memory"] and not job["manual"]):
                db.execute("UPDATE jobs SET status='cancelled',error=? WHERE id=?", ("Настройки изменились.", job["id"]))
                return 0
            for item in items:
                quote = item["quote"]
                digest = fingerprint(quote)
                if db.execute("SELECT 1 FROM tombstones WHERE digest=?", (digest,)).fetchone():
                    continue
                prior = db.execute("SELECT id FROM memories WHERE digest=?", (digest,)).fetchone()
                if prior:
                    identity = prior[0]
                else:
                    identity = uuid.uuid4().hex
                    slot = item.get("slot", "")
                    conflict = bool(slot and db.execute(
                        "SELECT 1 FROM memories WHERE slot=? AND kind=? AND status IN ('active','conflict')",
                        (slot, item["kind"])).fetchone())
                    if conflict:
                        db.execute("UPDATE memories SET status='conflict',updated=? WHERE slot=? AND kind=? AND origin='auto' AND status IN ('active','conflict')",
                                   (now(), slot, item["kind"]))
                    db.execute("""INSERT INTO memories(id,content,digest,kind,tier,slot,note,origin,status,created,updated)
                               VALUES(?,?,?,?,'long',?,?,'auto',?,?,?)""",
                               (identity, quote, digest, item["kind"], slot, item.get("note", ""),
                                "conflict" if conflict else "active", now(), now()))
                    self._index(db, identity, quote)
                    added += 1
                db.execute("INSERT OR IGNORE INTO sources VALUES(?,?)", (identity, job["message_id"]))
            db.execute("UPDATE jobs SET status='done',added=?,error=NULL WHERE id=?", (added, job["id"]))
        return added

    def memories(self, query="", message_id=None, identities=None):
        with self.connection() as db:
            clauses, parameters = [], []
            if message_id is not None:
                clauses.append("EXISTS (SELECT 1 FROM sources s WHERE s.memory_id=m.id AND s.message_id=?)")
                parameters.append(message_id)
            if identities is not None:
                if not identities:
                    return []
                clauses.append("m.id IN (" + ",".join("?" for _ in identities) + ")")
                parameters.extend(identities)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = db.execute("SELECT m.* FROM memories m" + where + " ORDER BY m.updated DESC", parameters).fetchall()
            result = []
            for row in rows:
                if query and normalized(query) not in normalized(row["content"] + " " + row["note"]):
                    continue
                record = dict(row)
                record["sources"] = [dict(source) for source in db.execute(
                    "SELECT m.id,m.chat_id,m.content,m.created FROM sources s JOIN messages m ON m.id=s.message_id WHERE s.memory_id=?",
                    (row["id"],))]
                result.append(record)
            return result

    def retrieve(self, query):
        with self.connection() as db:
            core = db.execute("SELECT id,content,origin FROM memories WHERE tier='core' AND status='active' ORDER BY updated DESC LIMIT 12").fetchall()
            tokens = list(terms(query))[:64]
            matching = []
            if tokens:
                placeholders = ",".join("?" for _ in tokens)
                matching = db.execute(f"""SELECT m.id,m.content,m.origin,COUNT(*) AS rank FROM memories m
                    JOIN memory_terms t ON t.memory_id=m.id WHERE t.term IN ({placeholders})
                    AND m.tier='long' AND m.status='active' GROUP BY m.id ORDER BY rank DESC,m.updated DESC LIMIT 8""", tokens).fetchall()
            return [{"id": row["id"], "content": row["content"], "origin": row["origin"]} for row in [*core, *matching]]

    def edit_memory(self, payload):
        identity = payload.get("id") or uuid.uuid4().hex
        content = payload.get("content", "")
        kind, tier = payload.get("kind", "fact"), payload.get("tier", "long")
        if not isinstance(content, str) or not 3 <= len(content.strip()) <= 1500 or kind not in KINDS or tier not in TIERS:
            raise BrainError("Проверьте текст, вид и уровень памяти.")
        content = content.strip()
        digest = fingerprint(content)
        with self.connection(write=True) as db:
            old = db.execute("SELECT * FROM memories WHERE id=?", (identity,)).fetchone()
            duplicate = db.execute("SELECT id FROM memories WHERE digest=? AND id!=?", (digest, identity)).fetchone()
            if duplicate:
                raise BrainError("Такая запись уже есть в памяти.", 409)
            if old:
                if old["digest"] != digest:
                    db.execute("INSERT OR IGNORE INTO tombstones VALUES(?)", (old["digest"],))
                db.execute("UPDATE memories SET content=?,digest=?,kind=?,tier=?,note='',origin='manual',confirmed=1,status='active',updated=? WHERE id=?",
                           (content, digest, kind, tier, now(), identity))
                if old["slot"]:
                    db.execute("UPDATE memories SET status='superseded' WHERE slot=? AND kind=? AND id!=?",
                               (old["slot"], kind, identity))
            else:
                if payload.get("id"):
                    raise BrainError("Запись памяти не найдена.", 404)
                db.execute("INSERT INTO memories(id,content,digest,kind,tier,origin,confirmed,created,updated) VALUES(?,?,?,?,?,'manual',1,?,?)",
                           (identity, content, digest, kind, tier, now(), now()))
            db.execute("DELETE FROM tombstones WHERE digest=?", (digest,))
            self._index(db, identity, content)
        return identity

    def delete_memory(self, identity):
        with self.connection(write=True) as db:
            row = db.execute("SELECT digest FROM memories WHERE id=?", (identity,)).fetchone()
            if row:
                db.execute("INSERT OR IGNORE INTO tombstones VALUES(?)", (row[0],))
                db.execute("DELETE FROM memories WHERE id=?", (identity,))

    def delete_chat(self, identity):
        with self.connection(write=True) as db:
            # Delete its facts too if they have no other source and were not confirmed manually.
            ids = [row[0] for row in db.execute("SELECT DISTINCT s.memory_id FROM sources s JOIN messages m ON m.id=s.message_id WHERE m.chat_id=?", (identity,))]
            db.execute("DELETE FROM chats WHERE id=?", (identity,))
            for memory_id in ids:
                db.execute("DELETE FROM memories WHERE id=? AND origin='auto' AND NOT EXISTS (SELECT 1 FROM sources WHERE memory_id=?)",
                           (memory_id, memory_id))

    def overview(self):
        with self.connection() as db:
            count = lambda table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            analysis = dict.fromkeys(("pending", "processing", "done", "failed", "cancelled"), 0)
            analysis.update({row[0]: row[1] for row in db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status")})
            return {"settings": self.config(db),
                    "analysis": analysis,
                    "chats": [dict(row) for row in db.execute("SELECT * FROM chats ORDER BY created DESC")],
                    "jobs": [dict(row) for row in db.execute("SELECT id,message_id,status,created,error,added FROM jobs ORDER BY created DESC LIMIT 50")],
                    "growth": {"memories": count("memories"), "sources": count("sources"),
                               "messages": count("messages"), "terms": count("memory_terms"),
                               "processed": db.execute("SELECT COUNT(*) FROM jobs WHERE status='done'").fetchone()[0]}}
