CREATE TABLE IF NOT EXISTS project_tracks(
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 code TEXT NOT NULL, title TEXT NOT NULL, position INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','active','waiting','done')),
 due_date TEXT, waiting_reason TEXT NOT NULL DEFAULT '', UNIQUE(project_id,code));
CREATE TABLE IF NOT EXISTS project_documents(
 id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
 title TEXT NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL,
 visibility TEXT NOT NULL CHECK(visibility IN ('owners','design','team')),
 actor_id INTEGER NOT NULL REFERENCES users(id));
CREATE TABLE IF NOT EXISTS task_comments(
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
 actor_id INTEGER NOT NULL REFERENCES users(id), body TEXT NOT NULL,
 at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')));
CREATE TABLE IF NOT EXISTS task_dependencies(
 task_id INTEGER NOT NULL REFERENCES tasks(id), depends_on_id INTEGER NOT NULL REFERENCES tasks(id),
 PRIMARY KEY(task_id,depends_on_id), CHECK(task_id<>depends_on_id));
CREATE INDEX IF NOT EXISTS tasks_by_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS comments_by_task ON task_comments(task_id);
CREATE TABLE IF NOT EXISTS task_materials(
 id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
 title TEXT NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL, actor_id INTEGER NOT NULL REFERENCES users(id));
