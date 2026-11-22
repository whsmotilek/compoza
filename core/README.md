# Compoza pilot backend

The stage-I backend provides session login, server-enforced role boundaries, project setup, task workflows, an audit log, and verified backup/restore commands. It does not implement financial reporting, photo uploads, delivery accounting, or full production templates.

## Runtime

Use Python 3.11+ with OpenSSL scrypt support. Install `core/requirements.txt`. Tests additionally require `httpx==0.28.1` and run with:

```sh
python -m unittest core.test_core -v
```

Configure `COMPOZA_DB` as an absolute path on a private persistent volume, `COMPOZA_ORIGINS` as comma-separated exact HTTPS origins (no paths), and `COMPOZA_STATIC_DIR` as the frontend directory. App and API should share one HTTPS origin. The static frontend is mounted at `/app/`; health is `/api/health`.

```sh
uvicorn core.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

SQLite is a deliberate small-pilot choice: one API worker, short transactions, busy timeout, durable local volume, no network filesystem. Before more concurrent writing or scaling to several workers, plan a PostgreSQL migration. Schema migrations are numbered: initial schema 1, task context and demo markers 2, workspace context and scoped materials 3. Initialization rejects an unknown schema version.

Terminate TLS at a reverse proxy. Do not expose the backend port publicly. Trust forwarded headers only from the exact proxy address and have that proxy replace incoming forwarding headers. The application reads the resolved ASGI client address, never raw `X-Forwarded-For`. Set worker/concurrency and request body limits at the server/proxy. Without trusted proxy setup, the IP limiter sees the proxy as one shared address.

## Sessions and permissions

Passwords use salted scrypt N=16384/r=8/p=5; hash strings record parameters. Random session tokens are stored only as SHA-256 hashes in SQLite. Sessions expire after eight hours. Cookies are HttpOnly, Secure, SameSite=Lax, host-only. Mutations require both an exact allowed Origin and a session CSRF token. Fetch `/api/auth/csrf` before login, then use the returned token in `X-CSRF-Token`. Login returns a new token. No tokens or passwords belong in localStorage.

Login counters persist across restarts: five failed attempts per account or twenty per resolved client IP within fifteen minutes trigger a fifteen-minute lock. Successful login resets only the account counter. Password changes have their own persistent five-attempt user limit. Responses do not distinguish missing accounts and wrong passwords.

- Owner: all current project/task data, users, audit. Separate personal accounts should be used for the two owners.
- Admin: all operational projects/tasks; no user administration or audit; no financial endpoints exist in stage I.
- Designer: assigned projects and their tasks; no other projects.
- Foreman: assigned projects and tasks.
- Master: assigned projects and only their own tasks; can start work or submit it for review, cannot approve or reopen submitted/completed work.

Project setup is editable only by owner/admin. Master assignment ownership versus designer/foreman approval remains a business question for the next stage. All current changes and audit writes share a transaction. Task updates require a version and reject stale updates with 409. Audit rows cannot be updated/deleted through SQLite triggers; this is not a cryptographic tamper-proof audit.

## Offline account administration

Run commands from the repository parent of `core`, under the service user. Never expose CLI operations via a public command runner.

```sh
python -m core.cli --db /private/data/core.sqlite3 migrate
python -m core.cli --db /private/data/core.sqlite3 add-user --username owner.example --name 'Owner example' --role owner --credentials /private/credentials/owner.json
python -m core.cli --db /private/data/core.sqlite3 reset-password --username owner.example --credentials /private/credentials/reset.json
python -m core.cli --db /private/data/core.sqlite3 disable-user --username owner.example
python -m core.cli --db /private/data/core.sqlite3 seed-demo
```

Generated passwords go only into a newly created mode-0600 file, never logs/stdout. CLI reset/disable revokes all sessions. Protect and remove credential handoff files after delivery through a private channel. `seed-demo` refuses a nonempty project table and creates only three synthetic, unassigned examples with `is_demo=1`; owner can assign them using the API. No real-person credentials are seeded.

Owner HTTP administration supports POST `/api/users`, PATCH `/api/users/{id}`, and POST `/api/auth/password`. Passwords supplied there require at least fourteen characters. Role changes, disable, and password resets revoke the target user's sessions. HTTP operations cannot disable or demote the last active owner. Offline CLI is a trusted recovery interface.

## Backup and restore validation

```sh
python -m core.cli --db /private/data/core.sqlite3 backup --output /private/backups/core-YYYYMMDD.sqlite3
python -m core.cli --db /private/backups/core-YYYYMMDD.sqlite3 restore-check --output /private/restore-check/core-restored.sqlite3
```

SQLite online backup creates a consistent private copy. `restore-check` restores to an independent file, checks integrity, foreign keys, schema version, and row counts. Destination files must not exist; production is never overwritten. Check the restored application against this independent copy before an actual disaster recovery switch. After an actual restore, clear restored sessions before serving it, since the backup may contain previously valid sessions. Configure retention, encrypted off-host copies, monitoring, and periodic restore exercises at deployment.

## Test evidence

The original seventeen core tests cover sessions/logout/expiry/disabled users, CSRF/origin/CORS, persistent account and IP limits, generic login errors, own-project/task isolation, owner/admin/foreman/master boundaries, master approval restrictions, optimistic task updates and atomic audit, account management/last owner, password reset revocation and limiting, project membership changes, schema-1-to-2 migration, and independently restored data. CLI bootstrap and restore smoke checks also passed. Local scrypt hash plus verification took about 0.17 s; target-server CPU limits require a separate runtime check.

Workspace v3 adds parallel manual tracks, scoped document links, task instructions and discussion, dependencies with audited authorized override, project metadata and scoped history. These are not automatic production templates, private file hosting, photo evidence, multi-executor tasks or financial accounting. Master reads only instructions explicitly shared with their assigned task. External file ACL must be checked independently.

Restore validation requires all 13 current tables and compares their complete row contents with the source snapshot. Version-2 backups should first be restored using the matching version-2 image, or upgraded on an isolated copy; never overwrite a live database to try a migration.

Eight workspace tests additionally cover parallel tracks, waiting reasons, document visibility and history privacy, cycle rejection and dependency overrides, task comments/instructions, full-table restore verification, paused workload and old project history. Total: 25 unique tests.
