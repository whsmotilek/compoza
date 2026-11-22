#!/bin/bash
set -euo pipefail
umask 077
root=/opt/compoza
test ! -e "$root/compose.yml" || { echo 'Existing deployment: use a reviewed upgrade'; exit 1; }
test "$(sha256sum /opt/vnachale-recovery/Caddyfile | cut -d' ' -f1)" = 7c078572d7836ee9213dffa9ee3fea955b08ee1d032d2a7b6196452e3b602133 || { echo 'Gateway changed; reconcile before install'; exit 1; }
test "$(docker inspect vnachale-recovery-gateway-1 --format '{{(index .NetworkSettings.Networks "projects-shared-proxy").IPAddress}}')" = 172.19.0.5
mkdir -p "$root/release" "$root/data" "$root/backups" "$root/private"
chmod 700 "$root/private"
chown 10001:10001 "$root/data" "$root/backups"
chmod 700 "$root/data" "$root/backups"
tar -xzf /tmp/compoza-source.tgz -C "$root/release"
docker build --pull=false -t compoza-core:stage1-20261122 -f "$root/release/deploy/Dockerfile" "$root/release"
cp "$root/release/deploy/compose.yml" "$root/compose.yml"
echo 'COMPOZA_RELEASE=stage1-20261122' > "$root/.env"
cd "$root"
docker compose -p compoza up -d
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:18190/api/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:18190/api/health
docker exec -i compoza-core python - < "$root/release/deploy/bootstrap-pilot.py"
cp /opt/vnachale-recovery/Caddyfile "$root/private/Caddyfile.before"
cat /opt/vnachale-recovery/Caddyfile "$root/release/deploy/Caddyfile.fragment" > "$root/private/Caddyfile.candidate"
docker cp "$root/private/Caddyfile.candidate" vnachale-recovery-gateway-1:/tmp/compoza-Caddyfile
docker exec vnachale-recovery-gateway-1 caddy validate --config /tmp/compoza-Caddyfile --adapter caddyfile
test "$(sha256sum /opt/vnachale-recovery/Caddyfile | cut -d' ' -f1)" = 7c078572d7836ee9213dffa9ee3fea955b08ee1d032d2a7b6196452e3b602133 || { echo 'Gateway changed during build; reconcile before reload'; exit 1; }
cat "$root/private/Caddyfile.candidate" > /opt/vnachale-recovery/Caddyfile
if ! docker exec vnachale-recovery-gateway-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile; then
  cat "$root/private/Caddyfile.before" > /opt/vnachale-recovery/Caddyfile
  docker exec vnachale-recovery-gateway-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
  exit 1
fi
install -m 700 "$root/release/deploy/backup.sh" "$root/backup.sh"
printf '17 3 * * * root /opt/compoza/backup.sh >> /opt/compoza/backup.log 2>&1\n' > /etc/cron.d/compoza-backup
chmod 644 /etc/cron.d/compoza-backup
"$root/backup.sh"
echo 'Compoza isolated installation finished; verify HTTPS and private handover next.'
