"""Delta Fare Map: local FastAPI server. Run with `uvicorn app:app` and open http://localhost:8000."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import date

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from deltamap import airports
from deltamap.cache import Store, explore_key
from deltamap.config import REGION_BY_ID, REGIONS, ROOT, load_settings
from deltamap.providers import DemoProvider, ProviderError, SerpApiProvider

log = logging.getLogger("deltamap")
settings = load_settings()
store = Store(settings.db_path)


class State:
    provider = None
    client: httpx.AsyncClient | None = None
    key_problem: str | None = None
    quota: dict | None = None
    quota_at: float = 0.0
    quota_lock = asyncio.Lock()


S = State()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    S.client = httpx.AsyncClient()
    if settings.provider == "serpapi" and settings.serpapi_key:
        S.provider = SerpApiProvider(settings, store, S.client)
        q = await S.provider.quota()  # free call; tells us whether the key works
        if not q.get("ok") and "invalid api key" in (q.get("error") or "").lower():
            S.key_problem = q["error"]
            log.warning("SerpApi key rejected (%s); falling back to demo data", q["error"])
            S.provider = DemoProvider(store)
        else:
            S.quota, S.quota_at = q, time.time()
    else:
        if settings.provider == "serpapi":
            S.key_problem = "No SerpApi key found (set SERPAPI_KEY or create serpapi.key)."
        S.provider = DemoProvider(store)
    yield
    await S.client.aclose()


app = FastAPI(title="Delta Fare Map", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


# -- helpers -----------------------------------------------------------------------
def _p() -> str:
    return S.provider.name


def _dates(depart: str, ret: str) -> None:
    try:
        d, r = date.fromisoformat(depart), date.fromisoformat(ret)
    except ValueError:
        raise HTTPException(400, "Dates must be YYYY-MM-DD")
    if r <= d:
        raise HTTPException(400, "Return date must be after the departure date")
    if d < date.today():
        raise HTTPException(400, "Departure date is in the past")


def _airport(code: str) -> str:
    code = (code or "").strip().upper()
    if not airports.get(code):
        raise HTTPException(400, f"Unknown airport code: {code or '(empty)'}")
    return code


def _tokhash(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()[:20]


def _explore_key(origin, depart, ret, region_id) -> str:
    return explore_key(_p(), settings.airline_filter, origin, depart, ret, region_id)


def _main_key(origin, dest, depart, ret) -> str:
    return f"{_p()}|{settings.airline_filter}|{origin}|{dest}|{depart}|{ret}"


def _regions(regions: str | None):
    if not regions:
        return list(REGIONS)
    picked = [REGION_BY_ID[r] for r in regions.split(",") if r in REGION_BY_ID]
    if not picked:
        raise HTTPException(400, "No known regions selected")
    return picked


def _domestic(origin: str, dest: str) -> bool:
    return airports.country(origin) == "US" and airports.country(dest) == "US"


async def quota(force: bool = False) -> dict:
    async with S.quota_lock:
        if force or S.quota is None or time.time() - S.quota_at > settings.quota_ttl:
            S.quota, S.quota_at = await S.provider.quota(), time.time()
        q = dict(S.quota)
    q["provider"] = _p()
    q["key_problem"] = S.key_problem
    q["low_warn"] = settings.low_quota_warn
    q["logged_calls_30d"] = store.calls_since("serpapi", 30 * 86400)
    return q


def _spent(n: int) -> None:
    """Keep the cached quota honest between Account API refreshes."""
    if S.quota and n:
        for k in ("left", "hour_left"):
            if S.quota.get(k) is not None:
                S.quota[k] = max(0, S.quota[k] - n)


async def guard(live_calls: int, confirm: bool) -> dict | None:
    """None if the live calls may go ahead, else a dict explaining why not."""
    if live_calls == 0 or _p() == "demo":
        return None
    q = await quota()
    if not q.get("ok"):
        return None  # Account API down: don't block, the search itself will report errors
    left, hour_left = q.get("left"), q.get("hour_left")
    if left is not None and left < live_calls:
        return {"blocked": "monthly", "left": left, "calls": live_calls,
                "message": f"Only {left} searches left this month; this needs {live_calls}."}
    if hour_left is not None and hour_left < live_calls:
        return {"blocked": "hourly", "left": left, "calls": live_calls,
                "message": f"Hourly limit reached ({hour_left} left this hour); try again later."}
    if left is not None and left < settings.low_quota_warn and not confirm:
        return {"confirm_needed": True, "left": left, "calls": live_calls,
                "message": f"{left} searches left this month. This uses {live_calls}. Continue?"}
    return None


def _apply_main(origin, depart, ret, dests: list[dict]) -> list[dict]:
    for d in dests:
        known, price = store.main_fare(_main_key(origin, d["code"], depart, ret), settings.map_ttl)
        if known and price is not None:
            d["explore_price"] = d["price"]
            d["price"] = price
            d["fare_is_main"] = "yes"
    return dests


# -- pages ---------------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/config")
async def config():
    return {
        "provider": _p(),
        "key_problem": S.key_problem,
        "default_origin": settings.default_origin,
        "airline_filter": settings.airline_filter,
        "regions": [{"id": r.id, "name": r.name} for r in REGIONS],
        "low_quota_warn": settings.low_quota_warn,
    }


@app.get("/api/airports")
async def airport_search(q: str = ""):
    return airports.search(q)


@app.get("/api/airport/{code}")
async def airport_get(code: str):
    a = airports.get(code)
    if not a:
        raise HTTPException(404, "Unknown airport")
    return {"code": code.upper(), "name": a["name"], "city": a["city"], "country": a["country"],
            "lat": a["lat"], "lon": a["lon"]}


@app.get("/api/quota")
async def quota_ep(refresh: bool = False):
    return await quota(force=refresh)


@app.get("/api/estimate")
async def estimate(origin: str, depart: str, ret: str, regions: str | None = None):
    origin = _airport(origin)
    _dates(depart, ret)
    regs = _regions(regions)
    cached = [r.id for r in regs if store.has(_explore_key(origin, depart, ret, r.id))]
    live = sum(S.provider.calls_for_explore(r) for r in regs if r.id not in cached)
    return {"live_calls": live, "cached_regions": cached,
            "guard": await guard(live, confirm=False), "provider": _p()}


@app.get("/api/explore")
async def explore(origin: str, depart: str, ret: str, regions: str | None = None, confirm: bool = False):
    origin = _airport(origin)
    _dates(depart, ret)
    regs = _regions(regions)
    todo = [r for r in regs if not store.has(_explore_key(origin, depart, ret, r.id))]
    live = sum(S.provider.calls_for_explore(r) for r in todo)
    block = await guard(live, confirm)

    async def run_region(region):
        key = _explore_key(origin, depart, ret, region.id)
        cached = store.get(key)
        if cached is not None:
            return {"region": region.id, "destinations": _apply_main(origin, depart, ret, cached),
                    "cached": True, "calls": 0}
        if block:
            return {"region": region.id, "destinations": [], "cached": False, "calls": 0,
                    "error": block["message"], "blocked": True}
        n = S.provider.calls_for_explore(region)
        try:
            dests = await S.provider.explore(origin, depart, ret, region)
        except ProviderError as e:
            return {"region": region.id, "destinations": [], "cached": False, "calls": n, "error": str(e)}
        finally:
            _spent(n if _p() != "demo" else 0)
        store.put(key, dests, settings.map_ttl)
        return {"region": region.id, "destinations": _apply_main(origin, depart, ret, dests),
                "cached": False, "calls": n}

    async def stream():
        if block and block.get("confirm_needed"):
            yield f"event: confirm\ndata: {json.dumps(block)}\n\n"
            return
        yield f"event: start\ndata: {json.dumps({'regions': len(regs), 'live_calls': 0 if block else live})}\n\n"
        for fut in asyncio.as_completed([run_region(r) for r in regs]):
            res = await fut
            yield f"event: region\ndata: {json.dumps(res)}\n\n"
        yield f"event: done\ndata: {json.dumps(await quota())}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _coords_for(itins: list[dict], extra: list[str]) -> dict:
    codes = set(extra)
    for it in itins:
        for s in it["segments"]:
            codes.update([s["from"], s["to"]])
    return {c: airports.coords(c) for c in codes if c and airports.coords(c)}


def _gf_url(origin, dest, depart, ret) -> str:
    from urllib.parse import quote
    return ("https://www.google.com/travel/flights?q="
            + quote(f"Flights from {origin} to {dest} on {depart} through {ret} on Delta"))


async def _cached_call(key, ttl, confirm, fn):
    hit = store.get(key)
    if hit is not None:
        hit["cached"] = True
        return hit
    block = await guard(1, confirm)
    if block:
        return JSONResponse(block, status_code=409)
    try:
        res = await fn()
    except ProviderError as e:
        raise HTTPException(502, str(e))
    finally:
        _spent(1 if _p() != "demo" else 0)
    store.put(key, res, ttl)
    res["cached"] = False
    return res


@app.get("/api/flights")
async def flights(origin: str, dest: str, depart: str, ret: str, confirm: bool = False):
    origin, dest = _airport(origin), _airport(dest)
    _dates(depart, ret)
    dom = _domestic(origin, dest)
    key = f"{_p()}|out|{settings.airline_filter}|{origin}|{dest}|{depart}|{ret}|{dom}"

    async def fn():
        res = await S.provider.outbound(origin, dest, depart, ret, exclude_basic=dom)
        res["main_confirmed"] = dom
        res["domestic"] = dom
        res["coords"] = _coords_for(res["itineraries"], [origin, dest])
        res["google_flights_url"] = res.get("google_flights_url") or _gf_url(origin, dest, depart, ret)
        if dom:
            prices = [it["price"] for it in res["itineraries"]]
            store.put_main_fare(_main_key(origin, dest, depart, ret), min(prices) if prices else None,
                                keep_lower=False)
        return res

    return await _cached_call(key, settings.flights_ttl, confirm, fn)


@app.get("/api/returns")
async def returns(origin: str, dest: str, depart: str, ret: str, token: str, confirm: bool = False):
    origin, dest = _airport(origin), _airport(dest)
    _dates(depart, ret)
    dom = _domestic(origin, dest)
    key = f"{_p()}|ret|{settings.airline_filter}|{_tokhash(token)}"

    async def fn():
        res = await S.provider.returns(origin, dest, depart, ret, token, exclude_basic=dom)
        res["coords"] = _coords_for(res["itineraries"], [origin, dest])
        res["google_flights_url"] = res.get("google_flights_url") or _gf_url(origin, dest, depart, ret)
        return res

    return await _cached_call(key, settings.flights_ttl, confirm, fn)


@app.get("/api/booking")
async def booking(origin: str, dest: str, depart: str, ret: str, token: str, confirm: bool = False):
    from deltamap.providers.base import classify_fare

    origin, dest = _airport(origin), _airport(dest)
    _dates(depart, ret)
    key = f"{_p()}|book|{_tokhash(token)}"

    async def fn():
        res = await S.provider.booking(origin, dest, depart, ret, token)
        for o in res["options"]:
            o["fare_is_main"] = classify_fare(o.get("fare_name"))
        return res

    res = await _cached_call(key, settings.flights_ttl, confirm, fn)
    if isinstance(res, dict):
        # Lowest Main Cabin (or better) price sold by Delta itself, for this itinerary.
        mains = [o["price"] for o in res["options"]
                 if o["fare_is_main"] == "yes" and o["leg"] == "together" and o.get("price") is not None
                 and "delta" in (o.get("book_with") or "").lower()]
        res["main_price"] = min(mains) if mains else None
        if res["main_price"] is not None:
            store.put_main_fare(_main_key(origin, dest, depart, ret), res["main_price"], keep_lower=True)
    return res


@app.get("/api/health")
async def health():
    return {"ok": True, "provider": _p()}
