#!/bin/sh
set -eu
umask 077
cd /opt/compoza
exec 9>/opt/compoza/backup.lock
flock -n 9 || exit 0
stamp=$(date -u +%Y%m%dT%H%M%SZ)
docker exec compoza-core python -m core.cli --db /data/core.sqlite3 backup --output "/backups/core-${stamp}.sqlite3"
docker exec compoza-core python -m core.cli --db "/backups/core-${stamp}.sqlite3" restore-check --output "/backups/restore-${stamp}.sqlite3"
rm -f "/opt/compoza/backups/restore-${stamp}.sqlite3"
find /opt/compoza/backups -type f -name 'core-*.sqlite3' -mtime +14 -delete
