FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app

COPY trade-ui/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY trade-ui/copy/app /app/service
RUN chown -R app:app /app

USER app
EXPOSE 8080
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8080"]
