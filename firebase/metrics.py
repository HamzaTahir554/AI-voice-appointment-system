"""
Per-request database accounting - development instrumentation.

Every repository call records how many round trips it made, how many
documents it read and how many it wrote, into a counter that belongs to the
HTTP request being served (a context variable, so concurrent requests never
mix their numbers).

Counting is always on - it is a few integer additions - so the tests can
assert that an endpoint does not regress to reading whole collections. It is
only REPORTED when PERF_DEBUG=1: the API then adds a `Server-Timing` header
(visible in the browser's developer tools, never in the interface) and logs
one line per request. Nothing is exposed in a normal deployment.

Reads are counted the way Firestore bills them: a document fetch is one read
(even when the document does not exist), a batch fetch one read per id asked
for, a query one read per document returned (minimum one), and a count()
aggregation one read per 1,000 index entries counted (minimum one).

`calls` are round trips: `update` is two (the write, then reading the
document back), everything else one. A query Firestore refuses - a missing
index - still cost a round trip and is counted as one with no reads.
"""
from __future__ import annotations

import contextvars
import os
import threading
import time
from dataclasses import dataclass, field
from functools import wraps


def enabled() -> bool:
    return os.environ.get("PERF_DEBUG", "").lower() in ("1", "true", "yes")


@dataclass
class RequestMetrics:
    calls: int = 0            # round trips to the database
    reads: int = 0            # billable document reads
    writes: int = 0           # document writes
    seconds: float = 0.0      # time spent waiting on the database
    operations: dict = field(default_factory=dict)
    # Reads may run in parallel threads for one request.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, operation: str, reads: int, writes: int, seconds: float,
               round_trips: int = 1) -> None:
        with self._lock:
            self.calls += round_trips
            self.reads += reads
            self.writes += writes
            self.seconds += seconds
            self.operations[operation] = self.operations.get(operation, 0) + 1

    def summary(self) -> str:
        return (f"calls={self.calls} reads={self.reads} writes={self.writes} "
                f"db_ms={self.seconds * 1000:.0f}")


_current: contextvars.ContextVar = contextvars.ContextVar("db_metrics", default=None)


def begin() -> RequestMetrics:
    """Start counting for the current request (called by the API middleware)."""
    metrics = RequestMetrics()
    _current.set(metrics)
    return metrics


def current() -> RequestMetrics | None:
    return _current.get()


def _reads_for(operation: str, result, args: tuple, kwargs: dict) -> int:
    if operation == "query":
        return max(1, len(result or []))
    if operation == "get_many":
        ids = kwargs.get("doc_ids", args[2] if len(args) > 2 else ())
        return max(1, len(set(str(i) for i in ids if i)))
    if operation == "count":
        return max(1, -(-int(result or 0) // 1000))
    if operation in ("get", "update"):
        return 1               # update reads the document back afterwards
    return 0


def _writes_for(operation: str) -> int:
    return 1 if operation in ("set", "update", "delete", "reserve_slot") else 0


def metered(operation: str):
    """Decorate a repository method so its cost lands on the request."""
    round_trips = 2 if operation == "update" else 1

    def decorate(method):
        @wraps(method)
        def wrapper(*args, **kwargs):
            started = time.perf_counter()
            try:
                result = method(*args, **kwargs)
            except Exception:
                metrics = _current.get()
                if metrics is not None:
                    metrics.record(operation + ":failed", 0, 0,
                                   time.perf_counter() - started)
                raise
            metrics = _current.get()
            if metrics is not None:
                metrics.record(operation, _reads_for(operation, result, args, kwargs),
                               _writes_for(operation),
                               time.perf_counter() - started, round_trips)
            return result
        return wrapper
    return decorate
