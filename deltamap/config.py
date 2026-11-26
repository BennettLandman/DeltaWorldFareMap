"""Settings, read once from the environment, .env and the key file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

KEY_FILES = ("serpapi.key", "serpai.key")


def _read_key() -> str | None:
    key = os.getenv("SERPAPI_KEY", "").strip()
    if key:
        return key
    for name in KEY_FILES:
        p = ROOT / name
        if p.is_file():
            text = p.read_text().strip()
            if text:
                return text
    return None


@dataclass(frozen=True)
class Area:
    """One Google Travel Explore call (1 search): a Google place ID (kgmid) to search within."""
    id: str       # "eur" for a whole region, "eur.nordic" for a deep-search area
    name: str
    area_id: str  # Google location kgmid (Freebase id); SerpApi accepts exactly one per call


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    area_id: str
    deep: tuple[Area, ...] = ()  # extra, smaller areas searched by "Deep search"

    @property
    def whole(self) -> Area:
        return Area(self.id, self.name, self.area_id)


def _areas(region_id: str, *items: tuple[str, str, str]) -> tuple[Area, ...]:
    return tuple(Area(f"{region_id}.{key}", name, kgmid) for key, name, kgmid in items)


# One Explore call (one search) per region. SerpApi rejects comma-joined area ids, and one call
# returns only Google's top ~30-50 priced cities, so big regions hide many airports.
# "Deep search" adds smaller areas inside each region (1 search each) to find the rest.
# Place ids are Freebase ids, looked up on Wikidata (property P646).
REGIONS: tuple[Region, ...] = (
    Region("nam", "North America", "/m/059g4", _areas(
        "nam",
        ("ak", "Alaska", "/m/0hjy"),
        ("hi", "Hawaii", "/m/03gh4"),
        ("west", "Western US", "/m/018hr_"),
        ("midwest", "Midwestern US", "/m/0q76g"),
        ("south", "Southern US", "/m/018hmm"),
        ("northeast", "Northeastern US", "/m/027rjr"),
        ("ca", "Canada", "/m/0d060g"),
        ("mx", "Mexico", "/m/0b90_r"),
    )),
    Region("car", "Caribbean", "/m/0261m", _areas(
        "car",
        ("greater", "Greater Antilles", "/m/01rll_"),
        ("lesser", "Lesser Antilles", "/m/0157g9"),
    )),
    Region("cam", "Central America", "/m/01tzh"),
    Region("sam", "South America", "/m/06n3y", _areas(
        "sam",
        ("br", "Brazil", "/m/015fr"),
        ("ar", "Argentina", "/m/0jgd"),
        ("cl", "Chile", "/m/01p1v"),
        ("co", "Colombia", "/m/01ls2"),
        ("pe", "Peru", "/m/016wzw"),
    )),
    Region("eur", "Europe", "/m/02j9z", _areas(
        "eur",
        ("isles", "British Isles", "/m/017jq"),
        ("nordic", "Nordic countries and Iceland", "/m/0140mf"),
        ("west", "Western Europe", "/m/0852h"),
        ("central", "Central Europe", "/m/01lkr"),
        ("south", "Southern Europe", "/m/0250wj"),
        ("east", "Eastern Europe", "/m/09b69"),
    )),
    Region("afr", "Africa", "/m/0dg3n1", _areas(
        "afr",
        ("north", "North Africa", "/m/05g2v"),
        ("west", "West Africa", "/m/0hqjh"),
        ("central", "Central Africa", "/m/02n0ld"),
        ("east", "East Africa", "/m/01mqb6"),
        ("south", "Southern Africa", "/m/02c59t"),
    )),
    Region("mde", "Middle East", "/m/04wsz", _areas(
        "mde",
        ("arabia", "Arabian Peninsula", "/m/0cvmv"),
        ("levant", "Levant", "/m/04l_r"),
    )),
    Region("asia", "Asia", "/m/0j0k", _areas(
        "asia",
        ("jp", "Japan", "/m/03_3d"),
        ("cn", "China", "/m/0d05w3"),
        ("east", "East Asia", "/m/02qnk"),
        ("se", "Southeast Asia", "/m/073q1"),
        ("south", "South Asia (India)", "/m/06nn1"),
    )),
    Region("oce", "Oceania", "/m/05nrg", _areas(
        "oce",
        ("au", "Australia", "/m/0chghy"),
        ("nz", "New Zealand", "/m/0ctw_b"),
        ("poly", "Polynesia", "/m/09xzx"),
        ("mela", "Melanesia", "/m/0lbqv"),
    )),
)
REGION_BY_ID = {r.id: r for r in REGIONS}

# Airline networks the map can show. Google Flights accepts an airline code or an alliance name.
AIRLINES = {"DL": "Delta", "SKYTEAM": "SkyTeam (all partners)"}


def search_areas(region: Region, deep: bool) -> list[Area]:
    """The Explore calls for one region: the whole region, plus its smaller areas when deep."""
    return [region.whole, *region.deep] if deep else [region.whole]


@dataclass(frozen=True)
class Settings:
    serpapi_key: str | None
    provider: str
    airline_filter: str
    low_quota_warn: int
    big_search_warn: int
    map_ttl: int = 6 * 3600
    flights_ttl: int = 3600
    quota_ttl: int = 60
    db_path: Path = ROOT / "cache.sqlite3"
    default_origin: str = "BNA"


def load_settings() -> Settings:
    key = _read_key()
    provider = (os.getenv("DELTAMAP_PROVIDER") or ("serpapi" if key else "demo")).lower()
    airlines = os.getenv("AIRLINE_FILTER", "DL").upper()
    return Settings(
        serpapi_key=key,
        provider=provider,
        airline_filter=airlines if airlines in AIRLINES else "DL",
        low_quota_warn=int(os.getenv("LOW_QUOTA_WARN", "20")),
        big_search_warn=int(os.getenv("BIG_SEARCH_WARN", "10")),
    )
