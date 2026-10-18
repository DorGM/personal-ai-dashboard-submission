"""Typed errors raised by the record repositories. Every message is safe to show to the user."""

from __future__ import annotations

# PRODUCT_SPEC §2.1 / §3.4: shown verbatim when a business key already exists.
DUPLICATE_MESSAGE = "נתון זה כבר הוכנס, אם תרצה להוסיף אליו נתונים תכניס זאת ידנית"


class RecordError(Exception):
    """Base class; `str(error)` is a user-facing message."""


class NotFound(RecordError):
    """No such record for this user (also raised for another user's id, so ids don't leak)."""

    def __init__(self) -> None:
        super().__init__("Record not found.")


class InvalidRecord(RecordError):
    """Input failed a business rule (date window, missing confirmation, out-of-range value)."""


class DuplicateRecord(RecordError):
    """An active record with the same business key exists (spec's Hebrew message)."""

    def __init__(self) -> None:
        super().__init__(DUPLICATE_MESSAGE)


class DeletedDuplicate(RecordError):
    """The business key is reserved by a soft-deleted record of this user.

    The user can restore that record, or delete it permanently and then add the new one.
    """

    def __init__(self, domain: str, record_id: int) -> None:
        super().__init__(
            "A deleted record with the same details exists in Recently deleted. "
            "Restore it, or delete it permanently and then add this one again."
        )
        self.domain = domain
        self.record_id = record_id
