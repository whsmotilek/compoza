#!/bin/sh
set -eu
umask 077
cd /opt/compoza
exec 9>/opt/compoza/backup.lock
flock -n 9 || exit 0
stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="/backups/full-${stamp}.tar.gz"
docker exec compoza-core python -m core.archive create --db /data/core.sqlite3 --uploads /data/uploads --output "$archive"
docker exec compoza-core python -m core.archive cleanup --db /data/core.sqlite3 --uploads /data/uploads
# Creation includes a full extraction, database and every photo digest verification.
find /opt/compoza/backups -type f -name 'full-*.tar.gz' -mtime +14 -delete
