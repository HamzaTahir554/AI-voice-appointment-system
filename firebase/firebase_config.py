"""
Firebase initialisation and the repository abstraction.

Two backends implement the same small interface:

  * `FirestoreRepository` - real Cloud Firestore via the Firebase Admin SDK.
  * `LocalRepository`     - an in-memory dictionary store.

The local backend exists so the whole system runs, and the test-suite passes,
on a machine with no Firebase project and no credentials. Add credentials to
`.env` and the exact same code talks to Firestore - no other file changes.

Credentials are read from environment variables only. A service-account JSON
must never be committed; see README section "Firebase setup".
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from config import (
    FIREBASE_CLIENT_EMAIL,
    FIREBASE_CREDENTIALS_FILE,
    FIREBASE_PRIVATE_KEY,
    FIREBASE_PROJECT_ID,
    USE_LOCAL_DB,
)

logger = logging.getLogger(__name__)


from firebase.metrics import metered  # noqa: E402


class DatabaseError(Exception):
    """Raised for backend failures the service layer should translate."""


class MissingIndex(DatabaseError):
    """Firestore refused a query because the composite index it needs has not
    been created. The message carries the console link Firestore provides;
    `firestore.indexes.json` lists every index this project declares."""


# Equality-style operators: Firestore serves any mix of these on different
# fields from its automatic single-field indexes. A range operator combined
# with an equality on another field, or a sort on another field, needs a
# composite index.
EQUALITY_OPS = ("==", "in", "array-contains", "array-contains-any")

# Callbacks told which collection was just written, so a cache in front of
# the repository can drop what it holds for it (firebase/cache.py).
_write_listeners: list[Callable[[str | None], None]] = []


def on_write(callback: Callable[[str | None], None]) -> None:
    if callback not in _write_listeners:
        _write_listeners.append(callback)


def _written(collection: str | None) -> None:
    for callback in list(_write_listeners):
        callback(collection)


# --------------------------------------------------------------------------
# Repository interface
# --------------------------------------------------------------------------
class BaseRepository:
    """
    The minimum surface the services need.

    Deliberately small: `get`, `get_many`, `set`, `update`, `query`, `count`,
    `delete` plus one atomic helper (`reserve_slot`) where double-booking
    must be prevented.
    """

    backend = "base"

    def get(self, collection: str, doc_id: str) -> dict | None:
        raise NotImplementedError

    def get_many(self, collection: str, doc_ids: Iterable[str]) -> dict[str, dict]:
        """{doc_id: record} for the ids that exist, in one round trip."""
        raise NotImplementedError

    def set(self, collection: str, doc_id: str, data: dict) -> dict:
        raise NotImplementedError

    def update(self, collection: str, doc_id: str, data: dict) -> dict:
        raise NotImplementedError

    def delete(self, collection: str, doc_id: str) -> bool:
        raise NotImplementedError

    def query(self, collection: str,
              filters: Iterable[tuple[str, str, Any]] = (),
              limit: int | None = None,
              order_by: str | None = None,
              descending: bool = False) -> list[dict]:
        raise NotImplementedError

    def count(self, collection: str,
              filters: Iterable[tuple[str, str, Any]] = ()) -> int:
        """How many documents match, without downloading them."""
        raise NotImplementedError

    def reserve_slot(self, collection: str, doc_id: str, data: dict,
                     conflict_filters: Iterable[tuple[str, str, Any]]) -> dict:
        """
        Atomically create `data` only if nothing matches `conflict_filters`.

        This is the double-booking guard: two callers asking for the same
        4 PM slot at the same moment must not both succeed.
        """
        raise NotImplementedError

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()


class SlotConflict(DatabaseError):
    """The requested slot was taken between the check and the write."""


# --------------------------------------------------------------------------
# Local in-memory backend
# --------------------------------------------------------------------------
_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a is not None and a > b,
    ">=": lambda a, b: a is not None and a >= b,
    "<": lambda a, b: a is not None and a < b,
    "<=": lambda a, b: a is not None and a <= b,
    "in": lambda a, b: a in b,
    "not-in": lambda a, b: a not in b,
    "array-contains": lambda a, b: isinstance(a, list) and b in a,
}


def declared_indexes(path: str | None = None) -> set[tuple]:
    """The composite indexes `firestore.indexes.json` declares, as
    (collection, ((field, "ASCENDING"|"DESCENDING"), ...)) tuples."""
    import json
    import os

    path = path or os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "firestore.indexes.json")
    try:
        with open(path, encoding="utf-8") as handle:
            spec = json.load(handle)
    except (OSError, ValueError):
        return set()
    found = set()
    for index in spec.get("indexes", []):
        fields = tuple((f.get("fieldPath"), f.get("order", "ASCENDING"))
                       for f in index.get("fields", []))
        found.add((index.get("collectionGroup"), fields))
    return found


def index_needed(collection: str, filters: Iterable[tuple[str, str, Any]],
                 order_by: str | None = None,
                 descending: bool = False) -> tuple | None:
    """The composite index Firestore would demand for this query, or None
    when its automatic single-field indexes are enough.

    Mirrors Firestore's rule: a range filter (or a sort) on one field
    combined with an equality filter on another needs a composite index of
    the equality field(s) followed by the range/sort field.
    """
    filters = list(filters)
    equal = sorted({f for f, op, _ in filters if op in EQUALITY_OPS})
    ranged = [f for f, op, _ in filters if op not in EQUALITY_OPS]
    sort: list[tuple[str, str]] = []
    if ranged:
        first = ranged[0]
        direction = "DESCENDING" if (descending and order_by == first) else "ASCENDING"
        sort.append((first, direction))
    if order_by and order_by not in [f for f, _ in sort]:
        sort.append((order_by, "DESCENDING" if descending else "ASCENDING"))
    if not sort or (not equal and len(sort) == 1):
        return None
    return (collection, tuple((f, "ASCENDING") for f in equal) + tuple(sort))


class LocalRepository(BaseRepository):
    """
    Dictionary-backed store with Firestore-compatible semantics.

    Not a database: data is lost when the process exits. It exists so the
    Dialog Manager can be developed and tested before Firebase is configured.

    It also enforces Firestore's index rule: a query that would need a
    composite index raises `MissingIndex` unless `firestore.indexes.json`
    declares that index. So a test can never pass here with a query that
    would fail against the real database. Set `indexes = set()` to behave
    like a project where no index has been deployed yet.
    """

    backend = "local"

    def __init__(self, indexes: set | None = None):
        self._data: dict[str, dict[str, dict]] = {}
        self._lock = threading.RLock()
        self.indexes = declared_indexes() if indexes is None else set(indexes)

    # ------------------------------------------------------------------
    def _collection(self, name: str) -> dict[str, dict]:
        return self._data.setdefault(name, {})

    def _check_index(self, collection, filters, order_by=None, descending=False):
        needed = index_needed(collection, filters, order_by, descending)
        if needed is None or needed in self.indexes:
            return
        # Firestore merges indexes for several equality fields: one per
        # equality field, each followed by the same range/sort fields.
        collection_name, fields = needed
        equal = [f for f in fields if f[0] in
                 {name for name, op, _ in filters if op in EQUALITY_OPS}]
        tail = tuple(f for f in fields if f not in equal)
        if len(equal) > 1 and all((collection_name, (e,) + tail) in self.indexes
                                  for e in equal):
            return
        raise MissingIndex(f"The query requires an index: {collection} "
                           + ", ".join(f"{name} {order}" for name, order in fields))

    @metered("get")
    def get(self, collection: str, doc_id: str) -> dict | None:
        with self._lock:
            doc = self._collection(collection).get(str(doc_id))
            return dict(doc) if doc else None

    @metered("get_many")
    def get_many(self, collection: str, doc_ids: Iterable[str]) -> dict[str, dict]:
        with self._lock:
            store = self._collection(collection)
            found = {}
            for doc_id in dict.fromkeys(str(i) for i in doc_ids if i):
                if doc_id in store:
                    found[doc_id] = dict(store[doc_id])
            return found

    @metered("set")
    def set(self, collection: str, doc_id: str, data: dict) -> dict:
        with self._lock:
            record = dict(data)
            record.setdefault("created_at", self.now())
            record["updated_at"] = self.now()
            self._collection(collection)[str(doc_id)] = record
        _written(collection)
        return dict(record)

    @metered("update")
    def update(self, collection: str, doc_id: str, data: dict) -> dict:
        with self._lock:
            existing = self._collection(collection).get(str(doc_id))
            if existing is None:
                raise DatabaseError(f"{collection}/{doc_id} does not exist")
            existing.update(data)
            existing["updated_at"] = self.now()
            saved = dict(existing)
        _written(collection)
        return saved

    @metered("delete")
    def delete(self, collection: str, doc_id: str) -> bool:
        with self._lock:
            removed = self._collection(collection).pop(str(doc_id), None) is not None
        _written(collection)
        return removed

    def _select(self, collection, filters, order_by=None, descending=False):
        filters = list(filters)
        self._check_index(collection, filters, order_by, descending)
        with self._lock:
            results = [dict(doc) for doc in self._collection(collection).values()
                       if all(self._matches(doc, f) for f in filters)]
        if order_by:
            # Firestore leaves out documents that lack the sort field.
            results = [r for r in results if r.get(order_by) is not None]
            results.sort(key=lambda r: r[order_by], reverse=descending)
        return results

    @metered("query")
    def query(self, collection: str,
              filters: Iterable[tuple[str, str, Any]] = (),
              limit: int | None = None,
              order_by: str | None = None,
              descending: bool = False) -> list[dict]:
        results = self._select(collection, filters, order_by, descending)
        if limit is not None:
            results = results[:limit]
        return results

    @metered("count")
    def count(self, collection: str,
              filters: Iterable[tuple[str, str, Any]] = ()) -> int:
        return len(self._select(collection, filters))

    @staticmethod
    def _matches(doc: dict, condition: tuple[str, str, Any]) -> bool:
        field, op, value = condition
        checker = _OPS.get(op)
        if checker is None:
            raise DatabaseError(f"unsupported operator {op!r}")
        return checker(doc.get(field), value)

    @metered("reserve_slot")
    def reserve_slot(self, collection: str, doc_id: str, data: dict,
                     conflict_filters: Iterable[tuple[str, str, Any]]) -> dict:
        # The lock makes check-then-write atomic, mirroring the Firestore
        # transaction used by the real backend. The inner steps are not
        # metered separately: against Firestore this is one transaction.
        with self._lock:
            if self._select(collection, conflict_filters):
                raise SlotConflict("slot already booked")
            record = dict(data)
            record.setdefault("created_at", self.now())
            record["updated_at"] = self.now()
            self._collection(collection)[str(doc_id)] = record
        _written(collection)
        return dict(record)


# --------------------------------------------------------------------------
# Firestore backend
# --------------------------------------------------------------------------
class FirestoreRepository(BaseRepository):
    """Cloud Firestore via firebase-admin."""

    backend = "firestore"

    def __init__(self, client):
        self.db = client

    @staticmethod
    def _failure(exc: Exception) -> DatabaseError:
        """A missing composite index is reported as such, so the caller can
        fall back to a simpler query instead of failing the request."""
        try:
            from google.api_core.exceptions import FailedPrecondition
        except ImportError:                            # pragma: no cover
            FailedPrecondition = ()
        if isinstance(exc, FailedPrecondition) and "index" in str(exc).lower():
            return MissingIndex(str(exc))
        return DatabaseError(str(exc))

    def _filtered(self, collection: str, filters: Iterable[tuple[str, str, Any]]):
        from google.cloud.firestore_v1.base_query import FieldFilter

        ref = self.db.collection(collection)
        for field, op, value in filters:
            ref = ref.where(filter=FieldFilter(field, op, value))
        return ref

    # ------------------------------------------------------------------
    @metered("get")
    def get(self, collection: str, doc_id: str) -> dict | None:
        try:
            snapshot = self.db.collection(collection).document(str(doc_id)).get()
        except Exception as exc:                       # pragma: no cover
            raise DatabaseError(str(exc)) from exc
        return snapshot.to_dict() if snapshot.exists else None

    @metered("get_many")
    def get_many(self, collection: str, doc_ids: Iterable[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(str(i) for i in doc_ids if i))
        if not ids:
            return {}
        try:
            refs = [self.db.collection(collection).document(i) for i in ids]
            return {snap.id: snap.to_dict() for snap in self.db.get_all(refs)
                    if snap.exists}
        except Exception as exc:                       # pragma: no cover
            raise DatabaseError(str(exc)) from exc

    @metered("set")
    def set(self, collection: str, doc_id: str, data: dict) -> dict:
        record = dict(data)
        record.setdefault("created_at", self.now())
        record["updated_at"] = self.now()
        try:
            self.db.collection(collection).document(str(doc_id)).set(record)
        except Exception as exc:                       # pragma: no cover
            raise DatabaseError(str(exc)) from exc
        _written(collection)
        return record

    @metered("update")
    def update(self, collection: str, doc_id: str, data: dict) -> dict:
        record = dict(data)
        record["updated_at"] = self.now()
        try:
            ref = self.db.collection(collection).document(str(doc_id))
            ref.update(record)
            _written(collection)
            return ref.get().to_dict()
        except Exception as exc:                       # pragma: no cover
            raise DatabaseError(str(exc)) from exc

    @metered("delete")
    def delete(self, collection: str, doc_id: str) -> bool:
        try:
            self.db.collection(collection).document(str(doc_id)).delete()
            _written(collection)
            return True
        except Exception as exc:                       # pragma: no cover
            raise DatabaseError(str(exc)) from exc

    @metered("query")
    def query(self, collection: str,
              filters: Iterable[tuple[str, str, Any]] = (),
              limit: int | None = None,
              order_by: str | None = None,
              descending: bool = False) -> list[dict]:
        try:
            from google.cloud import firestore as gcf

            ref = self._filtered(collection, filters)
            if order_by:
                ref = ref.order_by(order_by, direction=(
                    gcf.Query.DESCENDING if descending else gcf.Query.ASCENDING))
            if limit is not None:
                ref = ref.limit(limit)
            return [doc.to_dict() for doc in ref.stream()]
        except Exception as exc:                       # pragma: no cover
            raise self._failure(exc) from exc

    @metered("count")
    def count(self, collection: str,
              filters: Iterable[tuple[str, str, Any]] = ()) -> int:
        """A server-side aggregation: Firestore bills one read per 1,000
        index entries it counts, and no document is downloaded."""
        try:
            result = self._filtered(collection, filters).count().get()
            return int(result[0][0].value)
        except Exception as exc:                       # pragma: no cover
            raise self._failure(exc) from exc

    @metered("reserve_slot")
    def reserve_slot(self, collection: str, doc_id: str, data: dict,
                     conflict_filters: Iterable[tuple[str, str, Any]]) -> dict:
        """
        Firestore transaction: re-read the conflicting query inside the
        transaction, then write only if it is still empty. Firestore retries
        the callback automatically if another writer commits first.
        """
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter
            from firebase_admin import firestore

            record = dict(data)
            record.setdefault("created_at", self.now())
            record["updated_at"] = self.now()

            transaction = self.db.transaction()
            collection_ref = self.db.collection(collection)
            doc_ref = collection_ref.document(str(doc_id))

            query = collection_ref
            for field, op, value in conflict_filters:
                query = query.where(filter=FieldFilter(field, op, value))

            @firestore.transactional
            def _txn(txn):
                existing = list(query.limit(1).stream(transaction=txn))
                if existing:
                    raise SlotConflict("slot already booked")
                txn.set(doc_ref, record)
                return record

            saved = _txn(transaction)
            _written(collection)
            return saved
        except SlotConflict:
            raise
        except Exception as exc:                       # pragma: no cover
            raise DatabaseError(str(exc)) from exc


# --------------------------------------------------------------------------
# Initialisation
# --------------------------------------------------------------------------
_repository: BaseRepository | None = None
_init_lock = threading.Lock()


def _build_credentials():
    """Assemble Admin SDK credentials from env vars or a JSON file."""
    from firebase_admin import credentials

    if FIREBASE_CREDENTIALS_FILE:
        return credentials.Certificate(FIREBASE_CREDENTIALS_FILE)
    if FIREBASE_PROJECT_ID and FIREBASE_CLIENT_EMAIL and FIREBASE_PRIVATE_KEY:
        return credentials.Certificate({
            "type": "service_account",
            "project_id": FIREBASE_PROJECT_ID,
            "client_email": FIREBASE_CLIENT_EMAIL,
            "private_key": FIREBASE_PRIVATE_KEY,
            "token_uri": "https://oauth2.googleapis.com/token",
        })
    return None


def _has_firebase_config() -> bool:
    return bool(FIREBASE_CREDENTIALS_FILE or
                (FIREBASE_PROJECT_ID and FIREBASE_CLIENT_EMAIL
                 and FIREBASE_PRIVATE_KEY))


def init_repository(force_local: bool = False) -> BaseRepository:
    """
    Return the process-wide repository, creating it on first call.

    Falls back to the local backend - with a clear warning - whenever Firebase
    is not configured or the SDK is missing, so a missing `.env` degrades to a
    working demo instead of a crash.
    """
    global _repository
    with _init_lock:
        if _repository is not None:
            return _repository

        if force_local or USE_LOCAL_DB or not _has_firebase_config():
            if not _has_firebase_config() and not (force_local or USE_LOCAL_DB):
                logger.warning(
                    "Firebase is not configured (FIREBASE_PROJECT_ID / "
                    "FIREBASE_CLIENT_EMAIL / FIREBASE_PRIVATE_KEY missing). "
                    "Falling back to the in-memory store - data will NOT "
                    "persist. See README 'Firebase setup'.")
            _repository = LocalRepository()
            return _repository

        try:
            import firebase_admin
            from firebase_admin import firestore

            if not firebase_admin._apps:
                cred = _build_credentials()
                firebase_admin.initialize_app(cred)
            _repository = FirestoreRepository(firestore.client())
            logger.info("Connected to Firestore project %s", FIREBASE_PROJECT_ID)
        except ImportError:
            logger.warning("firebase-admin is not installed; using the local "
                           "store. Run: pip install firebase-admin")
            _repository = LocalRepository()
        except Exception as exc:                       # pragma: no cover
            logger.error("Firebase initialisation failed (%s); using the local "
                         "store so the service stays up.", exc)
            _repository = LocalRepository()
        return _repository


def get_repository() -> BaseRepository:
    return _repository or init_repository()


def reset_repository(repository: BaseRepository | None = None) -> None:
    """Swap the repository. Used by the tests to inject a clean store."""
    global _repository
    with _init_lock:
        _repository = repository
    _written(None)             # a new store: nothing cached is valid any more
