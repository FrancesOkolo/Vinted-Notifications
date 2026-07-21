#!/usr/bin/env sh
set -eu

APP_UID="${APP_UID:-10001}"
APP_GID="${APP_GID:-10001}"

mkdir -p /app/data /app/logs

owner_id() {
  stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1" 2>/dev/null
}

for directory in /app/data /app/logs; do
  if [ "$(owner_id "$directory")" != "$APP_UID" ]; then
    chown -R "$APP_UID:$APP_GID" "$directory"
  fi
  find "$directory" -type d -exec chmod 700 {} +
  find "$directory" -type f -exec chmod 600 {} +
done

# Runtime data includes bot credentials and chat IDs. New files are private to
# the unprivileged application account by default.
umask 0077

# Drop privileges and replace the entrypoint process with the application.
exec gosu "$APP_UID:$APP_GID" "$@"
