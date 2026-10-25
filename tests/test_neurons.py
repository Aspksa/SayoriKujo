"""Real persistence, association semantics, safe automation and bounded memory reads."""
from contextlib import closing
import sqlite3
import unittest
import uuid
import test_brain as base
import test_growth as growth_tests

brain=base.brain


class NeuronTests(unittest.TestCase):
    setUp=base.BrainTests.setUp
    turn=base.BrainTests.turn
    analyse=growth_tests.GrowthTests.analyse

    def memory(self,text='Русский интерфейс сохраняет важные записи',**extra):
        return self.service.store.edit_memory({'content':text,**extra})

    def drain(self):
        for _ in range(100):
            if not self.service.store.neurons.tick():return
        self.fail('Queue failed to drain')

    def pair(self):
        a=self.memory('Русский интерфейс сохраняет важные записи')
        b=self.memory('Русский интерфейс сохраняет важные решения')
        self.drain()
        return a,b

    def test_manual_confirmation_promotes_and_persists_without_provider_calls(self):
        before=len(self.cloud.calls)
        mid=self.memory()
        self.drain()
        self.assertEqual(self.service.store.memories(identities=[mid])[0]['tier'],'vault')
        reopened=brain.BrainService(self.root,self.cloud.factory,start_worker=False);self.addCleanup(reopened.close)
        self.assertEqual(reopened.store.memories(identities=[mid])[0]['tier'],'vault')
        self.assertEqual(len(self.cloud.calls),before)
        self.assertTrue(reopened.store.learning.versions(mid))
        self.assertEqual(reopened.store.neurons.state()['pending'],0)

    def test_three_distinct_user_sources_promote_without_confirmation(self):
        chat=None
        for _ in range(2):
            chat,_,_=self.turn('Мне нужен русский интерфейс.',chat)
            self.service.process_one()
        self.assertEqual(self.service.store.memories()[0]['tier'],'long')
        self.turn('Мне нужен русский интерфейс.',chat);self.service.process_one()
        row=self.service.store.memories()[0]
        self.assertEqual(row['tier'],'vault');self.assertFalse(row['confirmed'])
        self.assertEqual(len(row['sources']),3)

    def test_reanalysis_same_source_does_not_count_as_three(self):
        chat,_,_=self.turn();self.service.process_one()
        mid=self.service.store.messages(chat)[0]['id']
        for _ in range(3):self.service.store.enqueue(mid);self.service.process_one()
        self.assertEqual(self.service.store.memories()[0]['tier'],'long')

    def test_archive_core_and_tasks_are_never_auto_promoted(self):
        ids=[self.memory('Запись '+tier,tier=tier) for tier in ('core','archive')]
        ids.append(self.memory('Задача добавить русский интерфейс',kind='task'))
        self.drain()
        rows={row['id']:row for row in self.service.store.memories()}
        self.assertEqual([rows[mid]['tier'] for mid in ids],['core','archive','long'])
        self.assertNotIn(ids[1],{r['id'] for r in self.service.store.retrieve('Запись архив')})

    def test_explainable_symmetric_links_bounded_and_idempotent(self):
        a,b=self.pair()
        with self.service.store.connection() as db:
            links=self.service.store.neurons.links(db,a)
            self.assertEqual(len(links),1);self.assertEqual(links[0]['id'],b)
            self.assertGreaterEqual(len(links[0]['shared']),3)
            self.assertEqual(links[0]['coactivations'],0)
            self.assertEqual(self.service.store.neurons.links(db,b)[0]['id'],a)
        self.drain();self.assertEqual(self.service.store.neurons.state()['links'],1)
        graph=self.service.get('graph',{'lazy':['1']})
        self.assertTrue(any(e['kind']=='association' and len(e['shared'])>=3 for e in graph['edges']))
        passport=self.service.get('passport',{'node':['memory:'+a]})
        self.assertEqual(passport['neural_links'][0]['id'],b)

    def test_corrected_record_invalidates_link_immediately(self):
        a,b=self.pair();self.memory('Совсем другой текст о погоде',id=a)
        with self.service.store.connection() as db:self.assertEqual(self.service.store.neurons.links(db,b),[])
        self.drain();self.assertEqual(self.service.store.neurons.state()['links'],0)

    def test_deleted_record_cascades_links_and_queue(self):
        a,b=self.pair();self.service.store.delete_memory(a)
        with self.service.store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM synapses').fetchone()[0],0)
            self.assertFalse(db.execute('SELECT 1 FROM neural_queue WHERE memory_id=?',(a,)).fetchone())
        self.assertEqual(self.service.get('passport',{'node':['memory:'+b]})['neural_links'],[])

    def test_project_scope_isolated_from_other_project_and_global(self):
        self.analyse('Проект Sakura.',project={'name':'Sakura','quote':'Проект Sakura.'})
        self.analyse('Проект Kitsune.',project={'name':'Kitsune','quote':'Проект Kitsune.'})
        projects=self.service.state()['projects']
        a=self.memory(project_id=projects[0]['id'])
        b=self.memory('Русский интерфейс сохраняет важные решения',project_id=projects[1]['id'])
        c=self.memory('Русский интерфейс сохраняет важные задачи')
        self.drain();self.assertEqual(self.service.store.neurons.state()['links'],0)
        self.memory('Русский интерфейс сохраняет важные решения',id=b,project_id=projects[0]['id']);self.drain()
        self.assertEqual(self.service.store.neurons.state()['links'],1)
        self.memory('Русский интерфейс сохраняет важные решения',id=b,project_id=projects[1]['id'])
        with self.service.store.connection() as db:self.assertEqual(self.service.store.neurons.links(db,a),[])

    def test_coactivation_counts_only_actual_saved_joint_context(self):
        a,b=self.pair()
        self.turn('Русский интерфейс сохраняет важные записи и решения?')
        with self.service.store.connection() as db:
            link=self.service.store.neurons.links(db,a)[0]
            self.assertEqual(link['coactivations'],1)
        self.service.get('graph',{'lazy':['1']})
        with self.service.store.connection() as db:self.assertEqual(self.service.store.neurons.links(db,a)[0]['coactivations'],1)

    def test_joint_use_includes_memories_beyond_first_twenty_in_trace(self):
        a,b=self.pair()
        with self.service.store.connection(write=True) as db:
            self.service.store.neurons.activate(db,[a,b]+['0'+f'{i:031x}' for i in range(21)])
        with self.service.store.connection() as db:self.assertEqual(self.service.store.neurons.links(db,a)[0]['coactivations'],1)

    def test_reanalysis_preserves_joint_use_strength(self):
        a,b=self.pair()
        with self.service.store.connection(write=True) as db:
            self.service.store.neurons.activate(db,[a,b])
            self.service.store.neurons.enqueue(db,a)
        self.drain()
        with self.service.store.connection() as db:self.assertEqual(self.service.store.neurons.links(db,a)[0]['coactivations'],1)

    def test_degree_bound_and_archive_never_appears_as_association(self):
        ids=[self.memory('Русский интерфейс сохраняет важные записи '+str(i)) for i in range(20)]
        archive=self.memory('Русский интерфейс сохраняет важные записи архив',tier='archive')
        self.drain()
        with self.service.store.connection() as db:
            for mid in ids:self.assertLessEqual(len(self.service.store.neurons.links(db,mid)),12)
            self.assertFalse(db.execute('SELECT 1 FROM synapses WHERE first_id=? OR second_id=?',(archive,archive)).fetchone())

    def test_switches_pause_and_resume_pending_work(self):
        self.service.settings({'auto_neurons':False,'auto_vault':False})
        a=self.memory();self.drain()
        self.assertEqual(self.service.store.memories()[0]['tier'],'long')
        self.assertEqual(self.service.store.neurons.state()['pending'],1)
        self.service.settings({'auto_vault':True});self.drain()
        self.assertEqual(self.service.store.memories()[0]['tier'],'vault')
        self.assertEqual(self.service.store.neurons.state()['pending'],0)
        self.memory('Русский интерфейс сохраняет важные решения');self.drain()
        self.service.settings({'auto_neurons':True});self.drain()
        self.assertEqual(self.service.store.neurons.state()['links'],1)
        for key in ('auto_neurons','auto_vault'):
            with self.assertRaises(brain.BrainError):self.service.settings({key:'yes'})

    def test_queue_fairly_visits_more_than_recent_two_hundred(self):
        for index in range(205):self.memory('Уникальная запись '+str(index))
        self.assertEqual(self.service.store.neurons.tick(limit=7),7)
        self.assertEqual(self.service.store.neurons.state()['pending'],198)
        self.drain();self.assertEqual(self.service.store.neurons.state()['vault'],205)

    def test_paged_memory_keyset_covers_ties_once_and_honors_filters(self):
        for index in range(63):self.memory('Русская запись '+str(index),tier='vault' if index%2 else 'archive')
        with self.service.store.connection(write=True) as db:db.execute("UPDATE memories SET updated='2020-01-01T00:00:00+00:00'")
        page=self.service.get('memories',{'paged':['1']});self.assertEqual(len(page['memories']),50)
        second=self.service.get('memories',{'paged':['1'],'cursor':[page['cursor']]})
        ids=[r['id'] for r in page['memories']+second['memories']]
        self.assertEqual(len(ids),63);self.assertEqual(len(set(ids)),63);self.assertFalse(second['has_more'])
        filtered=self.service.get('memories',{'paged':['1'],'q':['РУССКАЯ'],'tier':['vault']})
        self.assertEqual(len(filtered['memories']),31)
        for cursor in ('!', 'a'*600,page['cursor']):
            with self.assertRaises(brain.BrainError):self.service.get('memories',{'paged':['1'],'tier':['archive'],'cursor':[cursor]})

    def test_page_bounds_source_bodies_without_losing_total(self):
        mid=self.memory()
        chat=self.service.store.create_chat()
        with self.service.store.connection(write=True) as db:
            for i in range(25):
                message=uuid.uuid4().hex
                db.execute("INSERT INTO messages(id,chat_id,role,content,created) VALUES(?,?,'user',?,?)",(message,chat,'Источник '+str(i),'2020-01-01T00:00:00+00:00'))
                db.execute('INSERT INTO sources VALUES(?,?)',(mid,message))
        row=self.service.get('memories',{'paged':['1']})['memories'][0]
        self.assertEqual(row['source_count'],25);self.assertEqual(len(row['sources']),20)
        self.assertEqual(len(self.service.store.memories()[0]['sources']),25)

    def test_old_best_match_is_not_lost_behind_newer_records(self):
        old=self.memory('Астрономия телескоп орбита спектр галактика',tier='vault')
        for i in range(115):self.memory('Астрономия заметка '+str(i))
        with self.service.store.connection(write=True) as db:db.execute("UPDATE memories SET updated='2000-01-01T00:00:00+00:00' WHERE id=?",(old,))
        result=self.service.store.retrieve('Астрономия телескоп орбита спектр галактика')
        self.assertEqual(result[0]['id'],old)

    def test_upgrade_backup_preserves_v3_and_secret_and_reopen_does_not_repeat(self):
        mid=self.memory();key=(self.root/'secret.json').read_bytes()
        self.service.close()
        with closing(sqlite3.connect(self.service.store.path)) as db:
            for table in ('neural_queue','synapses','neural_state'):db.execute('DROP TABLE '+table)
            db.execute('PRAGMA user_version=3')
        reopened=brain.BrainService(self.root,self.cloud.factory,start_worker=False);self.addCleanup(reopened.close)
        backups=list((self.root/'backups').glob('before-v4-*.sqlite3'));self.assertEqual(len(backups),1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT id FROM memories').fetchone()[0],mid)
        self.assertEqual((self.root/'secret.json').read_bytes(),key)
        again=brain.BrainService(self.root,self.cloud.factory,start_worker=False);self.addCleanup(again.close)
        self.assertEqual(len(list((self.root/'backups').glob('before-v4-*.sqlite3'))),1)
        self.assertEqual(again.store.neurons.state()['pending'],1)


if __name__=='__main__':unittest.main()
