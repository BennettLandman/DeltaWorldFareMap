# How Delta Fare Map works

These are technical notes for anyone who wants to understand, change or extend the app. For how to
use it, see the [user manual](USER_MANUAL.md).

## Architecture

```
Browser ── static/index.html + app.js (Leaflet map, plain JavaScript, no build step)
   │  fetch / Server-Sent Events
   ▼
app.py ── FastAPI on localhost
   │                          ┌──────────────────────────────────────────┐
   ├── deltamap/cache.py ───▶ │ cache.sqlite3                            │
   │                          │  cache       API answers with a TTL      │
   │                          │  calls       log of every search made    │
   │                          │  main_fares  confirmed Main Cabin prices │
   │                          └──────────────────────────────────────────┘
   └── deltamap/providers/
         serpapi.py ── httpx ──▶ serpapi.com  (google_travel_explore, google_flights, account.json)
         demo.py      invented, deterministic data, so the UI works without a key
```

The browser never talks to SerpApi directly, and the API key never leaves the server process.

### API endpoints (local)

| Endpoint | Purpose | Uses searches |
|---|---|---|
| `GET /api/config` | Provider, regions, defaults | no |
| `GET /api/airports?q=` | Airport autocomplete (offline, from `airportsdata`) | no |
| `GET /api/quota` | Searches left (SerpApi Account API, cached 60 s) | no |
| `GET /api/estimate` | How many searches a map search would use, and any guardrail | no |
| `GET /api/explore` | Map search, streamed per area as Server-Sent Events | 1 per uncached area |
| `GET /api/flights` | Outbound flights for one city | 1 (cached 1 h) |
| `GET /api/returns` | Return flights for a chosen outbound (`departure_token`) | 1 (cached 1 h) |
| `GET /api/booking` | Booking options and fare names (`booking_token`) | 1 (cached 1 h) |

The search endpoints take `airlines=DL|SKYTEAM`, and the map endpoints also take `deep=1`. Both are
part of every cache key.

When a guardrail stops a call, the endpoint returns HTTP 409 with a message. The page then asks
the user to confirm, or shows why the call was blocked.

## Data sources

**The map: Google Travel Explore** (`engine=google_travel_explore`). One call per region, with:

- `departure_id`: the home airport
- `arrival_area_id`: the region's Google location ID
- the dates
- `type=1` (round trip)
- `travel_mode=1` (flights only)
- `include_airlines=DL` or `SKYTEAM`

Each result has a price, the airport, coordinates, stops, duration and airline.

**City details: Google Flights** (`engine=google_flights`):

1. **Outbound flights**: a search for one destination. It returns `best_flights` and
   `other_flights`, each with a `departure_token`.
2. **Returns**: the same search plus that `departure_token`. Return prices are round-trip totals.
3. **Booking options**: the same search plus the return's `booking_token`. This is the only place
   the fare names appear (`booking_options[].together.option_title`, such as "Delta Main Classic").

US domestic searches add `exclude_basic=true`. SerpApi supports that only for US domestic economy.

**Airline networks.** `DL` returns flights sold under a Delta flight number, including Delta
codeshares on partners. `SKYTEAM` returns flights on any SkyTeam member, so partner-only routes
appear too. The app keeps every itinerary Google returns. Each one carries a `delta` flag for
whether it includes a Delta flight.

**Quota: the SerpApi Account API** (`account.json`). It reports `total_searches_left`,
`this_hour_searches` and `account_rate_limit_per_hour`, and calling it is free. The app calls it
at startup to validate the key, and refreshes it at most once a minute.

## Regions

`arrival_area_id` accepts exactly **one** Google location ID. One Explore call returns about 60–90
destinations, of which only about 30–50 are priced. So the world is split into nine
continent-sized regions in [`deltamap/config.py`](../deltamap/config.py):

| id | Region | Google location ID |
|---|---|---|
| `nam` | North America | `/m/059g4` |
| `car` | Caribbean | `/m/0261m` |
| `cam` | Central America | `/m/01tzh` |
| `sam` | South America | `/m/06n3y` |
| `eur` | Europe | `/m/02j9z` |
| `afr` | Africa | `/m/0dg3n1` |
| `mde` | Middle East | `/m/04wsz` |
| `asia` | Asia | `/m/0j0k` |
| `oce` | Oceania | `/m/05nrg` |

### Deep-search areas

A continent call still leaves airports out. For BNA in March, North America returned 44 cities,
with only Anchorage and Fairbanks in Alaska. Europe returned 31, with no Frankfurt, Brussels or
Iceland. Oceania returned 6. **Deep search** adds 37 smaller areas: countries and UN-style
sub-regions, each one more Explore call. They are listed in `config.py` as `Region.deep`.

Every area's Google ID is a Freebase ID, looked up on Wikidata (property P646). The continent IDs
the app already used match Wikidata exactly. The areas include:

| Area | ID | Area | ID |
|---|---|---|---|
| Alaska | `/m/0hjy` | Nordic countries | `/m/0140mf` |
| Hawaii | `/m/03gh4` | British Isles | `/m/017jq` |
| Western US | `/m/018hr_` | South Asia | `/m/06nn1` |
| Northeastern US | `/m/027rjr` | Southeast Asia | `/m/073q1` |
| Canada | `/m/0d060g` | Australia | `/m/0chghy` |
| Mexico | `/m/0b90_r` | New Zealand | `/m/0ctw_b` |

The whole-region call keeps its old cache key (`…|eur`), and areas are keyed `…|eur.nordic`. A
region searched normally therefore stays cached when deep search is turned on later. Results from
all areas are merged by airport, keeping the cheapest fare, and are tagged with their parent
region, so the region chips still filter them.

Explore can list one airport under two city names. For example, FLL appeared as both "Miami" and
"Fort Lauderdale". The parser keeps one entry per airport: the cheaper one, or on a tie, the one
whose name matches the airport's own city.

## Main Cabin logic

- Every Explore price starts as `fare_is_main = "unknown"` (shown as *may be Basic*).
- **Domestic:** `/api/flights` searches with `exclude_basic=true`. The cheapest result is stored
  in `main_fares` and replaces the map price.
- **International:** `/api/booking` classifies each fare name. Names containing *basic* are Basic.
  Names containing *main*, *classic*, *comfort*, *premium*, *first*, *business* or *standard* are
  Main or better. The lowest Main-or-better price is stored in `main_fares`, keeping the lower
  price if one is already stored, and replaces the map price. In Delta mode only fares **sold by
  Delta itself** count. In SkyTeam mode, a fare sold by any airline counts, but travel agencies
  don't.
- Later map searches for the same airport and dates (within 6 hours) apply the stored Main fares
  automatically.

## Caching and guardrails

| What | Cache time |
|---|---|
| Explore results, per airport, dates and region | 6 hours |
| Outbound flights, returns, booking options | 1 hour |
| Confirmed Main fares | 6 hours |
| Account quota | 60 seconds |

Before any call that uses searches, `guard()` in `app.py` checks the quota:

- **Monthly block:** fewer searches left than the call needs.
- **Hourly block:** the hourly rate limit would be exceeded.
- **Confirmation:** fewer than `LOW_QUOTA_WARN` searches left (default 20). The user must confirm.
- **Big search:** more than `BIG_SEARCH_WARN` searches in one action (default 10), such as a deep
  search. The user must confirm.

Every search the app makes is logged in the `calls` table. A transport error (a network blip) is
retried without being logged, since SerpApi never received it.

## API findings (spike, 2026-11-26)

Before building the app, `spike.py` ran 8 searches against SerpApi (BNA, Jan 20–27 2027) to
settle open design questions. What it found:

- **`arrival_area_id` takes one region ID.** Comma-joined IDs are rejected, and the rejected call
  was not charged.
- **Coverage per call.** One Explore call returns about 60–90 destinations, of which about 50–70%
  are priced: 35 in Europe and 48 in North America. With no region, only about 50 nearby US,
  Canada and Mexico cities come back.
- **Partners are included.** `include_airlines=DL` already includes partner codeshares. 27 of the
  35 Europe results were Air France/KLM/Delta combinations. `SKYTEAM` returned the same result for
  Dublin, so the default stays `DL`.
- **Explore matches Flights, but it's Basic.** Explore's price matched Google Flights' cheapest
  (Dublin, $520 in both), but that was Delta Main Basic. Main Classic was $760. For St. Louis,
  Explore showed $177, and `exclude_basic` showed $267 Main.
- **Fare names** appear only in booking options (`booking_token`), as `option_title`.

`spike.py` is kept so the findings can be re-checked. It is a dry run by default; `--yes` actually
spends searches.

## Extending

- **Another data source:** implement the provider interface in
  [`deltamap/providers/base.py`](../deltamap/providers/base.py), which has `explore`, `outbound`,
  `returns`, `booking` and `quota` (each map call is one `explore` for one `Area`). Then select it in `app.py`'s `lifespan`.
- **Another airline or alliance:** add its Google Flights code (an IATA code, or `ONEWORLD` / `STAR_ALLIANCE`) to `AIRLINES` in `config.py`. The
  Main Cabin logic and the delta.com link are Delta-specific.
- **More deep areas:** add an `Area` to a region's `deep` tuple in `config.py`, using the place's
  Freebase ID from Wikidata (P646).
- **Flexible dates (±3 days):** not built. It would multiply the Explore calls by the number of
  date pairs, so it needs a careful cost display.
