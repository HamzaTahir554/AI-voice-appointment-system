"""
Doctor service - `doctors` collection.

Name resolution lives here rather than in the entity extractor, so a doctor
mentioned by a caller is always matched against real database records. An
unrecognised name fails loudly instead of being accepted as a real doctor.
"""
from __future__ import annotations

import logging
import re
import unicodedata

from config import Collections
from firebase.firebase_config import DatabaseError, get_repository
from firebase.result import ServiceResult

logger = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"\b(dr\.?|doctor|ڈاکٹر)\b", re.I)


# Corrections put the negation on OPPOSITE sides in the two languages:
#   Urdu / Roman Urdu:  "Dr Ahmed nahi, Dr Asim"   -> negated name comes FIRST
#   English:            "not Dr Ahmed, Dr Sara"    -> negated name comes AFTER
# Handling only one order silently keeps the doctor the caller just rejected.
_URDU_REPLACEMENT = re.compile(
    r"(?:nahi|nahin|nhi|نہیں)\s*[,،]?\s*(.+)$", re.I | re.S)
_ENGLISH_REPLACEMENT = re.compile(
    r"\bnot\s+.+?\s*[,،]\s*(.+)$", re.I | re.S)


def _replacement_target(text: str) -> str | None:
    """
    The part of a correction that names the doctor the caller actually wants.

    Returns None when the utterance is not a correction, so ordinary requests
    are matched exactly as before.
    """
    raw = str(text)
    for pattern in (_ENGLISH_REPLACEMENT, _URDU_REPLACEMENT):
        match = pattern.search(raw)
        if not match:
            continue
        tail = match.group(1).strip()
        # A bare "nahi" with nothing after it is a refusal, not a replacement.
        if len(tail) >= 2:
            return tail
    return None


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text)).lower()
    return re.sub(r"\s+", " ", text).strip()


TITLES = ("dr", "dr.", "doctor", "prof", "prof.", "mr", "mrs", "ms")


def aliases_for(name: str, existing: list | None = None) -> list[str]:
    """The short forms a caller actually says for this doctor.

    Callers say "Dr Ahmed", never "D001", and `find_doctor` matches the name
    plus these aliases. Whenever a name is written - by the administrator or
    by the doctor themselves - the aliases have to follow it, or the voice
    assistant keeps answering to the old name. Non-Latin aliases already on
    the record (Urdu spellings) are kept, because they cannot be derived
    from a Latin name.
    """
    cleaned = " ".join(str(name or "").replace(".", " ").split())
    words = [word for word in cleaned.split(" ") if word.lower() not in TITLES]
    generated = []
    if words:
        generated.append(" ".join(words).lower())
        if len(words) > 1:
            generated.append(words[0].lower())
    kept = [a for a in (existing or []) if not str(a).isascii()]
    out: list[str] = []
    for alias in generated + kept:
        alias = str(alias).strip()
        if alias and alias not in out:
            out.append(alias)
    return out


class DoctorService:
    def __init__(self, repository=None):
        self.repo = repository or get_repository()

    # ------------------------------------------------------------------
    def list_doctors(self, active_only: bool = True) -> ServiceResult:
        try:
            docs = self.repo.query(Collections.DOCTORS)
        except DatabaseError as exc:
            logger.error("list_doctors failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        if active_only:
            docs = [d for d in docs if d.get("active", True)]
        return ServiceResult.success(docs)

    def get_doctor(self, doctor_id: str) -> ServiceResult:
        try:
            doc = self.repo.get(Collections.DOCTORS, doctor_id)
        except DatabaseError as exc:
            logger.error("get_doctor failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        if doc is None:
            return ServiceResult.failure("DOCTOR_NOT_FOUND",
                                         f"No doctor {doctor_id}")
        return ServiceResult.success(doc)

    # ------------------------------------------------------------------
    def find_doctor(self, text: str) -> ServiceResult:
        """
        Resolve free text ("Dr Ahmed", "احمد", "ahmed khan") to one doctor.

        Matching is longest-alias-first so a specific alias beats a generic
        one. Ambiguous matches are reported rather than guessed.
        """
        if not text or not text.strip():
            return ServiceResult.failure("DOCTOR_NOT_FOUND", "empty query")

        # "Dr Ahmed nahi, Dr Asim se karna hai" - the caller is REPLACING a
        # doctor. Without this, plain longest-alias matching picks "ahmed"
        # (the longer alias) and silently ignores the correction.
        replacement = _replacement_target(text)
        if replacement is not None:
            text = replacement

        listing = self.list_doctors()
        if not listing.ok:
            return listing
        doctors = listing.data
        needle = _normalize(text)
        stripped = _TITLE_RE.sub(" ", needle).strip()

        # 1. exact name match
        for doctor in doctors:
            if _normalize(doctor.get("name", "")) == needle:
                return ServiceResult.success(doctor)

        # 2. alias contained in the utterance, longest alias wins
        candidates: list[tuple[int, dict]] = []
        for doctor in doctors:
            aliases = [doctor.get("name", "")] + list(doctor.get("aliases", []))
            for alias in aliases:
                alias_norm = _TITLE_RE.sub(" ", _normalize(alias)).strip()
                if not alias_norm:
                    continue
                if re.search(rf"(?<![\w؀-ۿ]){re.escape(alias_norm)}"
                             rf"(?![\w؀-ۿ])", needle):
                    candidates.append((len(alias_norm), doctor))
                    break

        if not candidates and stripped:
            # 3. the caller replied with a bare name ("Ahmed") to "which doctor?"
            for doctor in doctors:
                aliases = [doctor.get("name", "")] + list(doctor.get("aliases", []))
                for alias in aliases:
                    alias_norm = _TITLE_RE.sub(" ", _normalize(alias)).strip()
                    if alias_norm and alias_norm in stripped:
                        candidates.append((len(alias_norm), doctor))
                        break

        if not candidates:
            return ServiceResult.failure("DOCTOR_NOT_FOUND",
                                         f"No doctor matching '{text}'")

        candidates.sort(key=lambda pair: pair[0], reverse=True)
        best_len = candidates[0][0]
        best = [d for length, d in candidates if length == best_len]
        unique = {d["doctor_id"]: d for d in best}
        if len(unique) > 1:
            return ServiceResult.failure(
                "DOCTOR_AMBIGUOUS",
                "Several doctors match: " +
                ", ".join(d.get("name", "") for d in unique.values()))
        return ServiceResult.success(candidates[0][1])

    # ------------------------------------------------------------------
    def update_doctor(self, doctor_id: str, fields: dict) -> ServiceResult:
        """
        Update selected fields of a doctor record (dashboard profile editing).

        Only the fields given are written; `doctor_id` can never be changed,
        and the record must already exist.
        """
        if not doctor_id:
            return ServiceResult.failure("INVALID_INPUT", "doctor_id required")
        patch = {k: v for k, v in (fields or {}).items() if k != "doctor_id"}
        if not patch:
            return ServiceResult.failure("INVALID_INPUT", "nothing to update")
        try:
            existing = self.repo.get(Collections.DOCTORS, doctor_id)
            if existing is None:
                return ServiceResult.failure("DOCTOR_NOT_FOUND",
                                             f"No doctor with ID {doctor_id}")
            saved = self.repo.update(Collections.DOCTORS, doctor_id, patch)
        except DatabaseError as exc:
            logger.error("update_doctor failed: %s", exc)
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)

    def create_doctor(self, doctor: dict) -> ServiceResult:
        doctor_id = doctor.get("doctor_id")
        if not doctor_id:
            return ServiceResult.failure("INVALID_INPUT", "doctor_id required")
        try:
            saved = self.repo.set(Collections.DOCTORS, doctor_id,
                                  {"active": True, **doctor})
        except DatabaseError as exc:
            return ServiceResult.failure("BACKEND_UNAVAILABLE", str(exc))
        return ServiceResult.success(saved)
