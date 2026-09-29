"""
A short-lived cache for the records that almost never change.

The dashboard re-reads the same few documents on nearly every request: the
one clinic record and the doctor register (names for the appointment table,
the administrator's doctor list, the statistics table). Reading them from
Firestore each time costs a round trip of half a second or more from a
clinic's connection, for data that changes a few times a month.

Correctness comes first:

  * Every write made through the repository - set, update, delete, a
    booking transaction - drops what is cached for that collection at once
    (`firebase_config.on_write`). So a change made through this API is
    visible on the very next request.
  * A change made by another process (a script, the Firebase console) is
    picked up when the entry expires: `TTL_SECONDS`, one minute.
  * Appointments, patients and notifications are never cached here. Only
    the collections listed in `CACHEABLE` are, and the booking rules read
    doctors through `DoctorService`, which does not use this cache, so a
    doctor who was just switched off can never be booked from a stale copy.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable

from config import Collections
from firebase import firebase_config

TTL_SECONDS = 60.0
CACHEABLE = {Collections.CLINICS, Collections.DOCTORS}

_entries: dict[tuple[str, str], tuple[float, Any]] = {}
_lock = threading.Lock()
_generation = {"value": 0}     # bumped by every invalidation


def cached(collection: str, key: str, loader: Callable[[], Any],
           ttl: float = TTL_SECONDS) -> Any:
    """The cached value for (collection, key), loading it when missing."""
    if collection not in CACHEABLE:
        return loader()
    slot = (collection, key)
    now = time.monotonic()
    with _lock:
        hit = _entries.get(slot)
        if hit and hit[0] > now:
            # A copy, so a caller that edits what it got cannot change what
            # the next request sees.
            return copy.deepcopy(hit[1])
        generation = _generation["value"]
    value = loader()
    with _lock:
        # A write that landed while we were reading makes this copy suspect.
        if _generation["value"] == generation:
            _entries[slot] = (now + ttl, copy.deepcopy(value))
    return value


def invalidate(collection: str | None = None) -> None:
    with _lock:
        _generation["value"] += 1
        if collection is None:
            _entries.clear()
            return
        for slot in [s for s in _entries if s[0] == collection]:
            _entries.pop(slot, None)


def clear() -> None:
    invalidate(None)


firebase_config.on_write(invalidate)
