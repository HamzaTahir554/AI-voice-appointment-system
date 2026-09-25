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


class DatabaseError(Exception):
    """Raised for backend failures the service layer should translate."""


# --------------------------------------------------------------------------
# Repository interface
# --------------------------------------------------------------------------
class BaseRepository:
    """
    The minimum surface the services need.

    Deliberately tiny: `get`, `set`, `update`, `query`, `delete` plus one
    atomic helper (`reserve_slot`) where double-booking must be prevented.
    """

    backend = "base"

    def get(self, collection: str, doc_id: str) -> dict | None:
        raise NotImplementedError

    def set(self, collection: str, doc_id: str, data: dict) -> dict:
        raise NotImplementedError

    def update(self, collection: str, doc_id: str, data: dict) -> dict:
        raise NotImplementedError

    def delete(self, collection: str, doc_id: str) -> bool:
        raise NotImplementedError

    def query(self, collection: str,
              filters: Iterable[tuple[str, str, Any]] = (),
              limit: int | None = None) -> list[dict]:
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


class LocalRepository(BaseRepository):
    """
    Dictionary-backed store with Firestore-compatible semantics.

    Not a database: data is lost when the process exits. It exists so the
    Dialog Manager can be developed and tested before Firebase is configured.
    """

    backend = "local"

    def __init__(self):
        self._data: dict[str, dict[str, dict]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def _collection(self, name: str) -> dict[str, dict]:
        return self._data.setdefault(name, {})

    def get(self, collection: str, doc_id: str) -> dict | None:
        with self._lock:
            doc = self._collection(collection).get(str(doc_id))
            return dict(doc) if doc else None

    def set(self, collection: str, doc_id: str, data: dict) -> dict:
        with self._lock:
            record = dict(data)
            record.setdefault("created_at", self.now())
            record["updated_at"] = self.now()
            self._collection(collection)[str(doc_id)] = record
            return dict(record)

    def update(self, collection: str, doc_id: str, data: dict) -> dict:
        with self._lock:
            existing = self._collection(collection).get(str(doc_id))
            if existing is None:
                raise DatabaseError(f"{collection}/{doc_id} does not exist")
            existing.update(data)
            existing["updated_at"] = self.now()
            return dict(existing)

    def delete(self, collection: str, doc_id: str) -> bool:
        with self._lock:
            return self._collection(collection).pop(str(doc_id), None) is not None

    def query(self, collection: str,
              filters: Iterable[tuple[str, str, Any]] = (),
              limit: int | None = None) -> list[dict]:
        with self._lock:
            results = []
            for doc in self._collection(collection).values():
                if all(self._matches(doc, f) for f in filters):
                    results.append(dict(doc))
            if limit is not None:
                results = results[:limit]
            return results

    @staticmethod
    def _matches(doc: dict, condition: tuple[str, str, Any]) -> bool:
        field, op, value = condition
        checker = _OPS.get(op)
        if checker is None:
            raise DatabaseError(f"unsupported operator {op!r}")
        return checker(doc.get(field), value)

    def reserve_slot(self, collection: str, doc_id: str, data: dict,
                     conflict_filters: Iterable[tuple[str, str, Any]]) -> dict:
        # The lock makes check-then-write atomic, mirroring the Firestore
        # transaction used by the real backend.
        with self._lock:
            if self.query(collection, conflict_filters, limit=1):
                raise SlotConflict("slot already booked")
            return self.set(collection, doc_id, data)


# --------------------------------------------------------------------------
# Firestore backend
# --------------------------------------------------------------------------
class FirestoreRepository(BaseRepository):
    """Cloud Firestore via firebase-admin."""

    backend = "firestore"

    def __init__(self, client):
        self.db = client

    # ------------------------------------------------------------------
    def get(self, collection: str, doc_id: str) -> dict | None:
        try:
            snapshot = self.db.collection(collection).document(str(doc_id)).get()
        except Exception as exc:                       # pragma: no cover
            raise DatabaseError(str(exc)) from exc
        return snapshot.to_dict() if snapshot.exists else None

    def set(self, collection: str, doc_id: str, data: dict) -> dict:
        record = dict(data)
        record.setdefault("created_at", self.now())
        record["updated_at"] = self.now()
        try:
            self.db.collection(collection).document(str(doc_id)).set(record)
        except Exception as exc:                       # pragma: no cover
            raise DatabaseError(str(exc)) from exc
        return record

    def update(self, collection: str, doc_id: str, data: dict) -> dict:
        record = dict(data)
        record["updated_at"] = self.now()
        try:
            ref = self.db.collection(collection).document(str(doc_id))
            ref.update(record)
            return ref.get().to_dict()
        except Exception as exc:                       # pragma: no cover
            raise DatabaseError(str(exc)) from exc

    def delete(self, collection: str, doc_id: str) -> bool:
        try:
            self.db.collection(collection).document(str(doc_id)).delete()
            return True
        except Exception as exc:                       # pragma: no cover
            raise DatabaseError(str(exc)) from exc

    def query(self, collection: str,
              filters: Iterable[tuple[str, str, Any]] = (),
              limit: int | None = None) -> list[dict]:
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            ref = self.db.collection(collection)
            for field, op, value in filters:
                ref = ref.where(filter=FieldFilter(field, op, value))
            if limit is not None:
                ref = ref.limit(limit)
            return [doc.to_dict() for doc in ref.stream()]
        except Exception as exc:                       # pragma: no cover
            raise DatabaseError(str(exc)) from exc

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

            return _txn(transaction)
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
