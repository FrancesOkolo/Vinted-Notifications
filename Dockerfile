FROM python:3.11-slim-bookworm

# --- build args / defaults for the runtime user ---
ARG APP_UID=10001
ARG APP_GID=10001
ARG APP_USER=appuser

WORKDIR /app

# System deps: gosu to drop privileges cleanly
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu \
 && rm -rf /var/lib/apt/lists/*

# Create runtime user/group and app dirs (inside image)
RUN groupadd -g ${APP_GID} ${APP_USER} \
 && useradd -u ${APP_UID} -g ${APP_GID} -M ${APP_USER} \
 && mkdir -p /app/data /app/logs

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

# Copy the rest of the application
COPY . .

# EntryPoint script (added below)
# Strip any CRLF line endings before use so a file edited on Windows can't
# break the container with `env: 'sh\r': No such file or directory`. This runs
# every build, so the image is self-healing regardless of how the file arrives.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
 && chmod +x /usr/local/bin/docker-entrypoint.sh

# Expose ports
EXPOSE 8000
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()" || exit 1

# Run as root for the entrypoint to adjust ownership if needed,
# then the entrypoint will drop to ${APP_USER}.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "vinted_notifications.py"]
