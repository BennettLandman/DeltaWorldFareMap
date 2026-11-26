"""Short-lived SQLite cache, a log of live API calls, and confirmed Main Cabin fares."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY, created REAL NOT NULL, ttl REAL NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calls (
                ts REAL NOT NULL, provider TEXT NOT NULL, engine TEXT NOT NULL, summary TEXT
            );
            CREATE TABLE IF NOT EXISTS main_fares (
                key TEXT PRIMARY KEY, price REAL, created REAL NOT NULL
            );
            """
        )
        self._db.commit()

    # -- cache ---------------------------------------------------------------
    def get(self, key: str) -> Any | None:
        with self._lock:
            row = self._db.execute(
                "SELECT created, ttl, payload FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        created, ttl, payload = row
        if time.time() - created > ttl:
            return None
        return json.loads(payload)

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def put(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cache (key, created, ttl, payload) VALUES (?, ?, ?, ?)",
                (key, time.time(), ttl, json.dumps(value)),
            )
            self._db.commit()

    # -- call log ------------------------------------------------------------
    def log_call(self, provider: str, engine: str, summary: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO calls (ts, provider, engine, summary) VALUES (?, ?, ?, ?)",
                (time.time(), provider, engine, summary),
            )
            self._db.commit()

    def calls_since(self, provider: str, seconds: float) -> int:
        with self._lock:
            (n,) = self._db.execute(
                "SELECT COUNT(*) FROM calls WHERE provider = ? AND ts > ?",
                (provider, time.time() - seconds),
            ).fetchone()
        return n

    # -- Main Cabin fares confirmed on click ----------------------------------
    def put_main_fare(self, key: str, price: float | None, keep_lower: bool = False) -> None:
        """Record the lowest Main Cabin fare found. keep_lower: never raise a known fare."""
        if keep_lower:
            known, old = self.main_fare(key, 6 * 3600)
            if known and old is not None and (price is None or old <= price):
                return
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO main_fares (key, price, created) VALUES (?, ?, ?)",
                (key, price, time.time()),
            )
            self._db.commit()

    def main_fare(self, key: str, max_age: float) -> tuple[bool, float | None]:
        with self._lock:
            row = self._db.execute(
                "SELECT price, created FROM main_fares WHERE key = ?", (key,)
            ).fetchone()
        if not row or time.time() - row[1] > max_age:
            return False, None
        return True, row[0]


def explore_key(provider: str, airline_filter: str, origin: str, depart: str, ret: str,
                region_id: str) -> str:
    return f"{provider}|explore|{airline_filter}|{origin}|{depart}|{ret}|{region_id}"
