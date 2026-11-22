"""Offline administrative commands; never expose this module as HTTP."""
import argparse
import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path
from .main import ROLES, audit, connect, migrate, password_hash

def backup(source, destination):
    if not Path(source).is_file():raise FileNotFoundError('Backup source does not exist')
    target=Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    try:
        with connect(source) as src, connect(target) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Backup integrity failed')
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise

def restore_check(source, destination):
    target=backup(source,destination)
    with connect(target) as c:
        if c.execute('PRAGMA foreign_key_check').fetchall():raise RuntimeError('Invalid foreign keys')
        version=c.execute('SELECT max(version) FROM schema_version').fetchone()[0]
        if version!=3:raise RuntimeError('Unexpected schema version')
        required={'schema_version','users','sessions','attempts','projects','members','tasks','audit','project_tracks','project_documents','task_comments','task_dependencies','task_materials'}
        present={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(present):raise RuntimeError('Required tables missing from backup')
        counts={}
        with connect(source) as src:
            for table in sorted(required):
                original=[tuple(r) for r in src.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                restored=[tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                encode=lambda rows:hashlib.sha256(json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode()).digest()
                if encode(original)!=encode(restored):raise RuntimeError('Restored data differs: '+table)
                counts[table]=len(restored)
        return {'schema_version':version,'users':counts['users'],'projects':counts['projects'],
                'audit':counts['audit'],'tables_verified':len(required),'row_counts':counts,'integrity':'ok'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--db',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('migrate')
    b=sub.add_parser('add-user');b.add_argument('--username',required=True);b.add_argument('--name',required=True);b.add_argument('--role',choices=ROLES,required=True);b.add_argument('--credentials',required=True)
    b=sub.add_parser('reset-password');b.add_argument('--username',required=True);b.add_argument('--credentials',required=True)
    b=sub.add_parser('disable-user');b.add_argument('--username',required=True)
    sub.add_parser('seed-demo')
    for cmd in ['backup','restore-check']:
        b=sub.add_parser(cmd);b.add_argument('--output',required=True)
    args=p.parse_args()
    if args.command=='backup':print(str(backup(args.db,args.output)));return
    if args.command=='restore-check':print(json.dumps(restore_check(args.db,args.output)));return
    migrate(args.db)
    with connect(args.db) as c:
        if args.command in ('add-user','reset-password'):
            password=secrets.token_urlsafe(24)
            target=Path(args.credentials);target.parent.mkdir(parents=True,exist_ok=True)
            # Refuse to overwrite an earlier credential file.
            fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            try:
                username=args.username.strip().casefold()
                if not username:raise ValueError('Empty username')
                if args.command=='add-user':
                    uid=c.execute('INSERT INTO users(username,name,role,password_hash) VALUES(?,?,?,?)',(username,args.name,args.role,password_hash(password))).lastrowid
                else:
                    u=c.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone()
                    if not u:raise ValueError('Unknown user')
                    uid=u['id'];c.execute('UPDATE users SET password_hash=? WHERE id=?',(password_hash(password),uid))
                    c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
                audit(c,None,args.command,'user',uid)
                with os.fdopen(fd,'w') as f:
                    f.write(json.dumps({'username':username,'password':password},ensure_ascii=False)+'\n')
                c.commit()
            except Exception:
                try:os.close(fd)
                except OSError:pass
                target.unlink(missing_ok=True)
                raise
            print('Account updated; credentials written to private file.')
        elif args.command=='disable-user':
            u=c.execute('SELECT id FROM users WHERE username=?',(args.username.strip().casefold(),)).fetchone()
            if not u:raise ValueError('Unknown user')
            c.execute('UPDATE users SET active=0 WHERE id=?',(u['id'],));c.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],))
            audit(c,None,'disable-user','user',u['id'])
        elif args.command=='seed-demo':
            if c.execute('SELECT count(*) FROM projects').fetchone()[0]:raise ValueError('Refusing to seed nonempty projects')
            # Synthetic unassigned examples; no real people, passwords or financial data.
            for name,city,kind in [('Учебный объект А','Москва','staging'),('Учебный объект Б','Санкт-Петербург','whitebox'),('Учебный объект В','Москва','concrete')]:
                pid=c.execute('INSERT INTO projects(name,city,type,is_demo) VALUES(?,?,?,1)',(name,city,kind)).lastrowid
                c.execute('INSERT INTO tasks(project_id,title) VALUES(?,?)',(pid,'Проверить карточку учебного объекта'))
            audit(c,None,'seed_demo','project')
if __name__=='__main__':main()
