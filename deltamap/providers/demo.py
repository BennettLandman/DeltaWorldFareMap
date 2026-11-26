"""Offline demo provider: plausible, deterministic fake fares so the UI works with no API key.

Every price, time and flight number here is invented. The UI labels it DEMO DATA.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from datetime import datetime, timedelta

from .. import airports
from ..config import Area
from .base import parse_flight_results

DESTS = {
    "nam": "ATL BOS DTW JFK LGA LAX MSP SEA SLC SFO ORD DFW DEN MCO MIA FLL TPA RDU AUS MSY PHX "
           "LAS SAN PDX DCA PHL HNL OGG CUN SJD PVR MEX YYZ YUL YVR".split(),
    "car": "SJU NAS MBJ PUJ AUA GCM".split(),
    "cam": "SJO LIR PTY GUA SAL".split(),
    "sam": "BOG LIM SCL EZE GRU UIO".split(),
    "eur": "LHR CDG AMS FCO MXP MAD BCN LIS DUB EDI FRA MUC ZRH ATH".split(),
    "afr": "JNB CPT LOS ACC DSS NBO".split(),
    "mde": "IST TLV DXB".split(),
    "asia": "ICN NRT HND PVG TPE MNL".split(),
    "oce": "SYD AKL".split(),
}
# Extra cities that only a deep-search area finds (the whole-region call "misses" them).
DEEP_DESTS = {
    "nam.ak": "ANC FAI JNU KTN SIT", "nam.hi": "HNL OGG KOA LIH", "nam.west": "BOI BZN JAC SMF ABQ TUS",
    "nam.midwest": "MKE CMH IND OMA DSM", "nam.south": "MEM SAV CHS ELP OKC",
    "nam.northeast": "PWM BTV BUF PIT SYR", "nam.ca": "YYC YEG YOW YHZ YWG", "nam.mx": "GDL MTY MID",
    "car.greater": "SDQ HAV", "car.lesser": "STT SXM ANU BGI",
    "sam.br": "GIG BSB REC FOR", "sam.ar": "COR MDZ", "sam.cl": "PUQ", "sam.co": "MDE CTG CLO", "sam.pe": "CUZ",
    "eur.isles": "MAN SNN GLA", "eur.nordic": "KEF OSL ARN CPH HEL BGO", "eur.west": "BRU DUS NCE GVA",
    "eur.central": "VIE PRG BUD BER", "eur.south": "VCE NAP OPO", "eur.east": "WAW KRK OTP SOF",
    "afr.north": "CMN RAK CAI TUN", "afr.west": "ABJ", "afr.central": "FIH DLA", "afr.east": "ADD DAR EBB",
    "afr.south": "DUR WDH", "mde.arabia": "DOH RUH JED AUH", "mde.levant": "AMM BEY",
    "asia.jp": "KIX FUK CTS", "asia.cn": "PEK CAN XMN", "asia.east": "HKG", "asia.se": "SIN BKK SGN HAN CGK DPS",
    "asia.south": "DEL BOM BLR MAA HYD", "oce.au": "BNE PER ADL", "oce.nz": "CHC WLG ZQN",
    "oce.poly": "PPT", "oce.mela": "NAN NOU",
}
# Cities reachable only on SkyTeam partners (shown in SkyTeam mode), keyed by area id.
SKYTEAM_DESTS = {
    "eur": "TLS LYS", "eur.east": "BEG", "asia": "HAN SGN", "asia.se": "SUB", "asia.cn": "CTU",
    "afr": "NBO EBB", "mde": "BEY", "sam": "COR", "nam": "GDL MTY", "oce.au": "OOL",
}
PARTNER_NAME = {"eur": "Air France", "asia": "Korean Air", "afr": "Kenya Airways", "mde": "Middle East Airlines",
                "sam": "Aerolíneas Argentinas", "nam": "Aeroméxico", "oce": "Virgin Australia"}
US_HUBS = ["ATL", "DTW", "MSP", "SLC", "JFK", "LAX", "SEA", "BOS"]
GATEWAYS = {  # where long-haul legs leave from / connect through
    "nam": US_HUBS, "car": ["ATL", "JFK"], "cam": ["ATL"], "sam": ["ATL", "JFK"],
    "eur": ["ATL", "JFK", "DTW", "BOS", "AMS", "CDG"],
    "afr": ["ATL", "JFK", "AMS", "CDG"], "mde": ["ATL", "JFK", "AMS", "CDG"],
    "asia": ["ATL", "DTW", "SEA", "LAX", "ICN"], "oce": ["LAX"],
}
PARTNER = {"CDG": ("Air France", "AF"), "AMS": ("KLM", "KL"), "ICN": ("Korean Air", "KE"),
           "LHR": ("Virgin Atlantic", "VS")}


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def _dist_km(a: str, b: str) -> float:
    (la1, lo1), (la2, lo2) = airports.coords(a), airports.coords(b)
    p1, p2, dl = math.radians(la1), math.radians(la2), math.radians(lo2 - lo1)
    return 6371 * math.acos(min(1, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))


def _region_of(code: str) -> str:
    for r, codes in DESTS.items():
        if code in codes:
            return r
    for area, codes in (*DEEP_DESTS.items(), *SKYTEAM_DESTS.items()):
        if code in codes.split():
            return area.split(".")[0]
    return "nam"


def _routes(origin: str, dest: str) -> list[list[str]]:
    """Candidate airport paths origin -> dest (nonstop, via one hub, via two)."""
    out = []
    reg = _region_of(dest)
    if reg == "nam":
        reg = _region_of(origin)  # return legs: connect through the far end's gateways
    if origin in US_HUBS or dest in US_HUBS or _h(origin, dest, "ns") % 4 == 0:
        if _dist_km(origin, dest) < 9000 or origin in US_HUBS:
            out.append([origin, dest])
    for hub in GATEWAYS[reg]:
        if hub not in (origin, dest):
            out.append([origin, hub, dest])
    if reg in ("eur", "afr", "mde", "asia"):
        for us in ("ATL", "DTW"):
            for over in PARTNER:
                if over in GATEWAYS[reg] and over not in (origin, dest) and us not in (origin, dest):
                    out.append([origin, over, us, dest] if _region_of(origin) == reg else [origin, us, over, dest])
    return out[:7]


def _price(origin, dest, depart, ret, stops, salt="") -> int:
    km = _dist_km(origin, dest)
    base = 140 + km * (0.11 if km < 3000 else 0.085)
    wobble = 0.75 + (_h(origin, dest, depart, ret, salt) % 1000) / 1000 * 0.7
    return int(round(base * wobble * (0.93 if stops else 1.08) / 10) * 10 - 1)


def _leg(path, date, seed) -> tuple[list[dict], list[dict], int]:
    t = datetime.fromisoformat(date) + timedelta(hours=6 + _h(seed, "dep") % 14, minutes=5 * (_h(seed) % 12))
    segs, lays = [], []
    for i, (a, b) in enumerate(zip(path, path[1:])):
        mins = int(_dist_km(a, b) / 780 * 60 + 35)
        name, code = PARTNER.get(a) or PARTNER.get(b) if i > 0 and (a in PARTNER or b in PARTNER) else ("Delta", "DL")
        arr = t + timedelta(minutes=mins)
        segs.append({
            "departure_airport": {"id": a, "name": airports.get(a)["name"], "time": t.strftime("%Y-%m-%d %H:%M")},
            "arrival_airport": {"id": b, "name": airports.get(b)["name"], "time": arr.strftime("%Y-%m-%d %H:%M")},
            "duration": mins,
            "airplane": "Airbus A330-900" if mins > 360 else ("Boeing 737-900" if mins > 120 else "Airbus A220-100"),
            "airline": name,
            "flight_number": f"{code} {100 + _h(seed, a, b) % 2800}",
            "travel_class": "Economy",
            "legroom": "31 in",
            "ticket_also_sold_by": ["Delta"] if code != "DL" else [],
            "overnight": arr.date() != t.date(),
        })
        if i < len(path) - 2:
            lay = 55 + _h(seed, b, "lay") % 150
            lays.append({"id": b, "name": airports.get(b)["name"], "duration": lay})
            t = arr + timedelta(minutes=lay)
    total = sum(s["duration"] for s in segs) + sum(l["duration"] for l in lays)
    return segs, lays, total


def _tok(d: dict) -> str:
    return "demo:" + base64.urlsafe_b64encode(json.dumps(d).encode()).decode()


def _untok(t: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(t.removeprefix("demo:")))


class DemoProvider:
    name = "demo"

    def __init__(self, store):
        self.store = store

    async def explore(self, origin, depart, ret, area: Area, region_id: str, airlines: str) -> list[dict]:
        if not airports.get(origin):
            return []
        codes = DESTS.get(area.id, []) if area.id == region_id else DEEP_DESTS.get(area.id, "").split()
        partner_only = SKYTEAM_DESTS.get(area.id, "").split() if airlines == "SKYTEAM" else []
        out = []
        for code in [*codes, *partner_only]:
            if code == origin or not airports.get(code):
                continue
            routes = _routes(origin, code)
            if not routes:
                continue
            best = min(routes, key=lambda p: _price(origin, code, depart, ret, len(p) - 2, "-".join(p)))
            stops = len(best) - 2
            a = airports.get(code)
            partner = code in partner_only
            out.append({
                "code": code, "city": a["city"], "country": a["country"], "country_code": a["country"],
                "lat": a["lat"], "lon": a["lon"],
                "price": _price(origin, code, depart, ret, stops, "-".join(best)),
                "airline": PARTNER_NAME.get(region_id, "SkyTeam partner") if partner else "Delta",
                "airline_code": "SKYTEAM" if partner else "DL", "stops": stops,
                "minutes": _leg(best, depart, (origin, code))[2],
                "fare_is_main": "unknown", "region": region_id, "link": None, "thumbnail": None,
            })
        return out

    def _options(self, path_from, path_to, date, other, base_seed, exclude_basic, token_key, token_extra):
        raws = []
        for path in _routes(path_from, path_to):
            for k in range(2):
                seed = (base_seed, "-".join(path), k)
                segs, lays, total = _leg(path, date, seed)
                price = _price(other[0], other[1], other[2], other[3], len(path) - 2, "-".join(path) + str(k))
                if exclude_basic:
                    price += 60
                raws.append({
                    "flights": segs, "layovers": lays, "total_duration": total, "price": price,
                    token_key: _tok({**token_extra, "path": path, "k": k, "price": price}),
                })
        return raws

    async def outbound(self, origin, dest, depart, ret, exclude_basic, airlines) -> dict:
        raws = self._options(origin, dest, depart, (origin, dest, depart, ret), (origin, dest, depart),
                             exclude_basic, "departure_token", {"o": origin, "d": dest})
        return self._payload(raws, origin, dest, depart, ret)

    async def returns(self, origin, dest, depart, ret, departure_token, exclude_basic, airlines) -> dict:
        sel = _untok(departure_token)
        raws = self._options(dest, origin, ret, (origin, dest, depart, ret), (dest, origin, ret, sel["path"]),
                             exclude_basic, "booking_token", {"o": origin, "d": dest, "out": sel["path"]})
        base = sel["price"]
        for r in raws:  # returns are priced as the round-trip total, cheapest ~= outbound price
            r["price"] = base + (r["price"] - min(x["price"] for x in raws))
            r["booking_token"] = _tok({**_untok(r["booking_token"]), "price": r["price"]})
        return self._payload(raws, origin, dest, depart, ret)

    async def booking(self, origin, dest, depart, ret, booking_token) -> dict:
        p = _untok(booking_token)["price"]
        intl = airports.country(dest) != "US" or airports.country(origin) != "US"
        rows = [("Delta Main Basic", p, ["No changes", "Seat assigned at check-in"]),
                ("Delta Main Classic", p + (130 if intl else 70), ["Free changes", "Seat selection included"]),
                ("Delta Comfort", p + (290 if intl else 160), ["Extra legroom", "Free changes"])]
        return {
            "options": [{"leg": "together", "book_with": "Delta", "is_airline": True, "price": pr,
                         "fare_name": n, "flights": [], "conditions": c, "baggage": []} for n, pr, c in rows],
            "google_flights_url": None,
        }

    def _payload(self, raws, origin, dest, depart, ret):
        its = parse_flight_results({"other_flights": raws})
        for i, it in enumerate(its[:3]):
            it["best"] = True
        return {"itineraries": its, "google_flights_url": None, "price_insights": None}

    async def quota(self) -> dict:
        return {"ok": True, "demo": True, "left": None, "per_month": None, "hour_left": None}
