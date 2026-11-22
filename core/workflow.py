"""Team execution, private photographic evidence and immutable acceptance attempts."""
import hashlib
import io
import json
import shutil
import os
import secrets
from pathlib import Path
from typing import Literal
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD=1_500_000
MAX_STORAGE=int(os.environ.get('COMPOZA_PHOTO_STORAGE_BYTES',str(1_073_741_824)))
Image.MAX_IMAGE_PIXELS=5_000_000

class Team(BaseModel):
    version:int=Field(ge=1)
    executor_ids:list[int]=Field(max_length=30)
    reviewer_id:int
class Submission(BaseModel):
    version:int=Field(ge=1)
    summary:str=Field(min_length=1,max_length=4000)
    photo_ids:list[str]=Field(default_factory=list,max_length=30)
    request_id:str=Field(min_length=16,max_length=80,pattern=r'^[a-zA-Z0-9_-]+$')
class Review(BaseModel):
    version:int=Field(ge=1)
    submission_id:int
    decision:Literal['accepted','rework']
    remarks:str=Field(default='',max_length=4000)
    reason_kind:Literal['workmanship','client_change','other']='workmanship'
    request_id:str=Field(min_length=16,max_length=80,pattern=r'^[a-zA-Z0-9_-]+$')

def enrich(c,t):
    t['executors']=[dict(r) for r in c.execute('SELECT u.id,u.name,u.active FROM users u JOIN task_executors e ON e.user_id=u.id WHERE e.task_id=? ORDER BY u.id',(t['id'],))]
    t['reviewer_name']=None
    if t.get('reviewer_id'):
        row=c.execute('SELECT name FROM users WHERE id=?',(t['reviewer_id'],)).fetchone()
        if row:t['reviewer_name']=row[0]
    return t

def install_workflow(app,db,session,csrf,allowed_project,visible_tasks,audit,uploads):
    uploads=Path(uploads);uploads.mkdir(parents=True,exist_ok=True);uploads.chmod(0o700)
    def task(c,u,tid):
        t=next((t for t in visible_tasks(c,u) if t['id']==tid),None)
        if not t:raise HTTPException(404,'Task not found')
        return t
    def version(t,v):
        if t['version']!=v:raise HTTPException(409,'Task changed; refresh and retry')
    def bump(c,t,status=None):
        c.execute('UPDATE tasks SET version=version+1,status=? WHERE id=?',(status or t['status'],t['id']))
    def reviewer(c,u,t):
        return u['role']=='owner' or (t['reviewer_id']==u['id'] and u['role'] in ('designer','foreman'))
    def can_submit(c,u,t):
        return t['reviewer_id']!=u['id'] and (u['role'] in ('owner','designer','foreman') or t['assignee_id']==u['id'] or any(x['id']==u['id'] for x in t['executors']))
    def photo_data(c,tid):
        return [dict(r) for r in c.execute('SELECT p.*,u.name actor_name FROM task_photos p JOIN users u ON u.id=p.actor_id WHERE p.task_id=? ORDER BY p.at,p.id',(tid,))]
    def result(c,u,t):
        subs=[]
        for s in c.execute('SELECT s.*,u.name actor_name FROM task_submissions s JOIN users u ON u.id=s.actor_id WHERE task_id=? ORDER BY s.id DESC',(t['id'],)):
            item=dict(s);r=c.execute('SELECT r.*,u.name actor_name FROM task_reviews r JOIN users u ON u.id=r.actor_id WHERE submission_id=?',(s['id'],)).fetchone();item['review']=dict(r) if r else None;subs.append(item)
        return {'task':t,'photos':photo_data(c,t['id']),'submissions':subs,'can_review':reviewer(c,u,t),'can_submit':can_submit(c,u,t)}
    @app.get('/api/tasks/{tid}/workflow')
    def detail(tid:int,request:Request):
        with db() as c:
            u=session(request,c);return result(c,u,task(c,u,tid))
    @app.post('/api/tasks/{tid}/team')
    def team(tid:int,body:Team,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c);c.execute('BEGIN IMMEDIATE');t=task(c,u,tid);version(t,body.version)
            if u['role'] not in ('owner','designer','foreman'):raise HTTPException(403,'Cannot assign execution team')
            if t['status'] in ('review','done'):raise HTTPException(409,'Return work before changing team')
            ids=set(body.executor_ids)
            if t['assignee_id']:ids.add(t['assignee_id'])
            if not ids:raise HTTPException(422,'Executors required')
            for uid in ids|{body.reviewer_id}:
                p=c.execute('SELECT * FROM users WHERE id=? AND active=1',(uid,)).fetchone()
                if not p:raise HTTPException(422,'Inactive participant')
                try:allowed_project(c,p,t['project_id'])
                except HTTPException:raise HTTPException(422,'Participant not assigned to project')
                if uid==body.reviewer_id and p['role'] not in ('owner','designer','foreman'):raise HTTPException(422,'Reviewer must be designer, foreman or owner')
            if body.reviewer_id in ids:raise HTTPException(422,'Reviewer cannot be an executor')
            c.execute('DELETE FROM task_executors WHERE task_id=?',(tid,))
            c.executemany('INSERT INTO task_executors VALUES(?,?)',[(tid,uid) for uid in ids])
            c.execute('UPDATE tasks SET reviewer_id=?,version=version+1 WHERE id=?',(body.reviewer_id,tid))
            audit(c,u['id'],'team_assigned','task',tid,{'executor_ids':sorted(ids),'reviewer_id':body.reviewer_id})
            return result(c,u,task(c,u,tid))
    @app.post('/api/tasks/{tid}/photos',status_code=201)
    async def upload(tid:int,request:Request):
        # Authenticate before reading bytes; stream limit also applies without Content-Length.
        with db() as c:
            csrf(request,c);u=session(request,c);t=task(c,u,tid)
            if not can_submit(c,u,t):raise HTTPException(403,'Cannot upload evidence')
            if t['status']!='in_progress':raise HTTPException(409,'Start or return work before uploading')
        if request.headers.get('content-type','').split(';')[0] not in ('image/jpeg','image/png','image/webp'):raise HTTPException(415,'JPEG, PNG or WebP required')
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>MAX_UPLOAD:raise HTTPException(413,'Photo exceeds upload limit')
        try:
            with Image.open(io.BytesIO(raw),formats=['JPEG','PNG','WEBP']) as im:
                if im.width*im.height>5_000_000:raise ValueError('Too many pixels')
                im=ImageOps.exif_transpose(im);im.thumbnail((2048,2048));im=im.convert('RGB')
                buf=io.BytesIO();im.save(buf,format='JPEG',quality=86);data=buf.getvalue()
        except (UnidentifiedImageError,ValueError,OSError,Image.DecompressionBombError):raise HTTPException(422,'Invalid or oversized image')
        photo_id=secrets.token_hex(24);target=uploads/(photo_id+'.jpg')
        try:
            with db() as c:
                # Recheck authorization/state under the write lock after upload/decoding.
                csrf(request,c);u=session(request,c);c.execute('BEGIN IMMEDIATE');t=task(c,u,tid)
                if t['status']!='in_progress' or not can_submit(c,u,t):raise HTTPException(409,'Task changed during upload')
                if shutil.disk_usage(uploads).free<1_073_741_824 or sum(p.stat().st_size for p in uploads.glob('*.jpg'))+len(data)>MAX_STORAGE:raise HTTPException(507,'Photo storage full; contact administrator')
                if c.execute('SELECT count(*) FROM task_photos WHERE task_id=? AND submission_id IS NULL',(tid,)).fetchone()[0]>=30:raise HTTPException(422,'At most 30 draft photos')
                with target.open('xb') as f:os.chmod(target,0o600);f.write(data);f.flush();os.fsync(f.fileno())
                c.execute('INSERT INTO task_photos(id,task_id,actor_id,sha256,size) VALUES(?,?,?,?,?)',(photo_id,tid,u['id'],hashlib.sha256(data).hexdigest(),len(data)))
                audit(c,u['id'],'photo_added','task',tid,{'photo_id':photo_id})
        except Exception:
            target.unlink(missing_ok=True);raise
        return {'id':photo_id}
    @app.get('/api/photos/{photo_id}')
    def photo(photo_id:str,request:Request):
        with db() as c:
            u=session(request,c);p=c.execute('SELECT * FROM task_photos WHERE id=?',(photo_id,)).fetchone()
            if not p:raise HTTPException(404,'Photo not found')
            task(c,u,p['task_id']);target=uploads/(p['id']+'.jpg')
            if not target.is_file():raise HTTPException(503,'Photo temporarily unavailable')
            return FileResponse(target,media_type='image/jpeg',headers={'Content-Disposition':'inline; filename="report.jpg"','Cache-Control':'no-store'})
    @app.post('/api/tasks/{tid}/photos/{photo_id}/remove')
    def remove_photo(tid:int,photo_id:str,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c);c.execute('BEGIN IMMEDIATE');t=task(c,u,tid)
            p=c.execute('SELECT * FROM task_photos WHERE id=? AND task_id=?',(photo_id,tid)).fetchone()
            if not p:raise HTTPException(404,'Photo not found')
            if p['submission_id'] or t['status']!='in_progress':raise HTTPException(409,'Submitted evidence is immutable')
            if p['actor_id']!=u['id'] and u['role']!='owner':raise HTTPException(403,'Cannot remove another author photo')
            c.execute('DELETE FROM task_photos WHERE id=?',(photo_id,));audit(c,u['id'],'draft_photo_removed','task',tid,{'photo_id':photo_id})
        # Keep unreferenced bytes until backup retention cleanup; avoids snapshot/delete races.
        return {'ok':True}
    @app.post('/api/tasks/{tid}/submit')
    def submit(tid:int,body:Submission,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c);c.execute('BEGIN IMMEDIATE');t=task(c,u,tid)
            prior=c.execute('SELECT * FROM task_submissions WHERE request_id=?',(body.request_id,)).fetchone()
            if prior:
                photos={r[0] for r in c.execute('SELECT id FROM task_photos WHERE submission_id=?',(prior['id'],))}
                if prior['task_id']!=tid or prior['actor_id']!=u['id'] or prior['summary']!=body.summary.strip() or photos!=set(body.photo_ids):raise HTTPException(409,'Request id already used')
                return {'submission_id':prior['id'],'task':t}
            version(t,body.version)
            if not can_submit(c,u,t):raise HTTPException(403,'Cannot submit this task')
            if t['status']!='in_progress':raise HTTPException(409,'Task must be in progress')
            if not t['reviewer_id']:raise HTTPException(422,'Assign reviewer first')
            reviewer_user=c.execute('SELECT * FROM users WHERE id=? AND active=1',(t['reviewer_id'],)).fetchone()
            if not reviewer_user or reviewer_user['role'] not in ('owner','designer','foreman'):raise HTTPException(422,'Reviewer unavailable')
            try:allowed_project(c,reviewer_user,t['project_id'])
            except HTTPException:raise HTTPException(422,'Reviewer unavailable')
            if t['reviewer_id']==u['id']:raise HTTPException(422,'Reviewer cannot submit own result')
            if not body.summary.strip():raise HTTPException(422,'Report summary required')
            ids=set(body.photo_ids)
            if t['category'] not in ('access','approval') and not ids:raise HTTPException(422,'Photo evidence required')
            for photo_id in ids:
                p=c.execute('SELECT * FROM task_photos WHERE id=? AND task_id=? AND submission_id IS NULL',(photo_id,tid)).fetchone()
                if not p or not (uploads/(photo_id+'.jpg')).is_file():raise HTTPException(422,'Photo unavailable for this submission')
            attempt=c.execute('SELECT coalesce(max(attempt),0)+1 FROM task_submissions WHERE task_id=?',(tid,)).fetchone()[0]
            context=json.dumps({k:t[k] for k in ('title','note','assignee_id','executors','reviewer_id','reviewer_name','due_date')},ensure_ascii=False)
            sid=c.execute('INSERT INTO task_submissions(task_id,attempt,actor_id,summary,context,request_id) VALUES(?,?,?,?,?,?)',(tid,attempt,u['id'],body.summary.strip(),context,body.request_id)).lastrowid
            c.executemany('UPDATE task_photos SET submission_id=? WHERE id=?',[(sid,p) for p in ids]);bump(c,t,'review')
            audit(c,u['id'],'work_submitted','task',tid,{'submission_id':sid,'attempt':attempt})
            return {'submission_id':sid,'task':task(c,u,tid)}
    @app.post('/api/tasks/{tid}/review')
    def review(tid:int,body:Review,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c);c.execute('BEGIN IMMEDIATE');t=task(c,u,tid)
            prior=c.execute('SELECT r.*,s.task_id FROM task_reviews r JOIN task_submissions s ON s.id=r.submission_id WHERE r.request_id=?',(body.request_id,)).fetchone()
            if prior:
                if prior['task_id']!=tid or prior['submission_id']!=body.submission_id or prior['actor_id']!=u['id'] or prior['decision']!=body.decision or prior['remarks']!=body.remarks.strip() or prior['reason_kind']!=body.reason_kind:raise HTTPException(409,'Request id already used')
                return {'task':t}
            version(t,body.version)
            if not reviewer(c,u,t):raise HTTPException(403,'Assigned reviewer required')
            s=c.execute('SELECT * FROM task_submissions WHERE id=? AND task_id=?',(body.submission_id,tid)).fetchone()
            if not s:raise HTTPException(404,'Submission not found')
            if s['actor_id']==u['id'] or t['assignee_id']==u['id'] or any(x['id']==u['id'] for x in t['executors']):raise HTTPException(403,'Cannot accept own work')
            if t['status']!='review' or c.execute('SELECT 1 FROM task_reviews WHERE submission_id=?',(s['id'],)).fetchone():raise HTTPException(409,'Submission already reviewed')
            if body.decision=='rework' and not body.remarks.strip():raise HTTPException(422,'Rework remarks required')
            if body.decision=='accepted' and c.execute("SELECT 1 FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_id WHERE d.task_id=? AND t.status<>'done'",(tid,)).fetchone():raise HTTPException(409,'Prerequisite tasks must be accepted first')
            c.execute('INSERT INTO task_reviews(submission_id,actor_id,decision,remarks,reason_kind,request_id) VALUES(?,?,?,?,?,?)',(s['id'],u['id'],body.decision,body.remarks.strip(),body.reason_kind,body.request_id));bump(c,t,'done' if body.decision=='accepted' else 'in_progress')
            audit(c,u['id'],'work_'+body.decision,'task',tid,{'submission_id':s['id'],'remarks':body.remarks.strip(),'reason_kind':body.reason_kind})
            return {'task':task(c,u,tid)}
    class Reopen(BaseModel):
        version:int=Field(ge=1)
        reason:str=Field(min_length=8,max_length=4000)
    @app.post('/api/tasks/{tid}/reopen')
    def reopen(tid:int,body:Reopen,request:Request):
        with db() as c:
            csrf(request,c);u=session(request,c);c.execute('BEGIN IMMEDIATE');t=task(c,u,tid);version(t,body.version)
            if not reviewer(c,u,t):raise HTTPException(403,'Assigned reviewer required')
            if t['status'] not in ('review','done'):raise HTTPException(409,'Work already open')
            pending=c.execute('SELECT s.id FROM task_submissions s LEFT JOIN task_reviews r ON r.submission_id=s.id WHERE s.task_id=? AND r.id IS NULL',(tid,)).fetchone()
            if pending:raise HTTPException(409,'Review submitted report first')
            if len(body.reason.strip())<8:raise HTTPException(422,'Reopening reason required')
            bump(c,t,'in_progress');audit(c,u['id'],'work_reopened','task',tid,{'reason':body.reason.strip()})
            return {'task':task(c,u,tid)}
