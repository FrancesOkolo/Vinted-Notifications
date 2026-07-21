# Updating the existing server

This is the primary deployment path for Frances' server. It keeps the current
layout unchanged:

- application: `/root/Vinted-Notifications`
- private environment file: `/root/vinted.env`
- container: `vinted-mine`
- persistent data: `/root/Vinted-Notifications/data`
- persistent logs: `/root/Vinted-Notifications/logs`
- web UI: port `8000`
- RSS: port `8080`

The current update needs no new service or manual database migration. Before
starting it, confirm `/root/vinted.env` contains Web UI credentials and a
persistent `VN_SECRET_KEY`; it must also contain `VN_RSS_TOKEN` if RSS is
enabled. Do not upload or replace `data/`, `logs/`, or `/root/vinted.env`.

## Security settings

Keep these in `/root/vinted.env`:

```dotenv
# Required if RSS is enabled. Keep it private and use a long random value.
VN_RSS_TOKEN=replace-with-a-long-random-token

# Restrict accepted Host headers. Include the values used by Docker health checks.
VN_WEB_TRUSTED_HOSTS=138.68.136.141,127.0.0.1,localhost

# Leave false when accessing through the encrypted SSH tunnel described below.
# Change to true only after putting the app behind an HTTPS reverse proxy.
VN_WEB_HTTPS=false
```

Protect the environment file before starting the container:

```bash
chmod 600 /root/vinted.env
chmod 700 /root/Vinted-Notifications/data /root/Vinted-Notifications/logs
```

The application now refuses a network-facing Web UI without both credentials
and a persistent `VN_SECRET_KEY`. RSS refuses to start or serve a feed without
`VN_RSS_TOKEN`. Localhost-only Web UI development can still run without
credentials.

With `VN_RSS_TOKEN` set, an RSS client can authenticate with either an
`Authorization: Bearer <token>` header or `?token=<token>` in its feed URL.
The header is preferable because URLs may be retained in browser history or
proxy logs.

## 1. Upload the source

Upload the changed source files into `/root/Vinted-Notifications`. Exclude:

- `.env` and every token, password, or chat ID
- `data/`, `logs/`, and database files
- backups and ZIP files
- `.git/`, `.venv/`, `venv/`, `__pycache__/`, and `*.pyc`

Keep the server's existing `/root/vinted.env`, `data/`, and `logs/`.

## 2. Build without touching the live container

```bash
cd /root/Vinted-Notifications
sed -i 's/\r$//' docker-entrypoint.sh
docker build -t my-vinted:candidate .
```

If the build fails, stop here. The live container is still running and has not
been changed.

## 3. Replace the container with an easy rollback

Stop the current container, make a database backup while SQLite is quiet, and
keep the old container under a temporary name:

```bash
docker stop vinted-mine
cp -a data/vinted_notifications.db data/vinted_notifications-before-update.db
docker rename vinted-mine vinted-mine-previous
```

Start the candidate using the same environment file, ports, and bind mounts:

```bash
docker run -d \
  --name vinted-mine \
  --env-file /root/vinted.env \
  -p 127.0.0.1:8000:8000 \
  -p 127.0.0.1:8080:8080 \
  -v /root/Vinted-Notifications/data:/app/data \
  -v /root/Vinted-Notifications/logs:/app/logs \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  --restart unless-stopped \
  my-vinted:candidate
```

The ports are deliberately bound to the server's loopback interface instead
of the public internet. From Windows, open an encrypted SSH tunnel and leave
that terminal running while using the application:

```powershell
ssh -L 8000:127.0.0.1:8000 -L 8080:127.0.0.1:8080 root@138.68.136.141
```

Then browse to `http://127.0.0.1:8000`. The SSH tunnel encrypts the traffic.
For permanent public access, put the loopback ports behind an HTTPS reverse
proxy with request-rate limiting, set `VN_WEB_HTTPS=true`, and set
`VN_WEB_TRUSTED_HOSTS` to the real domain plus `127.0.0.1,localhost`. Do not set
`VN_WEB_HTTPS=true` while using only the plain local URL through an SSH tunnel.

## 4. Verify

```bash
docker ps --filter name=vinted-mine
docker logs --tail 100 vinted-mine
curl -fsS http://127.0.0.1:8000/healthz
```

Expected health response:

```json
{"status":"ok"}
```

Then check the Dashboard and Configuration pages. The scraper may need one
normal query cycle before its health information becomes current.

Do not repeatedly restart a healthy container to test scraping. The scraper
now paces large query sets and pauses automatically after repeated Vinted
`403`/`429` responses.

## Roll back

If the new container does not become healthy, restore the stopped previous
container:

```bash
docker rm -f vinted-mine
docker rename vinted-mine-previous vinted-mine
docker start vinted-mine
curl -fsS http://127.0.0.1:8000/healthz
```

The current update has no new schema migration, so the previous container can
reuse the same database. Keep
`data/vinted_notifications-before-update.db` until the update has been
confirmed stable.

Verify the backup before relying on it:

```bash
docker run --rm --entrypoint python \
  -v /root/Vinted-Notifications/data:/backup:ro \
  my-vinted:candidate \
  -c "import sqlite3; c=sqlite3.connect('file:/backup/vinted_notifications-before-update.db?mode=ro', uri=True); assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'; print('Backup integrity: OK')"
```

Download a verified backup through WinSCP/SFTP to encrypted local storage. A
backup kept only on the same droplet is not protection against loss of that
droplet. Periodically restore a downloaded copy into a temporary local test
instance to prove the recovery procedure works.

After the candidate has run successfully for a suitable period, the stopped
previous container can be removed:

```bash
docker rm vinted-mine-previous
```

This removes only the old container. It does not remove the bind-mounted
`data/` or `logs/` directories.

## Local testing while the server is live

Only one process can receive Telegram commands for a bot token. Keep the
server in polling mode. If a local test uses the same token, use send-only
mode:

```powershell
$env:VN_TELEGRAM_POLLING = "false"
.\.venv\Scripts\python.exe vinted_notifications.py
```

Send-only mode still scrapes and can send duplicate listing alerts. For a
Web-UI-only check that does neither, run:

```powershell
.\.venv\Scripts\python.exe -c "from vinted_notifications import initialise_database; initialise_database(); from web_ui_plugin.web_ui import app; app.run(host='127.0.0.1', port=8000)"
```
