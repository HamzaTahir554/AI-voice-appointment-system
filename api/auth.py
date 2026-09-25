"""
Sign-in for the dashboard: doctors, and one super administrator.

The project had NO authentication before this: every API route was open and
the dashboard used a hardcoded demo password in its own JavaScript. This is
the smallest honest replacement - enough to keep one doctor out of another
doctor's appointments, to keep doctors out of the administration area, and
structured so it can be swapped for Firebase Authentication later without
touching the routes.

How it works
    POST /auth/login {user_id, password}
        -> checks the password against the environment (never the database,
           which holds no credentials), creates a random opaque token
        -> {token, role, doctor|admin}
    Every protected route then needs:  Authorization: Bearer <token>

Two roles, and they do not overlap:
    doctor      reads and writes ONLY its own doctor_id's data (/dashboard/*)
    superadmin  manages the doctors themselves (/admin/*), and cannot open a
                doctor's private dashboard

Where passwords live
    In `dashboard_accounts` (firebase/account_service.py), hashed with
    PBKDF2-HMAC-SHA256, so the administrator can issue and reset them and a
    doctor can change their own. An account with no stored password falls
    back to the environment password below, which is what makes an existing
    deployment keep working; once a password is stored, only that one opens
    the account.

What it is not
    Tokens live in this process's memory, so restarting the API signs
    everyone out. There is no self-service reset flow, no refresh tokens and
    no rate limiting. Production should replace this with Firebase
    Authentication and verify the ID token here instead.

Configuration (environment, never in code or in the frontend)
    DASHBOARD_PASSWORD            starting password for doctor accounts
    DASHBOARD_PASSWORD_D001       per-doctor override (D001 = the doctor id)
    DASHBOARD_SESSION_HOURS       session lifetime, default 12
    SUPERADMIN_ID                 the administrator's sign-in id, default ADMIN
    SUPERADMIN_PASSWORD           starting password for the administrator
If neither a stored nor an environment password exists, sign-in is refused,
so an unconfigured deployment is closed rather than open.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta

from fastapi import Header, HTTPException

logger = logging.getLogger("api.auth")

SESSION_HOURS = float(os.environ.get("DASHBOARD_SESSION_HOURS", 12))

ROLE_DOCTOR = "doctor"
ROLE_ADMIN = "superadmin"

_sessions: dict[str, dict] = {}
_lock = threading.Lock()


# --------------------------------------------------------------------------
# Passwords (environment only)
# --------------------------------------------------------------------------
def configured_password(doctor_id: str) -> str | None:
    """The password for this doctor, or None when none is configured."""
    specific = os.environ.get("DASHBOARD_PASSWORD_" + str(doctor_id).upper())
    return specific or os.environ.get("DASHBOARD_PASSWORD") or None


def password_configured() -> bool:
    return bool(os.environ.get("DASHBOARD_PASSWORD")) or any(
        key.startswith("DASHBOARD_PASSWORD_") for key in os.environ)


def _accounts():
    from firebase.account_service import AccountService
    return AccountService()


def verify(doctor_id: str, password: str) -> bool:
    """Constant-time password check for a doctor.

    The stored credential wins. Only an account that has never had one set
    falls back to the environment password, so changing a password here
    actually takes effect.
    """
    accounts = _accounts()
    if accounts.has_password(doctor_id):
        return accounts.verify(doctor_id, password)
    expected = configured_password(doctor_id)
    if not expected or password is None:
        return False
    return hmac.compare_digest(str(password), str(expected))


def admin_id() -> str:
    """The id the administrator signs in with. Never a doctor id."""
    return (os.environ.get("SUPERADMIN_ID") or "ADMIN").strip().upper()


def admin_configured() -> bool:
    return bool(os.environ.get("SUPERADMIN_PASSWORD")) or         _accounts().has_password(admin_id())


def verify_admin(user_id: str, password: str) -> bool:
    if str(user_id).strip().upper() != admin_id():
        return False
    accounts = _accounts()
    if accounts.has_password(admin_id()):
        return accounts.verify(admin_id(), password)
    expected = os.environ.get("SUPERADMIN_PASSWORD")
    if not expected or password is None:
        return False
    return hmac.compare_digest(str(password), str(expected))


def verify_account(account_id: str, role: str, password: str) -> bool:
    """One entry point for both roles, used by sign-in and by a password
    change that has to confirm the current password first."""
    if role == ROLE_ADMIN:
        return verify_admin(account_id, password)
    return verify(account_id, password)


def is_admin_id(user_id: str) -> bool:
    return str(user_id or "").strip().upper() == admin_id()


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
def create_session(account_id: str, role: str = ROLE_DOCTOR) -> dict:
    token = secrets.token_urlsafe(32)
    expires = datetime.now() + timedelta(hours=SESSION_HOURS)
    with _lock:
        _sessions[token] = {"account_id": account_id, "role": role,
                            "expires": expires}
    logger.info("session opened for %s (%s)", account_id, role)
    return {"token": token, "role": role,
            "expires_at": expires.isoformat(timespec="seconds")}


def end_session(token: str) -> None:
    with _lock:
        session = _sessions.pop(token, None)
    if session:
        logger.info("session closed for %s", session["account_id"])


def _session_for(token: str) -> dict | None:
    with _lock:
        session = _sessions.get(token)
        if session is None:
            return None
        if session["expires"] < datetime.now():
            _sessions.pop(token, None)
            return None
        return {"account_id": session["account_id"], "role": session["role"]}


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail={
        "error": "not_signed_in",
        "message": "Sign in to continue."
    })


def _forbidden(message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={
        "error": "wrong_role",
        "message": message
    })


def account_from_header(authorization: str | None) -> dict:
    """{'account_id', 'role'} for the bearer token, or 401."""
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    session = _session_for(token) if token else None
    if not session:
        raise _unauthorized()
    return session


def doctor_from_header(authorization: str | None) -> str:
    session = account_from_header(authorization)
    if session["role"] != ROLE_DOCTOR:
        raise _forbidden("This area belongs to a doctor account.")
    return session["account_id"]


def admin_from_header(authorization: str | None) -> str:
    session = account_from_header(authorization)
    if session["role"] != ROLE_ADMIN:
        raise _forbidden("Administrator access is required.")
    return session["account_id"]


# ------------------------------- FastAPI dependencies ---------------------
def current_account(authorization: str | None = Header(default=None)) -> dict:
    return account_from_header(authorization)


def current_doctor(authorization: str | None = Header(default=None)) -> str:
    return doctor_from_header(authorization)


def current_admin(authorization: str | None = Header(default=None)) -> str:
    return admin_from_header(authorization)


def end_sessions_for(account_id: str, keep: str | None = None) -> int:
    """Sign an account out everywhere else - used when its password changes."""
    account_id = str(account_id or "").upper()
    with _lock:
        doomed = [token for token, session in _sessions.items()
                  if str(session["account_id"]).upper() == account_id
                  and token != keep]
        for token in doomed:
            _sessions.pop(token, None)
    if doomed:
        logger.info("%d session(s) ended for %s after a credential change",
                    len(doomed), account_id)
    return len(doomed)


def token_from_header(authorization: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def clear_all_sessions() -> None:
    """Used by the tests so one test cannot sign another one in."""
    with _lock:
        _sessions.clear()
