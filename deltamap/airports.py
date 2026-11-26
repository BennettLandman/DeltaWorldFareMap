"""Airport lookup (codes, names, coordinates) from the bundled airportsdata list."""
from __future__ import annotations

from functools import lru_cache

import airportsdata

# Airports Delta or its partners actually serve get a boost in autocomplete so that
# "par" finds CDG before a grass strip.
_POPULAR = set(
    "ATL BNA BOS DTW JFK LGA LAX MSP SEA SLC SFO ORD DFW DEN MCO MIA FLL TPA RDU CLT AUS IAH "
    "MSY PHX LAS SAN PDX DCA IAD BWI PHL EWR CVG CMH IND MEM STL MCI PIT CLE BUF RSW PBI SAV "
    "CHS JAX HNL OGG CUN SJD PVR MEX SJU NAS MBJ PUJ AUA GCM BDA YYZ YUL YVR LHR CDG AMS FCO "
    "MXP MAD BCN LIS DUB EDI FRA MUC ZRH ATH IST TLV DXB ICN NRT HND PVG TPE MNL SYD AKL GRU "
    "EZE SCL LIM BOG UIO SJO LIR PTY GUA SAL JNB CPT LOS ACC DSS NBO CAI".split()
)


@lru_cache(maxsize=1)
def _db() -> dict[str, dict]:
    return airportsdata.load("IATA")


def get(code: str) -> dict | None:
    return _db().get((code or "").upper())


def coords(code: str) -> list[float] | None:
    a = get(code)
    return [a["lat"], a["lon"]] if a else None


def country(code: str) -> str | None:
    a = get(code)
    return a["country"] if a else None


def search(q: str, limit: int = 10) -> list[dict]:
    q = q.strip().lower()
    if not q:
        return []
    scored = []
    for code, a in _db().items():
        lc, city, name = code.lower(), (a["city"] or "").lower(), (a["name"] or "").lower()
        if lc == q:
            s = 0
        elif city.startswith(q):
            s = 1
        elif lc.startswith(q):
            s = 2
        elif q in name or q in city:
            s = 3
        else:
            continue
        if code not in _POPULAR:
            s += 2
        scored.append((s, code, a))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [
        {"code": c, "name": a["name"], "city": a["city"], "country": a["country"]}
        for _, c, a in scored[:limit]
    ]
