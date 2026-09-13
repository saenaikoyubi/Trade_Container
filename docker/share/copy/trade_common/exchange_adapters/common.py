from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any


def normalize_order_book(
    raw: dict[str, Any],
    *,
    received_at: datetime,
    request_duration_seconds: float,
) -> dict[str, Any]:
    book = dict(raw)
    identity = {
        "timestamp": book.get("timestamp"),
        "nonce": book.get("nonce"),
        "bids": book.get("bids") or [],
        "asks": book.get("asks") or [],
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    book["_market_data_id"] = hashlib.sha256(encoded).hexdigest()
    book["_received_at"] = received_at
    book["_request_duration_seconds"] = request_duration_seconds
    return book
