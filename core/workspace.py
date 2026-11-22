"""Project context, parallel tracks, evidence links and scoped discussion."""
import json
from datetime import date
from typing import Literal, Optional
from urllib.parse import urlparse
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

TRACKS = [('intake','Приемка и исходные данные'), ('design','Дизайн и согласования'),
          ('works','Работы на объекте'), ('supply','Закупки и поставки'),
          ('handover','Сдача и доработки')]

def ensure_tracks(c,pid):
    for i,(code,title) in enumerate(TRACKS):
        c.execute('INSERT OR IGNORE INTO project_tracks(project_id,code,title,position) VALUES(?,?,?,?)',(pid,code,title,i))

def documents(c,u,pid):
    levels=['owners','design','team'] if u['role']=='owner' else ['design','team'] if u['role']=='designer' else ['team'] if u['role'] in ('foreman','admin') else []
    return [dict(x) for x in c.execute('SELECT * FROM project_documents WHERE project_id=? ORDER BY id DESC',(pid,)) if x['visibility'] in levels]

class TrackPatch(BaseModel):
    status: Literal['planned','active','waiting','done']
    due_date: Optional[date] = None
    waiting_reason: str = Field(default='',max_length=2000)
class DocumentCreate(BaseModel):
    title: str = Field(min_length=1,max_length=200)
    url: str = Field(min_length=1,max_length=2000)
    kind: Literal['reference','drawing','estimate','contract','photo','other'] = 'other'
    visibility: Literal['owners','design','team'] = 'design'
class CommentCreate(BaseModel):
    body: str = Field(min_length=1,max_length=4000)
class TaskMaterial(BaseModel):
    kind: Literal['drawing','reference','photo','instruction'] = 'instruction'
    share_with_assignees: Literal[True]
    title: str = Field(min_length=1,max_length=200)
    url: str = Field(min_length=1,max_length=2000)
class Dependencies(BaseModel):
    version: int = Field(ge=1)
    ids: list[int] = Field(default_factory=list,max_length=50)

def install_routes(app,db,session,csrf,allowed_project,visible_tasks,audit):
    @app.get('/api/documents')
    def all_documents(request:Request):
        with db() as c:
            u=session(request,c);items=[]
            for p in c.execute('SELECT id,name FROM projects').fetchall():
                try:allowed_project(c,u,p['id'])
                except HTTPException as exc:
                    if exc.status_code==404:continue
                    raise
                items.extend({**d,'project_name':p['name']} for d in documents(c,u,p['id']))
            return {'items':items}
    def task(c,u,tid):
        t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        if not t:raise HTTPException(404,'Task not found')
        allowed_project(c,u,t['project_id'])
        if u['role']=='master' and t['assignee_id']!=u['id'] and not c.execute('SELECT 1 FROM task_executors WHERE task_id=? AND user_id=?',(tid,u['id'])).fetchone():raise HTTPException(404,'Task not found')
        return t
    @app.patch('/api/tracks/{tid}')
    def track_update(tid:int,body:TrackPatch,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c)
            tr=c.execute('SELECT * FROM project_tracks WHERE id=?',(tid,)).fetchone()
            if not tr:raise HTTPException(404,'Track not found')
            allowed_project(c,u,tr['project_id'])
            if u['role']=='master':raise HTTPException(403,'Cannot change project tracks')
            if body.status=='waiting' and not body.waiting_reason.strip():raise HTTPException(422,'Waiting reason required')
            c.execute('UPDATE project_tracks SET status=?,due_date=?,waiting_reason=? WHERE id=?',(body.status,body.due_date.isoformat() if body.due_date else None,body.waiting_reason.strip(),tid))
            audit(c,u['id'],'track_update','project',tr['project_id'],{'track':tr['title'],'status':body.status,'reason':body.waiting_reason})
            return dict(c.execute('SELECT * FROM project_tracks WHERE id=?',(tid,)).fetchone())
    @app.post('/api/projects/{pid}/documents',status_code=201)
    def document_create(pid:int,body:DocumentCreate,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c);allowed_project(c,u,pid)
            if u['role']=='master':raise HTTPException(403,'Cannot add project documents')
            if not body.title.strip() or not urlparse(body.url).netloc or urlparse(body.url).scheme!='https':raise HTTPException(422,'HTTPS link required')
            if body.kind in ('estimate','contract') and body.visibility=='team':raise HTTPException(422,'Sensitive document cannot be shared with team')
            if body.visibility=='owners' and u['role']!='owner':raise HTTPException(403,'Owner visibility requires owner')
            if u['role'] in ('foreman','admin') and (body.visibility!='team' or body.kind in ('estimate','contract')):raise HTTPException(403,'Foreman can add operational materials only')
            did=c.execute('INSERT INTO project_documents(project_id,title,url,kind,visibility,actor_id) VALUES(?,?,?,?,?,?)',(pid,body.title.strip(),body.url,body.kind,body.visibility,u['id'])).lastrowid
            audit(c,u['id'],'document_added','project',pid,{'title':body.title,'visibility':body.visibility})
            return {'id':did}
    @app.get('/api/tasks/{tid}/comments')
    def comments(tid:int,request:Request):
        with db() as c:
            u=session(request,c);task(c,u,tid)
            return {'items':[dict(x) for x in c.execute('SELECT tc.*,u.name actor_name FROM task_comments tc JOIN users u ON u.id=tc.actor_id WHERE task_id=? ORDER BY tc.id',(tid,))]}
    @app.get('/api/tasks/{tid}/materials')
    def task_materials(tid:int,request:Request):
        with db() as c:
            u=session(request,c);task(c,u,tid)
            return {'items':[dict(x) for x in c.execute('SELECT * FROM task_materials WHERE task_id=? ORDER BY id',(tid,))]}
    @app.post('/api/tasks/{tid}/materials',status_code=201)
    def add_task_material(tid:int,body:TaskMaterial,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c);task(c,u,tid)
            if u['role']=='master':raise HTTPException(403,'Reviewer must publish task instructions')
            if not body.title.strip() or urlparse(body.url).scheme!='https' or not urlparse(body.url).netloc:raise HTTPException(422,'HTTPS link required')
            mid=c.execute('INSERT INTO task_materials(task_id,title,url,kind,actor_id) VALUES(?,?,?,?,?)',(tid,body.title.strip(),body.url,body.kind,u['id'])).lastrowid
            audit(c,u['id'],'task_material_added','task',tid,{'title':body.title})
            return {'id':mid}
    @app.post('/api/tasks/{tid}/comments',status_code=201)
    def comment_create(tid:int,body:CommentCreate,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c);task(c,u,tid)
            if not body.body.strip():raise HTTPException(422,'Comment required')
            cid=c.execute('INSERT INTO task_comments(task_id,actor_id,body) VALUES(?,?,?)',(tid,u['id'],body.body.strip())).lastrowid
            audit(c,u['id'],'comment_added','task',tid)
            return {'id':cid}
    @app.post('/api/tasks/{tid}/dependencies')
    def dependencies(tid:int,body:Dependencies,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c);c.execute('BEGIN IMMEDIATE');t=task(c,u,tid)
            if u['role']=='master':raise HTTPException(403,'Cannot change dependencies')
            ids=set(body.ids)
            for did in ids:
                d=task(c,u,did)
                if did==tid or d['project_id']!=t['project_id']:raise HTTPException(422,'Dependency must be another task in this project')
            graph={}
            for row in c.execute('SELECT * FROM task_dependencies WHERE task_id<>?',(tid,)):graph.setdefault(row['task_id'],[]).append(row['depends_on_id'])
            graph[tid]=list(ids)
            def reaches(node,seen):
                if node==tid:return True
                if node in seen:return False
                seen.add(node)
                return any(reaches(n,seen) for n in graph.get(node,[]))
            if any(reaches(x,set()) for x in ids):raise HTTPException(422,'Cyclic dependency')
            if t['status'] in ('review','done'):raise HTTPException(409,'Reopen task before changing dependencies')
            if not c.execute('UPDATE tasks SET version=version+1 WHERE id=? AND version=?',(tid,body.version)).rowcount:raise HTTPException(409,'Task changed; refresh')
            c.execute('DELETE FROM task_dependencies WHERE task_id=?',(tid,))
            c.executemany('INSERT INTO task_dependencies VALUES(?,?)',[(tid,d) for d in ids]);audit(c,u['id'],'dependencies_updated','task',tid,{'depends_on':sorted(ids)})
            return dict(c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
