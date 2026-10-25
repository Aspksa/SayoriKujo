"""Persistent local associations, durable memory and a fair, bounded maintenance queue.

Nodes are saved memories, not biological neurons or model weights. Lexical links are
explanations of similarity, never evidence of truth. No provider calls or data expiry.
"""
from contextlib import closing
import json
import shutil
import sqlite3
import uuid

from .provider import BrainError
from .store import now, terms

STOP = terms('это этот эта эти есть быть будет будут чтобы который которая которые нужно можно очень самый самая самые также потому поэтому меня тебе тебя свой свои только уже ещё еще для при или как что все всё')


def concepts(text):
    return terms(text) - STOP


class Neurons:
    def __init__(self, store, backup=False):
        self.store = store
        self.error = None
        with store.connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
        if version < 4 and backup:
            folder = store.root / 'backups'
            folder.mkdir(exist_ok=True)
            with closing(sqlite3.connect(store.path)) as source, closing(sqlite3.connect(
                    folder / ('before-v4-' + uuid.uuid4().hex + '.sqlite3'))) as target:
                source.backup(target)
        with store.connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS neural_queue(memory_id TEXT PRIMARY KEY
                REFERENCES memories(id) ON DELETE CASCADE, queued TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS synapses(first_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                second_id TEXT REFERENCES memories(id) ON DELETE CASCADE, shared TEXT NOT NULL,
                similarity REAL NOT NULL, coactivations INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL,
                updated TEXT NOT NULL, PRIMARY KEY(first_id,second_id), CHECK(first_id<second_id))""")
            db.execute('CREATE INDEX IF NOT EXISTS synapse_target ON synapses(second_id)')
            db.execute('CREATE INDEX IF NOT EXISTS neural_queue_order ON neural_queue(queued,memory_id)')
            db.execute('CREATE INDEX IF NOT EXISTS memory_page_order ON memories(updated DESC,id DESC)')
            db.execute("""CREATE TABLE IF NOT EXISTS neural_state(id INTEGER PRIMARY KEY CHECK(id=1),
                last_run TEXT, processed INTEGER NOT NULL DEFAULT 0)""")
            db.execute('INSERT OR IGNORE INTO neural_state(id) VALUES(1)')
            if version < 4:
                db.execute('INSERT OR IGNORE INTO neural_queue SELECT id,created FROM memories')
                db.execute('PRAGMA user_version=4')

    def enqueue(self, db, memory_id, invalidate=False):
        db.execute('INSERT OR IGNORE INTO neural_queue VALUES(?,?)', (memory_id, now()))
        # A changed record must not retain a link based on its previous text/scope.
        if invalidate:
            db.execute('DELETE FROM synapses WHERE first_id=? OR second_id=?', (memory_id, memory_id))

    def eligible(self, db, row):
        return row['status'] == 'active' and row['tier'] != 'archive' and not self.store.growth.task_completed(db, row['id'])

    def tick(self, limit=16):
        with self.store.connection(write=True) as db:
            config = self.store.config(db)
            if not config['auto_neurons'] and not config['auto_vault']:
                return 0
            rows = db.execute('SELECT m.* FROM neural_queue q JOIN memories m ON m.id=q.memory_id ORDER BY q.queued,q.memory_id LIMIT ?', (limit,)).fetchall()
            for row in rows:
                mid = row['id']
                db.execute('DELETE FROM neural_queue WHERE memory_id=?', (mid,))
                if not self.eligible(db, row):
                    db.execute('DELETE FROM synapses WHERE first_id=? OR second_id=?', (mid,mid))
                    continue
                if config['auto_vault'] and row['tier'] == 'long' and row['kind'] != 'task':
                    sources = db.execute('SELECT COUNT(*) FROM sources WHERE memory_id=?', (mid,)).fetchone()[0]
                    if row['confirmed'] or sources >= 3:
                        self.store.learning.version(db, mid, 'До закрепления в особо долговременной памяти')
                        db.execute("UPDATE memories SET tier='vault',updated=? WHERE id=?", (now(),mid))
                        self.store.growth.event(db, 'memory_pinned', memory_id=mid)
                if not config['auto_neurons']:
                    continue
                tokens = sorted(concepts(row['content']))[:64]
                scope = self.store.growth.scope(db, mid)
                candidates = []
                if len(tokens) >= 3:
                    candidates = db.execute("""SELECT m.*,COUNT(*) AS overlap FROM memory_terms t
                        JOIN memories m ON m.id=t.memory_id LEFT JOIN memory_scopes sc ON sc.memory_id=m.id WHERE t.term IN (""" + ','.join('?' for _ in tokens) + """ )
                        AND m.id!=? AND m.status='active' AND m.tier!='archive'
                        AND COALESCE(sc.scope,'global')=? AND (?='global' OR EXISTS
                        (SELECT 1 FROM memory_projects p WHERE p.memory_id=m.id AND p.project_id=?))
                        GROUP BY m.id ORDER BY overlap DESC,m.id LIMIT 80""", tokens+[mid,'global' if scope=='global' else 'project',scope,scope]).fetchall()
                selected = set()
                scope = self.store.growth.scope(db, mid)
                for other in candidates:
                    if len(selected) >= 12:
                        break
                    if self.store.growth.scope(db,other['id']) != scope or not self.eligible(db, other):
                        continue
                    a,b = concepts(row['content']),concepts(other['content'])
                    shared = sorted(a & b)
                    similarity = len(shared) / max(1,len(a | b))
                    if len(shared) < 3 or similarity < .25:
                        continue
                    first,second = sorted((mid,other['id']))
                    old = db.execute('SELECT 1 FROM synapses WHERE first_id=? AND second_id=?',(first,second)).fetchone()
                    degree = db.execute('SELECT COUNT(*) FROM synapses WHERE first_id=? OR second_id=?',(other['id'],other['id'])).fetchone()[0]
                    if not old and degree >= 12:
                        continue
                    stamp=now()
                    db.execute("""INSERT INTO synapses(first_id,second_id,shared,similarity,created,updated) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(first_id,second_id) DO UPDATE SET shared=excluded.shared,
                        similarity=excluded.similarity,updated=excluded.updated""",(first,second,json.dumps(shared,ensure_ascii=False),similarity,stamp,stamp))
                    selected.add(other['id'])
                for link in db.execute('SELECT first_id,second_id FROM synapses WHERE first_id=? OR second_id=?',(mid,mid)).fetchall():
                    other = link[1] if link[0] == mid else link[0]
                    if other not in selected:
                        db.execute('DELETE FROM synapses WHERE first_id=? AND second_id=?',tuple(link))
            if rows:
                db.execute('UPDATE neural_state SET last_run=?,processed=processed+? WHERE id=1',(now(),len(rows)))
        self.error = None
        return len(rows)

    def safe_tick(self):
        try:
            return self.tick()
        except (BrainError, sqlite3.Error, OSError, ValueError):
            self.error = 'Автосвязи временно недоступны. Память сохранена; обработка повторится.'
            return 0

    def links(self, db, memory_id):
        result=[]
        for row in db.execute('SELECT * FROM synapses WHERE first_id=? OR second_id=? ORDER BY similarity DESC LIMIT 12',(memory_id,memory_id)):
            other = row['second_id'] if row['first_id'] == memory_id else row['first_id']
            a=db.execute('SELECT * FROM memories WHERE id=?',(memory_id,)).fetchone()
            b=db.execute('SELECT * FROM memories WHERE id=?',(other,)).fetchone()
            if not a or not b or not self.eligible(db,a) or not self.eligible(db,b) or self.store.growth.scope(db,a['id']) != self.store.growth.scope(db,b['id']):
                continue
            result.append({'id':other,'title':b['content'][:85],'shared':json.loads(row['shared']),
                'similarity':row['similarity'],'coactivations':row['coactivations']})
        return result

    def activate(self, db, ids):
        ids=sorted(set(ids))
        if len(ids) >= 2:
            # The trace is already bounded by the chat context budget. Include summary
            # memories too: a reply may contain more than twenty distinct records.
            placeholders=','.join('?' for _ in ids)
            db.execute('UPDATE synapses SET coactivations=coactivations+1 WHERE first_id IN ('+placeholders+') AND second_id IN ('+placeholders+')',ids+ids)

    def state(self):
        with self.store.connection() as db:
            state=dict(db.execute('SELECT * FROM neural_state WHERE id=1').fetchone())
            state.update(nodes=db.execute('SELECT COUNT(*) FROM memories').fetchone()[0],
                links=db.execute('SELECT COUNT(*) FROM synapses').fetchone()[0],
                pending=db.execute('SELECT COUNT(*) FROM neural_queue').fetchone()[0],
                vault=db.execute("SELECT COUNT(*) FROM memories WHERE tier='vault'").fetchone()[0])
        state.update(bytes=sum(p.stat().st_size for p in self.store.root.glob('brain.sqlite3*') if p.is_file()),
            free_bytes=shutil.disk_usage(self.store.root).free,error=self.error)
        return state
