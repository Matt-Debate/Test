"""Dataclasses mirroring db/schema.sql."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Every action the audit trail can carry, in the order they were added. The
# CHECK constraint in db/schema.sql lists the same strings; HistoryActionsTests
# fails if the two drift, and Database._migrate_history_actions widens the
# constraint on a database that predates an entry. Lives here rather than in
# the store because app/db.py needs it and must not import the store.
HISTORY_ACTIONS = (
    "create", "update", "mark_paid", "unmark_paid", "delete",
    # v0.13.0 — money that came back, and its undo
    "refund", "refund_delete",
    # v0.13.0 — the class tracker's own mutations, keyed by the funding payment
    "package_create", "package_update", "package_delete",
    "class_log", "class_unlog",
)


def generate_id() -> str:
    """12-char hex id — stable, non-sequential, URL-safe."""
    return uuid.uuid4().hex[:12]


@dataclass
class Expense:
    """One ledger row.

    ``amount`` is the EFFECTIVE figure — what was paid less what came back —
    and it is the field every total, chart and course rate reads. That is
    deliberate: nine portal sites and three store sites already sum a key
    called ``amount``, and a net figure under a new name would be a place for
    each of them to forget (which is how borrow took three releases to keep
    out of the totals). ``gross_amount`` is the figure before refunds — the
    one an edit form must show and ``update(amount=…)`` writes — and
    ``refunded`` is what came back. ``refunds`` lists each one.
    """

    id: str
    date: str
    amount: float
    currency: str = "CNY"
    category: Optional[str] = None
    description: Optional[str] = None
    paid: bool = False
    paid_date: Optional[str] = None
    submitted_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    gross_amount: Optional[float] = None
    refunded: float = 0.0
    refunds: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # a row built without refunds — every row before v0.13.0, and every
        # freshly created one — is its own gross figure
        if self.gross_amount is None:
            self.gross_amount = self.amount

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def column_values(self) -> dict[str, Any]:
        """The stored columns only — what an INSERT binds.

        ``to_dict`` now carries derived fields (``refunded``, ``refunds``,
        ``gross_amount``), and binding a list is a driver error; binding the
        net ``amount`` would be a ledger error. The column is the GROSS figure.
        """
        return {
            "id": self.id, "date": self.date, "amount": self.gross_amount,
            "currency": self.currency, "category": self.category,
            "description": self.description, "paid": self.paid,
            "paid_date": self.paid_date, "submitted_by": self.submitted_by,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


@dataclass
class HistoryEntry:
    id: str
    expense_id: str
    seq: int  # monotonic per-expense ordering
    action: str  # one of HISTORY_ACTIONS
    changed_by: Optional[str]
    changed_at: str
    snapshot: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
