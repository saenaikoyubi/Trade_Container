from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(service: str) -> None:
    log_dir = Path(os.getenv("LOG_DIR", "/app/var/log"))
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = TimedRotatingFileHandler(
        log_dir / f"{service}.jsonl", when="midnight", backupCount=30, encoding="utf-8", utc=True
    )
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [stream, file_handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))

