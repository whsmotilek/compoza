import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from .main import create_app,connect,password_hash
from .cli import backup,restore_check

ORIGIN='https://pilot.example.test'
class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'data.sqlite'
        self.app=create_app(self.path,[ORIGIN]);self.client=TestClient(self.app,base_url=ORIGIN)
        with connect(self.path) as c:
            for i,role in enumerate(['owner','admin','designer','designer','foreman','master'],1):
                c.execute('INSERT INTO users(id,username,name,role,password_hash) VALUES(?,?,?,?,?)',(i,role+str(i),role,role,password_hash('unit-test-only-password')))
            c.execute("INSERT INTO projects(id,name,city,type,designer_id,foreman_id) VALUES(1,'Test A','Москва','staging',3,5)")
            c.execute("INSERT INTO projects(id,name,city,type,designer_id) VALUES(2,'Test B','Петербург','concrete',4)")
            c.execute('INSERT INTO members VALUES(1,6)')
            c.execute("INSERT INTO tasks(id,project_id,title,assignee_id) VALUES(1,1,'Own task',6)")
            c.execute("INSERT INTO tasks(id,project_id,title,assignee_id) VALUES(2,1,'Other task',3)")
    def tearDown(self):self.tmp.cleanup()
    def login(self,username='owner1',password='unit-test-only-password',client=None):
        client=client or self.client
        token=client.get('/api/auth/csrf').json()['csrf_token']
        r=client.post('/api/auth/login',json={'username':username,'password':password},headers={'Origin':ORIGIN,'X-CSRF-Token':token})
        if r.status_code==200:self.csrf=r.json()['csrf_token']
        return r
    def headers(self):return {'Origin':ORIGIN,'X-CSRF-Token':self.csrf}
    def test_cookie_login_logout_and_revocation(self):
        r=self.login();self.assertEqual(r.status_code,200)
        self.assertIn('HttpOnly',r.headers.get('set-cookie'));self.assertIn('Secure',r.headers.get('set-cookie'))
        old=dict(self.client.cookies)
        self.assertEqual(self.client.get('/api/auth/me').status_code,200)
        self.assertEqual(self.client.post('/api/auth/logout',headers=self.headers()).status_code,200)
        self.client.cookies.update(old)
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)
    def test_csrf_and_origin(self):
        self.login()
        for headers in [{},{'Origin':ORIGIN},{'Origin':'https://evil.test','X-CSRF-Token':self.csrf}]:
            self.assertEqual(self.client.post('/api/auth/logout',headers=headers).status_code,403)
    def test_account_lock_persists_across_restart(self):
        for _ in range(5):self.assertEqual(self.login('owner1','incorrect').status_code,401)
        other=TestClient(create_app(self.path,[ORIGIN]),base_url=ORIGIN)
        self.assertEqual(self.login(client=other).status_code,429)
    def test_ip_lock_not_bypassed_by_account_names_or_forwarded_header(self):
        for i in range(20):self.assertEqual(self.login('missing'+str(i),'wrong').status_code,401)
        self.assertEqual(self.login('new-account','wrong').status_code,429)
    def test_generic_credentials_error(self):
        a=self.login('owner1','bad');b=self.login('missing','bad')
        self.assertEqual(a.json(),b.json())
    def test_designer_cannot_read_foreign_project_or_audit_or_users(self):
        self.login('designer3')
        self.assertEqual([p['id'] for p in self.client.get('/api/projects').json()['items']],[1])
        for url,status in [('/api/projects/2',404),('/api/audit',403),('/api/users',403)]:self.assertEqual(self.client.get(url).status_code,status)
        self.assertEqual(self.client.patch('/api/tasks/1',headers=self.headers(),json={'version':1,'status':'done'}).status_code,403)
    def test_master_only_own_tasks_and_submission(self):
        self.login('master6')
        self.assertEqual([t['id'] for t in self.client.get('/api/tasks').json()['items']],[1])
        self.assertEqual(self.client.get('/api/projects/1').json()['members'],[])
        self.assertEqual(self.client.patch('/api/tasks/2',headers=self.headers(),json={'version':1,'status':'review'}).status_code,404)
        self.assertEqual(self.client.patch('/api/tasks/1',headers=self.headers(),json={'version':1,'status':'done'}).status_code,403)
        self.assertEqual(self.client.patch('/api/tasks/1',headers=self.headers(),json={'version':1,'status':'review'}).status_code,403)
        self.assertEqual(self.client.post('/api/tasks',headers=self.headers(),json={'project_id':1,'title':'x'}).status_code,403)
    def test_admin_and_foreman_boundaries(self):
        self.login('admin2');self.assertEqual(self.client.get('/api/users').status_code,403)
        self.login('foreman5');self.assertEqual(self.client.get('/api/projects/2').status_code,404)
    def test_expired_session_and_disabled_user(self):
        self.login()
        with connect(self.path) as c:c.execute('UPDATE sessions SET expires=0')
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)
        self.login()
        with connect(self.path) as c:c.execute('UPDATE users SET active=0 WHERE id=1')
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)
    def test_task_validation_lock_and_atomic_audit(self):
        self.login()
        r=self.client.post('/api/tasks',headers=self.headers(),json={'project_id':1,'title':'Delivery','category':'delivery','note':'Call first','due_date':'2026-12-01','assignee_id':6})
        self.assertEqual(r.status_code,201);tid=r.json()['id']
        self.assertEqual(self.client.patch('/api/tasks/'+str(tid),headers=self.headers(),json={'version':1,'status':'in_progress'}).status_code,200)
        self.assertEqual(self.client.patch('/api/tasks/'+str(tid),headers=self.headers(),json={'version':1,'status':'done'}).status_code,409)
        with connect(self.path) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM audit WHERE entity_id=? AND entity='task'",(tid,)).fetchone()[0],2)
            with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM audit')
        self.assertEqual(self.client.post('/api/tasks',headers=self.headers(),json={'project_id':1,'title':'','assignee_id':4}).status_code,422)
        self.assertEqual(self.client.post('/api/tasks',headers=self.headers(),json={'project_id':1,'title':'x','assignee_id':4}).status_code,422)
    def test_backup_restored_copy_is_independent(self):
        self.login();dest=Path(self.tmp.name)/'backup.sqlite';restored=Path(self.tmp.name)/'restored.sqlite'
        backup(self.path,dest);result=restore_check(dest,restored)
        self.assertEqual(result['integrity'],'ok');self.assertEqual(result['users'],6)
        with connect(restored) as c:c.execute("UPDATE projects SET name='restored only' WHERE id=1")
        with connect(self.path) as c:self.assertEqual(c.execute('SELECT name FROM projects WHERE id=1').fetchone()[0],'Test A')
    def test_master_cannot_reopen_submitted_or_done(self):
        self.login('master6')
        for status in ('review','done'):
            with connect(self.path) as c:c.execute('UPDATE tasks SET status=? WHERE id=1',(status,))
            self.assertEqual(self.client.patch('/api/tasks/1',headers=self.headers(),json={'version':1,'status':'in_progress'}).status_code,403)
    def test_user_management_and_last_owner(self):
        self.login()
        self.assertEqual(self.client.patch('/api/users/1',headers=self.headers(),json={'active':False}).status_code,409)
        r=self.client.post('/api/users',headers=self.headers(),json={'username':'new.designer','name':'Test Designer','role':'designer','password':'long-test-password-123'})
        self.assertEqual(r.status_code,201)
        self.assertNotIn('password_hash',r.json())
        uid=r.json()['id']
        self.assertEqual(self.client.patch('/api/users/'+str(uid),headers=self.headers(),json={'role':'master','active':False}).status_code,200)
        self.assertEqual(self.login('new.designer','long-test-password-123').status_code,401)
    def test_password_change_revokes_sessions_and_persistent_limit(self):
        self.login()
        for _ in range(5):
            r=self.client.post('/api/auth/password',headers=self.headers(),json={'old_password':'wrong','password':'long-test-password-123'})
            self.assertEqual(r.status_code,403)
        self.assertEqual(self.client.post('/api/auth/password',headers=self.headers(),json={'old_password':'unit-test-only-password','password':'long-test-password-123'}).status_code,429)
        with connect(self.path) as c:c.execute("DELETE FROM attempts WHERE kind='password'")
        self.assertEqual(self.client.post('/api/auth/password',headers=self.headers(),json={'old_password':'unit-test-only-password','password':'long-test-password-123'}).status_code,200)
        self.assertEqual(self.client.get('/api/auth/me').status_code,401)
        self.assertEqual(self.login('owner1','long-test-password-123').status_code,200)
    def test_project_create_assignments_and_update(self):
        self.login()
        r=self.client.post('/api/projects',headers=self.headers(),json={'name':'Test New','city':'Москва','type':'whitebox','designer_id':3,'foreman_id':5,'member_ids':[6],'access_note':'Gate code demo'})
        self.assertEqual(r.status_code,201);pid=r.json()['id']
        self.assertEqual(self.client.patch('/api/projects/'+str(pid),headers=self.headers(),json={'status':'paused','pause_reason':'Waiting for client','member_ids':[]}).status_code,200)
        self.login('master6');self.assertEqual(self.client.get('/api/projects/'+str(pid)).status_code,404)
        self.assertEqual(self.client.post('/api/projects',headers=self.headers(),json={'name':'X','city':'M','type':'staging'}).status_code,403)
    def test_schema_one_upgrade(self):
        path=Path(self.tmp.name)/'old.sqlite'
        with connect(path) as c:c.executescript(Path(__file__).with_name('schema.sql').read_text())
        create_app(path,[ORIGIN])
        with connect(path) as c:
            self.assertEqual(c.execute('SELECT max(version) FROM schema_version').fetchone()[0],4)
            self.assertIn('note',[r[1] for r in c.execute('PRAGMA table_info(tasks)')])
    def test_cors_exact_origin(self):
        r=self.client.options('/api/auth/login',headers={'Origin':'https://evil.test','Access-Control-Request-Method':'POST'})
        self.assertNotIn('access-control-allow-origin',r.headers)
if __name__=='__main__':unittest.main()
