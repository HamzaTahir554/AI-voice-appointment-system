"""
Dashboard sign-in accounts: usernames and passwords that can be changed.

Until now a doctor signed in with their doctor id and a password read from
the server environment, which meant nobody could ever change one. The clinic
administrator has to be able to issue and reset credentials, and a doctor has
to be able to change their own password, so the credentials live in the
database like everything else:

    dashboard_accounts/{account_id}
        account_id      "D001", or the administrator's id
        username        what the person types to sign in (lowercase, unique)
        role            doctor | superadmin
        password_hash   PBKDF2-HMAC-SHA256, hex
        password_salt   16 random bytes, hex
        iterations      the work factor the hash was made with

**The password itself is never stored and never leaves this module.** Only
`public()` output is safe to send to a browser; it carries the username and
whether a password has been set, nothing else.

Accounts are created lazily: a doctor who has never had a password set has no
document here at all, and `api/auth.py` falls back to the environment
password so an existing deployment keeps working. The moment a password is
set here, that stored one is the only one that opens the account.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re

from config import Collections
from firebase.firebase_config import DatabaseError, get_repository
from firebase.result import ServiceResult

logger = logging.getLogger("firebase.accounts")

ROLE_DOCTOR = "doctor"
ROLE_ADMIN = "superadmin"

ITERATIONS = 120_000
MIN_PASSWORD = 8
MAX_PASSWORD = 128
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")


def hash_password(password: str, salt: bytes | None = None,
                  iterations: int = ITERATIONS) -> tuple[str, str, int]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"),
                                 salt, iterations)
    return digest.hex(), salt.hex(), iterations


def check_password_rules(password: str) -> str | None:
    """The message to show, or None when the password is acceptable."""
    text = str(password or "")
    if len(text.strip()) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(text) > MAX_PASSWORD:
        return "That password is too long."
    return None


def check_username_rules(username: str) -> str | None:
    text = str(username or "").strip().lower()
    if not USERNAME_RE.match(text):
        return ("A username is 3 to 32 characters: letters, digits, dots, "
                "dashes or underscores, starting with a letter or digit.")
    return None


class AccountService:
    """Credentials for the dashboard. Nothing here touches appointments."""

    def __init__(self, repository=None):
        self.repo = repository or get_repository()

    # ------------------------------------------------------------ reading
    def get(self, account_id: str) -> ServiceResult:
        try:
            record = self.repo.get(Collections.ACCOUNTS,
                                   str(account_id or "").upper())
        except DatabaseError as exc:
            logger.error("account read failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        if not record:
            return ServiceResult.failure("NOT_FOUND", "No such account")
        return ServiceResult.success(record)

    def find_by_username(self, username: str) -> ServiceResult:
        wanted = str(username or "").strip().lower()
        if not wanted:
            return ServiceResult.failure("NOT_FOUND", "No such account")
        try:
            rows = self.repo.query(Collections.ACCOUNTS,
                                   [("username", "==", wanted)])
        except DatabaseError as exc:
            logger.error("account lookup failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        if not rows:
            return ServiceResult.failure("NOT_FOUND", "No such account")
        return ServiceResult.success(rows[0])

    def list_accounts(self) -> ServiceResult:
        try:
            return ServiceResult.success(list(self.repo.query(Collections.ACCOUNTS)))
        except DatabaseError as exc:
            logger.error("account listing failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))

    @staticmethod
    def public(record: dict | None) -> dict:
        """The only shape that may be sent to a browser."""
        record = record or {}
        return {
            "account_id": record.get("account_id"),
            "username": record.get("username"),
            "role": record.get("role"),
            "has_password": bool(record.get("password_hash")),
        }

    def username_of(self, account_id: str) -> str | None:
        found = self.get(account_id)
        return found.data.get("username") if found.ok else None

    # ------------------------------------------------------------ writing
    def _save(self, account_id: str, fields: dict) -> ServiceResult:
        account_id = str(account_id or "").upper()
        try:
            existing = self.repo.get(Collections.ACCOUNTS, account_id)
            if existing:
                saved = self.repo.update(Collections.ACCOUNTS, account_id, fields)
            else:
                saved = self.repo.set(Collections.ACCOUNTS, account_id,
                                      {"account_id": account_id, **fields})
        except DatabaseError as exc:
            logger.error("account write failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)

    def set_password(self, account_id: str, password: str,
                     role: str = ROLE_DOCTOR) -> ServiceResult:
        problem = check_password_rules(password)
        if problem:
            return ServiceResult.failure("INVALID_PASSWORD", problem)
        digest, salt, iterations = hash_password(password)
        result = self._save(account_id, {
            "role": role,
            "password_hash": digest,
            "password_salt": salt,
            "iterations": iterations,
        })
        if result.ok:
            # The password itself is never written to a log.
            logger.info("password set for %s", str(account_id).upper())
        return result

    def set_username(self, account_id: str, username: str,
                     role: str = ROLE_DOCTOR,
                     reserved: set | None = None) -> ServiceResult:
        problem = check_username_rules(username)
        if problem:
            return ServiceResult.failure("INVALID_USERNAME", problem)
        wanted = str(username).strip().lower()
        account_id = str(account_id or "").upper()

        # A username must not collide with another account's username, nor
        # with an id somebody else can already sign in with.
        taken = self.find_by_username(wanted)
        if taken.ok and str(taken.data.get("account_id", "")).upper() != account_id:
            return ServiceResult.failure("USERNAME_TAKEN",
                                         "That username is already in use.")
        for value in (reserved or set()):
            if str(value).lower() == wanted and str(value).upper() != account_id:
                return ServiceResult.failure("USERNAME_TAKEN",
                                             "That username is already in use.")
        return self._save(account_id, {"username": wanted, "role": role})

    def clear(self, account_id: str) -> ServiceResult:
        """Used by the tests; leaves the doctor record untouched."""
        try:
            self.repo.delete(Collections.ACCOUNTS, str(account_id or "").upper())
        except DatabaseError as exc:
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success({"account_id": account_id})

    # ----------------------------------------------------------- checking
    def has_password(self, account_id: str) -> bool:
        found = self.get(account_id)
        return bool(found.ok and found.data.get("password_hash"))

    def verify(self, account_id: str, password: str) -> bool:
        """Constant-time check against the stored hash.

        False when the account has no stored password: the caller then
        decides whether an environment password may still be used.
        """
        found = self.get(account_id)
        if not found.ok or not found.data.get("password_hash"):
            return False
        record = found.data
        try:
            salt = bytes.fromhex(record.get("password_salt", ""))
        except ValueError:
            logger.error("account %s has an unreadable salt", account_id)
            return False
        digest, _, _ = hash_password(password or "", salt,
                                     int(record.get("iterations") or ITERATIONS))
        return hmac.compare_digest(digest, str(record.get("password_hash")))
