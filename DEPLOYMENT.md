# Test-server deployment

This fork builds its own image. It does not pull or publish the upstream image.

## Prepare secrets

Copy `.env.example` to `.env`, then replace the example password and secret
key. Generate independent values with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Keep `.env` private. Use a separate Telegram bot token and test chat ID, or
leave Telegram disabled. Never reuse the production database volume on the
test server.

## Build and start

```powershell
docker compose config
docker compose build --pull
docker compose up -d
docker compose ps
```

Ports 8000 and 8080 bind to `127.0.0.1` by default. On a remote server, use an
SSH tunnel instead of exposing them publicly:

```powershell
ssh -L 8000:127.0.0.1:8000 -L 8080:127.0.0.1:8080 user@test-server
```

Open `http://127.0.0.1:8000` and enter the web username and password from
`.env`.

## Required smoke tests

1. Confirm `http://127.0.0.1:8000/healthz` returns `{"status":"ok"}`.
2. Confirm the Configuration page never displays the stored Telegram token.
3. Add and remove a disposable query through the web interface.
4. Register, approve, revoke, and reapprove a test Telegram account.
5. Confirm two users following one query receive one shared scrape result.
6. Confirm RSS receives an item while Telegram is disabled.
7. Exercise a quiet-hours window that crosses midnight.
8. Restart the container and confirm settings and queries persist.

## Backup and rollback

Stop the container before copying the SQLite volume for a manual backup. Tag
images with `VN_IMAGE_TAG` so a previous image can be selected without using
`latest`. Do not delete the old volume until the test deployment has passed.
