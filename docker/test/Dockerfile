FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
COPY share/copy/requirements.txt /tmp/requirements.txt
COPY share/copy/requirements-test.txt /tmp/requirements-test.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt -r /tmp/requirements-test.txt
COPY share/copy/trade_common /app/trade_common
COPY trade-api/copy/app /app/trade_api_service
COPY trade-ui/copy/app /app/trade_ui_service
COPY db-migrate/copy/migrate.py /app/db_migrate.py
COPY db-migrate/copy/migrations /app/migrations
COPY db-backup/copy/backup.sh /app/db_scripts/backup.sh
COPY db-restore/copy/restore.sh /app/db_scripts/restore.sh
COPY db-restore/copy/verify.sh /app/db_scripts/verify.sh
COPY db-restore/copy/verify.sh /usr/local/bin/verify-restore
RUN chmod +x /usr/local/bin/verify-restore
COPY test/copy/tests /app/tests
CMD ["pytest", "-q", "/app/tests"]
