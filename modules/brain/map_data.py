"""Bounded, paginated map reads. Cursors are tied to a live parent and goal window."""
import base64
from datetime import datetime
import json
import re

from .provider import BrainError
from .growth import identity, text_hash
from .store import now


STRUCTURAL = {'contains', 'develops', 'belongs', 'remembers'}
NODE_FIELDS = ('id','entity_id','type','type_label','title','status','kind','tier','confirmed','project_id','branch_id','created','updated')


class MapBuilder:
    def __init__(self, growth, db, project_id=None, cap=240):
        self.growth, self.db, self.project_id, self.cap = growth, db, project_id, cap
        self.nodes, self.edges, self.descriptions = {}, {}, {}
        self.omitted = False
        self.add('brain')

    def allowed(self, data):
        if not self.project_id:
            return True
        if data['type'] == 'project':
            return data['entity_id'] == self.project_id
        if data['type'] == 'memory':
            return data.get('scope') in ('global', self.project_id)
        return data.get('project_id') in (None, self.project_id)

    def add(self, key):
        if key in self.nodes:
            return key
        if len(self.nodes) >= self.cap:
            self.omitted = True
            return None
        data = self.growth.describe(self.db, key, source_limit=0)
        if not data or not self.allowed(data):
            return None
        self.descriptions[key] = data
        node = {field:data[field] for field in NODE_FIELDS if field in data}
        node['stamp'] = text_hash(json.dumps({field:data.get(field) for field in (
            'content','status','kind','tier','scope','confirmed','updated','source_count','usage')},sort_keys=True,ensure_ascii=False))
        self.nodes[key] = node
        parents = []
        if data['type'] == 'project':
            parents = [('brain','contains')]
        elif data['type'] == 'branch':
            parents = [('project:'+data['project_id'] if data['project_id'] else 'brain','develops')]
        elif key.startswith('entry:'):
            parents = [('branch:'+data['branch_id'],'contains')]
        elif data['type'] == 'progress':
            parents = [('branch:'+row[0],'contains') for row in self.db.execute('SELECT branch_id FROM branch_progress WHERE progress_id=? ORDER BY branch_id',(data['entity_id'],))]
            if not parents:
                parents = [('project:'+data['project_id'],'contains')]
        elif data['type'] == 'memory':
            parents = [('project:'+row[0],'belongs') for row in self.db.execute('SELECT project_id FROM memory_projects WHERE memory_id=? ORDER BY project_id',(data['entity_id'],))]
            if not parents:
                parents = [('brain','remembers')]
        node['parent_ids'] = []
        if data['type'] in ('branch','project'):
            node['can_expand'] = True
        for parent,relation in parents:
            if self.add(parent):
                node['parent_ids'].append(parent)
                self.link(parent,key,relation)
        return key

    def link(self, source, target, kind):
        if source and target and source != target and source in self.nodes and target in self.nodes:
            self.edges[(source,target,kind)] = {'source':source,'target':target,'kind':kind}

    def hydrate(self, keys, eligible=None):
        def neighbor(key):
            if eligible and key not in self.nodes and not eligible(key):
                return None
            return self.add(key)
        for key in list(keys):
            if key not in self.nodes:
                continue
            data = self.descriptions[key]
            sources=[] if eligible and data['type'] in ('project','branch') else self.growth.describe(self.db,key,source_limit=2).get('sources',[])
            for source in sources:
                self.link(self.add('message:'+source['id']),key,'grounds')
            if key.startswith('progress:'):
                for prior in self.db.execute('SELECT id,kind FROM project_entries WHERE resolved_by=? ORDER BY created DESC LIMIT 30',(data['entity_id'],)):
                    self.link(key,neighbor('progress:'+prior[0]),'closes' if prior[1]=='todo' else 'supersedes')
            if key.startswith('entry:'):
                for relation in self.db.execute('SELECT * FROM development_relations WHERE entry_id=? OR target_id=? LIMIT 40',(data['entity_id'],data['entity_id'])):
                    self.link(neighbor('entry:'+relation['entry_id']),neighbor('entry:'+relation['target_id']),relation['kind'])
            if key.startswith('memory:'):
                for relation in self.growth.store.neurons.links(self.db,data['entity_id']):
                    target=neighbor('memory:'+relation['id'])
                    if target:
                        first,second=sorted((key,target))
                        self.link(first,second,'association')
                        edge=self.edges.get((first,second,'association'))
                        if edge is not None:
                            edge.update(shared=relation['shared'],similarity=relation['similarity'],coactivations=relation['coactivations'])
                for relation in self.db.execute('SELECT * FROM memory_links WHERE first_id=? OR second_id=? LIMIT 20',(data['entity_id'],data['entity_id'])):
                    self.link(neighbor('memory:'+relation['first_id']),neighbor('memory:'+relation['second_id']),'duplicate')
                if data['status']=='active':
                    for old in self.db.execute('''SELECT m.id FROM memory_series a JOIN memory_series b ON b.series_id=a.series_id
                        JOIN memories m ON m.id=b.memory_id WHERE a.memory_id=? AND m.id!=? AND m.status='superseded' LIMIT 15''',(data['entity_id'],data['entity_id'])):
                        self.link(key,neighbor('memory:'+old[0]),'supersedes')
            if key.startswith('message:'):
                message_id=key.split(':',1)[1]
                for row in self.db.execute("SELECT 'entry:'||id FROM development_entries WHERE message_id=? LIMIT 10",(message_id,)):
                    self.link(key,neighbor(row[0]),'grounds')
                for row in self.db.execute("SELECT 'progress:'||id FROM project_entries WHERE message_id=? LIMIT 10",(message_id,)):
                    self.link(key,neighbor(row[0]),'grounds')
                for row in self.db.execute("SELECT 'memory:'||memory_id FROM sources WHERE message_id=? LIMIT 10",(message_id,)):
                    self.link(key,neighbor(row[0]),'grounds')
        records = []
        for key,node in list(self.nodes.items()):
            if node['type']=='memory':
                records.append(self.db.execute('SELECT id,slot,kind,status FROM memories WHERE id=?',(node['entity_id'],)).fetchone())
        for index,a in enumerate(records):
            for b in records[index+1:]:
                if (a['slot'] and a['slot']==b['slot'] and a['kind']==b['kind'] and 'conflict' in (a['status'],b['status'])
                        and self.growth.scope(self.db,a['id'])==self.growth.scope(self.db,b['id'])):
                    self.link('memory:'+a['id'],'memory:'+b['id'],'conflicts')

    def result(self):
        return {'nodes':list(self.nodes.values()),'edges':list(self.edges.values()),'bounded':self.omitted}


class MapData:
    def __init__(self, growth):
        self.growth, self.store = growth, growth.store

    def parent(self, db, key, project_id=None):
        data = self.growth.describe(db,key,source_limit=0)
        if not data or data['type'] not in ('project','branch'):
            raise BrainError('Ветка удалена или недоступна.',404)
        owner = data['entity_id'] if data['type']=='project' else data['project_id']
        if project_id and owner not in (None,project_id):
            raise BrainError('Ветка не относится к выбранному проекту.',404)
        return data

    def goal(self, db, goal_id, project_id=None):
        identity(goal_id)
        row = db.execute("SELECT * FROM project_entries WHERE id=? AND kind='goal'",(goal_id,)).fetchone()
        if not row or (project_id and row['project_id']!=project_id):
            raise BrainError('Цель удалена или не относится к выбранному проекту.',404)
        branches = [r[0] for r in db.execute('SELECT branch_id FROM branch_progress WHERE progress_id=? ORDER BY branch_id',(goal_id,))]
        windows = []
        for branch_id in branches:
            end = db.execute("""SELECT MIN(p.created) FROM branch_progress b JOIN project_entries p ON p.id=b.progress_id
                WHERE b.branch_id=? AND p.kind='goal' AND p.created>?""",(branch_id,row['created'])).fetchone()[0]
            windows.append({'parent':'branch:'+branch_id,'start':row['created'],'end':end})
        if not windows:
            end = db.execute("SELECT MIN(created) FROM project_entries WHERE project_id=? AND kind='goal' AND created>?",(row['project_id'],row['created'])).fetchone()[0]
            windows.append({'parent':'project:'+row['project_id'],'start':row['created'],'end':end})
        return {'goal_id':goal_id,'project_id':row['project_id'],'title':row['content'],'windows':windows,
                'node_ids':['progress:'+goal_id],'counts':{'tasks':0,'open_tasks':0,'decisions':0,'done_reports':0},
                'note':'Записи того же направления за период цели. Завершение показано только там, где сохранена связь с задачей; отчёт пользователя не является проверкой кода.'}

    def rows_sql(self, data, window=None):
        key = data['entity_id']
        if data['type']=='branch':
            sql = """SELECT 'entry:'||e.id AS node_key,e.created,e.updated||'|'||COALESCE(e.manual_state,'') AS rev
                FROM development_entries e WHERE e.branch_id=? UNION ALL
                SELECT 'progress:'||p.id,p.created,p.status||'|'||COALESCE(p.resolved_by,'')||'|'||p.digest
                FROM branch_progress b JOIN project_entries p ON p.id=b.progress_id WHERE b.branch_id=?"""
            args = [key,key]
        else:
            sql = """SELECT 'branch:'||b.id AS node_key,b.created,b.updated||'|'||b.status||'|'||
                (SELECT COUNT(*) FROM branch_sources s WHERE s.branch_id=b.id) AS rev FROM development_branches b WHERE b.project_id=?
                UNION ALL SELECT 'memory:'||m.id,m.created,m.updated||'|'||m.status||'|'||m.tier||'|'||m.digest||'|'||
                (SELECT COUNT(*) FROM sources s WHERE s.memory_id=m.id)||'|'||COALESCE((SELECT count||last_used FROM memory_usage u WHERE u.memory_id=m.id),'')
                FROM memory_projects p JOIN memories m ON m.id=p.memory_id WHERE p.project_id=?
                UNION ALL SELECT 'progress:'||p.id,p.created,p.status||'|'||COALESCE(p.resolved_by,'')||'|'||p.digest
                FROM project_entries p WHERE p.project_id=? AND NOT EXISTS (SELECT 1 FROM branch_progress b WHERE b.progress_id=p.id)"""
            args = [key,key,key]
        sql = 'SELECT * FROM ('+sql+')'
        if window:
            sql += ' WHERE created>=?'
            args.append(window['start'])
            if window['end']:
                sql += ' AND created<?'
                args.append(window['end'])
        return sql,args

    def parent_revision(self, db, data, sql, args):
        # Stream metadata, never the full bodies of a large branch.
        import hashlib
        digest = hashlib.sha256()
        digest.update(json.dumps([data['id'],data['status'],data.get('updated'),data.get('source_count')]).encode())
        count = 0
        for row in db.execute(sql+' ORDER BY created DESC,node_key DESC',args):
            digest.update(json.dumps(tuple(row),ensure_ascii=False).encode())
            count += 1
        project_id = data['entity_id'] if data['type']=='project' else data.get('project_id')
        if project_id:
            state = db.execute('''SELECT COUNT(*),MAX(created),SUM(rowid),SUM(CASE WHEN resolved_by IS NOT NULL THEN 1 ELSE 0 END)
                FROM project_entries WHERE project_id=?''',(project_id,)).fetchone()
            digest.update(json.dumps(tuple(state)).encode())
        if data['type']=='branch':
            for row in db.execute('''SELECT r.entry_id,r.target_id,r.kind FROM development_relations r
                JOIN development_entries e ON e.id=r.entry_id WHERE e.branch_id=? ORDER BY r.entry_id,r.target_id,r.kind''',(data['entity_id'],)):
                digest.update(json.dumps(tuple(row)).encode())
        else:
            for row in db.execute('''SELECT l.first_id,l.second_id,l.created FROM memory_links l WHERE EXISTS
                (SELECT 1 FROM memory_projects p WHERE p.project_id=? AND p.memory_id IN (l.first_id,l.second_id))
                ORDER BY l.first_id,l.second_id''',(data['entity_id'],)):
                digest.update(json.dumps(tuple(row)).encode())
        if data['type']=='project':
            for row in db.execute('SELECT s.first_id,s.second_id,s.updated,s.coactivations FROM synapses s WHERE EXISTS (SELECT 1 FROM memory_projects p WHERE p.project_id=? AND p.memory_id=s.first_id) ORDER BY s.first_id,s.second_id',(data['entity_id'],)):
                digest.update(json.dumps(tuple(row)).encode())
        return digest.hexdigest(),count

    def in_goal(self, db, key, goal):
        if key in ('brain','project:'+goal['project_id'],'progress:'+goal['goal_id']):
            return True
        for window in goal['windows']:
            if key==window['parent']:
                return True
            data=self.parent(db,window['parent'],goal['project_id'])
            sql,args=self.rows_sql(data,window)
            if db.execute('SELECT 1 FROM ('+sql+') WHERE node_key=? LIMIT 1',args+[key]).fetchone():
                return True
            if key.startswith('progress:') and db.execute("SELECT 1 FROM project_entries p WHERE p.id=? AND 'progress:'||p.resolved_by IN (SELECT node_key FROM ("+sql+'))', [key.split(':',1)[1]]+args).fetchone():
                return True
            if key.startswith('entry:') and db.execute("SELECT 1 FROM development_relations r JOIN development_entries target ON target.id=r.target_id WHERE r.target_id=? AND target.created<? AND 'entry:'||r.entry_id IN (SELECT node_key FROM ("+sql+'))', [key.split(':',1)[1],window['start']]+args).fetchone():
                return True
            if key.startswith('message:'):
                linked="SELECT 'entry:'||id AS node_key FROM development_entries WHERE message_id=? UNION ALL SELECT 'progress:'||id FROM project_entries WHERE message_id=? UNION ALL SELECT 'memory:'||memory_id FROM sources WHERE message_id=?"
                if db.execute('SELECT 1 FROM ('+linked+') WHERE node_key IN (SELECT node_key FROM ('+sql+')) LIMIT 1',[key.split(':',1)[1]]*3+args).fetchone():
                    return True
        return False

    def cursor(self, parent, revision, goal_id, last):
        return base64.urlsafe_b64encode(json.dumps({'parent':parent,'revision':revision,'goal':goal_id,
            'before':[last['created'],last['node_key']]},separators=(',',':')).encode()).decode().rstrip('=')

    def parse_cursor(self, value, parent, revision, goal_id):
        try:
            if not isinstance(value,str) or not 1<=len(value)<=1024 or not re.fullmatch(r'[A-Za-z0-9_-]+',value):
                raise ValueError()
            data = json.loads(base64.urlsafe_b64decode(value+'='*((-len(value))%4)).decode('utf-8'))
            if (not isinstance(data,dict) or set(data)!= {'parent','revision','goal','before'} or data['parent']!=parent or data['goal']!=goal_id
                    or not isinstance(data['before'],list) or len(data['before'])!=2):
                raise ValueError()
            created,key = data['before']
            if not isinstance(created,str) or not 10<=len(created)<=40 or not datetime.fromisoformat(created).tzinfo:
                raise ValueError()
            if not isinstance(key,str) or not re.fullmatch(r'(?:entry|progress|branch|memory):[a-f0-9]{32}',key):
                raise ValueError()
            if data['revision']!=revision:
                raise BrainError('Ветка изменилась. Обновите карту и продолжите загрузку.',409)
            return created,key
        except (ValueError,TypeError,KeyError,UnicodeError):
            raise BrainError('Некорректный указатель страницы карты.') from None

    def page(self, db, builder, parent, limit=24, cursor=None, goal=None, project_id=None):
        if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=48:
            raise BrainError('Размер страницы карты должен быть от 1 до 48.')
        data = self.parent(db,parent,project_id)
        window = None
        if goal:
            window = next((w for w in goal['windows'] if w['parent']==parent),None)
            if not window:
                raise BrainError('Ветка не относится к выбранной цели.',404)
        sql,args = self.rows_sql(data,window)
        revision,total = self.parent_revision(db,data,sql,args)
        page_sql,page_args = 'SELECT * FROM ('+sql+')',list(args)
        if cursor:
            created,key = self.parse_cursor(cursor,parent,revision,goal['goal_id'] if goal else None)
            page_sql += ' WHERE (created<? OR (created=? AND node_key<?))'
            page_args += [created,created,key]
        rows = db.execute(page_sql+' ORDER BY created DESC,node_key DESC LIMIT ?',page_args+[limit+1]).fetchall()
        more,rows = len(rows)>limit,rows[:limit]
        builder.add(parent)
        keys,last = [],None
        for row in rows:
            if not builder.add(row['node_key']):
                more=True
                break
            keys.append(row['node_key'])
            last=row
            builder.link(parent,row['node_key'],'develops' if row['node_key'].startswith('branch:') else
                         'belongs' if row['node_key'].startswith('memory:') else 'contains')
        return {'parent':parent,'revision':revision,'total':total,'node_ids':keys,
                'through':[last['created'],last['node_key']] if last else None,
                'has_more':more,'cursor':self.cursor(parent,revision,goal['goal_id'] if goal else None,last) if more and last else None}

    def children(self, parent, cursor=None, limit=24, project_id=None, goal_id=None):
        identity(project_id,optional=True)
        with self.store.connection() as db:
            db.execute('BEGIN')
            goal = self.goal(db,goal_id,project_id) if goal_id else None
            if goal:
                project_id=goal['project_id']
            builder = MapBuilder(self.growth,db,project_id,cap=240)
            page = self.page(db,builder,parent,limit,cursor,goal,project_id)
            builder.hydrate(page['node_ids'],(lambda key:self.in_goal(db,key,goal)) if goal else None)
            result=builder.result()
            result.update(page=page,snapshot_at=now())
            if goal:
                result['goal_path']={'goal_id':goal_id,'node_ids':list(builder.nodes)}
            return result

    def goal_counts(self, db, goal):
        seen = set()
        for window in goal['windows']:
            data = self.parent(db,window['parent'],goal['project_id'])
            sql,args = self.rows_sql(data,window)
            query='''SELECT p.id,p.kind,p.resolved_by FROM project_entries p WHERE 'progress:'||p.id IN (SELECT node_key FROM ('''+sql+'''))
                AND NOT EXISTS (SELECT 1 FROM project_entries newer WHERE newer.project_id=p.project_id AND newer.kind=p.kind
                    AND newer.digest=p.digest AND newer.created>p.created)'''
            for row in db.execute(query,args):
                if row['id'] in seen:
                    continue
                seen.add(row['id'])
                if row['kind']=='todo':
                    goal['counts']['tasks']+=1
                    goal['counts']['open_tasks']+=row['resolved_by'] is None
                elif row['kind']=='decision':
                    goal['counts']['decisions']+=1
                elif row['kind']=='done':
                    goal['counts']['done_reports']+=1

    def graph(self, project_id=None, focus=None, reply_id=None, goal_id=None, retain=None):
        identity(project_id,optional=True)
        identity(reply_id,optional=True)
        if reply_id and goal_id:
            raise BrainError('Выберите путь ответа или путь цели.')
        with self.store.connection() as db:
            db.execute('BEGIN')
            if project_id and not db.execute('SELECT 1 FROM projects WHERE id=?',(project_id,)).fetchone():
                raise BrainError('Проект не найден.',404)
            goal = self.goal(db,goal_id,project_id) if goal_id else None
            if goal:
                project_id=goal['project_id']
            builder = MapBuilder(self.growth,db,project_id)
            pages=[]
            trace = self.growth.trace(db,reply_id) if reply_id else None
            if focus and (goal and not self.in_goal(db,focus,goal) or not builder.add(focus)):
                raise BrainError('Узел удалён или больше не доступен.',404)
            if retain and (not goal or self.in_goal(db,retain,goal)):
                builder.add(retain)
            if goal:
                builder.add('progress:'+goal_id)
                for window in goal['windows'][:6]:
                    pages.append(self.page(db,builder,window['parent'],24,goal=goal,project_id=project_id))
                self.goal_counts(db,goal)
            else:
                if trace:
                    for key in trace['node_ids']:
                        builder.add(key)
                args=[project_id] if project_id else []
                where=' WHERE id=?' if project_id else ''
                projects = db.execute('SELECT id FROM projects'+where+' ORDER BY created DESC,id DESC LIMIT 20',args).fetchall()
                for row in projects:
                    parent='project:'+row[0]
                    builder.add(parent)
                    pages.append(self.page(db,builder,parent,6,project_id=project_id))
                where=' WHERE project_id=? OR project_id IS NULL' if project_id else ''
                branches = db.execute('SELECT id FROM development_branches'+where+' ORDER BY updated DESC,id DESC LIMIT 20',args).fetchall()
                for row in branches:
                    builder.add('branch:'+row[0])
                if focus and focus.startswith('branch:'):
                    branches=[{'id':focus.split(':',1)[1]}]+[row for row in branches if row['id']!=focus.split(':',1)[1]]
                branches=list(dict.fromkeys([row['id'] for row in branches]+[n['entity_id'] for n in list(builder.nodes.values()) if n['type']=='branch']))
                for branch_id in branches[:24]:
                    pages.append(self.page(db,builder,'branch:'+branch_id,12 if focus=='branch:'+branch_id else 6 if len(branches)<=6 else 2,project_id=project_id))
                for row in db.execute("SELECT m.id FROM memories m LEFT JOIN memory_scopes s ON s.memory_id=m.id WHERE COALESCE(s.scope,'global')='global' ORDER BY m.updated DESC LIMIT 24"):
                    builder.add('memory:'+row[0])
            builder.hydrate(list(builder.nodes),(lambda key:self.in_goal(db,key,goal)) if goal else None)
            if trace:
                trace['missing_nodes']=len(set(trace['node_ids'])-builder.nodes.keys())
                trace['node_ids']=[key for key in trace['node_ids'] if key in builder.nodes]
            if goal:
                goal['node_ids']=list(builder.nodes)
            replies=[dict(row) for row in db.execute('''SELECT t.reply_id,t.created,m.chat_id,u.content AS prompt FROM reply_traces t
                JOIN messages m ON m.id=t.reply_id JOIN messages u ON u.id=t.user_id ORDER BY t.created DESC LIMIT 30''')]
            for reply in replies:
                reply['prompt']=reply['prompt'][:90]
            where=' AND project_id=?' if project_id else ''
            goals=[{'id':row['id'],'title':row['content'][:85]} for row in db.execute("SELECT id,content FROM project_entries WHERE kind='goal'"+where+' ORDER BY created DESC,id DESC LIMIT 30', [project_id] if project_id else [])]
            if goal and not any(item['id']==goal_id for item in goals):
                goals.insert(0,{'id':goal_id,'title':goal['title'][:85]})
            result=builder.result()
            result.update(branch_pages=pages,trace=trace,goal_path=goal,replies=replies,goals=goals,snapshot_at=now())
            return result
