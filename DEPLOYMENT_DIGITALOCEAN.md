# Deploying to DigitalOcean with WinSCP + Docker

A step-by-step runbook for this fork. You edit on Windows, upload the folder
with WinSCP, and run it in Docker on a DigitalOcean droplet.

> **Two rules that save pain later**
> 1. **Run only ONE polling instance per Telegram token.** The droplet should
>    use `VN_TELEGRAM_POLLING=true`. A local copy may run alongside it only in
>    send-only mode (`VN_TELEGRAM_POLLING=false`); otherwise both instances
>    fight over `getUpdates`. Local send-only scraping can still duplicate
>    outbound item alerts, so disable local Telegram when those are unwanted.
> 2. **Never expose port 8000 to the internet.** The compose file already binds
>    it to `127.0.0.1` on the droplet. You reach the UI through an SSH tunnel
>    (Step 7), so nothing is public. Keep it that way — the Config page shows
>    your Telegram token.

---

## 0. What you need

- A DigitalOcean droplet (Ubuntu 22.04 or 24.04 is fine, smallest size works).
- Its **public IP** and SSH access (the SSH key or root password you set when
  creating it).
- **WinSCP** (file upload) and an SSH client on Windows — either **PuTTY** or the
  built-in `ssh` in PowerShell/Windows Terminal.

---

## 1. Create / pick the droplet

If you don't have one yet: DigitalOcean → Create → Droplets → Ubuntu → add your
SSH key → Create. Note the public IP (referred to below as `DROPLET_IP`, and
your login user as `USER` — usually `root`).

---

## 2. Install Docker on the droplet (over SSH)

Open a terminal on Windows and connect:

```powershell
ssh USER@DROPLET_IP
```

Then, on the droplet, install Docker Engine + the Compose plugin:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Verify:

```bash
docker --version
docker compose version
```

Create the folder the app will live in:

```bash
sudo mkdir -p /opt/vinted-notifications
sudo chown $USER:$USER /opt/vinted-notifications
```

---

## 3. Prepare files on Windows (create your `.env`)

In your local project folder, copy `.env.example` to `.env` and fill it in.
Generate two independent random secrets (run twice):

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Your `.env` should look like:

```
VN_WEB_USERNAME=admin
VN_WEB_PASSWORD=<first generated value or your own strong password>
VN_SECRET_KEY=<second generated value, 32+ chars>
VN_WEB_PORT=8000
VN_RSS_PORT=8080
VN_TELEGRAM_POLLING=true
TZ=Europe/London
```

`.env` is git-ignored and holds secrets — keep it private.

---

## 4. Upload with WinSCP

1. Open WinSCP → **New Site**: File protocol **SFTP**, Host name `DROPLET_IP`,
   User name `USER`, and your key/password. Log in.
2. On the right (droplet) pane, go into `/opt/vinted-notifications`.
3. On the left (Windows) pane, open your project folder.
4. Select everything **except** these and drag them across:
   - `.venv/`  (Windows virtualenv — do not upload)
   - `__pycache__/`, `**/__pycache__/`, `*.pyc`
   - `.git/` (optional; not needed to run)
   - `logs/` (optional; the container makes its own)
   - the `data/` backups (`*-before-*.db`) — you only need the live
     `data/vinted_notifications.db`, and only if you want to bring your queries
     over (Step 6).

   **Do** include: `.env`, `Dockerfile`, `docker-compose.yml`,
   `docker-entrypoint.sh`, all `*.py`, `migrations/`, `web_ui_plugin/`,
   `telegram_bot_plugin/`, `rss_feed_plugin/`, `pyVintedVN/`, `initial_db.sql`,
   `requirements*.txt`.

Line endings are handled for you — the Dockerfile strips any Windows CRLF from
`docker-entrypoint.sh` during the build, so it can't break on Linux.

---

## 5. Build and start

Back in your SSH session:

```bash
cd /opt/vinted-notifications
docker compose config     # sanity-check; errors here mean a bad/missing .env
docker compose up -d --build
docker compose ps
```

The first start runs all database migrations automatically (adds the
`enabled` column, the notification outbox, sets the version to 1.1.0, etc.).

---

## 6. (Optional) Bring over your 129 queries + settings

`docker compose up` created a **fresh** database in a named volume. To use your
existing one instead (queries, Telegram token, banwords, quiet hours, history):

Make sure your local `data/vinted_notifications.db` was uploaded (Step 4), then:

```bash
cd /opt/vinted-notifications
docker compose stop
docker volume ls                       # note the volume ending in _VN_data
docker run --rm \
  -v <that_VN_data_volume>:/dest \
  -v "$PWD/data":/src:ro \
  alpine cp /src/vinted_notifications.db /dest/vinted_notifications.db
docker compose start
```

> **Before doing this, stop your local app for good** — from now on the droplet
> owns the database and the Telegram bot.
>
> **Alternative (start fresh):** skip this step and instead use the web UI's
> **"Add multiple queries at once"** box to paste your query URLs, then re-enter
> your Telegram token on the Config page.

---

## 7. Access the web UI securely (SSH tunnel)

The UI is bound to the droplet's localhost, so open an encrypted tunnel from
Windows:

```powershell
ssh -L 8000:127.0.0.1:8000 -L 8080:127.0.0.1:8080 USER@DROPLET_IP
```

Leave that window open, then browse to **http://127.0.0.1:8000** on your PC and
log in with the `VN_WEB_USERNAME` / `VN_WEB_PASSWORD` from your `.env`.

(PuTTY users: Connection → SSH → Tunnels → Source `8000`, Destination
`127.0.0.1:8000`, Add; repeat for 8080; then connect.)

---

## 8. Verify it's healthy

On the droplet:

```bash
curl -s localhost:8000/healthz          # -> {"status":"ok"}
docker compose logs --tail=50           # look for the 5 processes starting, no tracebacks
```

Through the tunnel, open the **Configuration** page → the **System Health**
panel should show the scraper as **OK** within a few minutes (after the first
scrape cycle). Then click **Send test message** to confirm Telegram delivery.

---

## 9. Updating later

1. Edit files on Windows.
2. WinSCP: upload only the changed files to `/opt/vinted-notifications`.
3. On the droplet: `docker compose up -d --build`.

Your database persists in the `VN_data` volume across rebuilds — migrations for
any new version run automatically on start.

---

## 10. Backups

The only irreplaceable file is the SQLite database. To back it up:

```bash
cd /opt/vinted-notifications
docker compose stop
docker run --rm -v <VN_data_volume>:/src:ro -v "$PWD":/backup alpine \
  cp /src/vinted_notifications.db /backup/vinted-backup-$(date +%F).db
docker compose start
```

Then download that `.db` with WinSCP and keep it somewhere safe.

---

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker compose config` errors about `VN_WEB_PASSWORD`/`VN_SECRET_KEY` | Your `.env` is missing or those keys are blank. Fill them in (Step 3). |
| Telegram "Conflict: terminated by other getUpdates" in logs | Two instances are polling one token. Keep `VN_TELEGRAM_POLLING=true` on the server and use `false` for a simultaneous local test. |
| Can't reach http://127.0.0.1:8000 | The SSH tunnel window (Step 7) must stay open; re-open it. |
| `env: 'sh\r'` on start | A stale image — rebuild with `docker compose up -d --build` (the Dockerfile strips CRLF). |
| Scraper shows "Blocked" in System Health | Vinted is rate-limiting/403-ing; raise **Query Refresh Delay** or pause some queries. |
| Container keeps restarting | `docker compose logs --tail=100` and read the traceback; usually a bad `.env` value or a corrupt uploaded `.db`. |
