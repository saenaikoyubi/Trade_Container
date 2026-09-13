FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
COPY share/copy/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY share/copy/trade_common /app/trade_common
COPY paper-executor/copy/app /app/service
RUN mkdir -p /app/var/log && chown -R app:app /app
USER app
CMD ["python", "-m", "service.main"]

