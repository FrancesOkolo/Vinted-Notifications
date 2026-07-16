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
done

# Group-writable by default for deployments that use a shared fsGroup.
umask 0002

# Drop privileges and replace the entrypoint process with the application.
exec gosu "$APP_UID:$APP_GID" "$@"
