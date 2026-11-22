import unittest
import tempfile
from pathlib import Path
from .test_core import CoreTests
from .main import connect
from .cli import backup,restore_check

class WorkspaceTests(unittest.TestCase):
    setUp=CoreTests.setUp
    tearDown=CoreTests.tearDown
    login=CoreTests.login
    headers=CoreTests.headers

    def test_parallel_tracks_and_waiting_reason(self):
        self.login()
        r=self.client.post('/api/projects',headers=self.headers(),json={'name':'Parallel','city':'Москва','type':'concrete','designer_id':3,'member_ids':[6]})
        pid=r.json()['id'];tracks=self.client.get(f'/api/projects/{pid}').json()['tracks'];self.assertEqual(len(tracks),5)
        for tr in tracks[:2]:
            self.assertEqual(self.client.patch('/api/tracks/'+str(tr['id']),headers=self.headers(),json={'status':'active'}).status_code,200)
        self.assertEqual(self.client.patch('/api/tracks/'+str(tracks[2]['id']),headers=self.headers(),json={'status':'waiting'}).status_code,422)
        self.assertEqual(self.client.patch('/api/tracks/'+str(tracks[2]['id']),headers=self.headers(),json={'status':'waiting','waiting_reason':'Ждем решение клиента'}).status_code,200)
        self.login('master6')
        self.assertEqual(self.client.patch('/api/tracks/'+str(tracks[0]['id']),headers=self.headers(),json={'status':'done'}).status_code,403)

    def test_documents_are_filtered_by_server(self):
        self.login()
        for visibility in ['owners','design','team']:
            self.assertEqual(self.client.post('/api/projects/1/documents',headers=self.headers(),json={'title':visibility,'url':'https://example.com/file','visibility':visibility}).status_code,201)
        self.assertEqual(self.client.post('/api/projects/1/documents',headers=self.headers(),json={'title':'Estimate','url':'https://example.com/file','visibility':'team','kind':'estimate'}).status_code,422)
        self.assertEqual(self.client.post('/api/projects/1/documents',headers=self.headers(),json={'title':'Bad','url':'javascript:alert(1)'}).status_code,422)
        self.login('foreman5')
        detail=self.client.get('/api/projects/1').json();self.assertEqual([x['title'] for x in detail['documents']],['team']);self.assertEqual([x['details']['title'] for x in detail['activity'] if x['action']=='document_added'],['team'])
        self.assertEqual([x['title'] for x in self.client.get('/api/documents').json()['items']],['team'])
        self.login('admin2');self.assertEqual(len(self.client.get('/api/projects/1').json()['documents']),1)
        self.login('designer3');self.assertEqual(len(self.client.get('/api/projects/1').json()['documents']),2)
        self.login('master6');self.assertEqual(self.client.get('/api/projects/1').json()['documents'],[])

    def test_dependencies_no_cycles_or_foreign_projects_and_enforced(self):
        self.login()
        self.assertEqual(self.client.post('/api/tasks/1/dependencies',headers=self.headers(),json={'version':1,'ids':[2]}).status_code,200)
        self.assertEqual(self.client.post('/api/tasks/2/dependencies',headers=self.headers(),json={'version':1,'ids':[1]}).status_code,422)
        self.assertEqual(self.client.post('/api/tasks/1/dependencies',headers=self.headers(),json={'version':1,'ids':[]}).status_code,409)
        self.assertEqual(self.client.patch('/api/tasks/1',headers=self.headers(),json={'version':2,'status':'in_progress'}).status_code,409)
        self.assertEqual(self.client.patch('/api/tasks/2',headers=self.headers(),json={'version':1,'status':'done'}).status_code,200)
        self.assertEqual(self.client.patch('/api/tasks/1',headers=self.headers(),json={'version':2,'status':'in_progress'}).status_code,200)

    def test_comments_and_context_do_not_leak(self):
        self.login();self.client.patch('/api/projects/1',headers=self.headers(),json={'client_name':'Private client','address':'Assigned address'})
        self.client.post('/api/tasks/2/comments',headers=self.headers(),json={'body':'Only project team'})
        self.login('master6')
        self.assertEqual(self.client.get('/api/tasks/2/comments').status_code,404)
        self.assertEqual(self.client.post('/api/tasks/1/comments',headers=self.headers(),json={'body':'Нужен пропуск до выезда'}).status_code,201)
        self.assertEqual(len(self.client.get('/api/tasks/1/comments').json()['items']),1)
        p=self.client.get('/api/projects/1').json()['project'];self.assertNotIn('client_name',p);self.assertNotIn('client_name',self.client.get('/api/projects').json()['items'][0]);self.assertEqual(p['address'],'Assigned address')
        self.login('designer4');self.assertEqual(self.client.get('/api/tasks/1/comments').status_code,404)

    def test_pause_requires_reason_and_history_is_scoped(self):
        self.login()
        self.assertEqual(self.client.patch('/api/projects/1',headers=self.headers(),json={'status':'paused'}).status_code,422)
        self.assertEqual(self.client.patch('/api/projects/1',headers=self.headers(),json={'status':'paused','pause_reason':'Ожидание ключей'}).status_code,200)
        self.login('designer3');detail=self.client.get('/api/projects/1').json()
        self.assertTrue(detail['activity']);self.assertTrue(all(x['entity'] in ('project','task') for x in detail['activity']))
        with connect(self.path) as c:self.assertEqual(c.execute('SELECT max(version) FROM schema_version').fetchone()[0],3)

    def test_reasoned_override_and_assigned_task_materials(self):
        self.login()
        self.assertEqual(self.client.post('/api/tasks/1/dependencies',headers=self.headers(),json={'version':1,'ids':[2]}).status_code,200)
        self.assertEqual(self.client.patch('/api/tasks/1',headers=self.headers(),json={'version':2,'status':'in_progress','override_reason':'Согласовано временное изменение порядка'}).status_code,200)
        data={'title':'Чертеж для работы','url':'https://example.com/instruction','share_with_assignees':True}
        self.assertEqual(self.client.post('/api/tasks/1/materials',headers=self.headers(),json=data).status_code,201)
        self.assertEqual(self.client.post('/api/tasks/1/materials',headers=self.headers(),json={**data,'kind':'estimate'}).status_code,422)
        self.login('master6')
        self.assertEqual(len(self.client.get('/api/tasks/1/materials').json()['items']),1)
        self.assertEqual(self.client.get('/api/tasks/2/materials').status_code,404)
        self.assertEqual(self.client.post('/api/tasks/1/materials',headers=self.headers(),json=data).status_code,403)
        self.assertEqual(self.client.patch('/api/tasks/1',headers=self.headers(),json={'version':3,'status':'review','override_reason':'Не разрешенное мастеру исключение'}).status_code,403)
        self.assertTrue(self.client.get('/api/projects/1').json()['blocked_task_ids'])

    def test_backup_requires_source_and_all_workspace_tables(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with self.assertRaises(FileNotFoundError):backup(root/'missing.sqlite',root/'target.sqlite')
            self.assertFalse((root/'missing.sqlite').exists())
            with connect(self.path) as c:c.execute('DROP TABLE task_comments')
            with self.assertRaises(RuntimeError):restore_check(self.path,root/'incomplete.sqlite')

    def test_paused_workload_and_old_project_history(self):
        self.login()
        self.client.patch('/api/projects/1',headers=self.headers(),json={'status':'paused','pause_reason':'Ждем заказчика'})
        with connect(self.path) as c:
            c.execute("UPDATE tasks SET due_date='2000-01-01'")
            for _ in range(510):c.execute("INSERT INTO audit(actor_id,action,entity,entity_id) VALUES(1,'update','project',2)")
        overview=self.client.get('/api/overview').json()
        self.assertEqual(overview['overdue_tasks'],0);self.assertEqual(overview['paused_projects'],1)
        self.assertTrue(self.client.get('/api/projects/1').json()['activity'])

del CoreTests
if __name__=='__main__':unittest.main()
