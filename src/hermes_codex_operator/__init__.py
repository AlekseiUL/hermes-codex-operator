"""Portable core for reliable Hermes-to-Codex task operation."""

from .journal import (
    BaselineConflict,
    BindingConflict,
    Finalization,
    IdempotencyConflict,
    InvalidTransition,
    Journal,
    JournalError,
    ReceiptConflict,
    RecordNotFound,
    Reservation,
    SQLiteJournal,
    TaskBusy,
)
from .models import (
    ChangedPathEvidence,
    ContractValidationError,
    OperationState,
    Receipt,
    VerificationEvidence,
    WorkContract,
)
from .engine import Operator, OperatorOutcome
from .catalog import CatalogOperator, CatalogResult, WatchPollResult

__version__ = "0.1.0"

__all__ = [
    "BaselineConflict",
    "BindingConflict",
    "ChangedPathEvidence",
    "ContractValidationError",
    "CatalogOperator",
    "CatalogResult",
    "Finalization",
    "IdempotencyConflict",
    "InvalidTransition",
    "Journal",
    "JournalError",
    "OperationState",
    "Receipt",
    "ReceiptConflict",
    "RecordNotFound",
    "Reservation",
    "SQLiteJournal",
    "Operator",
    "OperatorOutcome",
    "TaskBusy",
    "VerificationEvidence",
    "WorkContract",
    "WatchPollResult",
    "__version__",
]
