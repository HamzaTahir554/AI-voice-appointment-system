"""
Patient service - `patients` collection.

Callers are identified by phone number, so a returning patient is recognised
rather than duplicated on every call.
"""
from __future__ import annotations

import logging
import re

from config import Collections
from firebase.firebase_config import DatabaseError, get_repository
from firebase.result import ServiceResult

logger = logging.getLogger(__name__)

# Pakistani mobile formats: 03001234567 / +923001234567.
_PHONE_RE = re.compile(r"^(?:\+?92|0)3\d{9}$")


def normalize_phone(phone: str | None) -> str | None:
    """Reduce any accepted format to the canonical 03XXXXXXXXX."""
    if not phone:
        return None
    digits = re.sub(r"[\s\-()]", "", str(phone))
    if digits.startswith("+92"):
        digits = "0" + digits[3:]
    elif digits.startswith("92") and len(digits) == 12:
        digits = "0" + digits[2:]
    return digits if _PHONE_RE.match(digits) else None


class PatientService:
    def __init__(self, repository=None):
        self.repo = repository or get_repository()

    # ------------------------------------------------------------------
    def get_patient(self, patient_id: str) -> ServiceResult:
        try:
            doc = self.repo.get(Collections.PATIENTS, patient_id)
        except DatabaseError as exc:
            logger.error("get_patient failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        if doc is None:
            return ServiceResult.failure("PATIENT_NOT_FOUND",
                                         f"No patient {patient_id}")
        return ServiceResult.success(doc)

    def find_by_phone(self, phone: str) -> ServiceResult:
        normalized = normalize_phone(phone)
        if normalized is None:
            return ServiceResult.failure("INVALID_PHONE",
                                         f"'{phone}' is not a valid number")
        try:
            matches = self.repo.query(Collections.PATIENTS,
                                      [("phone", "==", normalized)], limit=1)
        except DatabaseError as exc:
            logger.error("find_by_phone failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        if not matches:
            return ServiceResult.failure("PATIENT_NOT_FOUND",
                                         f"No patient with phone {normalized}")
        return ServiceResult.success(matches[0])

    # ------------------------------------------------------------------
    def create_patient(self, name: str | None = None, phone: str | None = None,
                       patient_id: str | None = None) -> ServiceResult:
        normalized = normalize_phone(phone)
        if phone and normalized is None:
            return ServiceResult.failure("INVALID_PHONE",
                                         f"'{phone}' is not a valid number")
        try:
            if patient_id is None:
                existing = self.repo.query(Collections.PATIENTS)
                patient_id = f"P{len(existing) + 1:03d}"
            record = {
                "patient_id": patient_id,
                "name": name,
                "phone": normalized,
            }
            saved = self.repo.set(Collections.PATIENTS, patient_id, record)
        except DatabaseError as exc:
            logger.error("create_patient failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)

    def get_or_create(self, name: str | None = None,
                      phone: str | None = None) -> ServiceResult:
        """
        Find a returning caller by phone, otherwise register them.

        Prevents a duplicate patient record on every inbound call.
        """
        if phone:
            found = self.find_by_phone(phone)
            if found.ok:
                # Fill in a name we did not have before.
                if name and not found.data.get("name"):
                    return self.update_patient(found.data["patient_id"],
                                               {"name": name})
                return found
            if found.error == "BACKEND_UNAVAILABLE":
                return found
        return self.create_patient(name=name, phone=phone)

    def update_patient(self, patient_id: str, fields: dict) -> ServiceResult:
        if "phone" in fields:
            normalized = normalize_phone(fields["phone"])
            if normalized is None:
                return ServiceResult.failure("INVALID_PHONE")
            fields = {**fields, "phone": normalized}
        try:
            updated = self.repo.update(Collections.PATIENTS, patient_id, fields)
        except DatabaseError as exc:
            logger.error("update_patient failed: %s", exc)
            return ServiceResult.failure("PATIENT_NOT_FOUND", str(exc))
        return ServiceResult.success(updated)
