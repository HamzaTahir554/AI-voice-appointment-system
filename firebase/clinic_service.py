"""
Clinic service - the `clinics` collection.

This system has ONE clinic. `primary()` is that record (config.CLINIC_ID),
and every screen and every spoken reply resolves the clinic through here, so
changing the name in the administration area changes it everywhere at once -
doctor profiles, appointment details and what the assistant says on the
phone. Doctors do not own clinics and cannot create or choose one.

`get_clinic(id)` still exists for appointment records written before the
clinic was centralised; it reads whatever document that id points at.
"""
from __future__ import annotations

import logging

from config import CLINIC_ID, Collections
from firebase.firebase_config import DatabaseError, get_repository
from firebase.result import ServiceResult

logger = logging.getLogger(__name__)


class ClinicService:
    def __init__(self, repository=None):
        self.repo = repository or get_repository()

    def get_clinic(self, clinic_id: str) -> ServiceResult:
        try:
            doc = self.repo.get(Collections.CLINICS, clinic_id)
        except DatabaseError as exc:
            logger.error("get_clinic failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        if doc is None:
            return ServiceResult.failure("CLINIC_NOT_FOUND",
                                         f"No clinic {clinic_id}")
        return ServiceResult.success(doc)

    def primary(self) -> ServiceResult:
        """The one clinic. Created empty the first time it is asked for, so a
        fresh deployment has somewhere for the administrator to type into."""
        found = self.get_clinic(CLINIC_ID)
        if found.ok:
            return found
        if found.error == "BACKEND_UNAVAILABLE":
            return found
        try:
            created = self.repo.set(Collections.CLINICS, CLINIC_ID, {
                "clinic_id": CLINIC_ID,
                "name": "",
                "address": "",
                "city": "",
                "phone": "",
                "active": True,
            })
        except DatabaseError as exc:
            logger.error("could not create the clinic record: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        logger.info("clinic record %s created", CLINIC_ID)
        return ServiceResult.success(created)

    def get_clinic_for_doctor(self, doctor: dict) -> ServiceResult:
        """Every doctor works at the one clinic, so this is `primary()`.

        The argument is kept because the booking rules and the dialogue
        manager call it with a doctor record.
        """
        return self.primary()

    def list_clinics(self) -> ServiceResult:
        try:
            docs = self.repo.query(Collections.CLINICS)
        except DatabaseError as exc:
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success([d for d in docs if d.get("active", True)])

    def update_clinic(self, clinic_id: str, fields: dict) -> ServiceResult:
        """Update selected clinic fields (dashboard clinic information)."""
        if not clinic_id:
            return ServiceResult.failure("INVALID_INPUT", "clinic_id required")
        patch = {k: v for k, v in (fields or {}).items() if k != "clinic_id"}
        if not patch:
            return ServiceResult.failure("INVALID_INPUT", "nothing to update")
        try:
            existing = self.repo.get(Collections.CLINICS, clinic_id)
            if existing is None:
                return ServiceResult.failure("CLINIC_NOT_FOUND",
                                             f"No clinic with ID {clinic_id}")
            saved = self.repo.update(Collections.CLINICS, clinic_id, patch)
        except DatabaseError as exc:
            logger.error("update_clinic failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)

    def create_clinic(self, clinic: dict) -> ServiceResult:
        clinic_id = clinic.get("clinic_id")
        if not clinic_id:
            return ServiceResult.failure("INVALID_INPUT", "clinic_id required")
        try:
            saved = self.repo.set(Collections.CLINICS, clinic_id,
                                  {"active": True, **clinic})
        except DatabaseError as exc:
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)
