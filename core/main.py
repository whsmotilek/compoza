"""Compoza pilot API. Run behind an HTTPS reverse proxy on localhost only."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Literal, Optional
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .workspace import ensure_tracks, documents, install_routes
from .workflow import enrich, install_workflow

ROLES = ('owner', 'admin', 'designer', 'foreman', 'master')
COOKIE = '__Host-compoza_session'
CSRF_COOKIE = '__Host-compoza_csrf'
SESSION_SECONDS = 8 * 3600

def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=5)
    return f'scrypt$16384$8$5${salt.hex()}${digest.hex()}'

def password_ok(password, stored):
    try:
        parts=stored.split('$')
        if len(parts)==3:
            _,salt,expected=parts;n,r,p=16384,8,1
        else:
            _,n,r,p,salt,expected=parts;n,r,p=int(n),int(r),int(p)
        if n!=16384 or r!=8 or p not in (1,5):return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=n, r=r, p=p)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False

def connect(path):
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=10000')
    return c

def migrate(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as c:
        c.executescript(Path(__file__).with_name('schema.sql').read_text())
        version=c.execute('SELECT max(version) FROM schema_version').fetchone()[0]
        if version not in (1,2,3,4):raise RuntimeError('Unsupported schema version')
        if version==1:
            for table,col,definition in [('projects','access_note',"TEXT NOT NULL DEFAULT ''"),('projects','is_demo','INTEGER NOT NULL DEFAULT 0'),('tasks','category',"TEXT NOT NULL DEFAULT 'task'"),('tasks','note',"TEXT NOT NULL DEFAULT ''")]:
                if col not in [r[1] for r in c.execute('PRAGMA table_info('+table+')')]:
                    c.execute('ALTER TABLE '+table+' ADD COLUMN '+col+' '+definition)
            c.execute('INSERT INTO schema_version VALUES(2)')
        if version<3:
            for col,definition in [('address',"TEXT NOT NULL DEFAULT ''"),('client_name',"TEXT NOT NULL DEFAULT ''"),('target_date','TEXT'),('pause_reason',"TEXT NOT NULL DEFAULT ''")]:
                if col not in [r[1] for r in c.execute('PRAGMA table_info(projects)')]:
                    c.execute('ALTER TABLE projects ADD COLUMN '+col+' '+definition)
            c.executescript(Path(__file__).with_name('schema-v3.sql').read_text())
            c.execute('INSERT OR IGNORE INTO schema_version VALUES(3)')
        c.executescript(Path(__file__).with_name('schema-v3.sql').read_text())
        if 'reviewer_id' not in [r[1] for r in c.execute('PRAGMA table_info(tasks)')]:
            c.execute('ALTER TABLE tasks ADD COLUMN reviewer_id INTEGER REFERENCES users(id)')
        c.executescript(Path(__file__).with_name('schema-v4.sql').read_text())
        if 'context' not in [r[1] for r in c.execute('PRAGMA table_info(task_submissions)')]:c.execute("ALTER TABLE task_submissions ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
        if version<4:
            c.execute('INSERT OR IGNORE INTO task_executors SELECT id,assignee_id FROM tasks WHERE assignee_id IS NOT NULL')
            c.execute('INSERT OR IGNORE INTO schema_version VALUES(4)')
        for row in c.execute('SELECT id FROM projects').fetchall():ensure_tracks(c,row['id'])
    path.chmod(0o600)

def audit(c, actor, action, entity, eid=None, details=None):
    c.execute('INSERT INTO audit(actor_id,action,entity,entity_id,details) VALUES(?,?,?,?,?)',
              (actor, action, entity, eid, json.dumps(details or {}, ensure_ascii=False)))

def public_user(row):
    return {k: row[k] for k in ('id','username','name','role')}

class Login(BaseModel):
    username: str = Field(min_length=1,max_length=100)
    password: str = Field(min_length=1,max_length=256)
class TaskCreate(BaseModel):
    project_id: int
    title: str = Field(min_length=1,max_length=300)
    assignee_id: Optional[int] = None
    due_date: Optional[date] = None
    category: Literal['task','delivery','access','approval'] = 'task'
    note: str = Field(default='',max_length=4000)
class TaskPatch(BaseModel):
    override_reason: Optional[str] = Field(default=None,min_length=8,max_length=2000)
    version: int = Field(ge=1)
    title: Optional[str] = Field(default=None,min_length=1,max_length=300)
    status: Optional[Literal['todo','in_progress','review','done']] = None
    assignee_id: Optional[int] = None
    due_date: Optional[date] = None
    category: Optional[Literal['task','delivery','access','approval']] = None
    note: Optional[str] = Field(default=None,max_length=4000)


class UserCreate(BaseModel):
    username: str = Field(min_length=1,max_length=100,pattern=r'^[a-zA-Z0-9_.-]+$')
    name: str = Field(min_length=1,max_length=200)
    role: Literal['owner','admin','designer','foreman','master']
    password: str = Field(min_length=14,max_length=256)
class UserPatch(BaseModel):
    active: Optional[bool] = None
    role: Optional[Literal['owner','admin','designer','foreman','master']] = None
    password: Optional[str] = Field(default=None,min_length=14,max_length=256)
class PasswordChange(BaseModel):
    old_password: str = Field(min_length=1,max_length=256)
    password: str = Field(min_length=14,max_length=256)
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1,max_length=200)
    city: str = Field(min_length=1,max_length=100)
    type: Literal['staging','whitebox','concrete']
    designer_id: Optional[int] = None
    foreman_id: Optional[int] = None
    member_ids: list[int] = Field(default_factory=list,max_length=100)
    access_note: str = Field(default='',max_length=2000)
class ProjectPatch(BaseModel):
    address: Optional[str] = Field(default=None,max_length=500)
    client_name: Optional[str] = Field(default=None,max_length=200)
    target_date: Optional[date] = None
    pause_reason: Optional[str] = Field(default=None,max_length=2000)
    name: Optional[str] = Field(default=None,min_length=1,max_length=200)
    city: Optional[str] = Field(default=None,min_length=1,max_length=100)
    status: Optional[Literal['active','paused','closed']] = None
    designer_id: Optional[int] = None
    foreman_id: Optional[int] = None
    member_ids: Optional[list[int]] = Field(default=None,max_length=100)
    access_note: Optional[str] = Field(default=None,max_length=2000)


def create_app(db_path=None, origins=None, secure=True):
    path = str(db_path or os.environ.get('COMPOZA_DB', '/var/lib/compoza/core.sqlite3'))
    origins = origins or os.environ.get('COMPOZA_ORIGINS','https://whsmotilek.github.io').split(',')
    origins = [x.strip().rstrip('/') for x in origins if x.strip()]
    if not origins or any(x == '*' for x in origins):
        raise RuntimeError('Exact COMPOZA_ORIGINS required')
    migrate(path)
    app = FastAPI(title='Compoza API', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_path = path
    app.add_middleware(CORSMiddleware,allow_origins=origins,allow_credentials=True,
                       allow_methods=['GET','POST','PATCH'],allow_headers=['Content-Type','X-CSRF-Token'])
    session_cookie = COOKIE if secure else 'compoza_session'
    csrf_cookie = CSRF_COOKIE if secure else 'compoza_csrf'
    dummy_hash = password_hash(secrets.token_urlsafe(24))
    def set_cookie(response,key,value,max_age):
        response.set_cookie(key,value,httponly=True,secure=secure,samesite='lax',path='/',max_age=max_age)
    @contextmanager
    def db():
        c = connect(path)
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
    def session(request,c):
        token = request.cookies.get(session_cookie,'')
        row = c.execute('SELECT s.csrf,s.expires,u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1',
                        (hashlib.sha256(token.encode()).hexdigest(),int(time.time()))).fetchone()
        if not row: raise HTTPException(401,'Authentication required')
        return row
    def csrf(request,c,anonymous=False):
        if request.headers.get('origin') not in origins:
            raise HTTPException(403,'Origin not allowed')
        token = request.headers.get('x-csrf-token','')
        expected = request.cookies.get(csrf_cookie,'') if anonymous else session(request,c)['csrf']
        if not token or not expected or not hmac.compare_digest(token,expected):
            raise HTTPException(403,'Invalid CSRF token')
    def allowed_project(c,u,pid):
        p = c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
        if not p: raise HTTPException(404,'Project not found')
        if u['role'] in ('owner','admin') or p['designer_id']==u['id'] or p['foreman_id']==u['id']:
            return p
        if c.execute('SELECT 1 FROM members WHERE project_id=? AND user_id=?',(pid,u['id'])).fetchone(): return p
        raise HTTPException(404,'Project not found')
    def visible_projects(c,u):
        rows=[dict(p) for p in c.execute('SELECT p.*,u.name designer_name FROM projects p LEFT JOIN users u ON u.id=p.designer_id ORDER BY p.id') if u['role'] in ('owner','admin') or p['designer_id']==u['id'] or p['foreman_id']==u['id'] or c.execute('SELECT 1 FROM members WHERE project_id=? AND user_id=?',(p['id'],u['id'])).fetchone()]
        if u['role']=='master':return [{k:p[k] for k in ('id','name','city','type','status','address','access_note','is_demo')} for p in rows]
        return rows
    def visible_tasks(c,u,pid=None):
        ids = [p['id'] for p in visible_projects(c,u)]
        rows = c.execute('SELECT t.*,p.name project_name,u.name assignee_name FROM tasks t JOIN projects p ON p.id=t.project_id LEFT JOIN users u ON u.id=t.assignee_id ORDER BY t.id').fetchall()
        return [enrich(c,dict(t)) for t in rows if t['project_id'] in ids and (pid is None or t['project_id']==pid) and (u['role']!='master' or t['assignee_id']==u['id'] or c.execute('SELECT 1 FROM task_executors WHERE task_id=? AND user_id=?',(t['id'],u['id'])).fetchone())]
    def check_assignee(c,pid,uid):
        if uid is None:return
        user = c.execute('SELECT * FROM users WHERE id=? AND active=1',(uid,)).fetchone()
        if not user:raise HTTPException(422,'Unknown assignee')
        try:allowed_project(c,user,pid)
        except HTTPException:raise HTTPException(422,'Assignee not assigned to project')
    @app.middleware('http')
    async def security(request,call_next):
        response = await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        return response
    @app.get('/api/health')
    def health():return {'status':'ok','schema_version':4}
    @app.get('/api/auth/csrf')
    def csrf_get(request:Request,response:Response):
        with db() as c:
            try:token=session(request,c)['csrf']
            except HTTPException:token=secrets.token_urlsafe(32)
        set_cookie(response,csrf_cookie,token,SESSION_SECONDS)
        return {'csrf_token':token}
    @app.post('/api/auth/login')
    def login(body:Login,request:Request,response:Response):
        username=body.username.strip().casefold()
        now=int(time.time())
        # Proxy must supply a validated client address (uvicorn trusts only loopback).
        ip=request.client.host if request.client else 'unknown'
        keys=[('account',username),('ip',ip)]
        rejected=False
        with db() as c:
            csrf(request,c,True)
            c.execute('BEGIN IMMEDIATE')
            for kind,key in keys:
                row=c.execute('SELECT * FROM attempts WHERE kind=? AND key=?',(kind,key)).fetchone()
                if row and row['locked_until']>now:raise HTTPException(429,'Too many attempts; try later')
            u=c.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone()
            valid=password_ok(body.password,u['password_hash'] if u else dummy_hash)
            if not u or not valid:
                for kind,key in keys:
                    row=c.execute('SELECT * FROM attempts WHERE kind=? AND key=?',(kind,key)).fetchone()
                    failures=(row['failures'] if row and now-row['window_start']<900 else 0)+1
                    start=row['window_start'] if row and now-row['window_start']<900 else now
                    threshold=5 if kind=='account' else 20
                    c.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?,?)',(kind,key,failures,start,now+900 if failures>=threshold else 0))
                audit(c,None,'login_failed','auth')
                rejected=True
            else:
                # Account counter resets, shared-IP counter does not reset on a successful login.
                c.execute('DELETE FROM attempts WHERE kind=? AND key=?',('account',username))
                old=request.cookies.get(session_cookie,'')
                c.execute('DELETE FROM sessions WHERE token_hash=? OR expires<=?',(hashlib.sha256(old.encode()).hexdigest(),now))
                token=secrets.token_urlsafe(32);csrf_token=secrets.token_urlsafe(32)
                c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),u['id'],csrf_token,now+SESSION_SECONDS))
                audit(c,u['id'],'login','auth')
        if rejected:raise HTTPException(401,'Invalid username or password')
        set_cookie(response,session_cookie,token,SESSION_SECONDS)
        set_cookie(response,csrf_cookie,csrf_token,SESSION_SECONDS)
        return {'user':public_user(u),'csrf_token':csrf_token}
    @app.get('/api/auth/me')
    def me(request:Request):
        with db() as c:
            u=session(request,c)
            return {'user':public_user(u),'csrf_token':u['csrf']}
    @app.post('/api/auth/logout')
    def logout(request:Request,response:Response):
        with db() as c:
            csrf(request,c);u=session(request,c)
            c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(request.cookies.get(session_cookie,'').encode()).hexdigest(),))
            audit(c,u['id'],'logout','auth')
        response.delete_cookie(session_cookie,path='/',secure=secure,httponly=True,samesite='lax')
        response.delete_cookie(csrf_cookie,path='/',secure=secure,httponly=True,samesite='lax')
        return {'ok':True}
    @app.get('/api/projects')
    def projects(request:Request):
        with db() as c:
            u=session(request,c);rows=visible_projects(c,u)
            for p in rows:
                tasks=visible_tasks(c,u,p['id']);p['task_count']=len(tasks);p['open_tasks']=sum(t['status']!='done' for t in tasks)
            return {'items':rows}
    @app.get('/api/projects/{pid}')
    def project(pid:int,request:Request):
        with db() as c:
            u=session(request,c);p=allowed_project(c,u,pid)
            members=c.execute('SELECT DISTINCT u.id,u.name,u.role,u.active FROM users u LEFT JOIN members m ON m.user_id=u.id WHERE u.id IN (?,?) OR m.project_id=?',(p['designer_id'],p['foreman_id'],pid)).fetchall()
            project_data=dict(p)
            if u['role']=='master':
                project_data={k:project_data[k] for k in ('id','name','city','type','status','address','access_note','is_demo')}
            own_tasks=visible_tasks(c,u,pid);ids={t['id'] for t in own_tasks}
            activity=[]
            if u['role']!='master':
                for row in c.execute("SELECT a.*,u.name actor_name FROM audit a LEFT JOIN users u ON u.id=a.actor_id WHERE (a.entity='project' AND a.entity_id=?) OR (a.entity='task' AND a.entity_id IN (SELECT id FROM tasks WHERE project_id=?)) ORDER BY a.id DESC LIMIT 200",(pid,pid)):
                    if (row['entity']=='project' and row['entity_id']==pid) or (row['entity']=='task' and row['entity_id'] in ids):
                        item=dict(row);item['details']=json.loads(item['details'])
                        if item['action']=='document_added':
                            visibility=item['details'].get('visibility')
                            if visibility=='owners' and u['role']!='owner':continue
                            if visibility=='design' and u['role'] in ('foreman','admin'):continue
                        activity.append(item)
                        if len(activity)>=30:break
            return {'project':project_data,'tasks':own_tasks,'members':[dict(x) for x in members] if u['role']!='master' else [],'documents':documents(c,u,pid),'tracks':[dict(x) for x in c.execute('SELECT * FROM project_tracks WHERE project_id=? ORDER BY position',(pid,))] if u['role']!='master' else [],'blocked_task_ids':[t['id'] for t in own_tasks if c.execute("SELECT 1 FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_id WHERE d.task_id=? AND t.status<>'done' LIMIT 1",(t['id'],)).fetchone()],'dependencies':[dict(x) for x in c.execute('SELECT d.* FROM task_dependencies d JOIN tasks t ON t.id=d.task_id WHERE t.project_id=?',(pid,)) if x['task_id'] in ids and x['depends_on_id'] in ids],'activity':activity}
    @app.get('/api/tasks')
    def tasks(request:Request):
        with db() as c:return {'items':visible_tasks(c,session(request,c))}
    @app.post('/api/tasks',status_code=201)
    def task_create(body:TaskCreate,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c);allowed_project(c,u,body.project_id)
            if u['role']=='master':raise HTTPException(403,'Cannot create tasks')
            if not body.title.strip():raise HTTPException(422,'Title required')
            check_assignee(c,body.project_id,body.assignee_id)
            tid=c.execute('INSERT INTO tasks(project_id,title,assignee_id,due_date,category,note) VALUES(?,?,?,?,?,?)',(body.project_id,body.title.strip(),body.assignee_id,body.due_date.isoformat() if body.due_date else None,body.category,body.note)).lastrowid
            if body.assignee_id:c.execute('INSERT INTO task_executors VALUES(?,?)',(tid,body.assignee_id))
            audit(c,u['id'],'create','task',tid)
            return dict(c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
    @app.patch('/api/tasks/{tid}')
    def task_update(tid:int,body:TaskPatch,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c);c.execute('BEGIN IMMEDIATE')
            t=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
            if not t:raise HTTPException(404,'Task not found')
            allowed_project(c,u,t['project_id'])
            if u['role']=='master' and t['assignee_id']!=u['id'] and not c.execute('SELECT 1 FROM task_executors WHERE task_id=? AND user_id=?',(tid,u['id'])).fetchone():raise HTTPException(404,'Task not found')
            if t['version']!=body.version:raise HTTPException(409,'Task changed; refresh and retry')
            fields=body.model_dump(exclude_unset=True);fields.pop('version')
            override_reason=fields.pop('override_reason',None)
            if override_reason and u['role'] not in ('owner','admin','designer'):raise HTTPException(403,'Cannot override dependency')
            if not fields:raise HTTPException(422,'No changes')
            if u['role']=='master':
                if t['assignee_id']!=u['id'] and not c.execute('SELECT 1 FROM task_executors WHERE task_id=? AND user_id=?',(tid,u['id'])).fetchone():raise HTTPException(404,'Task not found')
                if t['status'] in ('review','done'):raise HTTPException(403,'Submitted work must be returned by reviewer')
                if set(fields)!={'status'} or body.status not in ('in_progress','review'):raise HTTPException(403,'Master can submit own work for review')
            if fields.get('status') in ('review','done'):raise HTTPException(403,'Use photo report and acceptance workflow')
            if t['status'] in ('review','done'):
                raise HTTPException(409,'Submitted work is locked; use acceptance workflow')
            if 'title' in fields and (fields['title'] is None or not fields['title'].strip()):raise HTTPException(422,'Title required')
            if any(k in fields and fields[k] is None for k in ('category','note')):raise HTTPException(422,'Category and note cannot be null')
            if 'status' in fields and fields['status'] is None:raise HTTPException(422,'Status required')
            if fields.get('status') in ('in_progress','review','done'):
                pending=c.execute("SELECT 1 FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_id WHERE d.task_id=? AND t.status<>'done' LIMIT 1",(tid,)).fetchone()
                if pending and not override_reason:raise HTTPException(409,'Prerequisite tasks must be accepted first')
            if 'assignee_id' in fields:
                check_assignee(c,t['project_id'],fields['assignee_id'])
                if fields['assignee_id'] and fields['assignee_id']==t['reviewer_id']:raise HTTPException(422,'Reviewer cannot be an executor')
            fields={k:(v.isoformat() if isinstance(v,date) else v.strip() if isinstance(v,str) else v) for k,v in fields.items()}
            sql=', '.join(k+'=?' for k in fields)
            changed=c.execute(f'UPDATE tasks SET {sql},version=version+1 WHERE id=? AND version=?',(*fields.values(),tid,body.version)).rowcount
            if not changed:raise HTTPException(409,'Task changed; refresh and retry')
            if fields.get('assignee_id'):c.execute('INSERT OR IGNORE INTO task_executors VALUES(?,?)',(tid,fields['assignee_id']))
            audit(c,u['id'],'update','task',tid,{**fields,**({'override_reason':override_reason} if override_reason else {})})
            return dict(c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone())
    def owner(request,c):
        u=session(request,c)
        if u['role']!='owner':raise HTTPException(403,'Owner access required')
        return u
    @app.post('/api/users',status_code=201)
    def user_create(body:UserCreate,request:Request):
        with db() as c:
            csrf(request,c);u=owner(request,c)
            try:
                uid=c.execute('INSERT INTO users(username,name,role,password_hash) VALUES(?,?,?,?)',(body.username.casefold(),body.name.strip(),body.role,password_hash(body.password))).lastrowid
            except sqlite3.IntegrityError:raise HTTPException(409,'Username already exists')
            audit(c,u['id'],'create','user',uid,{'role':body.role})
            return public_user(c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone())
    @app.patch('/api/users/{uid}')
    def user_update(uid:int,body:UserPatch,request:Request):
        with db() as c:
            csrf(request,c);u=owner(request,c);c.execute('BEGIN IMMEDIATE')
            target=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not target:raise HTTPException(404,'User not found')
            fields=body.model_dump(exclude_unset=True)
            if not fields or any(v is None for v in fields.values()):raise HTTPException(422,'Non-null changes required')
            if target['role']=='owner' and target['active'] and (fields.get('active') is False or fields.get('role','owner')!='owner'):
                if c.execute("SELECT count(*) FROM users WHERE role='owner' AND active=1").fetchone()[0]<=1:raise HTTPException(409,'Cannot remove last active owner')
            if 'password' in fields:fields['password_hash']=password_hash(fields.pop('password'))
            sql=', '.join(k+'=?' for k in fields)
            c.execute(f'UPDATE users SET {sql} WHERE id=?',(*fields.values(),uid))
            c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
            audit(c,u['id'],'update','user',uid,{'fields':['password' if k=='password_hash' else k for k in fields]})
            return {**public_user(c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()),'active':bool(c.execute('SELECT active FROM users WHERE id=?',(uid,)).fetchone()[0])}
    @app.post('/api/auth/password')
    def own_password(body:PasswordChange,request:Request,response:Response):
        rejected=False
        with db() as c:
            csrf(request,c);u=session(request,c);c.execute('BEGIN IMMEDIATE')
            now=int(time.time());key=str(u['id'])
            row=c.execute("SELECT * FROM attempts WHERE kind='password' AND key=?",(key,)).fetchone()
            if row and row['locked_until']>now:raise HTTPException(429,'Too many attempts; try later')
            if not password_ok(body.old_password,u['password_hash']):
                failures=(row['failures'] if row and now-row['window_start']<900 else 0)+1
                start=row['window_start'] if row and now-row['window_start']<900 else now
                c.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?,?)',('password',key,failures,start,now+900 if failures>=5 else 0))
                audit(c,u['id'],'password_change_failed','auth');rejected=True
            else:
                c.execute('UPDATE users SET password_hash=? WHERE id=?',(password_hash(body.password),u['id']))
                c.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],))
                c.execute("DELETE FROM attempts WHERE kind='password' AND key=?",(key,))
                audit(c,u['id'],'password_change','user',u['id'])
        if rejected:raise HTTPException(403,'Current password incorrect')
        response.delete_cookie(session_cookie,path='/',secure=secure,httponly=True,samesite='lax')
        return {'ok':True,'login_required':True}
    def project_members(c,designer,foreman,members):
        for uid,expected in [(designer,'designer'),(foreman,'foreman')]:
            if uid is not None:
                target=c.execute('SELECT * FROM users WHERE id=? AND active=1',(uid,)).fetchone()
                if not target or target['role']!=expected:raise HTTPException(422,'Invalid '+expected)
        for uid in members:
            if not c.execute('SELECT 1 FROM users WHERE id=? AND active=1',(uid,)).fetchone():raise HTTPException(422,'Unknown project member')
    @app.post('/api/projects',status_code=201)
    def project_create(body:ProjectCreate,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c)
            if u['role'] not in ('owner','admin'):raise HTTPException(403,'Cannot create project')
            project_members(c,body.designer_id,body.foreman_id,body.member_ids)
            if not body.name.strip() or not body.city.strip():raise HTTPException(422,'Name and city required')
            pid=c.execute('INSERT INTO projects(name,city,type,designer_id,foreman_id,access_note) VALUES(?,?,?,?,?,?)',(body.name.strip(),body.city.strip(),body.type,body.designer_id,body.foreman_id,body.access_note)).lastrowid
            for uid in set(body.member_ids):c.execute('INSERT INTO members VALUES(?,?)',(pid,uid))
            audit(c,u['id'],'create','project',pid)
            ensure_tracks(c,pid)
            return dict(c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone())
    @app.patch('/api/projects/{pid}')
    def project_update(pid:int,body:ProjectPatch,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c)
            if u['role'] not in ('owner','admin'):raise HTTPException(403,'Cannot change project setup')
            p=allowed_project(c,u,pid);fields=body.model_dump(exclude_unset=True)
            if not fields:raise HTTPException(422,'No changes')
            for key in ('name','city','status','access_note','member_ids'):
                if key in fields and (fields[key] is None or (key in ('name','city') and not fields[key].strip())):raise HTTPException(422,'Invalid '+key)
            project_members(c,fields.get('designer_id',p['designer_id']),fields.get('foreman_id',p['foreman_id']),fields.get('member_ids',[]))
            members=fields.pop('member_ids',None)
            if fields.get('status')=='paused' and not fields.get('pause_reason',p['pause_reason']).strip():raise HTTPException(422,'Pause reason required')
            if any(fields.get(k,'') is None for k in ('address','client_name','pause_reason')):raise HTTPException(422,'Text fields cannot be null')
            fields={k:(v.isoformat() if isinstance(v,date) else v) for k,v in fields.items()}
            if fields:
                sql=', '.join(k+'=?' for k in fields)
                c.execute(f'UPDATE projects SET {sql} WHERE id=?',(*fields.values(),pid))
            if members is not None:
                c.execute('DELETE FROM members WHERE project_id=?',(pid,))
                for uid in set(members):c.execute('INSERT INTO members VALUES(?,?)',(pid,uid))
            audit(c,u['id'],'update','project',pid,{'fields':list(fields)+(['member_ids'] if members is not None else [])})
            return dict(c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone())
    @app.get('/api/overview')
    def overview(request:Request):
        with db() as c:
            u=session(request,c);projects=visible_projects(c,u);active_ids={p['id'] for p in projects if p['status']=='active'};tasks=visible_tasks(c,u);active_tasks=[t for t in tasks if t['project_id'] in active_ids];today=date.today().isoformat()
            return {'projects':len(visible_projects(c,u)),'open_tasks':sum(t['status']!='done' for t in tasks),'active_projects':len(active_ids),'paused_projects':sum(p['status']=='paused' for p in projects),'overdue_tasks':sum(bool(t['due_date']) and t['due_date']<today and t['status']!='done' for t in active_tasks),'pending_review':sum(t['status']=='review' for t in tasks),'role':u['role']}
    @app.get('/api/users')
    def users(request:Request):
        with db() as c:
            u=session(request,c)
            if u['role']!='owner':raise HTTPException(403,'Owner access required')
            return {'items':[{**public_user(x),'active':bool(x['active'])} for x in c.execute('SELECT * FROM users ORDER BY id')]}
    @app.get('/api/audit')
    def audit_list(request:Request):
        with db() as c:
            u=session(request,c)
            if u['role']!='owner':raise HTTPException(403,'Owner access required')
            return {'items':[dict(x) for x in c.execute('SELECT a.*,u.name actor_name FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 200')]}
    install_routes(app,db,session,csrf,allowed_project,visible_tasks,audit)
    install_workflow(app,db,session,csrf,allowed_project,visible_tasks,audit,os.environ.get('COMPOZA_UPLOADS',str(Path(path).parent/'uploads')))
    static_dir = os.environ.get('COMPOZA_STATIC_DIR')
    if static_dir:
        app.mount('/app', StaticFiles(directory=static_dir, html=True), name='app')
    return app
