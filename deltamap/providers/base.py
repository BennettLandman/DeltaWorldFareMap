"""Provider interface and the plain-dict data model shared by every fare source.

Destination: code, city, country, lat, lon, price, airline, airline_code, stops,
             minutes, fare_is_main ("yes" | "no" | "unknown"), region, link
Itinerary:   price, minutes, segments[], layovers[], departure_token, booking_token,
             delta (bool: a Delta flight number or Delta-sold ticket), extensions[]
Segment:     flight_number, airline, operated_by, sold_by[], from, from_name, to,
             to_name, depart, arrive ("YYYY-MM-DD HH:MM" local), minutes, aircraft,
             overnight, legroom, travel_class
"""
from __future__ import annotations

from typing import Any, Protocol

from ..config import Region


class ProviderError(Exception):
    pass


class Provider(Protocol):
    name: str

    async def explore(self, origin: str, depart: str, ret: str, region: Region) -> list[dict]: ...

    async def outbound(
        self, origin: str, dest: str, depart: str, ret: str, exclude_basic: bool
    ) -> dict: ...

    async def returns(
        self, origin: str, dest: str, depart: str, ret: str, departure_token: str, exclude_basic: bool
    ) -> dict: ...

    async def booking(self, origin: str, dest: str, depart: str, ret: str, booking_token: str) -> dict: ...

    async def quota(self) -> dict: ...

    def calls_for_explore(self, region: Region) -> int: ...


def is_delta_segment(seg: dict) -> bool:
    fn = (seg.get("flight_number") or "").replace(" ", "").upper()
    return (
        fn.startswith("DL")
        or (seg.get("airline") or "").lower().startswith("delta")
        or any("delta" in (s or "").lower() for s in seg.get("sold_by") or [])
    )


def parse_segment(raw: dict) -> dict:
    dep, arr = raw.get("departure_airport") or {}, raw.get("arrival_airport") or {}
    return {
        "flight_number": raw.get("flight_number"),
        "airline": raw.get("airline"),
        "operated_by": raw.get("plane_and_crew_by"),
        "sold_by": raw.get("ticket_also_sold_by") or [],
        "from": dep.get("id"),
        "from_name": dep.get("name"),
        "to": arr.get("id"),
        "to_name": arr.get("name"),
        "depart": dep.get("time"),
        "arrive": arr.get("time"),
        "minutes": raw.get("duration"),
        "aircraft": raw.get("airplane"),
        "overnight": bool(raw.get("overnight")),
        "legroom": raw.get("legroom"),
        "travel_class": raw.get("travel_class"),
    }


def parse_itinerary(raw: dict) -> dict:
    segs = [parse_segment(s) for s in raw.get("flights") or []]
    return {
        "price": raw.get("price"),
        "minutes": raw.get("total_duration"),
        "segments": segs,
        "layovers": [
            {"code": l.get("id"), "name": l.get("name"), "minutes": l.get("duration"),
             "overnight": bool(l.get("overnight"))}
            for l in raw.get("layovers") or []
        ],
        "departure_token": raw.get("departure_token"),
        "booking_token": raw.get("booking_token"),
        "delta": any(is_delta_segment(s) for s in segs),
        "extensions": raw.get("extensions") or [],
    }


def parse_flight_results(data: dict[str, Any], airline_filter: str) -> list[dict]:
    """best_flights + other_flights -> itineraries, keeping only ones with a Delta flight."""
    out = []
    for group in ("best_flights", "other_flights"):
        for raw in data.get(group) or []:
            it = parse_itinerary(raw)
            it["best"] = group == "best_flights"
            out.append(it)
    if airline_filter != "DL":
        # SKYTEAM (or other) mode: Google returns partner-only trips too; keep Delta ones.
        out = [it for it in out if it["delta"]]
    out = [it for it in out if it["price"] is not None]
    out.sort(key=lambda it: it["price"])
    return out


def parse_booking_options(data: dict[str, Any]) -> list[dict]:
    opts = []
    for raw in data.get("booking_options") or []:
        parts = []
        if raw.get("separate_tickets"):
            for leg in ("departing", "returning"):
                if raw.get(leg):
                    parts.append((leg, raw[leg]))
        elif raw.get("together"):
            parts.append(("together", raw["together"]))
        for leg, s in parts:
            opts.append({
                "leg": leg,
                "book_with": s.get("book_with"),
                "is_airline": bool(s.get("airline")),
                "price": s.get("price"),
                "fare_name": s.get("option_title"),
                "flights": s.get("marketed_as") or [],
                "conditions": s.get("extensions") or [],
                "baggage": s.get("baggage_prices") or [],
            })
    return opts


def classify_fare(name: str | None) -> str:
    """Map a seller's fare name to yes (Main or better) / no (Basic) / unknown."""
    n = (name or "").lower()
    if not n:
        return "unknown"
    if "basic" in n:
        return "no"
    if any(w in n for w in ("main", "classic", "comfort", "premium", "first", "business", "standard")):
        return "yes"
    return "unknown"
