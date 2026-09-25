"""
Notification service - the `notifications` collection.

The Appointment Backend already WRITES notification records when a doctor
blocks a day (cancellation_service._queue_notification): one record per
affected patient, with `status: "pending"` and the message to deliver. This
service reads that queue for the dashboard and marks items the doctor has
seen.

Delivery (SMS / automated call) is NOT implemented. `NotificationSender`
below is the seam for it: implement `send_sms` and `place_call` against a
real provider, take the credentials from environment variables, and call
`mark_sent`. Nothing here invents a provider or a key.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from config import Collections, TIMEZONE
from firebase.firebase_config import DatabaseError, get_repository
from firebase.result import ServiceResult

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, repository=None):
        self.repo = repository or get_repository()

    # ------------------------------------------------------------------
    def list_for_doctor(self, doctor_id: str, limit: int = 50) -> ServiceResult:
        """Newest first. These are the messages queued for this doctor's patients."""
        try:
            rows = self.repo.query(Collections.NOTIFICATIONS,
                                   [("doctor_id", "==", doctor_id)])
        except DatabaseError as exc:
            logger.error("list_for_doctor failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
        return ServiceResult.success(rows[:limit])

    def unread_count(self, doctor_id: str) -> int:
        result = self.list_for_doctor(doctor_id, limit=200)
        if not result.ok:
            return 0
        return sum(1 for row in result.data if not row.get("read_by_doctor"))

    def mark_read(self, notification_id: str) -> ServiceResult:
        """`read_by_doctor` is added alongside the delivery `status`; the two
        are different things and the delivery status is never touched here."""
        try:
            existing = self.repo.get(Collections.NOTIFICATIONS, notification_id)
            if existing is None:
                return ServiceResult.failure("NOT_FOUND", "No such notification")
            saved = self.repo.update(Collections.NOTIFICATIONS, notification_id,
                                     {"read_by_doctor": True})
        except DatabaseError as exc:
            logger.error("mark_read failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)

    def mark_all_read(self, doctor_id: str) -> ServiceResult:
        listing = self.list_for_doctor(doctor_id, limit=200)
        if not listing.ok:
            return listing
        changed = 0
        for row in listing.data:
            if row.get("read_by_doctor"):
                continue
            try:
                self.repo.update(Collections.NOTIFICATIONS, row["notification_id"],
                                 {"read_by_doctor": True})
                changed += 1
            except DatabaseError as exc:
                logger.error("mark_all_read failed on %s: %s",
                             row.get("notification_id"), exc)
        return ServiceResult.success({"updated": changed})

    # ------------------------------------------------------------------
    def pending_deliveries(self, limit: int = 100) -> ServiceResult:
        """Records still waiting for SMS / voice delivery."""
        try:
            rows = self.repo.query(Collections.NOTIFICATIONS,
                                   [("status", "==", "pending")])
        except DatabaseError as exc:
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(rows[:limit])

    def mark_sent(self, notification_id: str, channel: str) -> ServiceResult:
        """Called by a delivery worker once a message really went out."""
        try:
            saved = self.repo.update(Collections.NOTIFICATIONS, notification_id, {
                "status": "sent",
                "delivered_channel": channel,
                "delivered_at": datetime.now(TIMEZONE).isoformat(),
            })
        except DatabaseError as exc:
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)


# --------------------------------------------------------------------------
# Delivery seam - not implemented
# --------------------------------------------------------------------------
class NotificationSender:
    """
    Interface for the SMS / automated-call layer.

    Implement this against a provider (Twilio, Jazz, Telenor, an Asterisk
    originate call ...) and read every credential from the environment.
    """

    name = "unconfigured"

    def available(self) -> bool:
        return False

    def send_sms(self, phone: str, message: str) -> bool:
        raise NotImplementedError

    def place_call(self, phone: str, message: str) -> bool:
        raise NotImplementedError


class UnconfiguredSender(NotificationSender):
    """
    What runs today: it records that a message would be sent and returns
    False, so the queue keeps its `pending` status and nothing silently
    pretends a patient was told.
    """

    def send_sms(self, phone: str, message: str) -> bool:
        logger.info("SMS not sent (no provider configured): %s chars to %s",
                    len(message or ""), phone)
        return False

    def place_call(self, phone: str, message: str) -> bool:
        logger.info("call not placed (no provider configured) to %s", phone)
        return False


def get_sender() -> NotificationSender:
    """
    The sender for this deployment.

    A provider is only used when NOTIFICATION_PROVIDER names one and its
    credentials are in the environment. No provider is bundled, so this
    currently always returns the unconfigured sender.
    """
    provider = os.environ.get("NOTIFICATION_PROVIDER", "").strip().lower()
    if provider and provider != "none":
        logger.warning("NOTIFICATION_PROVIDER=%s is set but no sender is "
                       "implemented yet; messages stay queued", provider)
    return UnconfiguredSender()
