import asyncio
import logging
import time

import httpx
from fastapi import APIRouter

from .config import settings
from .schemas import RatePair, RatesOut

log = logging.getLogger("nightjar.rates")

# The USD to KES rate for the wall. Decoration, like the weather: cached, and every failure ends in
# "unavailable" or "old", never an error. open.er-api.com is free with no key and updates once a
# day, so one call every few hours is plenty. Its terms ask for attribution: the console says so.
router = APIRouter(prefix="/api", tags=["rates"])

URL = "https://open.er-api.com/v6/latest/USD"
# The currencies the wall cycles through, in order: US dollar, pound, yuan.
PAIRS = ["USD", "GBP", "CNY"]
TIMEOUT = 8

_cache: dict = {"at": 0.0, "value": None}
_lock = asyncio.Lock()


def parse(data: dict) -> dict | None:
    try:
        if data.get("result") != "success":
            return None
        table = data["rates"]
        kes = float(table["KES"])
        if kes <= 0:
            return None
        # Rates are per US dollar, so a pound costs KES / GBP shillings. A currency the source did
        # not quote is left out, never guessed.
        pairs = [{"code": "USD", "rate": round(kes, 2)}]
        for code in PAIRS[1:]:
            per_usd = table.get(code)
            if isinstance(per_usd, (int, float)) and per_usd > 0:
                pairs.append({"code": code, "rate": round(kes / per_usd, 2)})
        return {"rate": round(kes, 2), "pairs": pairs, "updated_unix": int(data.get("time_last_update_unix") or 0) or None}
    except (KeyError, TypeError, ValueError):
        return None


async def _fetch() -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(URL)
        resp.raise_for_status()
        return resp.json()


def _out(value: dict | None, stale: bool) -> RatesOut:
    if value is None:
        return RatesOut(available=False)
    return RatesOut(available=True, stale=stale, **{**value, "pairs": [RatePair(**p) for p in value["pairs"]]})


@router.get("/rates", response_model=RatesOut)
async def rates() -> RatesOut:
    if not settings.rates_enabled:
        return RatesOut(available=False)
    ttl = settings.rates_ttl_min * 60
    async with _lock:
        age = time.time() - _cache["at"]
        if _cache["value"] is None or age >= ttl:
            try:
                parsed = parse(await _fetch())
                if parsed is not None:
                    _cache.update(at=time.time(), value=parsed)
            except Exception as exc:
                log.warning("rates fetch failed: %s", type(exc).__name__)
        # the source only updates daily, so "old" means several missed refreshes, not one
        return _out(_cache["value"], stale=time.time() - _cache["at"] >= 4 * ttl)
