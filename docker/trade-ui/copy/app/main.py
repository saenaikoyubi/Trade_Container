from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles


STATIC_DIR = Path(__file__).resolve().parent / "static"
ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")
API_BASE_URL = os.getenv("TRADE_API_BASE_URL", "http://trade-api:8000").rstrip("/")
API_TOKEN_FILE = os.getenv("API_TOKEN_FILE", "/run/api-secrets/api_token")

app = FastAPI(
    title="Trade History UI",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class UiOrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    exchange_id: str = Field(min_length=1, max_length=32)
    symbol: str = Field(min_length=1, max_length=64)
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    reduce_only: bool = False

    @model_validator(mode="after")
    def validate_limit_price(self):
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit_price is required for limit orders")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("limit_price must be omitted for market orders")
        return self


class UiPositionClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    exchange_id: str = Field(min_length=1, max_length=32)
    symbol: str | None = Field(default=None, min_length=1, max_length=64)


class UiControlUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    reason: str | None = Field(default=None, max_length=500)


@app.middleware("http")
async def limit_mutation_body(request: Request, call_next):
    if request.method == "POST" and request.url.path.startswith("/ui-api/"):
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        if content_length > 16_384:
            return Response(
                content=b'{"detail":"request body is too large"}',
                status_code=413,
                media_type="application/json",
            )
    return await call_next(request)


def _api_token() -> str:
    path = Path(API_TOKEN_FILE)
    if not path.is_file():
        raise RuntimeError("API token is unavailable")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("API token is unavailable")
    return token


def _query_params(request: Request, allowed: set[str]) -> list[tuple[str, str]]:
    return [(key, value) for key, value in request.query_params.multi_items() if key in allowed]


async def _proxy_get(
    path: str,
    params: list[tuple[str, str]] | None = None,
    *,
    timeout_seconds: float = 8.0,
) -> Response:
    try:
        token = _api_token()
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            upstream = await client.get(
                f"{API_BASE_URL}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
    except (OSError, RuntimeError, httpx.HTTPError):
        return Response(
            content=b'{"detail":"trade API is unavailable"}',
            status_code=503,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


async def _proxy_post(path: str, payload: dict | None = None) -> Response:
    try:
        token = _api_token()
        async with httpx.AsyncClient(timeout=8.0) as client:
            upstream = await client.post(
                f"{API_BASE_URL}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
    except (OSError, RuntimeError, httpx.HTTPError):
        return Response(
            content=b'{"detail":"trade API is unavailable"}',
            status_code=503,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    supplied = urlsplit(origin)
    expected = urlsplit(str(request.base_url))
    if (supplied.scheme, supplied.netloc) != (expected.scheme, expected.netloc):
        raise HTTPException(status_code=403, detail="cross-origin mutation is not allowed")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/ui-api/history/orders", include_in_schema=False)
async def order_history(request: Request):
    allowed = {"from", "to", "exchange_id", "symbol", "side", "status", "strategy_id", "cursor", "limit"}
    return await _proxy_get("/api/v1/history/orders", _query_params(request, allowed))


@app.get("/ui-api/history/fills", include_in_schema=False)
async def fill_history(request: Request):
    allowed = {"from", "to", "exchange_id", "symbol", "side", "cursor", "limit"}
    return await _proxy_get("/api/v1/history/fills", _query_params(request, allowed))


@app.get("/ui-api/current-positions", include_in_schema=False)
async def current_positions(request: Request):
    allowed = {"exchange_id", "symbol"}
    return await _proxy_get(
        "/api/v1/current-positions",
        _query_params(request, allowed),
        timeout_seconds=20.0,
    )


@app.get("/ui-api/exchanges", include_in_schema=False)
async def exchanges():
    return await _proxy_get("/api/v1/exchanges")


@app.get("/ui-api/trading-control", include_in_schema=False)
async def trading_control():
    return await _proxy_get("/api/v1/trading-control")


@app.post("/ui-api/orders", include_in_schema=False)
async def create_order(payload: UiOrderCreate, request: Request):
    _require_same_origin(request)
    upstream_payload = payload.model_dump(mode="json", exclude_none=True)
    upstream_payload["strategy_id"] = "manual"
    return await _proxy_post(
        "/api/v1/orders",
        upstream_payload,
    )


@app.post("/ui-api/positions/close", include_in_schema=False)
async def close_positions(payload: UiPositionClose, request: Request):
    _require_same_origin(request)
    upstream_payload = payload.model_dump(mode="json", exclude_none=True)
    upstream_payload["strategy_id"] = "manual"
    return await _proxy_post(
        "/api/v1/positions/close",
        upstream_payload,
    )


@app.post("/ui-api/close-only", include_in_schema=False)
async def set_close_only(payload: UiControlUpdate, request: Request):
    _require_same_origin(request)
    return await _proxy_post(
        "/api/v1/close-only",
        payload.model_dump(mode="json", exclude_none=True),
    )


@app.get("/ui-api/orders/{order_id}/fills", include_in_schema=False)
async def order_fills(order_id: str):
    if not ORDER_ID_PATTERN.fullmatch(order_id):
        return Response(
            content=b'{"detail":"invalid order id"}',
            status_code=422,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    return await _proxy_get(f"/api/v1/orders/{order_id}/fills")


@app.post("/ui-api/orders/{order_id}/cancel", include_in_schema=False)
async def cancel_order(order_id: str, request: Request):
    _require_same_origin(request)
    if not ORDER_ID_PATTERN.fullmatch(order_id):
        return Response(
            content=b'{"detail":"invalid order id"}',
            status_code=422,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    return await _proxy_post(f"/api/v1/orders/{order_id}/cancel")
