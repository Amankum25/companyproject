"""Read-only live Deribit Testnet dashboard API."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import load_config

ROOT = Path(__file__).resolve().parent
API_URL = "https://test.deribit.com/api/v2"
config = load_config()
_token: str | None = None
_token_expires_at = 0.0

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
        raise RuntimeError(body["error"].get("message", "Deribit API request failed"))
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
            account_error = str(exc)

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
    }
