"""Domain errors and their HTTP mapping."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for expected, client-visible failures."""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DuplicatePatientError(DomainError):
    status_code = 409
    code = "duplicate_patient_in_week"


class InvalidImportPayloadError(DomainError):
    """The streamed import body is malformed or fails schema validation."""

    status_code = 422
    code = "invalid_import_payload"


class PayloadTooLargeError(DomainError):
    """The import body exceeds the byte cap and is rejected unread."""

    status_code = 413
    code = "payload_too_large"


class WeekAlreadyImportedError(DomainError):
    status_code = 409
    code = "week_already_imported"


class ImportNotFoundError(DomainError):
    status_code = 404
    code = "import_version_not_found"


class PublicationNotFoundError(DomainError):
    status_code = 404
    code = "publication_version_not_found"


class BudgetExhaustedError(DomainError):
    status_code = 409
    code = "privacy_budget_exhausted"


class StorageError(DomainError):
    """A write failed; the transaction was rolled back and no budget was spent."""

    status_code = 503
    code = "storage_write_failed"
