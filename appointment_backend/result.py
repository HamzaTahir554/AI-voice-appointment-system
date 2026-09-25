"""
The uniform result object every backend operation returns (spec section 15).

One shape for success and failure means the Dialog Manager, the Ollama judge
and the response validator all consume the same structure - and the judge can
be told, simply, "trust `success`".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OperationResult:
    """
    Result of a book / cancel / reschedule / check operation.

    `data` carries operation-specific extras (alternative slots, the doctor
    record, the old date on a reschedule) so the response layer never has to
    query the database again to phrase an answer.
    """
    success: bool
    operation: str                      # book | cancel | reschedule | check | availability
    appointment_id: str | None = None
    status: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None

    # ------------------------------------------------------------------
    @classmethod
    def ok(cls, operation: str, appointment_id: str | None = None,
           status: str | None = None, **data: Any) -> "OperationResult":
        return cls(success=True, operation=operation,
                   appointment_id=appointment_id, status=status, data=data)

    @classmethod
    def fail(cls, operation: str, code: str, message: str,
             **data: Any) -> "OperationResult":
        return cls(success=False, operation=operation,
                   error={"code": code, "message": message}, data=data)

    # ------------------------------------------------------------------
    @property
    def error_code(self) -> str | None:
        return self.error["code"] if self.error else None

    @property
    def error_message(self) -> str | None:
        return self.error["message"] if self.error else None

    @property
    def alternative_slots(self) -> list[str]:
        return list(self.data.get("alternative_slots", []))

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, exactly as the spec describes it."""
        return {
            "success": self.success,
            "operation": self.operation,
            "appointment_id": self.appointment_id,
            "status": self.status,
            "data": self.data,
            "error": self.error,
        }

    # NOTE: deliberately NO __bool__ here. Defining it as `return self.success`
    # makes every failure result falsy, so guards written as
    # `if failure: return failure` silently pass validation through. Callers
    # must test `.success` or `is not None` explicitly.
