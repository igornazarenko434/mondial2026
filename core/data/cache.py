"""Tiny on-disk cache so we fetch slow/scraped data once per day, not per call
(respects the 'cache static data' golden rule). JSON-based — no heavy deps.
"""
from __future__ import annotations
import json
import os
import tempfile
import time
from typing import Callable


def cached_json(path: str | None, ttl_hours: float, producer: Callable[[], object]):
    """Return cached JSON if it exists and is younger than ttl_hours; else call
    `producer`, cache the result, and return it. path=None disables caching.

    Race-safe: two daemon workers can miss the cache in the same tick (4-window
    dispatcher fires up to 4 jobs per tick into a ThreadPoolExecutor). The
    historical bug used `f"{path}.tmp"` as a hardcoded sentinel — the second
    `os.replace(tmp, path)` raised FileNotFoundError because the first writer
    had already consumed the tmp file (see Switzerland T-24h, 2026-06-17). We
    use `tempfile.mkstemp` to get a process+thread-unique tmp name so concurrent
    writers never collide on the rename. The final result is identical (the
    producer is deterministic) so last-writer-wins is fine.
    """
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
        _atomic_write_json(path, data)
    return data


def _atomic_write_json(path: str, data: object, **dump_kwargs) -> None:
    """Write `data` as JSON to `path` atomically. Safe under concurrent writers.

    Extra `dump_kwargs` are forwarded to json.dump (e.g. indent=2, ensure_ascii=False).
    """
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)
    # mkstemp guarantees a unique filename — no two writers ever produce the same
    # tmp path, so `os.replace(tmp, path)` never finds its source already
    # consumed by another worker's rename.
    fd, tmp = tempfile.mkstemp(
        dir=target_dir,
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, **dump_kwargs)
        os.replace(tmp, path)                 # atomic on the same filesystem
    except Exception:
        # If anything between mkstemp and the successful replace fails, clean up.
        # After a successful os.replace, `tmp` no longer exists at the original
        # name, so this unlink is only relevant on failure.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
