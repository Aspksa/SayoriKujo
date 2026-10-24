"""Lazy map pages, goal provenance and cursor consistency; no real provider."""
from contextlib import closing
import base64
import json
import sqlite3
import unittest
import uuid

import test_brain as base
import test_growth as growth_tests

brain=base.brain


class MapTests(unittest.TestCase):
    setUp=base.BrainTests.setUp
    turn=base.BrainTests.turn
    analyse=growth_tests.GrowthTests.analyse
    seed=growth_tests.GrowthTests.seed

    def bulk(self,count=80):
        chat,project,branch,question,options=self.seed()
        quotes=[f'Что проверить {index:04d}?' for index in range(count)]
        self.analyse('Ветка Голос.\n'+'\n'.join(quotes),chat,branch={'name':'Голос','quote':'Ветка Голос.'})
        source=self.service.store.messages(chat)[-2]
        with self.service.store.connection(write=True) as db:
            for index,quote in enumerate(quotes):
                db.execute('INSERT INTO development_entries(id,branch_id,message_id,kind,content,digest,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                    (f'{index+1:032x}',branch['entity_id'],source['id'],'question',quote,uuid.uuid4().hex,source['created'],source['created']))
        return chat,project,branch,question,options

    def children(self,parent,limit=24,cursor=None,**extra):
        query={'parent':[parent],'limit':[str(limit)],**{k:[v] for k,v in extra.items()}}
        if cursor:query['cursor']=[cursor]
        return self.service.get('graph-children',query)

    def goal_seed(self,name='Sakura'):
        text=f'Проект {name}. Цель: сделать помощницу. Осталось: добавить поиск.'
        chat=self.analyse(text,project={'name':name,'quote':f'Проект {name}.'},progress=[
            {'kind':'goal','quote':'Цель: сделать помощницу.'},{'kind':'todo','quote':'Осталось: добавить поиск.'}])
        project=next(p for p in self.service.state()['projects'] if p['name']==name)
        goal=next(e for e in project['entries'] if e['kind']=='goal')
        task=next(e for e in project['entries'] if e['kind']=='todo')
        return chat,project,goal,task

    def test_lazy_initial_graph_loads_a_window_and_exposes_real_page_totals(self):
        chat,project,branch,*_=self.bulk(240)
        before=len(self.cloud.calls)
        result=self.service.get('graph',{'lazy':['1']})
        page=next(p for p in result['branch_pages'] if p['parent']==branch['id'])
        self.assertEqual(page['total'],243)
        self.assertTrue(page['has_more'])
        self.assertLess(len(page['node_ids']),50)
        ids={node['id'] for node in result['nodes']}
        self.assertLessEqual(len(ids),240)
        self.assertTrue(all(e['source'] in ids and e['target'] in ids for e in result['edges']))
        self.assertTrue(all('stamp' in node and 'parent_ids' in node for node in result['nodes']))
        self.assertEqual(len(self.cloud.calls),before)

    def test_keyset_pages_cover_every_child_once_including_equal_timestamps(self):
        _,_,branch,*_=self.bulk(71)
        cursor=None;seen=[]
        while True:
            result=self.children(branch['id'],limit=7,cursor=cursor)
            seen+=result['page']['node_ids']
            if not result['page']['has_more']:break
            self.assertTrue(result['page']['cursor']);cursor=result['page']['cursor']
        self.assertEqual(len(seen),74)
        self.assertEqual(len(set(seen)),74)
        with self.service.store.connection() as db:
            expected={'entry:'+r[0] for r in db.execute('SELECT id FROM development_entries WHERE branch_id=?',(branch['entity_id'],))}
        self.assertEqual(set(seen),expected)

    def test_invalid_cursor_limits_and_parent_kinds_are_rejected(self):
        _,_,branch,*_=self.bulk()
        for limit in ('0','49','100','-1','1.2','x'):
            with self.subTest(limit=limit),self.assertRaises(brain.BrainError):
                self.service.get('graph-children',{'parent':[branch['id']],'limit':[limit]})
        for value in ('!','A','a'*1100,base64.urlsafe_b64encode(b'{}').decode().rstrip('=')):
            with self.subTest(cursor=value[:10]),self.assertRaises(brain.BrainError):self.children(branch['id'],cursor=value)
        for parent in ('brain','branch:bad','memory:'+uuid.uuid4().hex):
            with self.assertRaises(brain.BrainError):self.children(parent)

    def test_cursor_cannot_be_reused_for_another_branch_or_goal(self):
        _,_,branch,*_=self.bulk()
        page=self.children(branch['id'],1)['page']
        _,_,other,*_=self.seed('Kitsune')
        with self.assertRaises(brain.BrainError):self.children(other['id'],cursor=page['cursor'])
        _,_,goal,_=self.goal_seed('Momiji')
        with self.assertRaises(brain.BrainError):self.children(branch['id'],cursor=page['cursor'],goal_id=goal['id'])

    def test_changed_or_deleted_child_invalidates_old_cursor(self):
        _,_,branch,question,*_=self.bulk()
        page=self.children(branch['id'],1)['page']
        self.service.post('development-entry',{'id':question['entity_id'],'action':'resolve'})
        with self.assertRaises(brain.BrainError) as error:self.children(branch['id'],cursor=page['cursor'])
        self.assertEqual(error.exception.status,409)
        page=self.children(branch['id'],1)['page']
        self.service.post('development-entry',{'id':question['entity_id'],'action':'delete'})
        with self.assertRaises(brain.BrainError) as error:self.children(branch['id'],cursor=page['cursor'])
        self.assertEqual(error.exception.status,409)
        self.assertNotIn(question['id'],{n['id'] for n in self.children(branch['id'])['nodes']})

    def test_unrelated_project_changes_do_not_invalidate_a_branch_cursor(self):
        _,_,branch,*_=self.bulk()
        page=self.children(branch['id'],1)['page']
        self.seed('Kitsune')
        result=self.children(branch['id'],cursor=page['cursor'])
        self.assertEqual(result['page']['revision'],page['revision'])

    def test_project_scope_applies_to_initial_and_subsequent_pages(self):
        _,a,branch,*_=self.bulk()
        _,b,other,*_=self.seed('Kitsune')
        with self.assertRaises(brain.BrainError):self.children(branch['id'],project_id=b['id'])
        result=self.service.get('graph',{'lazy':['1'],'project_id':[b['id']]})
        self.assertNotIn(branch['id'],{n['id'] for n in result['nodes']})
        self.assertNotIn('project:'+a['id'],{n['id'] for n in result['nodes']})

    def test_project_pages_cover_branches_and_scoped_memories(self):
        _,project,branch,*_=self.seed()
        memory=self.service.store.edit_memory({'content':'Правило проекта Sakura','project_id':project['id']})
        first=self.children('project:'+project['id'],1)
        second=self.children('project:'+project['id'],1,first['page']['cursor'])
        self.assertEqual(set(first['page']['node_ids']+second['page']['node_ids']),{branch['id'],'memory:'+memory})

    def test_usage_and_group_changes_invalidate_project_page_cache(self):
        _,project,*_=self.seed()
        first=self.service.store.edit_memory({'content':'Короткие ответы без лишних вступлений','kind':'preference','project_id':project['id']})
        second=self.service.store.edit_memory({'content':'Короткие ответы без лишних вступлений и повторов','kind':'preference','project_id':project['id']})
        page=self.children('project:'+project['id'],1)['page']
        with self.service.store.connection(write=True) as db:self.service.store.growth.use(db,first,'2099-01-01T00:00:00+00:00')
        with self.assertRaises(brain.BrainError) as error:self.children('project:'+project['id'],cursor=page['cursor'])
        self.assertEqual(error.exception.status,409)
        self.service.post('consolidate',{})
        suggestion=next(s for s in self.service.get('consolidation',{})['suggestions'] if s['kind']=='similar')
        page=self.children('project:'+project['id'],1)['page']
        self.service.post('suggestion',{'id':suggestion['id'],'action':'group'})
        with self.assertRaises(brain.BrainError):self.children('project:'+project['id'],cursor=page['cursor'])

    def test_retained_selection_survives_initial_window_shift(self):
        _,_,branch,question,*_=self.bulk(100)
        result=self.service.get('graph',{'lazy':['1'],'retain':[question['id']]})
        self.assertIn(question['id'],{n['id'] for n in result['nodes']})
        self.service.post('development-entry',{'id':question['entity_id'],'action':'delete'})
        result=self.service.get('graph',{'lazy':['1'],'retain':[question['id']]})
        self.assertNotIn(question['id'],{n['id'] for n in result['nodes']})

    def test_goal_path_contains_real_progress_and_explicit_closure_only(self):
        chat,project,goal,task=self.goal_seed()
        self.analyse('Решили: использовать SQLite. Сделано: добавлен поиск.',chat,progress=[
            {'kind':'decision','quote':'Решили: использовать SQLite.'},
            {'kind':'done','quote':'Сделано: добавлен поиск.','resolves':[task['id']]}])
        result=self.service.get('graph',{'lazy':['1'],'goal_id':[goal['id']]})
        self.assertEqual(result['goal_path']['counts'],{'tasks':1,'open_tasks':0,'decisions':1,'done_reports':1})
        self.assertTrue(any(e['kind']=='closes' and e['target']=='progress:'+task['id'] for e in result['edges']))
        self.assertNotIn('completed',result['goal_path'])
        self.assertTrue(all(n.get('project_id') in (None,project['id']) for n in result['nodes']))

    def test_goal_without_completion_does_not_claim_success(self):
        _,_,goal,_=self.goal_seed()
        result=self.service.get('graph',{'lazy':['1'],'goal_id':[goal['id']]})
        self.assertEqual(result['goal_path']['counts']['done_reports'],0)
        self.assertEqual(result['goal_path']['counts']['open_tasks'],1)
        self.assertFalse(any(e['kind']=='closes' for e in result['edges']))

    def test_goal_window_excludes_other_branch_and_later_goal(self):
        chat,project,goal,_=self.goal_seed()
        self.analyse('Ветка Голос. Какой голос выбрать?',chat,branch={'name':'Голос','quote':'Ветка Голос.'},entries=[{'kind':'question','quote':'Какой голос выбрать?'}])
        question=next(n for n in self.service.get('graph',{})['nodes'] if n['type']=='question')
        with self.service.store.connection() as db:
            parent='branch:'+db.execute('SELECT branch_id FROM branch_progress WHERE progress_id=?',(goal['id'],)).fetchone()[0]
        self.analyse('Ветка Развитие проекта. Цель: добавить видео. Осталось: добавить камеру.',chat,
            branch={'name':'Развитие проекта','quote':'Ветка Развитие проекта.'},progress=[
                {'kind':'goal','quote':'Цель: добавить видео.'},{'kind':'todo','quote':'Осталось: добавить камеру.'}])
        result=self.service.get('graph',{'lazy':['1'],'goal_id':[goal['id']]})
        self.assertNotIn(question['id'],{n['id'] for n in result['nodes']})
        self.assertFalse(any('камеру' in n['title'] for n in result['nodes']))
        with self.assertRaises(brain.BrainError):self.service.get('graph',{'lazy':['1'],'goal_id':[goal['id']],'focus':[question['id']]})
        retained=self.service.get('graph',{'lazy':['1'],'goal_id':[goal['id']],'retain':[question['id']]})
        self.assertNotIn(question['id'],{n['id'] for n in retained['nodes']})
        page=self.children(parent,goal_id=goal['id'])
        self.assertFalse(any('камеру' in n['title'] for n in page['nodes']))

    def test_goal_scope_and_reply_path_are_not_mixed(self):
        chat,a,goal,_=self.goal_seed()
        _,b,*_=self.seed('Kitsune')
        with self.assertRaises(brain.BrainError):self.service.get('graph',{'lazy':['1'],'project_id':[b['id']],'goal_id':[goal['id']]})
        reply=self.service.store.messages(chat)[-1]['id']
        with self.assertRaises(brain.BrainError):self.service.get('graph',{'lazy':['1'],'reply_id':[reply],'goal_id':[goal['id']]})

    def test_source_retain_within_goal_has_a_real_grounds_edge(self):
        chat,_,goal,_=self.goal_seed()
        source='message:'+self.service.store.messages(chat)[0]['id']
        result=self.service.get('graph',{'lazy':['1'],'goal_id':[goal['id']],'retain':[source]})
        self.assertTrue(any(e['source']==source and e['kind']=='grounds' for e in result['edges']))

    def test_node_stamp_changes_only_after_saved_data_changes(self):
        _,_,_,question,_=self.seed()
        def stamp():return next(n['stamp'] for n in self.service.get('graph',{'lazy':['1']})['nodes'] if n['id']==question['id'])
        before=stamp();self.assertEqual(stamp(),before)
        self.service.post('development-entry',{'id':question['entity_id'],'action':'resolve'})
        self.assertNotEqual(stamp(),before)

    def test_read_only_map_does_not_change_data_key_or_trigger_provider(self):
        _,_,branch,question,_=self.seed()
        key=(self.root/'secret.json').read_bytes();calls=len(self.cloud.calls)
        with self.service.store.connection() as db:count=db.execute('SELECT COUNT(*) FROM growth_events').fetchone()[0]
        for _ in range(3):
            self.service.get('graph',{'lazy':['1']});self.children(branch['id']);self.service.get('passport',{'node':[question['id']]})
        self.assertEqual((self.root/'secret.json').read_bytes(),key);self.assertEqual(len(self.cloud.calls),calls)
        with self.service.store.connection() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM growth_events').fetchone()[0],count)

    def test_deleted_parent_and_goal_are_not_reconstructed(self):
        chat,project,goal,_=self.goal_seed()
        result=self.service.get('graph',{'lazy':['1'],'goal_id':[goal['id']]})
        parent=result['branch_pages'][0]['parent']
        self.service.store.delete_chat(chat)
        with self.assertRaises(brain.BrainError):self.children(parent)
        with self.assertRaises(brain.BrainError):self.service.get('graph',{'lazy':['1'],'goal_id':[goal['id']]})


if __name__=='__main__':unittest.main()
