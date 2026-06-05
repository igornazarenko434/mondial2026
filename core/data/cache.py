"""Tiny on-disk cache so we fetch slow/scraped data once per day, not per call
(respects the 'cache static data' golden rule). JSON-based — no heavy deps.
"""
from __future__ import annotations
import json
import os
import time
from typing import Callable


def cached_json(path: str | None, ttl_hours: float, producer: Callable[[], object]):
    """Return cached JSON if it exists and is younger than ttl_hours; else call
    `producer`, cache the result, and return it. path=None disables caching."""
    if path and os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h <= ttl_hours:
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass                          # corrupt cache → re-produce
    data = producer()
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)                 # atomic write
    return data
