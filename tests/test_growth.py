"""Memory graph, development evidence and local consolidation; synthetic provider only."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import test_brain as base

brain = base.brain


class GrowthTests(unittest.TestCase):
    setUp = base.BrainTests.setUp
    turn = base.BrainTests.turn

    def analyse(self, text, chat=None, items=(), project=None, branch=None, entries=(), progress=()):
        self.cloud.analysis = {'items':list(items),'project':project,'progress':list(progress),
                               'development':{'branch':branch,'entries':list(entries)}}
        chat, _, _ = self.turn(text, chat)
        self.assertTrue(self.service.process_one())
        return chat

    def seed(self, name='Sakura', branch='Голос', chat=None):
        text=f'Проект {name}. Ветка {branch}. Какой тембр выбрать? Вариант: низкий голос. Вариант: мягкий голос.'
        chat=self.analyse(text,chat,project={'name':name,'quote':f'Проект {name}.'},
            branch={'name':branch,'quote':f'Ветка {branch}.'},entries=[
                {'kind':'question','quote':'Какой тембр выбрать?'},
                {'kind':'option','quote':'Вариант: низкий голос.'},
                {'kind':'option','quote':'Вариант: мягкий голос.'}])
        project=next(p for p in self.service.state()['projects'] if p['name']==name)
        graph=self.service.get('graph',{'project_id':[project['id']]})
        question=next(n for n in graph['nodes'] if n['type']=='question')
        branch_node=next(n for n in graph['nodes'] if n['type']=='branch')
        options=[n for n in graph['nodes'] if n['type']=='option']
        return chat,project,branch_node,question,options

    def test_graph_branches_questions_options_and_sources_survive_restart(self):
        chat,project,branch,question,options=self.seed()
        graph=self.service.get('graph',{})
        ids={n['id'] for n in graph['nodes']}
        self.assertTrue(all(e['source'] in ids and e['target'] in ids for e in graph['edges']))
        self.assertTrue(any(e['kind']=='develops' and e['target']==branch['id'] for e in graph['edges']))
        passport=self.service.get('passport',{'node':[question['id']]})
        self.assertEqual(passport['sources'][0]['chat_id'],chat)
        self.assertIn(passport['content'],passport['sources'][0]['content'])
        self.assertEqual(passport['status'],'open')
        self.assertEqual(self.service.store.memories(),[])
        reopened=brain.BrainService(self.root,self.cloud.factory,start_worker=False)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get('passport',{'node':[question['id']]}),passport)

    def test_explicit_choice_answers_question_and_selects_option(self):
        chat,_,_,question,options=self.seed()
        self.analyse('Выбираю мягкий голос.',chat,entries=[{'kind':'choice','quote':'Выбираю мягкий голос.',
            'targets':[question['entity_id'],options[1]['entity_id']]}])
        self.assertEqual(self.service.store.growth.passport(question['id'])['status'],'resolved')
        self.assertEqual(self.service.store.growth.passport(options[1]['id'])['status'],'selected')
        self.assertEqual(self.service.store.growth.passport(options[0]['id'])['status'],'proposed')
        graph=self.service.get('graph',{})
        self.assertTrue({'answers','selects'}<={e['kind'] for e in graph['edges']})

    def test_changed_choice_supersedes_previous_and_deleting_new_source_restores_it(self):
        chat,project,branch,question,options=self.seed()
        def choose(option,text):
            full='Проект Sakura. Ветка Голос. '+text
            return self.analyse(full,project={'name':'Sakura','quote':'Проект Sakura.'},branch={'name':'Голос','quote':'Ветка Голос.'},
                entries=[{'kind':'choice','quote':text,'targets':[question['entity_id'],option['entity_id']]}])
        first=choose(options[0],'Выбираю низкий голос.')
        second=choose(options[1],'Теперь выбираю мягкий голос.')
        self.assertEqual(self.service.store.growth.passport(options[0]['id'])['status'],'proposed')
        self.assertEqual(self.service.store.growth.passport(options[1]['id'])['status'],'selected')
        self.service.store.delete_chat(second)
        self.assertEqual(self.service.store.growth.passport(options[0]['id'])['status'],'selected')
        self.assertEqual(self.service.store.growth.passport(question['id'])['status'],'resolved')
        self.service.store.delete_chat(first)
        self.assertEqual(self.service.store.growth.passport(question['id'])['status'],'open')

    def test_same_branch_name_different_projects_and_cross_branch_targets_rejected(self):
        chat,a,branch_a,question_a,_=self.seed()
        other,b,branch_b,question_b,options_b=self.seed('Kitsune')
        self.assertNotEqual(branch_a['entity_id'],branch_b['entity_id'])
        self.analyse('Выбираю мягкий голос.',chat,entries=[{'kind':'choice','quote':'Выбираю мягкий голос.',
            'targets':[question_b['entity_id'],options_b[1]['entity_id']]}])
        self.assertEqual(self.service.store.growth.passport(question_b['id'])['status'],'open')
        graph=self.service.get('graph',{'project_id':[a['id']]})
        self.assertNotIn(branch_b['id'],{n['id'] for n in graph['nodes']})
        with self.assertRaises(brain.BrainError):
            self.service.get('graph',{'project_id':[a['id']],'focus':[question_b['id']]})

    def test_ambiguous_project_does_not_create_global_branch(self):
        chat,*_=self.seed();self.seed('Kitsune')
        self.analyse('Sakura и Kitsune. Ветка Камера. Какую камеру выбрать?',chat,
            branch={'name':'Камера','quote':'Ветка Камера.'},entries=[{'kind':'question','quote':'Какую камеру выбрать?'}])
        graph=self.service.get('graph',{})
        self.assertFalse(any(n['title']=='Камера' for n in graph['nodes']))

    def test_grounding_and_uncertain_material_cannot_be_promoted_to_facts(self):
        original='Ветка Голос. Возможно, низкий тембр подойдёт. Какой голос выбрать?'
        record={'items':[{'quote':'Возможно, низкий тембр подойдёт.','kind':'fact'}],
            'progress':[{'kind':'done','quote':'Возможно, низкий тембр подойдёт.'}],
            'development':{'branch':{'name':'Придуманная тема','quote':'Ветка Голос.'},'entries':[
                {'kind':'hypothesis','quote':'Возможно, низкий тембр подойдёт.'},
                {'kind':'question','quote':'Какой голос выбрать?'},{'kind':'lesson','quote':'Всё уже работает.'},
                {'kind':[],'quote':'Ветка Голос.'}]}}
        parsed=brain.validate_analysis(json.dumps(record),original)
        self.assertIsNone(parsed['development']['branch'])
        self.assertEqual([e['kind'] for e in parsed['development']['entries']],['hypothesis','question'])
        self.assertEqual(parsed['items'],[]);self.assertEqual(parsed['progress'],[])
        secret='token=secretsecret'
        parsed=brain.validate_analysis(json.dumps({'items':[],'development':{'entries':[{'kind':'question','quote':secret}]}}),secret)
        self.assertEqual(parsed['development']['entries'],[])

    def test_questions_manual_resolution_pause_and_tombstone(self):
        chat,_,branch,question,_=self.seed()
        self.service.post('development-entry',{'id':question['entity_id'],'action':'resolve'})
        self.assertEqual(self.service.store.growth.passport(question['id'])['status'],'resolved')
        self.service.post('development-entry',{'id':question['entity_id'],'action':'reopen'})
        self.assertEqual(self.service.store.growth.passport(question['id'])['status'],'open')
        self.service.post('development-branch',{'id':branch['entity_id'],'status':'paused'})
        self.assertIsNone(self.service.store.growth.analysis_context(chat,'Продолжай'))
        self.service.post('development-branch',{'id':branch['entity_id'],'status':'active'})
        self.assertEqual(self.service.store.growth.analysis_context(chat,'Продолжай')['id'],branch['entity_id'])
        self.service.post('development-entry',{'id':question['entity_id'],'action':'delete'})
        source=self.service.store.messages(chat)[0]['id']
        self.service.store.enqueue(source);self.service.process_one()
        self.assertFalse(any(n['type']=='question' for n in self.service.get('graph',{})['nodes']))

    def test_trace_records_actual_context_detects_edits_and_omits_deleted_nodes(self):
        text='Мне нужен русский интерфейс.'
        first=self.analyse(text,items=[{'kind':'preference','quote':text}])
        memory=self.service.store.memories()[0]
        self.turn('Какой интерфейс мне нужен?',first)
        reply=self.service.store.messages(first)[-1]
        result=self.service.get('graph',{'reply_id':[reply['id']]})
        self.assertIn('memory:'+memory['id'],result['trace']['node_ids'])
        self.assertEqual(result['trace']['changed_memories'],0)
        with self.service.store.connection() as db:
            trace=json.loads(db.execute('SELECT data FROM reply_traces WHERE reply_id=?',(reply['id'],)).fetchone()[0])
            self.assertEqual(trace['memory_ids'],reply['used_memory'])
            self.assertNotIn(text,json.dumps(trace,ensure_ascii=False))
            self.assertEqual(db.execute('SELECT count FROM memory_usage WHERE memory_id=?',(memory['id'],)).fetchone()[0],1)
        self.service.store.edit_memory({'id':memory['id'],'content':'Мне нужен английский интерфейс.','kind':'preference'})
        self.assertEqual(self.service.get('graph',{'reply_id':[reply['id']]})['trace']['changed_memories'],1)
        self.service.store.delete_memory(memory['id'])
        trace=self.service.get('graph',{'reply_id':[reply['id']]})['trace']
        self.assertNotIn('memory:'+memory['id'],trace['node_ids'])
        self.assertGreaterEqual(trace['missing_nodes'],1)

    def test_trace_contains_project_progress_and_development_only_when_sent(self):
        chat,project,branch,question,_=self.seed()
        self.turn('Продолжай с голосом',chat)
        reply=self.service.store.messages(chat)[-1]
        graph=self.service.get('graph',{'reply_id':[reply['id']]})
        self.assertIn(branch['id'],graph['trace']['node_ids'])
        self.assertIn(question['id'],graph['trace']['node_ids'])
        self.assertIn('project:'+project['id'],graph['trace']['node_ids'])
        self.assertIn('Текущая ветка развития',json.dumps(self.cloud.calls[-1],ensure_ascii=False))

    def test_oversized_auxiliary_block_is_not_claimed_in_trace(self):
        chat,project,branch,question,_=self.seed()
        huge={'id':branch['entity_id'],'name':'Голос','entries':[{'id':question['entity_id'],'kind':'question','quote':'x'*8000}]}
        with patch.object(self.service.store.growth,'analysis_context',return_value=huge):
            self.turn('Продолжай',chat)
        reply=self.service.store.messages(chat)[-1]
        trace=self.service.get('graph',{'reply_id':[reply['id']]})['trace']
        self.assertNotIn(branch['id'],trace['node_ids'])
        self.assertNotIn(question['id'],trace['node_ids'])
        prompt=json.dumps(self.cloud.calls[-1],ensure_ascii=False)
        self.assertNotIn('x'*8000,prompt)

    def test_repeat_request_does_not_duplicate_trace_or_usage(self):
        self.analyse('Мне нужны краткие ответы.',items=[{'kind':'preference','quote':'Мне нужны краткие ответы.'}])
        chat,request,_=self.turn('Как отвечать?')
        self.turn('Как отвечать?',chat,request)
        with self.service.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reply_traces').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count FROM memory_usage').fetchone()[0],1)

    def similar(self):
        quotes=['Я предпочитаю короткие ответы без длинных вступлений.',
                'Я предпочитаю короткие ответы без длинных вступлений и повторов.']
        for quote in quotes:self.analyse(quote,items=[{'kind':'preference','quote':quote,'slot':''}])
        result=self.service.get('consolidation',{})
        suggestion=next(s for s in result['suggestions'] if s['kind']=='similar' and s['status']=='pending')
        return suggestion,quotes

    def test_consolidation_group_is_reversible_preserves_sources_and_checks_equivalence(self):
        suggestion,quotes=self.similar();before=len(self.cloud.calls)
        self.service.post('suggestion',{'id':suggestion['id'],'action':'group'})
        self.assertEqual(len(self.service.store.memories()),2)
        self.assertTrue(all(m['sources'] for m in self.service.store.memories()))
        self.assertEqual(len(self.service.store.retrieve('Что я предпочитаю?')),1)
        checks=self.service.state()['exams']
        self.assertEqual((checks['total'],checks['passed']),(2,2))
        self.assertTrue(any(c['grouped'] for c in checks['details']))
        self.service.post('suggestion',{'id':suggestion['id'],'action':'undo'})
        self.assertEqual(len(self.service.store.retrieve('Что я предпочитаю?')),2)
        self.assertEqual(len(self.cloud.calls),before)

    def test_edit_invalidates_group_and_stale_suggestion_cannot_apply(self):
        suggestion,_=self.similar()
        first=suggestion['first']
        self.service.store.edit_memory({'id':first['id'],'content':'Теперь я предпочитаю подробные ответы.','kind':'preference'})
        with self.assertRaises(brain.BrainError) as error:self.service.post('suggestion',{'id':suggestion['id'],'action':'group'})
        self.assertEqual(error.exception.status,409)
        self.assertEqual(len(self.service.store.retrieve('Что предпочитаю?')),2)

    def test_group_is_removed_when_member_is_edited(self):
        suggestion,_=self.similar()
        self.service.post('suggestion',{'id':suggestion['id'],'action':'group'})
        self.service.store.edit_memory({'id':suggestion['first']['id'],'content':'Мне нужны подробные ответы.','kind':'preference'})
        with self.service.store.connection() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM memory_links').fetchone()[0],0)

    def test_same_quotes_in_different_projects_are_never_grouped(self):
        for name in ('Sakura','Kitsune'):
            text=f'Проект {name}. Осталось: добавить русский голос.'
            self.analyse(text,project={'name':name,'quote':f'Проект {name}.'},items=[{'kind':'task','scope':'project','quote':'Осталось: добавить русский голос.'}])
        self.assertEqual(self.service.get('consolidation',{})['suggestions'],[])

    def test_conflict_suggestion_and_user_resolution(self):
        for text in ('Мне нужен русский интерфейс.','Мне нужен английский интерфейс.'):
            self.analyse(text,items=[{'kind':'preference','slot':'interface.language','quote':text}])
        suggestion=next(s for s in self.service.get('consolidation',{})['suggestions'] if s['kind']=='conflict')
        self.assertEqual(suggestion['status'],'pending')
        row=self.service.store.memories(identities=[suggestion['first_id']])[0]
        self.service.store.edit_memory({'id':row['id'],'content':row['content'],'kind':row['kind'],'tier':row['tier']})
        self.assertFalse(any(s['kind']=='conflict' and s['status']=='pending' for s in self.service.get('consolidation',{})['suggestions']))

    def make_old(self):
        text='Мне нужны короткие ответы.'
        chat=self.analyse(text,items=[{'kind':'preference','quote':text}])
        row=self.service.store.memories()[0]
        old=(datetime.now(timezone.utc)-timedelta(days=200)).isoformat()
        with self.service.store.connection(write=True) as db:db.execute('UPDATE memories SET created=?,updated=? WHERE id=?',(old,old,row['id']))
        self.service.post('consolidate',{})
        suggestion=next(s for s in self.service.get('consolidation',{})['suggestions'] if s['kind']=='archive')
        return chat,row,suggestion

    def test_archive_is_only_suggested_and_user_can_restore_version(self):
        chat,row,suggestion=self.make_old()
        self.assertEqual(self.service.store.memories()[0]['tier'],'long')
        self.service.post('suggestion',{'id':suggestion['id'],'action':'archive'})
        self.assertEqual(self.service.store.memories()[0]['tier'],'archive')
        history=self.service.store.learning.versions(row['id'])
        previous=next(v for v in history if v['reason']=='До переноса в архив из сна памяти')
        self.service.post('restore-version',{'id':previous['id']})
        self.assertEqual(self.service.store.memories()[0]['tier'],'long')

    def test_recent_use_invalidates_archive_suggestion(self):
        chat,row,suggestion=self.make_old()
        self.turn('Как мне отвечать?',chat)
        with self.assertRaises(brain.BrainError) as error:self.service.post('suggestion',{'id':suggestion['id'],'action':'archive'})
        self.assertEqual(error.exception.status,409)
        self.service.post('consolidate',{})
        self.assertFalse(any(s['kind']=='archive' and s['status']=='pending' for s in self.service.get('consolidation',{})['suggestions']))

    def test_manual_and_core_memories_are_not_archive_candidates(self):
        _,row,_=self.make_old()
        self.service.store.edit_memory({'id':row['id'],'content':row['content'],'kind':row['kind'],'tier':'core'})
        with self.service.store.connection(write=True) as db:db.execute("UPDATE memories SET created='2000-01-01T00:00:00+00:00',updated='2000-01-01T00:00:00+00:00'")
        self.service.post('consolidate',{})
        self.assertFalse(any(s['kind']=='archive' and s['status']=='pending' for s in self.service.get('consolidation',{})['suggestions']))

    def test_local_automatic_schedule_can_be_disabled_and_manual_run_uses_no_cloud(self):
        self.service.settings({'auto_consolidation':False})
        self.assertFalse(self.service.store.growth.consolidate(force=True))
        self.assertTrue(self.service.store.growth.consolidate(manual=True))
        self.assertFalse(self.service.store.growth.consolidate())
        self.assertEqual(self.cloud.calls,[])
        self.service.settings({'auto_consolidation':True})
        self.assertFalse(self.service.store.growth.consolidate())
        with self.service.store.connection(write=True) as db:db.execute("UPDATE consolidation_state SET last_run='2000-01-01T00:00:00+00:00'")
        self.assertTrue(self.service.store.growth.consolidate())

    def test_consolidation_failure_does_not_undo_saved_analysis(self):
        with patch.object(self.service.store.growth,'consolidate',side_effect=sqlite3.OperationalError('busy')):
            self.analyse('Мне нужен русский интерфейс.',items=[{'kind':'preference','quote':'Мне нужен русский интерфейс.'}])
        self.assertEqual(self.service.state()['jobs'][0]['status'],'done')
        self.assertIn('недоступна',self.service.state()['development']['error'])

    def test_source_deletion_removes_graph_history_and_unconfirmed_branch(self):
        chat,project,branch,question,options=self.seed()
        self.assertTrue(self.service.get('growth-history',{})['events'])
        self.service.store.delete_chat(chat)
        self.assertEqual([n['id'] for n in self.service.get('graph',{})['nodes']],['brain'])
        self.assertEqual(self.service.get('growth-history',{})['events'],[])
        self.assertEqual(self.service.get('graph',{})['replies'],[])

    def test_invalid_new_identifiers_actions_and_nonexistent_nodes_are_rejected(self):
        for route,query in [('graph',{'project_id':['bad']}),('graph',{'reply_id':['../']}),('passport',{'node':['memory:bad']}),
                            ('passport',{'node':['settings:abc']}),('growth-history',{'project_id':[['bad']]})]:
            with self.subTest(route=route),self.assertRaises(brain.BrainError):self.service.get(route,query)
        for route,payload in [('suggestion',{'id':[],'action':'group'}),('development-entry',{'id':'bad','action':'delete'}),
                              ('development-branch',{'id':'bad','status':'active'})]:
            with self.subTest(route=route),self.assertRaises(brain.BrainError):self.service.post(route,payload)
        with self.assertRaises(brain.BrainError):self.service.get('passport',{'node':['memory:'+uuid.uuid4().hex]})

    def test_v2_upgrade_backs_up_once_and_preserves_keys_legacy_traces_and_usage(self):
        self.analyse('Мне нужен русский интерфейс.',items=[{'kind':'preference','quote':'Мне нужен русский интерфейс.'}])
        self.turn('Какой интерфейс мне нужен?')
        key=(self.root/'secret.json').read_bytes()
        self.service.close()
        with closing(sqlite3.connect(self.service.store.path)) as db:
            for name in ('development_branches','branch_sources','development_entries','development_relations','branch_progress',
                'development_tombstones','reply_traces','memory_usage','growth_events','memory_links','consolidation_suggestions','consolidation_state'):
                db.execute('DROP TABLE '+name)
            db.execute('PRAGMA user_version=2')
        reopened=brain.BrainService(self.root,self.cloud.factory,start_worker=False);self.addCleanup(reopened.close)
        self.assertEqual((self.root/'secret.json').read_bytes(),key)
        backups=list((self.root/'backups').glob('before-v3-*.sqlite3'));self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
        graph=reopened.get('graph',{});self.assertEqual(len(graph['replies']),2)
        trace=reopened.get('graph',{'reply_id':[graph['replies'][0]['reply_id']]})['trace'];self.assertTrue(trace['legacy'])
        memory=reopened.store.memories()[0]
        self.assertEqual(reopened.store.growth.passport('memory:'+memory['id'])['usage']['count'],1)
        again=brain.BrainService(self.root,self.cloud.factory,start_worker=False);self.addCleanup(again.close)
        self.assertEqual(len(list((self.root/'backups').glob('before-v3-*.sqlite3'))),1)

    def test_focusing_old_branch_loads_its_older_entries(self):
        chat,project,branch,question,_=self.seed()
        for i in range(52):
            name='Направление'+str(i)
            self.analyse('Ветка '+name+'. Что проверить дальше?',chat,
                branch={'name':name,'quote':'Ветка '+name+'.'},entries=[{'kind':'question','quote':'Что проверить дальше?'}])
        graph=self.service.get('graph',{'focus':[branch['id']]})
        self.assertIn(question['id'],{node['id'] for node in graph['nodes']})
        self.assertTrue(any(edge['source']==branch['id'] and edge['target']==question['id'] for edge in graph['edges']))

    def test_consolidation_candidates_and_map_are_bounded(self):
        self.service.settings({'auto_consolidation':False})
        for i in range(205):self.service.store.edit_memory({'content':'Я предпочитаю короткие ответы без вступлений '+str(i),'kind':'preference'})
        self.service.post('consolidate',{})
        suggestions=self.service.get('consolidation',{})['suggestions']
        self.assertLessEqual(len(suggestions),60)
        graph=self.service.get('graph',{})
        self.assertTrue(graph['bounded']);self.assertLessEqual(len(graph['nodes']),240)
        ids={n['id'] for n in graph['nodes']}
        self.assertTrue(all(e['source'] in ids and e['target'] in ids for e in graph['edges']))


if __name__=='__main__':unittest.main()
