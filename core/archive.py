"""Full private snapshots: consistent SQLite copy plus every referenced photo."""
import hashlib
import fcntl
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import time
import re
from .cli import backup, restore_check
from .main import connect

@contextmanager
def photo_archive_lock(uploads):
    root=Path(uploads);root.mkdir(parents=True,exist_ok=True)
    with (root/'.archive.lock').open('a') as lock:
        os.chmod(root/'.archive.lock',0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)

def full_backup(source,uploads,destination):
    with photo_archive_lock(uploads):return _full_backup(source,uploads,destination)

def _full_backup(source, uploads, destination):
    destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        root=Path(tmp);db=backup(source,root/'core.sqlite3')
        restore_check(db,root/'verified.sqlite3')
        manifest={'format':1,'files':{},'database_sha256':hashlib.sha256(db.read_bytes()).hexdigest()}
        with connect(db) as c:
            for r in c.execute('SELECT id,sha256,size FROM task_photos ORDER BY id'):
                name=r['id']+'.jpg';p=Path(uploads)/name
                if not p.is_file() or p.stat().st_size!=r['size'] or hashlib.sha256(p.read_bytes()).hexdigest()!=r['sha256']:raise RuntimeError('Missing or corrupted photo: '+r['id'])
                manifest['files']['uploads/'+name]={'sha256':r['sha256'],'size':r['size']}
        (root/'manifest.json').write_text(json.dumps(manifest,sort_keys=True))
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        try:
            with os.fdopen(fd,'wb') as out,tarfile.open(fileobj=out,mode='w:gz') as tar:
                tar.add(db,arcname='core.sqlite3');tar.add(root/'manifest.json',arcname='manifest.json')
                for name in manifest['files']:tar.add(Path(uploads)/Path(name).name,arcname=name)
            verify_archive(destination)
        except Exception:
            destination.unlink(missing_ok=True);raise
    return {'photos':len(manifest['files']),'integrity':'ok'}

def verify_archive(source):
    with tempfile.TemporaryDirectory(dir=Path(source).parent) as tmp,tarfile.open(source,'r:gz') as tar:
        root=Path(tmp);members=tar.getmembers()
        for member in members:
            p=Path(member.name)
            if p.is_absolute() or '..' in p.parts or not member.isfile():raise RuntimeError('Unsafe archive entry')
            if member.name not in ('core.sqlite3','manifest.json') and not (len(p.parts)==2 and p.parts[0]=='uploads' and p.suffix=='.jpg'):raise RuntimeError('Unexpected archive entry')
            target=root/p;target.parent.mkdir(parents=True,exist_ok=True)
            with tar.extractfile(member) as inp,target.open('xb') as out:
                while chunk:=inp.read(1024*1024):out.write(chunk)
        manifest=json.loads((root/'manifest.json').read_text())
        if hashlib.sha256((root/'core.sqlite3').read_bytes()).hexdigest()!=manifest['database_sha256']:raise RuntimeError('Database digest mismatch')
        report=restore_check(root/'core.sqlite3',root/'restored.sqlite3')
        with connect(root/'restored.sqlite3') as c:
            expected={'uploads/'+r['id']+'.jpg':{'sha256':r['sha256'],'size':r['size']} for r in c.execute('SELECT * FROM task_photos')}
        if expected!=manifest['files']:raise RuntimeError('Photo manifest differs from database')
        if {m.name for m in members}!={'core.sqlite3','manifest.json',*expected}:raise RuntimeError('Archive entries differ from manifest')
        for name,meta in expected.items():
            p=root/name
            if not p.is_file() or p.stat().st_size!=meta['size'] or hashlib.sha256(p.read_bytes()).hexdigest()!=meta['sha256']:raise RuntimeError('Photo digest mismatch')
        return {**report,'photos_verified':len(expected)}

def cleanup_orphans(source,uploads,days=14):
    with photo_archive_lock(uploads):return _cleanup_orphans(source,uploads,days)

def _cleanup_orphans(source,uploads,days):
    # Never remove a referenced image; age guard protects concurrent uploads.
    cutoff=time.time()-days*86400;removed=0
    with connect(source) as c:
        c.execute('BEGIN IMMEDIATE')
        referenced={r[0]+'.jpg' for r in c.execute('SELECT id FROM task_photos')}
        for p in Path(uploads).glob('*.jpg'):
            if re.fullmatch(r'[0-9a-f]{48}\.jpg',p.name) and p.name not in referenced and p.stat().st_mtime<cutoff:
                p.unlink();removed+=1
    return {'unreferenced_photos_removed':removed}

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command',required=True)
    b=sub.add_parser('create');b.add_argument('--db',required=True);b.add_argument('--uploads',required=True);b.add_argument('--output',required=True)
    b=sub.add_parser('verify');b.add_argument('archive')
    b=sub.add_parser('cleanup');b.add_argument('--db',required=True);b.add_argument('--uploads',required=True)
    a=p.parse_args()
    if a.command=='cleanup':print(json.dumps(cleanup_orphans(a.db,a.uploads)));raise SystemExit(0)
    print(json.dumps(full_backup(a.db,a.uploads,a.output) if a.command=='create' else verify_archive(a.archive)))
