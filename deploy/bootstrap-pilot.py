"""Run once inside the isolated container; writes passwords only to a private file."""
import json
import os
import secrets
from pathlib import Path
from core.main import connect, migrate, password_hash, audit

db = os.environ.get('COMPOZA_DB', '/data/core.sqlite3')
migrate(db)
destination = Path('/data/initial-access.json')
if destination.exists():
    raise SystemExit('Refusing to overwrite credentials')
accounts = [
    ('matvey', 'Матвей', 'owner'),
    ('erzhena', 'Эржена', 'owner'),
    ('evgeniy', 'Евгений', 'owner'),
    ('admin.pilot', 'Администратор · пример', 'admin'),
    ('designer.pilot', 'Дизайнер · пример', 'designer'),
    ('designer.other', 'Другой дизайнер · пример', 'designer'),
    ('foreman.pilot', 'Прораб · пример', 'foreman'),
    ('master.pilot', 'Мастер · пример', 'master'),
]
result = []
with connect(db) as c:
    if c.execute('SELECT count(*) FROM users').fetchone()[0]:
        raise SystemExit('Database already initialized; no changes made')
    ids = {}
    for username, name, role in accounts:
        password = secrets.token_urlsafe(24)
        uid = c.execute('INSERT INTO users(username,name,role,password_hash) VALUES(?,?,?,?)',
                        (username, name, role, password_hash(password))).lastrowid
        ids[username] = uid
        result.append(dict(username=username, name=name, role=role, password=password))
    examples = [('Пример · Квартира с отделкой', 'Москва', 'staging', 'designer.pilot'),
                ('Пример · Whitebox', 'Санкт-Петербург', 'whitebox', 'designer.other'),
                ('Пример · Квартира без отделки', 'Москва', 'concrete', 'designer.pilot')]
    for number, (name, city, kind, designer) in enumerate(examples, 1):
        pid = c.execute('INSERT INTO projects(name,city,type,designer_id,foreman_id,is_demo,access_note) VALUES(?,?,?,?,?,1,?)',
                        (name, city, kind, ids[designer], ids['foreman.pilot'],
                         'Учебные данные. Перед выездом согласовать пропуск и получение ключей.')).lastrowid
        c.execute('INSERT INTO members VALUES(?,?)', (pid, ids['master.pilot']))
        for title, category, assignee in [('Оформить пропуск на объект', 'access', designer),
                                          ('Согласовать время доставки', 'delivery', designer),
                                          ('Проверить размеры перед работой', 'task', 'master.pilot')]:
            c.execute("INSERT INTO tasks(project_id,title,category,note,assignee_id,due_date) VALUES(?,?,?,?,?,date('now'))",
                      (pid, title, category, 'Пример задачи. Уточнить готовность материалов и условия доступа до выезда.', ids[assignee]))
    audit(c, None, 'bootstrap_pilot', 'system', details={'synthetic_projects': 3})
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'url': 'https://compoza.95-163-227-180.sslip.io/app/', 'accounts': result}, f, ensure_ascii=False, indent=2)
    c.commit()
print('Pilot initialized. Credentials are in private storage; no passwords printed.')
