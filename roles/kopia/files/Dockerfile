FROM docker.io/kopia/kopia:0.23.1 AS kopia
FROM docker.io/library/python:3.14.10-slim-trixie
COPY --from=kopia /bin/kopia /usr/local/bin/kopia
WORKDIR /app
COPY --chmod=755 backup.py restore.py runtime.py prepare.sh cleanup.sh /app/
RUN mkdir -p /config /cache /logs /source /restore \
    && chown -R 1000:1000 /config /cache /logs /source /restore
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/config \
    KOPIA_CONFIG_PATH=/config/repository.config KOPIA_LOG_DIR=/logs \
    KOPIA_CACHE_DIRECTORY=/cache
USER 1000:1000
ENTRYPOINT ["python3", "/app/runtime.py"]
