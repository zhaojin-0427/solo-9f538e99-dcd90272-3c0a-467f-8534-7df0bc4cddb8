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
