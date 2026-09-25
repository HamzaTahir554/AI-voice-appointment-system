"""
Uniform return type for every service call.

Services never raise at the Dialog Manager. A backend problem becomes
`ServiceResult.failure(...)`, which the response generator turns into a polite
spoken sentence - one bad Firestore call must not drop a live phone call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ServiceResult:
    ok: bool
    data: Any = None
    error: str | None = None      # machine-readable code, e.g. DOCTOR_NOT_FOUND
    message: str | None = None    # human-readable detail for logs

    @classmethod
    def success(cls, data: Any = None) -> "ServiceResult":
        return cls(ok=True, data=data)

    @classmethod
    def failure(cls, error: str, message: str | None = None) -> "ServiceResult":
        return cls(ok=False, error=error, message=message)

    def __bool__(self) -> bool:
        return self.ok
