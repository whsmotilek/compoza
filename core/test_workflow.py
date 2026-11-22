import hashlib
import io
import json
import sqlite3
import tarfile
from pathlib import Path
from fastapi.testclient import TestClient
from PIL import Image
from .test_core import CoreTests,ORIGIN
from .main import connect,create_app,password_hash,migrate
from .archive import full_backup,verify_archive,cleanup_orphans

class WorkflowTests(CoreTests):
    # Inherit fixture helpers, not the unrelated test methods (removed below).
    def setUp(self):
        super().setUp()
        with connect(self.path) as c:
            c.execute("INSERT INTO users VALUES(7,'master7','Second master','master',?,1)",(password_hash('unit-test-only-password'),))
            c.execute('INSERT INTO members VALUES(1,7)')
        image=Image.new('RGB',(80,60),'#5B2824');buf=io.BytesIO();image.save(buf,format='JPEG');self.jpeg=buf.getvalue()
    def post(self,url,data):return self.client.post(url,headers=self.headers(),json=data)
    def team(self):
        self.login();r=self.post('/api/tasks/1/team',{'version':1,'executor_ids':[6,7],'reviewer_id':3});self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.patch('/api/tasks/1',headers=self.headers(),json={'version':2,'status':'in_progress'}).status_code,200)
    def upload(self,tid=1,data=None):
        return self.client.post(f'/api/tasks/{tid}/photos',headers={**self.headers(),'Content-Type':'image/jpeg'},content=self.jpeg if data is None else data)
    def submit(self,version=3,request_id='submission-request-0001'):
        photo=self.upload();self.assertEqual(photo.status_code,201,photo.text)
        body={'version':version,'summary':'Выполнены стены и откосы','photo_ids':[photo.json()['id']],'request_id':request_id}
        r=self.post('/api/tasks/1/submit',body);self.assertEqual(r.status_code,200,r.text);return r.json(),body
    def test_two_executors_full_rework_and_acceptance(self):
        self.team();self.login('master7');self.assertEqual(len(self.client.get('/api/tasks').json()['items']),1)
        self.assertEqual(self.post('/api/tasks/1/comments',{'body':'Комментарий второго исполнителя'}).status_code,201)
        self.assertEqual(self.client.get('/api/tasks/1/materials').status_code,200)
        r,body=self.submit();sid=r['submission_id'];photo=body['photo_ids'][0]
        # Network retry returns the original submission, no duplicate history.
        self.assertEqual(self.post('/api/tasks/1/submit',body).json()['submission_id'],sid)
        self.assertEqual(self.upload().status_code,409)
        self.assertEqual(self.post('/api/tasks/1/photos/'+photo+'/remove',{}).status_code,409)
        self.login('foreman5');review={'version':4,'submission_id':sid,'decision':'rework','remarks':'Исправить стык у окна','reason_kind':'workmanship','request_id':'review-request-000001'}
        self.assertEqual(self.post('/api/tasks/1/review',review).status_code,403)
        self.login('designer3');self.assertEqual(self.post('/api/tasks/1/review',{**review,'remarks':'   '}).status_code,422)
        self.assertEqual(self.post('/api/tasks/1/review',review).status_code,200)
        self.assertEqual(self.post('/api/tasks/1/review',review).status_code,200)
        self.login('master6');r2,body2=self.submit(5,'submission-request-0002');self.assertNotEqual(sid,r2['submission_id'])
        self.login('designer3');self.assertEqual(self.post('/api/tasks/1/review',{'version':6,'submission_id':r2['submission_id'],'decision':'accepted','remarks':'Замечания устранены','request_id':'review-request-000002'}).status_code,200)
        detail=self.client.get('/api/tasks/1/workflow').json();self.assertEqual(detail['task']['status'],'done');self.assertEqual(len(detail['photos']),2);self.assertEqual([s['attempt'] for s in detail['submissions']],[2,1]);self.assertEqual(detail['submissions'][1]['review']['remarks'],review['remarks'])
        self.assertEqual(self.post('/api/tasks/1/reopen',{'version':7,'reason':'Дополнительные работы по решению заказчика'}).status_code,200)
        self.assertEqual(len(self.client.get('/api/tasks/1/workflow').json()['submissions']),2)
        with connect(self.path) as c:
            for query in ['DELETE FROM task_submissions','DELETE FROM task_reviews','DELETE FROM task_photos WHERE submission_id IS NOT NULL']:
                with self.assertRaises(sqlite3.IntegrityError):c.execute(query)
    def test_private_photos_and_immediate_scope_revocation(self):
        self.team();self.login('master7');photo=self.upload().json()['id'];url='/api/photos/'+photo
        self.assertEqual(self.client.get(url).status_code,200);self.assertEqual(self.client.get(url).headers['cache-control'],'no-store')
        self.login('designer4');self.assertEqual(self.client.get(url).status_code,404)
        self.login();self.assertEqual(self.post('/api/tasks/1/team',{'version':3,'executor_ids':[6],'reviewer_id':3}).status_code,200)
        self.login('master7');self.assertEqual(self.client.get(url).status_code,404)
        anon=TestClient(self.app,base_url=ORIGIN);self.assertEqual(anon.get(url).status_code,401)
        self.login('master6');self.assertEqual(self.client.get(url).status_code,200)
        with connect(self.path) as c:c.execute('DELETE FROM members WHERE project_id=1 AND user_id=6')
        self.assertEqual(self.client.get(url).status_code,404)
    def test_photo_validation_csrf_and_cross_task_injection(self):
        self.team();self.login('master6')
        self.assertEqual(self.upload(data=b'not an image').status_code,422)
        self.assertEqual(self.upload(data=b'x'*1_500_001).status_code,413)
        self.assertEqual(self.client.post('/api/tasks/1/photos',headers={'Content-Type':'image/jpeg'},content=self.jpeg).status_code,403)
        self.assertEqual(self.client.post('/api/tasks/1/photos',headers={**self.headers(),'Content-Type':'image/svg+xml'},content=b'<svg/>').status_code,415)
        self.assertEqual(self.upload(2).status_code,404)
        p=self.upload().json()['id'];self.login()
        with connect(self.path) as c:c.execute("UPDATE tasks SET status='in_progress',reviewer_id=5 WHERE id=2")
        self.assertEqual(self.post('/api/tasks/2/submit',{'version':1,'summary':'Other','photo_ids':[p],'request_id':'foreign-photo-00001'}).status_code,422)
    def test_team_assignment_validation_and_stale_writes(self):
        self.login()
        for payload in [dict(executor_ids=[6,4],reviewer_id=3),dict(executor_ids=[6],reviewer_id=6),dict(executor_ids=[6],reviewer_id=4),dict(executor_ids=[6,3],reviewer_id=3)]:
            self.assertEqual(self.post('/api/tasks/1/team',{'version':1,**payload}).status_code,422)
        self.team();self.assertEqual(self.post('/api/tasks/1/team',{'version':1,'executor_ids':[6],'reviewer_id':3}).status_code,409)
        self.login('master7');self.assertEqual(self.post('/api/tasks/1/team',{'version':3,'executor_ids':[6],'reviewer_id':3}).status_code,403)
    def test_submission_requirements_and_self_review(self):
        self.team();self.login('master6')
        payload={'version':3,'summary':'  ','photo_ids':['missing'],'request_id':'submission-bad-0001'}
        self.assertEqual(self.post('/api/tasks/1/submit',payload).status_code,422)
        r,body=self.submit();self.login('owner1')
        self.assertEqual(self.client.patch('/api/tasks/1',headers=self.headers(),json={'version':4,'status':'done'}).status_code,403)
        self.assertEqual(self.client.patch('/api/tasks/1',headers=self.headers(),json={'version':4,'title':'change result'}).status_code,409)
        self.login('master6');self.assertEqual(self.post('/api/tasks/1/review',{'version':4,'submission_id':r['submission_id'],'decision':'accepted','request_id':'review-self-00000001'}).status_code,403)
        self.login('designer3');self.assertEqual(self.post('/api/tasks/1/review',{'version':3,'submission_id':r['submission_id'],'decision':'accepted','request_id':'review-stale-000001'}).status_code,409)
    def test_full_archive_restores_photos_and_detects_corruption(self):
        self.team();self.login('master6');self.submit()
        root=Path(self.tmp.name);archive=root/'full.tar.gz';uploads=root/'uploads'
        self.assertEqual(full_backup(self.path,uploads,archive)['photos'],1)
        verified=verify_archive(archive);self.assertEqual(verified['photos_verified'],1);self.assertEqual(verified['tables_verified'],17)
        photo=next(uploads.glob('*.jpg'));photo.write_bytes(b'corrupted')
        with self.assertRaises(RuntimeError):full_backup(self.path,uploads,root/'bad.tar.gz')
        self.assertFalse((root/'bad.tar.gz').exists())
        self.assertEqual(verify_archive(archive)['photos_verified'],1)
    def test_concurrent_submissions_only_one_attempt(self):
        from concurrent.futures import ThreadPoolExecutor
        self.team();self.login('master6');pid=self.upload().json()['id']
        payload={'version':3,'summary':'Готово','photo_ids':[pid]}
        clients=[TestClient(self.app,base_url=ORIGIN) for _ in range(2)]
        for client in clients:client.cookies.update(dict(self.client.cookies))
        def send(i):return clients[i].post('/api/tasks/1/submit',headers=self.headers(),json={**payload,'request_id':'concurrent-submit-000'+str(i)}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(send,range(2)))
        self.assertEqual(sorted(codes),[200,409])
        with connect(self.path) as c:self.assertEqual(c.execute('SELECT count(*) FROM task_submissions').fetchone()[0],1)
        for client in clients:client.close()

    def test_text_confirmation_for_access_and_photo_requirement_for_work(self):
        self.team();self.login('master6')
        payload={'version':3,'summary':'Разрешение получено у управляющего','photo_ids':[],'request_id':'text-confirmation-001'}
        self.assertEqual(self.post('/api/tasks/1/submit',payload).status_code,422)
        with connect(self.path) as c:c.execute("UPDATE tasks SET category='access' WHERE id=1")
        self.assertEqual(self.post('/api/tasks/1/submit',payload).status_code,200)

    def test_reviewer_revoked_before_submission(self):
        self.team();self.login('master6');pid=self.upload().json()['id']
        with connect(self.path) as c:c.execute('UPDATE users SET active=0 WHERE id=3')
        payload={'version':3,'summary':'Результат','photo_ids':[pid],'request_id':'revoked-reviewer-0001'}
        self.assertEqual(self.post('/api/tasks/1/submit',payload).status_code,422)
        with connect(self.path) as c:self.assertEqual(c.execute('SELECT status FROM tasks WHERE id=1').fetchone()[0],'in_progress')

    def test_cleanup_keeps_referenced_and_recent_files(self):
        import os,time
        self.team();self.login('master6');self.submit()
        root=Path(self.tmp.name)/'uploads';referenced=next(root.glob('*.jpg'))
        old=root/('a'*48+'.jpg');recent=root/('b'*48+'.jpg')
        old.write_bytes(self.jpeg);recent.write_bytes(self.jpeg)
        timestamp=time.time()-15*86400
        os.utime(old,(timestamp,timestamp));os.utime(referenced,(timestamp,timestamp))
        self.assertEqual(cleanup_orphans(self.path,root)['unreferenced_photos_removed'],1)
        self.assertTrue(referenced.exists());self.assertTrue(recent.exists());self.assertFalse(old.exists())

    def test_schema_three_preserves_existing_data(self):
        self.team()
        with connect(self.path) as c:before=c.execute('SELECT password_hash FROM users ORDER BY id').fetchall();c.execute('DELETE FROM schema_version WHERE version=4')
        migrate(self.path)
        with connect(self.path) as c:self.assertEqual([tuple(r) for r in before],[tuple(r) for r in c.execute('SELECT password_hash FROM users ORDER BY id')]);self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0],2)

# Avoid running inherited tests twice.
for name in list(CoreTests.__dict__):
    if name.startswith('test_') and name not in WorkflowTests.__dict__:setattr(WorkflowTests,name,None)
del CoreTests
if __name__=='__main__':
    import unittest
    unittest.main()
