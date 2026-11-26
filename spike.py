"""Phase-1 spike: answer the design's open questions with as few SerpApi searches as possible.

    .venv/bin/python spike.py --depart 2027-01-20 --ret 2027-01-27          # dry run, spends nothing
    .venv/bin/python spike.py --depart 2027-01-20 --ret 2027-01-27 --yes    # spends 7 searches

Already run on 2026-11-26; findings are in docs/HOW_IT_WORKS.md. Kept for re-checking later.

Searches (default 7):
  1  Explore  BNA -> Europe,        include_airlines=DL
  2  Explore  BNA -> North America, include_airlines=DL
  3  Explore  BNA -> Europe,        include_airlines=SKYTEAM  (cities DL misses?)
  4  Flights  BNA -> cheapest Europe city, DL                 (does Explore's price match Flights?)
  5  Flights  returns for the cheapest outbound (departure_token)
  6  Flights  booking options for the cheapest return (booking_token) (fare names)
  7  Flights  BNA -> cheapest US city, DL, exclude_basic=true  (Main price vs Explore price)
  --full-map adds the other 7 regions (+7 searches), so the first map in the app is free.

Raw JSON goes to spike_out/. Explore results are also written into the app's cache (6 h), so a map
search in the app with the same airport and dates reuses them instead of searching again.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import httpx

from deltamap import airports
from deltamap.cache import Store, explore_key
from deltamap.config import REGIONS, ROOT, load_settings
from deltamap.providers.base import parse_booking_options, parse_flight_results
from deltamap.providers.serpapi import SerpApiProvider, parse_explore

OUT = ROOT / "spike_out"
R = {r.id: r for r in REGIONS}


def save(name: str, data: dict) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(data, indent=2))


def summarize_explore(label: str, data: dict, region_id: str) -> list[dict]:
    raw = data.get("destinations") or []
    dests = parse_explore(data, region_id)
    countries = sorted({d["country"] or "?" for d in dests})
    airlines = {}
    for d in dests:
        airlines[d["airline_code"]] = airlines.get(d["airline_code"], 0) + 1
    print(f"\n[{label}] {len(raw)} raw destinations, {len(dests)} priced")
    print(f"  countries ({len(countries)}): {', '.join(countries)}")
    print(f"  airline codes: {airlines}")
    dates = {(r.get('start_date'), r.get('end_date')) for r in raw}
    print(f"  start/end dates seen: {sorted(dates, key=str)[:5]}")
    for d in sorted(dests, key=lambda d: d["price"])[:8]:
        print(f"  {d['code']:>4} {d['city'][:22]:<22} ${d['price']:<6} stops={d['stops']} {d['minutes']}m {d['airline']}")
    return dests


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", default="BNA")
    ap.add_argument("--depart", required=True)
    ap.add_argument("--ret", required=True)
    ap.add_argument("--yes", action="store_true", help="actually spend searches")
    ap.add_argument("--full-map", action="store_true", help="also fetch the other 2 regions (+2 searches)")
    a = ap.parse_args()

    s = load_settings()
    if not s.serpapi_key:
        print("No SerpApi key found (SERPAPI_KEY, serpapi.key or serpai.key).")
        return 1
    store = Store(s.db_path)
    async with httpx.AsyncClient() as client:
        p = SerpApiProvider(s, store, client)
        q = await p.quota()
        if not q.get("ok"):
            print(f"Account check failed: {q.get('error')}")
            return 1
        planned = 14 if a.full_map else 7
        print(f"Account: {q['left']} searches left ({q.get('plan')}), {q.get('hour_left')} this hour. "
              f"This spike plans {planned}.")
        if q["left"] is not None and q["left"] < planned:
            print("Not enough searches left; aborting.")
            return 1
        if not a.yes:
            print("Dry run: nothing spent. Add --yes to run.")
            return 0

        o, dep, ret = a.origin, a.depart, a.ret
        common = {"currency": "USD", "hl": "en", "gl": "us", "adults": 1, "travel_class": 1}

        def explore_params(area_id, airline):
            return {"engine": "google_travel_explore", "departure_id": o, "arrival_area_id": area_id,
                    "outbound_date": dep, "return_date": ret, "type": 1, "travel_mode": 1,
                    "include_airlines": airline, **common}

        def seed(region_id, dests):
            store.put(explore_key("serpapi", "DL", o, dep, ret, region_id), dests, s.map_ttl)

        # 1-3: Explore
        jobs = [("explore_eur_DL", "eur", "DL"), ("explore_nam_DL", "nam", "DL"),
                ("explore_eur_SKYTEAM", "eur", "SKYTEAM")]
        if a.full_map:
            jobs += [(f"explore_{r}_DL", r, "DL") for r in R if r not in ("eur", "nam")]
        results = await asyncio.gather(*(
            p.search(explore_params(R[rid].area_id, al), name) for name, rid, al in jobs))
        parsed = {}
        for (name, rid, al), data in zip(jobs, results):
            save(name, data)
            parsed[name] = summarize_explore(name, data, rid)
            if al == "DL":
                seed(rid, parsed[name])

        eur_dl = {d["code"] for d in parsed["explore_eur_DL"]}
        eur_st = {d["code"]: d for d in parsed["explore_eur_SKYTEAM"]}
        only_st = sorted(set(eur_st) - eur_dl)
        print(f"\nSKYTEAM-only Europe cities (not in DL results): {len(only_st)}")
        for c in only_st[:20]:
            d = eur_st[c]
            print(f"  {c} {d['city']} ${d['price']} via {d['airline']}")

        # 4-6: international drill-down
        eur = sorted(parsed["explore_eur_DL"], key=lambda d: d["price"])
        if eur:
            tgt = eur[0]
            fp = {"engine": "google_flights", "departure_id": o, "arrival_id": tgt["code"], "outbound_date": dep,
                  "return_date": ret, "type": 1, "include_airlines": "DL", **common}
            out = await p.search(fp, f"spike flights {tgt['code']}")
            save(f"flights_{tgt['code']}", out)
            its = parse_flight_results(out, "DL")
            print(f"\n[flights {o}-{tgt['code']}] {len(its)} itineraries; Explore said ${tgt['price']}, "
                  f"Flights cheapest ${its[0]['price'] if its else '—'}")
            print(f"  google_flights_url present: {bool((out.get('search_metadata') or {}).get('google_flights_url'))}")
            for it in its[:4]:
                print(f"  ${it['price']} {it['minutes']}m via {[l['code'] for l in it['layovers']]} "
                      f"{[x['flight_number'] for x in it['segments']]} delta={it['delta']}")
            if its and its[0]["departure_token"]:
                rp = dict(fp, departure_token=its[0]["departure_token"])
                back = await p.search(rp, f"spike returns {tgt['code']}")
                save(f"returns_{tgt['code']}", back)
                rits = parse_flight_results(back, "DL")
                print(f"\n[returns] {len(rits)} itineraries, cheapest ${rits[0]['price'] if rits else '—'}; "
                      f"booking_token on first: {bool(rits and rits[0]['booking_token'])}")
                if rits and rits[0]["booking_token"]:
                    bp = {"engine": "google_flights", "departure_id": o, "arrival_id": tgt["code"],
                          "outbound_date": dep, "return_date": ret, "type": 1,
                          "booking_token": rits[0]["booking_token"], **common}
                    book = await p.search(bp, f"spike booking {tgt['code']}")
                    save(f"booking_{tgt['code']}", book)
                    print("\n[booking options] seller / fare name / price")
                    for b in parse_booking_options(book):
                        print(f"  {b['book_with']} / {b['fare_name']!r} / ${b['price']}  {b['conditions'][:3]}")

        # 7: domestic Main Cabin check
        us = sorted((d for d in parsed["explore_nam_DL"] if airports.country(d["code"]) == "US"),
                    key=lambda d: d["price"])
        if us:
            tgt = us[0]
            fp = {"engine": "google_flights", "departure_id": o, "arrival_id": tgt["code"], "outbound_date": dep,
                  "return_date": ret, "type": 1, "include_airlines": "DL", "exclude_basic": "true", **common}
            dom = await p.search(fp, f"spike main {tgt['code']}")
            save(f"flights_main_{tgt['code']}", dom)
            its = parse_flight_results(dom, "DL")
            print(f"\n[domestic {o}-{tgt['code']} exclude_basic] Explore ${tgt['price']} vs Main "
                  f"${its[0]['price'] if its else '—'} ({len(its)} itineraries)")

        q2 = await p.quota()
        print(f"\nDone at {datetime.now():%H:%M}. Searches left: {q2.get('left')} (was {q['left']}). "
              f"Raw JSON in {OUT}/")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
