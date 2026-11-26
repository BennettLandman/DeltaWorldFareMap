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
class Region:
    id: str
    name: str
    area_id: str  # Google location kgmid (Freebase id); SerpApi accepts exactly one per call


# One Explore call (one search) per region. SerpApi rejects comma-joined area ids, and one
# call returns at most ~60-90 destinations, so regions are kept to single continents.
REGIONS: tuple[Region, ...] = (
    Region("nam", "North America", "/m/059g4"),
    Region("car", "Caribbean", "/m/0261m"),
    Region("cam", "Central America", "/m/01tzh"),
    Region("sam", "South America", "/m/06n3y"),
    Region("eur", "Europe", "/m/02j9z"),
    Region("afr", "Africa", "/m/0dg3n1"),
    Region("mde", "Middle East", "/m/04wsz"),
    Region("asia", "Asia", "/m/0j0k"),
    Region("oce", "Oceania", "/m/05nrg"),
)
REGION_BY_ID = {r.id: r for r in REGIONS}


@dataclass(frozen=True)
class Settings:
    serpapi_key: str | None
    provider: str
    airline_filter: str
    low_quota_warn: int
    map_ttl: int = 6 * 3600
    flights_ttl: int = 3600
    quota_ttl: int = 60
    db_path: Path = ROOT / "cache.sqlite3"
    default_origin: str = "BNA"


def load_settings() -> Settings:
    key = _read_key()
    provider = (os.getenv("DELTAMAP_PROVIDER") or ("serpapi" if key else "demo")).lower()
    return Settings(
        serpapi_key=key,
        provider=provider,
        airline_filter=os.getenv("AIRLINE_FILTER", "DL").upper(),
        low_quota_warn=int(os.getenv("LOW_QUOTA_WARN", "20")),
    )
