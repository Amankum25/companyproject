"""Read-only live Deribit Testnet dashboard API."""

from __future__ import annotations

import asyncio
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import load_config

ROOT = Path(__file__).resolve().parent
API_URL = "https://test.deribit.com/api/v2"
config = load_config()
_token: str | None = None
_token_expires_at = 0.0
_pending_previews: dict[str, dict[str, Any]] = {}


class PreviewRequest(BaseModel):
    legs: list[str] = Field(min_length=1, max_length=2)
    amount: str = "0.1"


class SubmitRequest(BaseModel):
    preview_id: str
    confirmation: str

app = FastAPI(title="BTC Options Dashboard", docs_url=None, redoc_url=None)
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")


async def rpc(method: str, params: dict[str, Any] | None = None, token: str | None = None) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = {"jsonrpc": "2.0", "id": int(time.time_ns() % 2_000_000_000), "method": method, "params": params or {}}
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.post(API_URL, json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
    if "error" in body:
        error = body["error"]
        raise RuntimeError(f"{error.get('code', 'Deribit API error')}: {error.get('message', 'Deribit API request failed')}")
    return body.get("result")


async def account_token() -> str | None:
    global _token, _token_expires_at
    if not config.api_key or not config.api_secret:
        return None
    if _token and time.monotonic() < _token_expires_at:
        return _token
    result = await rpc("public/auth", {
        "grant_type": "client_credentials",
        "client_id": config.api_key,
        "client_secret": config.api_secret,
    })
    _token = result["access_token"]
    _token_expires_at = time.monotonic() + max(30, int(result.get("expires_in", 900)) - 30)
    return _token


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/api/dashboard")
async def dashboard() -> dict[str, Any]:
    if config.venue != "DERIBIT":
        raise HTTPException(503, "This live dashboard currently supports the configured Deribit Testnet only.")
    try:
        index, instruments, summaries = await asyncio.gather(
            rpc("public/get_index_price", {"index_name": "btc_usd"}),
            rpc("public/get_instruments", {"currency": "BTC", "kind": "option", "expired": False}),
            rpc("public/get_book_summary_by_currency", {"currency": "BTC", "kind": "option"}),
        )
        price = float(index["index_price"])
        books = {row["instrument_name"]: row for row in summaries}
        by_expiry: dict[int, dict[float, dict[str, dict[str, Any]]]] = {}
        for instrument in instruments:
            book = books.get(instrument["instrument_name"])
            if not book:
                continue
            expiry = int(instrument["expiration_timestamp"])
            strike = float(instrument["strike"])
            option_type = instrument["option_type"]
            by_expiry.setdefault(expiry, {}).setdefault(strike, {})[option_type] = {**instrument, **book}

        now_ms = int(time.time() * 1000)
        chosen: tuple[int, float, dict[str, Any], dict[str, Any]] | None = None
        for expiry in sorted(by_expiry):
            if expiry <= now_ms:
                continue
            pairs = [(strike, sides) for strike, sides in by_expiry[expiry].items() if "call" in sides and "put" in sides]
            if pairs:
                strike, sides = min(pairs, key=lambda item: abs(item[0] - price))
                chosen = expiry, strike, sides["call"], sides["put"]
                break

        pair = None
        if chosen:
            expiry, strike, call, put = chosen
            pair = {
                "expiry": datetime.fromtimestamp(expiry / 1000, timezone.utc).strftime("%d %b %Y"),
                "strike": strike,
                "call": quote(call),
                "put": quote(put),
                "premium_btc": round(float(call.get("mark_price") or 0) + float(put.get("mark_price") or 0), 8),
            }

        account = None
        account_error = None
        try:
            token = await account_token()
            if token:
                summary = await rpc("private/get_account_summary", {"currency": "BTC", "extended": True}, token)
                account = {"equity_btc": summary.get("equity"), "available_btc": summary.get("available_funds"), "initial_margin_btc": summary.get("initial_margin")}
            else:
                account_error = "Add Deribit Testnet API credentials to .env to display account balances."
        except Exception as exc:
            account_error = (
                "Deribit rejected the client ID/secret (13004 invalid_credentials). "
                "Verify both values are from the Deribit Testnet API page and update .env."
                if "13004" in str(exc) or "invalid_credentials" in str(exc)
                else str(exc)
            )

        return {
            "venue": "Deribit Testnet",
            "mode": "READ ONLY",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "btc_index": price,
            "pair": pair,
            "account": account,
            "account_error": account_error,
            "market_status": "connected",
            "option_count": len(instruments),
        }
    except (httpx.HTTPError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise HTTPException(502, f"Could not load live Deribit Testnet data: {exc}") from exc


def quote(instrument: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": instrument.get("instrument_name"),
        "bid": instrument.get("bid_price"),
        "ask": instrument.get("ask_price"),
        "mark": instrument.get("mark_price"),
        "iv": instrument.get("mark_iv"),
        "open_interest": instrument.get("open_interest"),
        "volume": instrument.get("volume_usd"),
        "timestamp": instrument.get("timestamp"),
        "min_amount": instrument.get("min_trade_amount", 0.1),
        "amount_step": instrument.get("amount_step", 0.1),
    }


@app.post("/api/orders/preview")
async def preview_orders(request: PreviewRequest) -> dict[str, Any]:
    legs = request.legs
    if len(set(legs)) != len(legs) or any(leg not in {"call", "put"} for leg in legs):
        raise HTTPException(400, "Select Call, Put, or both exactly once.")
    try:
        amount = Decimal(request.amount)
    except InvalidOperation as exc:
        raise HTTPException(400, "Enter a valid BTC option amount.") from exc
    if not amount.is_finite() or amount <= 0:
        raise HTTPException(400, "Order amount must be a positive number.")
    max_amount = Decimal(config.order_quantity)
    if amount > max_amount:
        raise HTTPException(400, f"Order amount cannot exceed ORDER_QUANTITY ({max_amount} BTC) configured in .env.")
    if config.venue != "DERIBIT":
        raise HTTPException(503, "Order entry is enabled only for Deribit Testnet.")
    if not config.api_key or not config.api_secret:
        raise HTTPException(401, "Add Deribit Testnet client ID and secret to the private .env file.")

    try:
        token = await account_token()
    except Exception as exc:
        raise _auth_error(exc) from exc
    if not token:
        raise HTTPException(401, "Deribit Testnet credentials are not configured.")

    snapshot = await dashboard()
    pair = snapshot.get("pair")
    if not pair:
        raise HTTPException(409, "No matched BTC call and put quotes are available. Refresh and try again.")

    now_ms = int(time.time() * 1000)
    order_legs: dict[str, dict[str, Any]] = {}
    for leg in legs:
        item = pair[leg]
        price = item.get("ask")
        timestamp = item.get("timestamp")
        if price is None or float(price) <= 0:
            raise HTTPException(409, f"The {leg} has no valid ask price. Refresh the quotes.")
        if timestamp is not None and now_ms - int(timestamp) > 15_000:
            raise HTTPException(409, f"The {leg} quote is stale. Refresh the preview.")
        minimum = Decimal(str(item.get("min_amount") or "0.1"))
        step = Decimal(str(item.get("amount_step") or minimum))
        if amount < minimum or (amount / step) % 1 != 0:
            raise HTTPException(400, f"Amount must be at least {minimum} BTC and use increments of {step} BTC.")
        order_legs[leg] = {
            "instrument_name": item["name"],
            "price": str(price),
            "amount": str(amount),
            "timestamp": timestamp,
        }

    preview_id = secrets.token_urlsafe(24)
    _pending_previews[preview_id] = {
        "created_at": time.monotonic(),
        "legs": order_legs,
        "strike": pair["strike"],
        "expiry": pair["expiry"],
        "amount": str(amount),
    }
    _expire_previews()
    return {
        "preview_id": preview_id,
        "expires_in": 20,
        "environment": "Deribit Testnet",
        "strike": pair["strike"],
        "expiry": pair["expiry"],
        "amount": str(amount),
        "legs": {
            leg: {"instrument_name": item["instrument_name"], "limit_price_btc": item["price"]}
            for leg, item in order_legs.items()
        },
    }


@app.post("/api/orders/submit")
async def submit_orders(request: SubmitRequest) -> dict[str, Any]:
    _expire_previews()
    preview = _pending_previews.pop(request.preview_id, None)
    if not preview:
        raise HTTPException(410, "This preview expired or was already used. Refresh the quotes and preview again.")
    sides = list(preview["legs"])
    expected = "SELL CALL AND PUT" if len(sides) == 2 else f"SELL {sides[0].upper()}"
    if request.confirmation != expected:
        raise HTTPException(400, f"Confirmation text did not match. Type exactly: {expected}")

    try:
        token = await account_token()
    except Exception as exc:
        raise _auth_error(exc) from exc
    if not token:
        raise HTTPException(401, "Deribit Testnet credentials are not configured.")

    # Recheck the selected instruments and quotes after the user confirmation.
    snapshot = await dashboard()
    pair = snapshot.get("pair")
    if not pair or pair.get("strike") != preview["strike"] or pair.get("expiry") != preview["expiry"]:
        raise HTTPException(409, "The selected option pair changed. Refresh the dashboard and preview again.")
    now_ms = int(time.time() * 1000)
    for leg, item in preview["legs"].items():
        current = pair.get(leg) or {}
        if current.get("name") != item["instrument_name"] or str(current.get("ask")) != item["price"]:
            raise HTTPException(409, "The option quote changed after preview. Refresh and preview again.")
        if current.get("timestamp") is not None and now_ms - int(current["timestamp"]) > 15_000:
            raise HTTPException(409, "The option quote is stale. Refresh and preview again.")

    accepted: list[dict[str, Any]] = []
    for leg in sides:
        item = preview["legs"][leg]
        try:
            result = await rpc("private/sell", {
                "instrument_name": item["instrument_name"],
                "amount": float(item["amount"]),
                "type": "limit",
                "price": float(item["price"]),
                "time_in_force": "good_til_cancelled",
            }, token)
        except Exception as exc:
            detail = str(exc)
            if accepted:
                raise HTTPException(502, f"{leg.title()} order failed after {accepted[0]['leg']} was accepted. Check Deribit Testnet orders immediately. Exchange error: {detail}") from exc
            if "13004" in detail or "invalid_credentials" in detail:
                raise _auth_error(exc) from exc
            raise HTTPException(502, f"Deribit rejected the {leg} order: {detail}") from exc
        order = result.get("order", {})
        accepted.append({
            "leg": leg,
            "instrument_name": item["instrument_name"],
            "order_id": order.get("order_id"),
            "state": order.get("order_state"),
            "amount": order.get("amount", item["amount"]),
            "price": order.get("price", item["price"]),
        })
    return {"environment": "Deribit Testnet", "orders": accepted}


def _expire_previews() -> None:
    now = time.monotonic()
    for preview_id, preview in list(_pending_previews.items()):
        if now - preview["created_at"] > 20:
            _pending_previews.pop(preview_id, None)


def _auth_error(exc: Exception) -> HTTPException:
    detail = str(exc)
    if "13004" in detail or "invalid_credentials" in detail:
        return HTTPException(401, "Deribit rejected the client ID/secret (13004 invalid_credentials). Verify both values are from the Deribit Testnet API page and replace them in .env.")
    return HTTPException(401, f"Deribit Testnet authentication failed: {detail}")
