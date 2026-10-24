"""Source-grounded development branches, live memory graph and local consolidation.

No model calls, executable generated code, fabricated scores or implicit data deletion.
"""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
import sqlite3
import uuid

from .provider import BrainError
from .store import now, normalized, terms


ENTRY_KINDS = {"question", "option", "choice", "lesson", "hypothesis"}
KINDS_RU = {"brain": "Общая память", "project": "Проект", "branch": "Ветка развития",
            "memory": "Воспоминание", "message": "Сообщение", "progress": "Прогресс",
            "question": "Открытый вопрос", "option": "Вариант решения", "choice": "Выбор пользователя",
            "lesson": "Сообщённый опыт", "hypothesis": "Предположение"}
EVENTS_RU = {"branch_created": "Создана ветка развития", "entry_added": "Добавлена запись развития",
             "progress_added": "Обновлён прогресс", "memory_added": "Сохранено воспоминание",
             "memory_imported": "Воспоминание добавлено в карту при обновлении", "memory_changed": "Изменено воспоминание",
             "memory_restored": "Восстановлена версия памяти", "grouped": "Повторы объединены в группу",
             "ungrouped": "Объединение отменено", "archived": "Запись перенесена в архив",
             "branch_changed": "Изменено состояние ветки", "question_changed": "Изменено состояние вопроса"}
CHOICE = re.compile(r"\b(?:выбира\w*|выбра\w*|решили|решил|подходит|берём|берем|оставляем|остановимся|ответ\w*)\b", re.I)
UUID = re.compile(r"[a-f0-9]{32}\Z")
CLOSED_TASK_SQL = """m.kind='task' AND EXISTS (
    SELECT 1 FROM project_entries task JOIN sources source ON source.message_id=task.message_id AND source.memory_id=m.id
    JOIN project_entries done ON done.id=task.resolved_by
    WHERE task.kind='todo' AND task.status='resolved' AND task.content=m.content
    AND EXISTS (SELECT 1 FROM memory_projects p WHERE p.memory_id=m.id AND p.project_id=task.project_id)
    AND (m.origin='auto' OR m.updated<=done.created)
    AND done.created>=(SELECT MAX(msg.created) FROM sources s JOIN messages msg ON msg.id=s.message_id WHERE s.memory_id=m.id)
    AND NOT EXISTS (SELECT 1 FROM project_entries newer JOIN sources s ON s.message_id=newer.message_id AND s.memory_id=m.id
        WHERE newer.kind='todo' AND newer.content=m.content AND newer.project_id=task.project_id AND newer.created>task.created))"""


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def identity(value, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not UUID.fullmatch(value):
        raise BrainError("Некорректный идентификатор.")
    return value


class Growth:
    closed_task_sql = CLOSED_TASK_SQL
    def __init__(self, store, backup=False):
        self.store = store
        self.error = None
        self.migrate(backup)

    def migrate(self, backup):
        with self.store.connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] >= 3:
                return
        if backup:
            folder = self.store.root / 'backups'
            folder.mkdir(exist_ok=True)
            with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(
                    folder / ('before-v3-' + uuid.uuid4().hex + '.sqlite3'))) as target:
                source.backup(target)
        statements = [
            """CREATE TABLE IF NOT EXISTS development_branches(id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,scope_key TEXT NOT NULL,
                name TEXT NOT NULL,normalized_name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',
                created TEXT NOT NULL,updated TEXT NOT NULL,UNIQUE(scope_key,normalized_name))""",
            """CREATE TABLE IF NOT EXISTS branch_sources(branch_id TEXT REFERENCES development_branches(id) ON DELETE CASCADE,
                message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,PRIMARY KEY(branch_id,message_id))""",
            """CREATE TABLE IF NOT EXISTS development_entries(id TEXT PRIMARY KEY,
                branch_id TEXT NOT NULL REFERENCES development_branches(id) ON DELETE CASCADE,
                message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,kind TEXT NOT NULL,
                content TEXT NOT NULL,digest TEXT NOT NULL,created TEXT NOT NULL,manual_state TEXT,updated TEXT NOT NULL,
                UNIQUE(branch_id,message_id,kind,digest))""",
            """CREATE TABLE IF NOT EXISTS development_relations(entry_id TEXT REFERENCES development_entries(id) ON DELETE CASCADE,
                target_id TEXT REFERENCES development_entries(id) ON DELETE CASCADE,kind TEXT NOT NULL,
                PRIMARY KEY(entry_id,target_id,kind))""",
            """CREATE TABLE IF NOT EXISTS branch_progress(branch_id TEXT REFERENCES development_branches(id) ON DELETE CASCADE,
                progress_id TEXT REFERENCES project_entries(id) ON DELETE CASCADE,PRIMARY KEY(branch_id,progress_id))""",
            "CREATE TABLE IF NOT EXISTS development_tombstones(digest TEXT PRIMARY KEY)",
            """CREATE TABLE IF NOT EXISTS reply_traces(reply_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,data TEXT NOT NULL,created TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS memory_usage(memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
                count INTEGER NOT NULL,last_used TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS growth_events(id TEXT PRIMARY KEY,event_key TEXT UNIQUE,kind TEXT NOT NULL,created TEXT NOT NULL,
                memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                other_memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                branch_id TEXT REFERENCES development_branches(id) ON DELETE CASCADE,
                entry_id TEXT REFERENCES development_entries(id) ON DELETE CASCADE,
                progress_id TEXT REFERENCES project_entries(id) ON DELETE CASCADE,
                message_id TEXT REFERENCES messages(id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS memory_links(first_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                second_id TEXT REFERENCES memories(id) ON DELETE CASCADE,created TEXT NOT NULL,PRIMARY KEY(first_id,second_id))""",
            """CREATE TABLE IF NOT EXISTS consolidation_suggestions(id TEXT PRIMARY KEY,kind TEXT NOT NULL,
                first_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                second_id TEXT REFERENCES memories(id) ON DELETE CASCADE,basis TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',created TEXT NOT NULL,updated TEXT NOT NULL,UNIQUE(kind,first_id,second_id,basis))""",
            "CREATE TABLE IF NOT EXISTS consolidation_state(id INTEGER PRIMARY KEY CHECK(id=1),last_run TEXT,examined INTEGER NOT NULL DEFAULT 0)",
            "CREATE INDEX IF NOT EXISTS development_branch_entries ON development_entries(branch_id,created)",
            "CREATE INDEX IF NOT EXISTS development_relation_target ON development_relations(target_id)",
            "CREATE INDEX IF NOT EXISTS project_entry_source ON project_entries(message_id,kind)",
            "CREATE INDEX IF NOT EXISTS growth_event_time ON growth_events(created)",
        ]
        with self.store.connection(write=True) as db:
            if db.execute('PRAGMA user_version').fetchone()[0] >= 3:
                return
            for statement in statements:
                db.execute(statement)
            db.execute('INSERT OR IGNORE INTO consolidation_state(id) VALUES(1)')
            for memory in db.execute('SELECT id FROM memories').fetchall():
                self.event(db, 'memory_imported', 'import:' + memory[0], memory_id=memory[0])
            # Legacy usage is counted so old, previously used memories are not treated as unused.
            for reply in db.execute("SELECT id,chat_id,created,used_memory FROM messages WHERE role='assistant' AND status='done'").fetchall():
                user = db.execute("SELECT id FROM messages WHERE chat_id=? AND role='user' AND status='done' AND created<=? ORDER BY created DESC,rowid DESC LIMIT 1",
                                  (reply['chat_id'], reply['created'])).fetchone()
                if user:
                    db.execute('INSERT OR IGNORE INTO reply_traces VALUES(?,?,?,?)', (reply['id'],user[0],json.dumps({'legacy':True,'memory_ids':json.loads(reply['used_memory'])}),reply['created']))
                for memory_id in set(json.loads(reply['used_memory'])):
                    if db.execute('SELECT 1 FROM memories WHERE id=?', (memory_id,)).fetchone():
                        self.use(db, memory_id, reply['created'])
            for project in db.execute('SELECT id FROM projects').fetchall():
                progress = db.execute('SELECT * FROM project_entries WHERE project_id=? ORDER BY created', (project[0],)).fetchall()
                if progress:
                    branch = self.branch(db, project[0], 'Развитие проекта', progress[0]['message_id'])
                    for entry in progress:
                        db.execute('INSERT OR IGNORE INTO branch_progress VALUES(?,?)', (branch, entry['id']))
                        self.event(db, 'progress_added', 'progress:' + entry['id'], progress_id=entry['id'], message_id=entry['message_id'])
            db.execute('PRAGMA user_version=3')

    def event(self, db, kind, key=None, **refs):
        fields = ('memory_id', 'other_memory_id', 'branch_id', 'entry_id', 'progress_id', 'message_id')
        db.execute('INSERT OR IGNORE INTO growth_events VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, key, kind, now(), *(refs.get(field) for field in fields)))
        db.execute('DELETE FROM growth_events WHERE id IN (SELECT id FROM growth_events ORDER BY created DESC,rowid DESC LIMIT -1 OFFSET 3000)')

    def branch(self, db, project_id, name, message_id):
        scope = project_id or 'global'
        prior = db.execute('SELECT id FROM development_branches WHERE scope_key=? AND normalized_name=?', (scope, normalized(name))).fetchone()
        if prior:
            result = prior[0]
        else:
            result = uuid.uuid4().hex
            db.execute('INSERT INTO development_branches(id,project_id,scope_key,name,normalized_name,created,updated) VALUES(?,?,?,?,?,?,?)',
                       (result, project_id, scope, name, normalized(name), now(), now()))
            self.event(db, 'branch_created', 'branch:' + result, branch_id=result, message_id=message_id)
        db.execute('INSERT OR IGNORE INTO branch_sources VALUES(?,?)', (result, message_id))
        return result

    def branch_for(self, db, chat_id, project_id, text='', before=None):
        candidates = db.execute("SELECT * FROM development_branches WHERE scope_key=? AND status='active' ORDER BY updated DESC", (project_id or 'global',)).fetchall()
        matches = [row for row in candidates if ' ' + row['normalized_name'] + ' ' in ' ' + normalized(text) + ' ']
        if len(matches) == 1:
            return dict(matches[0])
        if len(matches) > 1:
            return None
        # Inherit only an already established branch at this message's time.
        prior = db.execute("""SELECT b.* FROM branch_sources s JOIN development_branches b ON b.id=s.branch_id
            JOIN messages m ON m.id=s.message_id WHERE m.chat_id=? AND b.scope_key=? AND b.status='active'
            AND m.created<=? ORDER BY m.created DESC,m.rowid DESC LIMIT 1""", (chat_id, project_id or 'global', before or now())).fetchone()
        return dict(prior) if prior else None

    def sync_analysis(self, db, job, analysis):
        context = db.execute('SELECT * FROM analysis_contexts WHERE message_id=?', (job['message_id'],)).fetchone()
        for row in db.execute('SELECT memory_id FROM sources WHERE message_id=?', (job['message_id'],)).fetchall():
            self.event(db, 'memory_added', 'memory:' + row[0], memory_id=row[0], message_id=job['message_id'])
        if not context or context['status'] == 'ambiguous':
            return
        project_id = context['project_id']
        development = analysis.get('development') or {}
        proposal, entries = development.get('branch'), development.get('entries', [])
        progress = db.execute('SELECT * FROM project_entries WHERE message_id=?', (job['message_id'],)).fetchall()
        source_time = db.execute('SELECT created FROM messages WHERE id=?', (job['message_id'],)).fetchone()[0]
        branch_id = None
        if proposal:
            branch_id = self.branch(db, project_id, proposal['name'], job['message_id'])
        elif entries or progress:
            current = self.branch_for(db, job['chat_id'], project_id, job['content'], source_time)
            branch_id = current['id'] if current else self.branch(db, project_id, 'Развитие проекта' if project_id else 'Общие направления', job['message_id'])
        if not branch_id:
            return
        db.execute('INSERT OR IGNORE INTO branch_sources VALUES(?,?)', (branch_id, job['message_id']))
        db.execute('UPDATE development_branches SET updated=MAX(updated,?) WHERE id=?', (source_time, branch_id))
        for item in entries:
            digest = text_hash(branch_id + '\0' + item['kind'] + '\0' + normalized(item['quote']))
            if db.execute('SELECT 1 FROM development_tombstones WHERE digest=?', (digest,)).fetchone():
                continue
            entry_id = uuid.uuid4().hex
            cursor = db.execute('INSERT OR IGNORE INTO development_entries(id,branch_id,message_id,kind,content,digest,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                               (entry_id, branch_id, job['message_id'], item['kind'], item['quote'], digest, source_time, now()))
            if not cursor.rowcount:
                continue
            self.event(db, 'entry_added', 'entry:' + entry_id, entry_id=entry_id, message_id=job['message_id'])
            # A model cannot close another branch's question or choose a future option.
            if item['kind'] == 'choice' and CHOICE.search(item['quote']):
                for target_id in item.get('targets', []):
                    target = db.execute('SELECT * FROM development_entries WHERE id=? AND branch_id=? AND created<=?', (target_id, branch_id, source_time)).fetchone()
                    if target and target['kind'] in ('question', 'option'):
                        relation = 'answers' if target['kind'] == 'question' else 'selects'
                        db.execute('INSERT OR IGNORE INTO development_relations VALUES(?,?,?)', (entry_id, target_id, relation))
                        if relation == 'answers':
                            others = db.execute('''SELECT e.id,e.created FROM development_relations r JOIN development_entries e ON e.id=r.entry_id
                                WHERE r.target_id=? AND r.kind='answers' AND e.id!=?''', (target_id, entry_id)).fetchall()
                            for other in others:
                                newer, older = (entry_id,other['id']) if source_time >= other['created'] else (other['id'],entry_id)
                                db.execute("INSERT OR IGNORE INTO development_relations VALUES(?,?,'supersedes')", (newer,older))
        for entry in progress:
            db.execute('INSERT OR IGNORE INTO branch_progress VALUES(?,?)', (branch_id, entry['id']))
            self.event(db, 'progress_added', 'progress:' + entry['id'], progress_id=entry['id'], message_id=job['message_id'])

    def entry_state(self, db, entry):
        if entry['manual_state']:
            return entry['manual_state']
        incoming = db.execute('''SELECT r.kind FROM development_relations r JOIN development_entries e ON e.id=r.entry_id
            WHERE r.target_id=? AND NOT EXISTS (SELECT 1 FROM development_relations newer WHERE newer.target_id=e.id AND newer.kind='supersedes')
            ORDER BY e.created DESC,e.rowid DESC LIMIT 1''', (entry['id'],)).fetchone()
        if incoming:
            return 'resolved' if incoming[0] == 'answers' else 'superseded' if incoming[0] == 'supersedes' else 'selected'
        return 'open' if entry['kind'] in ('question', 'hypothesis') else 'proposed' if entry['kind'] == 'option' else 'reported'

    def analysis_context(self, chat_id, text):
        with self.store.connection() as db:
            project_id = self.store.learning.focus(db, chat_id, text)
            branch = self.branch_for(db, chat_id, project_id, text)
            if not branch:
                return None
            entries = db.execute('SELECT * FROM development_entries WHERE branch_id=? ORDER BY created DESC,rowid DESC LIMIT 10', (branch['id'],)).fetchall()
            return {'id': branch['id'], 'name': branch['name'], 'entries': [
                {'id': entry['id'], 'kind': entry['kind'], 'quote': entry['content'], 'state': self.entry_state(db, entry)} for entry in entries]}

    def memory_changed(self, db, memory_id, kind='memory_changed'):
        db.execute('DELETE FROM memory_links WHERE first_id=? OR second_id=?', (memory_id, memory_id))
        self.event(db, kind, memory_id=memory_id)

    def task_completed(self, db, memory_id):
        return bool(db.execute('SELECT 1 FROM memories m WHERE m.id=? AND ('+CLOSED_TASK_SQL+')', (memory_id,)).fetchone())

    def use(self, db, memory_id, created):
        db.execute('INSERT INTO memory_usage VALUES(?,1,?) ON CONFLICT(memory_id) DO UPDATE SET count=count+1,last_used=MAX(last_used,excluded.last_used)', (memory_id, created))

    def record_turn(self, db, reply_id, user_id, trace):
        safe = trace or {}
        db.execute('INSERT INTO reply_traces VALUES(?,?,?,?)', (reply_id, user_id, json.dumps(safe), now()))
        ids = set(safe.get('memory_ids', [])) | set(safe.get('summary_memory_ids', []))
        for memory_id in ids:
            if db.execute('SELECT 1 FROM memories WHERE id=?', (memory_id,)).fetchone():
                self.use(db, memory_id, now())

    def collapse(self, db, records):
        parents = {}
        def find(value):
            parents.setdefault(value, value)
            while parents[value] != value:
                parents[value] = parents[parents[value]]
                value = parents[value]
            return value
        for link in db.execute("""SELECT l.first_id,l.second_id FROM memory_links l JOIN memories a ON a.id=l.first_id
                JOIN memories b ON b.id=l.second_id WHERE a.status='active' AND b.status='active' AND a.tier!='archive' AND b.tier!='archive'"""):
            a, b = find(link[0]), find(link[1])
            parents[b] = a
        result, seen = [], set()
        for row in records:
            key = find(row['id'])
            if key not in seen:
                result.append(row)
                seen.add(key)
        return result

    def equivalents(self, db, memory_id):
        seen, pending = {memory_id}, [memory_id]
        while pending:
            current = pending.pop()
            for row in db.execute('SELECT first_id,second_id FROM memory_links WHERE first_id=? OR second_id=?', (current, current)):
                other = row[1] if row[0] == current else row[0]
                if other not in seen:
                    seen.add(other)
                    pending.append(other)
        return seen

    def basis(self, db, first_id, second_id=None, archive=False):
        values = []
        for memory_id in (first_id, second_id):
            if not memory_id:
                continue
            row = db.execute('SELECT id,digest,kind,tier,slot,origin,status,updated FROM memories WHERE id=?', (memory_id,)).fetchone()
            if not row:
                return None
            snapshot = dict(row)
            snapshot['scope'] = self.scope(db, memory_id)
            if archive:
                usage = db.execute('SELECT * FROM memory_usage WHERE memory_id=?', (memory_id,)).fetchone()
                snapshot['usage'] = dict(usage) if usage else None
            values.append(snapshot)
        return text_hash(json.dumps(values, sort_keys=True))

    def scope(self, db, memory_id):
        row = db.execute('SELECT scope FROM memory_scopes WHERE memory_id=?', (memory_id,)).fetchone()
        if not row or row[0] == 'global':
            return 'global'
        project = db.execute('SELECT project_id FROM memory_projects WHERE memory_id=?', (memory_id,)).fetchone()
        return project[0] if project else 'unassigned:' + memory_id

    def consolidate(self, manual=False, force=False):
        with self.store.connection(write=True) as db:
            config = self.store.config(db)
            last = db.execute('SELECT last_run FROM consolidation_state WHERE id=1').fetchone()[0]
            if not manual and (not config['auto_consolidation'] or (not force and last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 1800)):
                return False
            rows = db.execute("SELECT * FROM memories WHERE tier!='archive' AND status IN ('active','conflict') ORDER BY updated DESC LIMIT 200").fetchall()
            scoped = {row['id']: self.scope(db, row['id']) for row in rows}
            tokenized = {row['id']: terms(row['content']) for row in rows}
            proposed = set()
            per_kind = {'similar': 0, 'conflict': 0, 'archive': 0}
            def suggest(kind, a, b=None):
                first, second = sorted((a['id'], b['id'])) if b else (a['id'], None)
                basis = self.basis(db, first, second, archive=kind == 'archive')
                existing = db.execute('SELECT id,status FROM consolidation_suggestions WHERE kind=? AND first_id=? AND second_id IS ? AND basis=?', (kind, first, second, basis)).fetchone()
                if existing and existing['status'] in ('dismissed', 'applied'):
                    return
                if per_kind[kind] >= {'similar':60,'conflict':30,'archive':20}[kind]:
                    return
                per_kind[kind] += 1
                if not existing:
                    db.execute('INSERT INTO consolidation_suggestions(id,kind,first_id,second_id,basis,created,updated) VALUES(?,?,?,?,?,?,?)',
                               (uuid.uuid4().hex, kind, first, second, basis, now(), now()))
                elif existing['status'] == 'stale':
                    db.execute("UPDATE consolidation_suggestions SET status='pending',updated=? WHERE id=?", (now(),existing['id']))
                proposed.add((kind, first, second, basis))
            for i, first in enumerate(rows):
                for second in rows[i+1:]:
                    if first['kind'] != second['kind'] or scoped[first['id']] != scoped[second['id']]:
                        continue
                    if first['slot'] and first['slot'] == second['slot'] and (first['status'] == 'conflict' or second['status'] == 'conflict'):
                        suggest('conflict', first, second)
                    elif first['status'] == second['status'] == 'active':
                        a, b = tokenized[first['id']], tokenized[second['id']]
                        if len(a & b) >= 3 and len(a & b) / max(1, len(a | b)) >= .6:
                            pair = sorted((first['id'], second['id']))
                            if not db.execute('SELECT 1 FROM memory_links WHERE first_id=? AND second_id=?', pair).fetchone():
                                suggest('similar', first, second)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
            old = db.execute("""SELECT m.* FROM memories m LEFT JOIN memory_usage u ON u.memory_id=m.id
                WHERE m.status='active' AND m.tier='long' AND m.origin='auto' AND m.confirmed=0
                AND m.updated<? AND COALESCE(u.last_used,m.created)<? ORDER BY m.updated LIMIT 100""", (cutoff, cutoff)).fetchall()
            for row in old:
                suggest('archive', row)
            for suggestion in db.execute("SELECT * FROM consolidation_suggestions WHERE status='pending'").fetchall():
                if (suggestion['kind'], suggestion['first_id'], suggestion['second_id'], suggestion['basis']) not in proposed:
                    db.execute("UPDATE consolidation_suggestions SET status='stale',updated=? WHERE id=?", (now(), suggestion['id']))
            db.execute('UPDATE consolidation_state SET last_run=?,examined=? WHERE id=1', (now(), len(rows) + len(old)))
            db.execute("DELETE FROM consolidation_suggestions WHERE status!='pending' AND id IN (SELECT id FROM consolidation_suggestions WHERE status!='pending' ORDER BY updated DESC LIMIT -1 OFFSET 500)")
        self.error = None
        return True

    def safe_consolidate(self, force=False):
        try:
            return self.consolidate(force=force)
        except (BrainError, sqlite3.Error, OSError, ValueError):
            self.error = 'Локальная консолидация временно недоступна. Память сохранена; проверку можно повторить.'
            return False

    def suggestions(self):
        with self.store.connection() as db:
            result = []
            for row in db.execute("SELECT * FROM consolidation_suggestions WHERE status IN ('pending','applied') ORDER BY updated DESC LIMIT 100"):
                if row['status'] == 'pending' and row['basis'] != self.basis(db, row['first_id'], row['second_id'], row['kind'] == 'archive'):
                    continue
                item = dict(row)
                item['first'] = dict(db.execute('SELECT id,content,kind,tier,status FROM memories WHERE id=?', (row['first_id'],)).fetchone())
                item['second'] = dict(db.execute('SELECT id,content,kind,tier,status FROM memories WHERE id=?', (row['second_id'],)).fetchone()) if row['second_id'] else None
                if row['status'] == 'applied' and row['kind'] == 'similar' and not db.execute('SELECT 1 FROM memory_links WHERE first_id=? AND second_id=?', (row['first_id'], row['second_id'])).fetchone():
                    continue
                result.append(item)
            return result

    def suggestion_action(self, suggestion_id, action):
        identity(suggestion_id)
        if action not in ('dismiss', 'group', 'undo', 'archive'):
            raise BrainError('Неизвестное действие.')
        with self.store.connection(write=True) as db:
            row = db.execute('SELECT * FROM consolidation_suggestions WHERE id=?', (suggestion_id,)).fetchone()
            if not row:
                raise BrainError('Предложение больше не доступно.', 404)
            if action == 'undo':
                if row['kind'] != 'similar' or row['status'] != 'applied':
                    raise BrainError('Это объединение уже отменено.', 409)
                db.execute('DELETE FROM memory_links WHERE first_id=? AND second_id=?', (row['first_id'], row['second_id']))
                db.execute("UPDATE consolidation_suggestions SET status='dismissed',updated=? WHERE id=?", (now(), suggestion_id))
                self.event(db, 'ungrouped', memory_id=row['first_id'], other_memory_id=row['second_id'])
            else:
                if row['status'] != 'pending' or row['basis'] != self.basis(db, row['first_id'], row['second_id'], row['kind'] == 'archive'):
                    raise BrainError('Записи изменились. Обновите предложения перед действием.', 409)
                if action == 'dismiss':
                    db.execute("UPDATE consolidation_suggestions SET status='dismissed',updated=? WHERE id=?", (now(), suggestion_id))
                elif action == 'group' and row['kind'] == 'similar':
                    db.execute('INSERT OR IGNORE INTO memory_links VALUES(?,?,?)', (row['first_id'], row['second_id'], now()))
                    db.execute("UPDATE consolidation_suggestions SET status='applied',updated=? WHERE id=?", (now(), suggestion_id))
                    self.event(db, 'grouped', memory_id=row['first_id'], other_memory_id=row['second_id'])
                elif action == 'archive' and row['kind'] == 'archive':
                    self.store.learning.version(db, row['first_id'], 'До переноса в архив из сна памяти')
                    db.execute("UPDATE memories SET tier='archive',updated=? WHERE id=?", (now(), row['first_id']))
                    self.store.learning.version(db, row['first_id'], 'Перенесено в архив пользователем')
                    db.execute("UPDATE consolidation_suggestions SET status='applied',updated=? WHERE id=?", (now(), suggestion_id))
                    self.event(db, 'archived', memory_id=row['first_id'])
                else:
                    raise BrainError('Действие не подходит этому предложению.')
        self.store.learning.safe_checks()

    def entry_action(self, entry_id, action):
        identity(entry_id)
        with self.store.connection(write=True) as db:
            entry = db.execute('SELECT * FROM development_entries WHERE id=?', (entry_id,)).fetchone()
            if not entry:
                raise BrainError('Запись развития не найдена.', 404)
            if action == 'delete':
                db.execute('INSERT OR IGNORE INTO development_tombstones VALUES(?)', (entry['digest'],))
                db.execute('DELETE FROM development_entries WHERE id=?', (entry_id,))
            elif action in ('resolve', 'reopen') and entry['kind'] == 'question':
                db.execute('UPDATE development_entries SET manual_state=?,updated=? WHERE id=?', ('resolved' if action == 'resolve' else 'open', now(), entry_id))
                self.event(db, 'question_changed', entry_id=entry_id)
            else:
                raise BrainError('Действие недоступно для этой записи.')
            self.cleanup(db)

    def branch_action(self, branch_id, status):
        identity(branch_id)
        if status not in ('active', 'paused'):
            raise BrainError('Неизвестное состояние ветки.')
        with self.store.connection(write=True) as db:
            if not db.execute('SELECT 1 FROM development_branches WHERE id=?', (branch_id,)).fetchone():
                raise BrainError('Ветка не найдена.', 404)
            db.execute('UPDATE development_branches SET status=?,updated=? WHERE id=?', (status, now(), branch_id))
            self.event(db, 'branch_changed', branch_id=branch_id)

    def cleanup(self, db):
        db.execute('''DELETE FROM development_branches WHERE NOT EXISTS (SELECT 1 FROM branch_sources s WHERE s.branch_id=development_branches.id)
            AND NOT EXISTS (SELECT 1 FROM development_entries e WHERE e.branch_id=development_branches.id)
            AND NOT EXISTS (SELECT 1 FROM branch_progress p WHERE p.branch_id=development_branches.id)''')

    def compact_state(self):
        with self.store.connection() as db:
            row = dict(db.execute('SELECT last_run,examined FROM consolidation_state WHERE id=1').fetchone())
            row['error'] = self.error
            row['revision'] = '|'.join(str(value) for query in (
                'SELECT COUNT(*),MAX(created) FROM growth_events', 'SELECT COUNT(*),MAX(updated) FROM memories',
                'SELECT COUNT(*),MAX(updated) FROM development_entries', 'SELECT COUNT(*),MAX(updated) FROM development_branches',
                'SELECT COUNT(*),MAX(updated) FROM consolidation_suggestions', 'SELECT COUNT(*) FROM reply_traces',
                'SELECT COUNT(*) FROM messages', 'SELECT last_run FROM consolidation_state') for value in db.execute(query).fetchone())
            return row

    def source(self, db, message_id):
        row = db.execute('SELECT id,chat_id,role,content,created,status FROM messages WHERE id=?', (message_id,)).fetchone()
        return dict(row) if row else None

    def describe(self, db, key, source_limit=2):
        if key == 'brain':
            return {'id': key, 'type': 'brain', 'type_label': 'Общая память', 'title': 'Общая память', 'content': 'Проекты, направления, записи и подтверждающие источники.', 'status': 'local', 'sources': []}
        if not isinstance(key, str) or ':' not in key:
            raise BrainError('Некорректный узел карты.')
        kind, entity_id = key.split(':', 1)
        identity(entity_id)
        table = {'project': 'projects', 'branch': 'development_branches', 'memory': 'memories', 'message': 'messages',
                 'entry': 'development_entries', 'progress': 'project_entries'}.get(kind)
        if not table:
            raise BrainError('Неизвестный тип узла.')
        row = db.execute('SELECT * FROM ' + table + ' WHERE id=?', (entity_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        result = {'id': key, 'entity_id': entity_id, 'type': kind, 'created': data['created'], 'sources': [],
                  'status': data.get('status', 'reported'), 'content': data.get('content', data.get('name', ''))}
        if kind == 'memory':
            result.update(kind=data['kind'], tier=data['tier'], origin=data['origin'], confirmed=bool(data['confirmed']),
                          note=data['note'], scope=self.scope(db, entity_id), updated=data['updated'])
            result['source_count'] = db.execute('SELECT COUNT(*) FROM sources WHERE memory_id=?', (entity_id,)).fetchone()[0]
            result['sources'] = [self.source(db, source[0]) for source in db.execute('SELECT message_id FROM sources WHERE memory_id=? ORDER BY rowid DESC LIMIT ?', (entity_id,source_limit))]
            usage = db.execute('SELECT count,last_used FROM memory_usage WHERE memory_id=?', (entity_id,)).fetchone()
            result['usage'] = dict(usage) if usage else {'count': 0, 'last_used': None}
            if self.task_completed(db, entity_id):
                result['status'] = 'completed'
        elif kind == 'message':
            result.update(role=data['role'], chat_id=data['chat_id'], status=data['status'])
        elif kind == 'project':
            result['source_count'] = db.execute('SELECT COUNT(*) FROM project_sources WHERE project_id=?', (entity_id,)).fetchone()[0]
            result['sources'] = [self.source(db, source[0]) for source in db.execute('SELECT message_id FROM project_sources WHERE project_id=? ORDER BY rowid DESC LIMIT ?', (entity_id,source_limit))]
        elif kind == 'branch':
            result.update(project_id=data['project_id'], status=data['status'])
            result['source_count'] = db.execute('SELECT COUNT(*) FROM branch_sources WHERE branch_id=?', (entity_id,)).fetchone()[0]
            result['sources'] = [self.source(db, source[0]) for source in db.execute('SELECT message_id FROM branch_sources WHERE branch_id=? ORDER BY rowid DESC LIMIT ?', (entity_id,source_limit))]
        else:
            result['sources'] = [self.source(db, data['message_id'])]
            result['kind'] = data['kind']
            if kind == 'entry':
                branch = db.execute('SELECT project_id FROM development_branches WHERE id=?', (data['branch_id'],)).fetchone()
                result.update(type=data['kind'], branch_id=data['branch_id'], project_id=branch[0], status=self.entry_state(db, row))
            else:
                result.update(project_id=data['project_id'], status='active' if data['resolved_by'] is None else data['status'])
                if db.execute('SELECT 1 FROM project_entries WHERE project_id=? AND kind=? AND digest=? AND created>? LIMIT 1',
                              (data['project_id'],data['kind'],data['digest'],data['created'])).fetchone():
                    result['status'] = 'superseded'
        result['sources'] = [source for source in result['sources'] if source]
        result['title'] = result['content'][:85]
        result['type_label'] = KINDS_RU.get(result['type'], result['type'])
        return result

    def passport(self, key):
        with self.store.connection() as db:
            db.execute('BEGIN')
            result = self.describe(db, key, source_limit=20)
            if result is None:
                raise BrainError('Узел удалён или больше не доступен.', 404)
            if result['type'] == 'memory':
                series = db.execute('SELECT series_id FROM memory_series WHERE memory_id=?', (result['entity_id'],)).fetchone()
                result['versions'] = [{**dict(row), 'snapshot': json.loads(row['snapshot'])} for row in db.execute('''SELECT v.* FROM memory_versions v
                    JOIN memory_series s ON s.memory_id=v.memory_id WHERE s.series_id=? ORDER BY v.created DESC,v.rowid DESC LIMIT 30''', (series[0],))] if series else []
            return result

    def trace(self, db, reply_id):
        row = db.execute('SELECT t.*,m.chat_id FROM reply_traces t JOIN messages m ON m.id=t.reply_id WHERE t.reply_id=?', (reply_id,)).fetchone()
        if not row:
            raise BrainError('Для этого ответа путь памяти не сохранён.', 404)
        data = json.loads(row['data'])
        keys = ['brain']
        if data.get('project_id'):
            keys.append('project:' + data['project_id'])
        keys += ['memory:' + value for value in dict.fromkeys(data.get('memory_ids', []) + data.get('summary_memory_ids', []))]
        keys += ['progress:' + value for value in data.get('progress_ids', [])]
        keys += ['entry:' + value for value in data.get('development_ids', [])]
        keys += ['message:' + value for value in data.get('summary_source_ids', [])]
        if data.get('branch_id'):
            keys.append('branch:' + data['branch_id'])
        changed = 0
        for memory_id, digest in data.get('memory_hashes', {}).items():
            live = db.execute('SELECT content FROM memories WHERE id=?', (memory_id,)).fetchone()
            if live and text_hash(live[0]) != digest:
                changed += 1
        return {'reply_id': reply_id, 'chat_id': row['chat_id'], 'node_ids': keys, 'created': row['created'], 'changed_memories': changed,
                'recent_message_count': len(data.get('recent_ids', [])), 'legacy': bool(data.get('legacy'))}

    def graph(self, project_id=None, focus=None, reply_id=None):
        identity(project_id, optional=True)
        identity(reply_id, optional=True)
        with self.store.connection() as db:
            db.execute('BEGIN')
            if project_id and not db.execute('SELECT 1 FROM projects WHERE id=?', (project_id,)).fetchone():
                raise BrainError('Проект не найден.', 404)
            nodes, edges, omitted = {}, [], [0]
            def add(key):
                if key not in nodes:
                    data = self.describe(db, key, source_limit=0)
                    if data:
                        if project_id:
                            allowed = (data['entity_id'] == project_id if data['type'] == 'project' else
                                data.get('scope') in ('global', project_id) if data['type'] == 'memory' else
                                data.get('project_id') in (None, project_id))
                            if not allowed:
                                return None
                        if len(nodes) >= 240:
                            omitted[0] += 1
                            return None
                        nodes[key] = {k: data.get(k) for k in ('id','entity_id','type','type_label','title','status','kind','tier','confirmed','project_id') if k in data}
                return key if key in nodes else None
            def link(first, second, kind):
                if first and second and first != second:
                    edges.append({'source': first, 'target': second, 'kind': kind})
            add('brain')
            trace = self.trace(db, reply_id) if reply_id else None
            if focus:
                if not add(focus):
                    raise BrainError('Узел удалён или больше не доступен.', 404)
                if focus.startswith('branch:'):
                    branch_id = focus.split(':',1)[1]
                    for entry in db.execute('SELECT id FROM development_entries WHERE branch_id=? ORDER BY created DESC LIMIT 30', (branch_id,)):
                        link(focus,add('entry:'+entry[0]),'contains')
                    for progress in db.execute('SELECT progress_id FROM branch_progress WHERE branch_id=? ORDER BY rowid DESC LIMIT 30', (branch_id,)):
                        link(focus,add('progress:'+progress[0]),'contains')
            if trace:
                for key in trace['node_ids']:
                    add(key)
            where, args = (' WHERE id=?', [project_id]) if project_id else ('', [])
            for row in db.execute('SELECT id FROM projects' + where + ' ORDER BY created DESC LIMIT 20', args):
                link('brain', add('project:' + row[0]), 'contains')
            where, args = (' WHERE project_id=? OR project_id IS NULL', [project_id]) if project_id else ('', [])
            for row in db.execute('SELECT * FROM development_branches' + where + ' ORDER BY updated DESC LIMIT 30', args):
                key = add('branch:' + row['id'])
                link(add('project:' + row['project_id']) if row['project_id'] else 'brain', key, 'develops')
            where, args = (' AND (b.project_id=? OR b.project_id IS NULL)', [project_id]) if project_id else ('', [])
            for row in db.execute('''SELECT e.* FROM development_entries e JOIN development_branches b ON b.id=e.branch_id
                     WHERE 1=1''' + where + ' ORDER BY e.created DESC,e.rowid DESC LIMIT 50', args):
                link(add('branch:' + row['branch_id']), add('entry:' + row['id']), 'contains')
            where, args = (" WHERE p.project_id=?", [project_id]) if project_id else ('', [])
            for row in db.execute('SELECT p.*,b.branch_id FROM project_entries p LEFT JOIN branch_progress b ON b.progress_id=p.id' + where + ' ORDER BY p.created DESC LIMIT 45', args):
                link(add('branch:' + row['branch_id']) if row['branch_id'] else add('project:' + row['project_id']), add('progress:' + row['id']), 'contains')
            where, args = (" WHERE COALESCE(s.scope,'global')='global' OR EXISTS (SELECT 1 FROM memory_projects p WHERE p.memory_id=m.id AND p.project_id=?)", [project_id]) if project_id else ('', [])
            for row in db.execute('SELECT m.id FROM memories m LEFT JOIN memory_scopes s ON s.memory_id=m.id' + where + ' ORDER BY m.updated DESC LIMIT 65', args):
                add('memory:' + row[0])
            # Hydrate relations only for included nodes; capped source nodes keep the map bounded.
            for key in list(nodes):
                data = self.describe(db, key)
                if key.startswith('memory:'):
                    for row in db.execute('SELECT project_id FROM memory_projects WHERE memory_id=?', (data['entity_id'],)):
                        link(add('project:' + row[0]), key, 'belongs')
                    if data['scope'] == 'global':
                        link('brain', key, 'remembers')
                    if data['status'] == 'active':
                        for old in db.execute('''SELECT m.id FROM memory_series s JOIN memory_series old ON old.series_id=s.series_id
                            JOIN memories m ON m.id=old.memory_id WHERE s.memory_id=? AND m.id!=? AND m.status='superseded' LIMIT 15''', (data['entity_id'],data['entity_id'])):
                            link(key, add('memory:'+old[0]), 'supersedes')
                if key.startswith('progress:'):
                    prior = db.execute('SELECT id,kind FROM project_entries WHERE resolved_by=?', (data['entity_id'],)).fetchall()
                    for row in prior:
                        link(key, add('progress:' + row[0]), 'closes' if row['kind'] == 'todo' else 'supersedes')
                for source in data.get('sources', [])[:2]:
                    if len(nodes) < 220:
                        link(add('message:' + source['id']), key, 'grounds')
            for relation in db.execute('SELECT * FROM development_relations'):
                a, b = 'entry:' + relation['entry_id'], 'entry:' + relation['target_id']
                if a in nodes or b in nodes:
                    link(add(a), add(b), relation['kind'])
            for relation in db.execute('SELECT * FROM memory_links'):
                a, b = 'memory:' + relation['first_id'], 'memory:' + relation['second_id']
                if a in nodes or b in nodes:
                    link(add(a), add(b), 'duplicate')
            # Same property within the same scope is a possible contradiction, not a truth verdict.
            memory_ids = [n['entity_id'] for n in nodes.values() if n['type'] == 'memory']
            records = [db.execute('SELECT * FROM memories WHERE id=?', (value,)).fetchone() for value in memory_ids]
            for i, a in enumerate(records):
                for b in records[i+1:]:
                    if a['slot'] and a['slot'] == b['slot'] and a['kind'] == b['kind'] and self.scope(db,a['id']) == self.scope(db,b['id']):
                        if 'conflict' in (a['status'], b['status']):
                            link('memory:'+a['id'], 'memory:'+b['id'], 'conflicts')
            if trace:
                trace['missing_nodes'] = len(set(trace['node_ids']) - nodes.keys())
                trace['node_ids'] = [key for key in trace['node_ids'] if key in nodes]
            unique_edges = {(edge['source'],edge['target'],edge['kind']):edge for edge in edges if edge['source'] in nodes and edge['target'] in nodes}
            replies = [dict(row) for row in db.execute('''SELECT t.reply_id,t.created,m.chat_id,u.content AS prompt FROM reply_traces t
                JOIN messages m ON m.id=t.reply_id JOIN messages u ON u.id=t.user_id ORDER BY t.created DESC LIMIT 30''')]
            for reply in replies:
                reply['prompt'] = reply['prompt'][:90]
            bounded = any(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] > cap for table,cap in (
                ('memories',65),('projects',20),('development_branches',30),('development_entries',50),('project_entries',45)))
            return {'nodes': list(nodes.values()), 'edges': list(unique_edges.values()), 'trace': trace, 'replies': replies,
                    'bounded': bounded or len(nodes) >= 200 or bool(omitted[0]), 'revision': self.compact_revision(db)}

    def compact_revision(self, db):
        return str(db.execute('SELECT COUNT(*) FROM growth_events').fetchone()[0])

    def history(self, project_id=None):
        identity(project_id, optional=True)
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM growth_events ORDER BY created DESC,rowid DESC LIMIT 300').fetchall()
            result = []
            for row in rows:
                key = next((prefix + ':' + row[field] for field,prefix in (('entry_id','entry'), ('progress_id','progress'),
                    ('memory_id','memory'),('branch_id','branch'),('message_id','message')) if row[field]), None)
                node = self.describe(db, key, source_limit=0) if key else None
                if not node:
                    continue
                if project_id:
                    if node['type'] == 'memory' and node['scope'] not in ('global', project_id):
                        continue
                    if node.get('project_id') not in (None, project_id):
                        continue
                result.append({'id': row['id'], 'kind': row['kind'], 'label': EVENTS_RU.get(row['kind'],row['kind']),
                               'created': row['created'], 'node_id': key, 'title': node['title']})
                if len(result) >= 100:
                    break
            return result
