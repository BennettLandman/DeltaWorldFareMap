"""SerpApi provider: Google Travel Explore for the map, Google Flights for drill-down."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .. import airports
from ..cache import Store
from ..config import Area, Settings
from .base import ProviderError, parse_booking_options, parse_flight_results

SEARCH_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"
NO_RESULTS = "hasn't returned any results"


class SerpApiProvider:
    name = "serpapi"

    def __init__(self, settings: Settings, store: Store, client: httpx.AsyncClient):
        if not settings.serpapi_key:
            raise ProviderError("No SerpApi key configured")
        self.s = settings
        self.key = settings.serpapi_key
        self.store = store
        self.client = client

    # -- transport -------------------------------------------------------------
    async def search(self, params: dict[str, Any], summary: str) -> dict:
        """One SerpApi search. Every call here can use a search from the monthly quota."""
        params = {k: v for k, v in params.items() if v is not None}
        params["api_key"] = self.key
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self.client.get(SEARCH_URL, params=params, timeout=90)
            except httpx.TransportError as e:  # network blip: retry, nothing was charged
                last = e
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            self.store.log_call(self.name, params.get("engine", "?"), summary)
            if r.status_code >= 500:
                last = ProviderError(f"SerpApi HTTP {r.status_code}")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                raise ProviderError(f"SerpApi returned non-JSON (HTTP {r.status_code})")
            err = data.get("error")
            if err:
                if NO_RESULTS in err:
                    return {"_empty": True, "search_metadata": data.get("search_metadata", {})}
                raise ProviderError(f"SerpApi: {err}")
            return data
        raise ProviderError(f"SerpApi unreachable: {last}")

    def _common(self) -> dict[str, Any]:
        return {"currency": "USD", "hl": "en", "gl": "us", "adults": 1, "travel_class": 1}

    # -- map -------------------------------------------------------------------
    async def explore(self, origin: str, depart: str, ret: str, area: Area, region_id: str,
                      airlines: str) -> list[dict]:
        data = await self.search(
            {
                "engine": "google_travel_explore",
                "departure_id": origin,
                "arrival_area_id": area.area_id,
                "outbound_date": depart,
                "return_date": ret,
                "type": 1,
                "travel_mode": 1,
                "include_airlines": airlines,
                **self._common(),
            },
            f"explore {origin} {area.id} {airlines} {depart}/{ret}",
        )
        return parse_explore(data, region_id)

    # -- drill-down ------------------------------------------------------------
    def _flight_params(self, origin, dest, depart, ret, exclude_basic, airlines) -> dict[str, Any]:
        return {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": dest,
            "outbound_date": depart,
            "return_date": ret,
            "type": 1,
            "include_airlines": airlines,
            "exclude_basic": "true" if exclude_basic else None,
            **self._common(),
        }

    async def outbound(self, origin, dest, depart, ret, exclude_basic, airlines) -> dict:
        data = await self.search(
            self._flight_params(origin, dest, depart, ret, exclude_basic, airlines),
            f"flights {origin}-{dest} {airlines} {depart}/{ret}",
        )
        return _flights_payload(data)

    async def returns(self, origin, dest, depart, ret, departure_token, exclude_basic, airlines) -> dict:
        p = self._flight_params(origin, dest, depart, ret, exclude_basic, airlines)
        p["departure_token"] = departure_token
        data = await self.search(p, f"returns {dest}-{origin} {airlines} {depart}/{ret}")
        return _flights_payload(data)

    async def booking(self, origin, dest, depart, ret, booking_token) -> dict:
        data = await self.search(
            {
                "engine": "google_flights",
                "departure_id": origin,
                "arrival_id": dest,
                "outbound_date": depart,
                "return_date": ret,
                "type": 1,
                "booking_token": booking_token,
                **self._common(),
            },
            f"booking {origin}-{dest} {depart}/{ret}",
        )
        return {
            "options": parse_booking_options(data),
            "google_flights_url": (data.get("search_metadata") or {}).get("google_flights_url"),
        }

    # -- quota (Account API: free, does not use a search) ------------------------
    async def quota(self) -> dict:
        try:
            r = await self.client.get(ACCOUNT_URL, params={"api_key": self.key}, timeout=20)
            d = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return {"ok": False, "error": f"Account API unreachable: {e}"}
        if d.get("error"):
            return {"ok": False, "error": d["error"]}
        per_hour = d.get("account_rate_limit_per_hour")
        this_hour = d.get("this_hour_searches") or 0
        return {
            "ok": True,
            "left": d.get("total_searches_left"),
            "per_month": d.get("searches_per_month"),
            "used_this_month": d.get("this_month_usage"),
            "hour_left": (per_hour - this_hour) if per_hour is not None else None,
            "per_hour": per_hour,
            "plan": d.get("plan_name"),
            "renews": d.get("plan_renewal_date"),
        }


def parse_explore(data: dict, region_id: str) -> list[dict]:
    """Explore destinations -> Destination dicts, one per airport.

    Explore can list one airport under two names (FLL as both Miami and Fort Lauderdale);
    keep the cheaper, and on a tie the name that matches the airport's own city."""
    out: dict[str, dict] = {}
    for raw in data.get("destinations") or []:
        price = raw.get("flight_price")
        code = (raw.get("destination_airport") or {}).get("code")
        if price is None or not code:
            continue  # car-only or unpriced entry
        gps = raw.get("gps_coordinates") or {}
        lat, lon = gps.get("latitude"), gps.get("longitude")
        if lat is None:
            c = airports.coords(code)
            lat, lon = (c or [None, None])
        if lat is None:
            continue
        d = {
            "code": code,
            "city": raw.get("name"),
            "country": raw.get("country"),
            "country_code": airports.country(code),
            "lat": lat,
            "lon": lon,
            "price": price,
            "airline": raw.get("airline"),
            "airline_code": raw.get("airline_code"),
            "stops": raw.get("number_of_stops"),
            "minutes": raw.get("flight_duration"),
            "fare_is_main": "unknown",
            "region": region_id,
            "link": raw.get("link"),
            "thumbnail": raw.get("thumbnail"),
        }
        cur = out.get(code)
        own_city = (airports.get(code) or {}).get("city")
        if cur is None or d["price"] < cur["price"] or (
            d["price"] == cur["price"] and d["city"] == own_city and cur["city"] != own_city
        ):
            out[code] = d
    return list(out.values())


def _flights_payload(data: dict) -> dict:
    return {
        "itineraries": parse_flight_results(data),
        "google_flights_url": (data.get("search_metadata") or {}).get("google_flights_url"),
        "price_insights": data.get("price_insights"),
    }
