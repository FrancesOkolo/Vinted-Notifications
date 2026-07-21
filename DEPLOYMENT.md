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

The current update needs no new service or manual database migration. All new
security environment variables are optional, so the existing launch command
continues to work unchanged. Do not upload or replace `data/`, `logs/`, or
`/root/vinted.env`.

## Optional security settings

Add these to `/root/vinted.env` when you are ready to enable them:

```dotenv
# Require this token for the RSS feed. Keep it private and use a long random value.
VN_RSS_TOKEN=replace-with-a-long-random-token

# Restrict accepted Host headers. Include the values used by Docker health checks.
VN_WEB_TRUSTED_HOSTS=138.68.136.141,127.0.0.1,localhost

# Leave false while accessing http://IP:8000 directly. Change to true only after
# putting the app behind an HTTPS reverse proxy.
VN_WEB_HTTPS=false
```

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
  -p 8000:8000 \
  -p 8080:8080 \
  -v /root/Vinted-Notifications/data:/app/data \
  -v /root/Vinted-Notifications/logs:/app/logs \
  --restart unless-stopped \
  my-vinted:candidate
```

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
