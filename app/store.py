"""Store — all reads/writes for the household ledger.

Contract guarantees implemented here (docs/FEATURE_CONTRACT.md §6):
  * server-authoritative validation (amount > 0, YYYY-MM-DD dates,
    paid ⇒ paid_date, unknown update fields rejected);
  * every mutation writes exactly one append-only ``expense_history`` row in
    the SAME transaction as the primary write (finding M3);
  * timestamps are application-managed UTC ISO strings (portable SQL);
  * token minting is a first-class, operator-only capability (finding M2).
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from math import ceil, fsum, isfinite
from typing import Any, Optional

from .db import Database
from .models import Expense, HistoryEntry, generate_id


class ValidationError(ValueError):
    """Caller-supplied data failed validation."""


class NotFoundError(KeyError):
    """No expense with that id.

    Subclasses KeyError so the HTTP layer's existing ``except KeyError`` → 404
    mapping is untouched, but ``str()`` is coaching rather than a repr'd id.
    The MCP path has no translation layer of its own, so the agent used to see
    the bare id — which says nothing about how to retry.
    """

    def __init__(self, expense_id: Any):
        self.expense_id = expense_id
        super().__init__(
            f"no expense with id {expense_id!r} — ids come from expenses_list; "
            "or target it by query=<a word from its description> instead"
        )

    def __str__(self) -> str:  # KeyError.__str__ repr()s args[0]
        return self.args[0]


class PackageNotFoundError(NotFoundError):
    """No class package with that id.

    Separate from NotFoundError because the inherited message points at
    ``expenses_list``, which cannot produce a package id — a cross-reference
    that names something unable to help is what P3 forbids.
    """

    def __init__(self, package_id: Any):
        KeyError.__init__(
            self,
            f"no class package with id {package_id!r} — ids come from "
            "classes_list; or target it by query=<a word from the course name> "
            "instead"
        )
        self.expense_id = None
        self.package_id = package_id


_ALLOWED_UPDATE_FIELDS = frozenset(
    {"date", "amount", "currency", "category", "description", "submitted_by"}
)
_EXPENSE_COLS = (
    "id, date, amount, currency, category, description, "
    "paid, paid_date, submitted_by, created_at, updated_at"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Canonical category list. `category` stays free text in the database (the MCP
# can write anything), but these are what the portal offers and what analytics
# group by. Grouped for the phone dropdown; the group labels are UI-only.
# Mirrored in app/portal.html — CategoryParityTests fails if the two drift.
CATEGORIES = (
    ("living",        "生活费",       "Living"),
    ("aden-edu",      "Aden 教育",    "Aden · education"),
    ("aden-sports",   "Aden 运动",    "Aden · sports"),
    ("aden-clothes",  "Aden 衣服",    "Aden · clothes"),
    ("aden-other",    "Aden 其他",    "Aden · other"),
    ("food",          "食品",         "Food"),
    ("home",          "家居",         "Home"),
    ("utilities",     "水电",         "Utilities"),
    ("internet",      "网络",         "Internet"),
    ("mobile",        "手机",         "Mobile"),
    ("transport",     "交通",         "Transport"),
    ("travel",        "旅行",         "Travel"),
    ("entertainment", "娱乐",         "Entertainment"),
    ("clothes",       "衣服",         "Clothes"),
    ("medical",       "医疗",         "Medical"),
    ("borrow",        "我垫付",       "I paid (owed back to me)"),
    ("other",         "其他",         "Other"),
)
CATEGORY_KEYS = tuple(key for key, _zh, _en in CATEGORIES)

# 'borrow' is the one category with arithmetic attached: she fronted the money,
# so it is owed back to HER rather than being household spending. It is kept out
# of every expense total and reported on its own — repaying her must not read as
# the family having spent that money.
BORROW_CATEGORY = "borrow"

# ── class tracker ────────────────────────────────────────────────────────
# Two shapes of prepaid course, distinguished by what the money buys:
#   per_class — a pack of N classes. Attending consumes one; what is left is
#               classes you still own.
#   period    — a flat month/semester fee. Nothing is consumed; what matters is
#               the classes that did NOT happen, which are owed back.
CLASS_KINDS = ("per_class", "period")
# A missed class is owed back either way, but the cause is kept because only
# one of them is worth arguing about: 'missed_school' is reclaimable,
# 'missed_us' is what the household forfeited.
CLASS_EVENT_KINDS = ("attended", "missed_school", "missed_us")
_MISSED_EVENT_KINDS = ("missed_school", "missed_us")
# Which payments the Classes tab offers to link a course to. A view concern,
# NOT a money rule: the store links a package to any expense and MCP
# `classes_add` still can, which is the escape hatch for a course paid under
# some other key. The ledger carries a year of future-dated living-expense
# rows, and date-ordered they buried the four payments that were actually
# courses under twelve months of rent.
CLASS_CATEGORIES = ("aden-edu", "aden-sports")
# category is nullable, and `category <> 'borrow'` is NULL (not true) for a NULL
# category, which would silently drop uncategorised rows from every total.
_NOT_BORROW = "COALESCE(category, '') <> 'borrow'"
_IS_BORROW = "COALESCE(category, '') = 'borrow'"


def is_class_category(category: Optional[str]) -> bool:
    """Whether a payment's category makes it a course payment.

    Compared case- and space-insensitively because `category` is free text and
    the MCP demonstrably drifts from the canonical keys — the live ledger holds
    twelve rows written as 'living expenses' rather than 'living'. A payment
    that silently fails to appear in the dropdown cannot explain itself, so the
    match is forgiving about everything except the actual word.
    """
    return str(category or "").strip().lower() in CLASS_CATEGORIES


class _ConstraintLost(Exception):
    """Internal: a class_packages constraint fired during INSERT.

    Raised to unwind the failed transaction before working out WHICH one. The
    diagnosis needs a query, and a query inside an aborted Postgres
    transaction is itself an error — so it has to happen outside.
    """


def _is_integrity_error(exc: BaseException) -> bool:
    """A UNIQUE/CHECK/FK violation, on either driver.

    Matched by class name rather than by importing psycopg, because the test
    suite must run with no Postgres driver installed at all (P8).
    """
    names = {type(exc).__name__ for exc in (exc,)} | {
        base.__name__ for base in type(exc).__mro__
    }
    return bool(names & {"IntegrityError", "UniqueViolation", "ForeignKeyViolation"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _household_now() -> datetime:
    """Now in the household's timezone (APP_TZ, default China).

    Server clocks run UTC; a family in China adding an expense after
    08:00 CST would otherwise get 'yesterday'. Falling back to UTC is the safe
    move for a live service — but it puts a China household on the wrong day
    after 08:00 CST, so it must not be silent. `tzdata` is a pinned runtime
    dependency precisely so that branch stays unreachable;
    BacklogRegressionTests proves the zone data is present and APP_TZ honoured.
    """
    import os

    tz_name = os.environ.get("APP_TZ", "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name))
    except Exception as exc:
        import sys

        print(
            f"WARNING: APP_TZ={tz_name!r} unusable ({exc!r}); dates fall back to UTC",
            file=sys.stderr,
        )
        return datetime.now(timezone.utc)


def _seconds_until_midnight_from(now: datetime) -> int:
    """Whole seconds from `now` until the household's next midnight.

    Subtracting two aware datetimes that carry the SAME ZoneInfo does
    wall-clock arithmetic, which is an hour wrong either side of a DST change.
    Shanghai has no DST, but APP_TZ is configurable and a helper that is only
    correct for one zone is a trap. Both sides are converted to UTC first, so
    the answer is real elapsed time.

    ceil, not truncate: int() lands a fraction of a second BEFORE midnight, and
    the page would roll the date over that much early.
    """
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    delta = midnight.astimezone(timezone.utc) - now.astimezone(timezone.utc)
    return max(1, ceil(delta.total_seconds()))


def today_and_midnight() -> tuple[str, int]:
    """The household's date AND how much of it is left, from ONE clock read.

    Read separately, a request straddling midnight returns yesterday's date
    beside a nearly-whole-day countdown — which tells the page to keep offering
    yesterday for another day, the exact defect the countdown exists to fix.
    """
    now = _household_now()
    return now.strftime("%Y-%m-%d"), _seconds_until_midnight_from(now)


def seconds_until_midnight() -> int:
    """Whole seconds until the household's next midnight.

    The portal extrapolates `today` from elapsed time while a page stays open.
    Elapsed time alone rolls the date over 24h after the response rather than
    at midnight, so a page loaded at 23:50 kept offering yesterday for almost
    a whole day. It cannot work this out for itself: a DATE says nothing about
    how much of it is left.
    """
    return _seconds_until_midnight_from(_household_now())


def today_str() -> str:
    """Today's date in the household's timezone (APP_TZ, default China).

    Server clocks run UTC; a family in China adding an expense after
    08:00 CST would otherwise get 'yesterday'.
    """
    return _household_now().strftime("%Y-%m-%d")


# tolerated decoration around spoken/pasted amounts: ¥300, 300块, 1,200元, "300 rmb"
_AMOUNT_NOISE_RE = re.compile(r"[¥￥,，\s]|元|块|rmb|cny", re.IGNORECASE)

# LIKE metacharacters in a user's search string. Unescaped, query='%' matched
# every row in the ledger — an agent asked to find one expense got all of them.
_LIKE_SPECIALS_RE = re.compile(r"([\\%_])")


class Store:
    #: the household's only currency — see _validate_currency
    CURRENCY = "CNY"

    def __init__(self, db: Database):
        self.db = db

    # ── validation ────────────────────────────────────────────────────────
    # Error strings double as agent coaching: an LLM that calls wrongly reads
    # the message and self-corrects on the next call.
    @staticmethod
    def _validate_amount(amount: Any) -> float:
        original = amount
        if isinstance(amount, str):
            amount = _AMOUNT_NOISE_RE.sub("", amount)
        try:
            val = float(amount)
        except (TypeError, ValueError):
            raise ValidationError(
                f"amount {original!r} not understood — pass digits, e.g. 300, "
                "'¥300' or '300块' (Chinese numerals like 三百 must be converted "
                "to digits first)"
            )
        if not (val > 0):
            raise ValidationError(f"amount must be greater than 0, got {val}")
        if val > 1e12:
            # No household expense is a trillion yuan, and without a ceiling
            # two absurd rows make fsum() return inf in summarize() — the same
            # unrecoverable 500-on-every-load as an infinite amount, one layer
            # up. Rejecting the write is the only place this can be stopped.
            raise ValidationError(
                f"amount {original!r} is implausibly large — check the digits"
            )
        if not isfinite(val):
            # `inf > 0` is True, so this passed the check above and committed.
            # JSONResponse serialises with allow_nan=False, so EVERY later
            # /api/list then 500s — and the row can only be removed by someone
            # with database access. One bad write locks her out of the portal.
            raise ValidationError(
                f"amount {original!r} is not a real number — pass digits, e.g. 300"
            )
        return val

    @staticmethod
    def _validate_date(value: Any, field: str = "date") -> str:
        text = str(value).strip() if value is not None else ""
        if not text:
            raise ValidationError(
                f"{field} is required — YYYY-MM-DD, or omit it to default to today"
            )
        if not _DATE_RE.match(text):
            raise ValidationError(
                f"{field} {text!r} invalid — use YYYY-MM-DD (e.g. 2026-07-14); "
                "convert relative words like 昨天/yesterday to a real date, or "
                "omit the field to default to today"
            )
        # The regex only pins the SHAPE. '2026-13-01' and '2026-02-30' matched
        # it and were stored, and a date no calendar has is not merely untidy —
        # though the two fail differently downstream, which is why BOTH have to
        # be refused here rather than handled in the portal:
        #   '2026-13-01' → Date.parse gives NaN, and NaN is neither <= 30 nor
        #     > 30, so the Due tab's two halves both rejected it and the row
        #     rendered in no section at all (v0.12.0's own defect, via MCP);
        #   '2026-02-30' → normalises silently to early March, so it renders,
        #     sorts and totals as a row due on a day she never entered. No
        #     renderer can detect that; only refusing the write can.
        # strptime is deliberately paired with the regex rather than replacing
        # it: it would otherwise accept unpadded '2026-8-1'.
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            raise ValidationError(
                f"{field} {text!r} is not a real date — check the month and day "
                "(there is no 13th month, and no 30th of February)"
            ) from None
        return text

    @classmethod
    def _validate_currency(cls, value: Any) -> str:
        """This ledger is CNY-only, and says so rather than quietly lying.

        `currency` has always been stored, and every total — summarize(), the
        portal cards, the charts — adds `amount` without ever consulting it. One
        non-CNY row would therefore make every monetary figure in the app
        silently wrong.

        This guard is not decorative. The portal UI has no currency field and
        the MCP exposes no parameter, but `/api/submit` and `/api/update` both
        read `currency` straight out of the request body (app/api.py), so any
        link holder could reach it. Carrying an exchange rate would be a real
        feature this household has no use for; refusing is the honest option.
        """
        text = (str(value).strip().upper() if value else "") or cls.CURRENCY
        if text != cls.CURRENCY:
            raise ValidationError(
                f"currency {text!r} not supported — this ledger is "
                f"{cls.CURRENCY} only. Every total adds amounts without "
                "converting them, so one foreign row would make all of them "
                f"wrong. Convert the amount to {cls.CURRENCY} first."
            )
        return text

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_expense(row: dict[str, Any]) -> Expense:
        return Expense(
            id=row["id"],
            date=row["date"],
            amount=float(row["amount"]),
            currency=row["currency"],
            category=row["category"],
            description=row["description"],
            paid=bool(row["paid"]),
            paid_date=row["paid_date"],
            submitted_by=row["submitted_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _fetch(self, tx, expense_id: str) -> Optional[dict[str, Any]]:
        return tx.query_one(
            f"SELECT {_EXPENSE_COLS} FROM expenses WHERE id = :id", {"id": expense_id}
        )

    def _write_history(
        self, tx, expense_id: str, action: str,
        changed_by: Optional[str], snapshot: dict[str, Any],
    ) -> None:
        tx.execute(
            "INSERT INTO expense_history (id, expense_id, seq, action, changed_by, changed_at, snapshot) "
            "VALUES (:id, :expense_id, "
            "(SELECT COUNT(*) FROM expense_history WHERE expense_id = :expense_id), "
            ":action, :changed_by, :changed_at, :snapshot)",
            {
                "id": generate_id(),
                "expense_id": expense_id,
                "action": action,
                "changed_by": changed_by,
                "changed_at": _utc_now_iso(),
                "snapshot": json.dumps(snapshot, ensure_ascii=False),
            },
        )

    # ── mutations (each = one atomic transaction incl. history) ──────────
    def create(
        self, *, date: str, amount: Any, currency: str = "CNY",
        category: Optional[str] = None, description: Optional[str] = None,
        submitted_by: Optional[str] = None,
        paid: bool = False, paid_date: Optional[str] = None,
    ) -> Expense:
        """Insert one expense, optionally already paid.

        ``paid`` exists so "昨天交了300的足球课" is a single transaction. The MCP
        used to create the row and then mark it paid in a second transaction: if
        the second failed, the row persisted as unpaid while the tool reported
        failure — the same-transaction history guarantee broken at the tool
        boundary. One row in, one ``create`` history entry describing it.
        """
        date = self._validate_date(date)
        amount = self._validate_amount(amount)
        currency = self._validate_currency(currency)
        paid = bool(paid)
        # An already-paid expense with no payment date was paid today: someone
        # is telling us about it now. Defaulting to the DUE date instead reads
        # better until you enter a bill dated next December and it records a
        # payment four months in the future, landing in the wrong month's total.
        # One rule, defined here — callers must not layer a second one on top.
        paid_date = self._validate_date(paid_date or today_str(), field="paid_date") if paid else None
        now = _utc_now_iso()
        expense = Expense(
            id=generate_id(), date=date, amount=amount, currency=currency,
            category=category, description=description, paid=paid,
            paid_date=paid_date, submitted_by=submitted_by,
            created_at=now, updated_at=now,
        )
        with self.db.tx() as tx:
            tx.execute(
                "INSERT INTO expenses (id, date, amount, currency, category, description, "
                "paid, paid_date, submitted_by, created_at, updated_at) "
                "VALUES (:id, :date, :amount, :currency, :category, :description, "
                ":paid, :paid_date, :submitted_by, :created_at, :updated_at)",
                expense.to_dict(),
            )
            self._write_history(tx, expense.id, "create", submitted_by, expense.to_dict())
        return expense

    def update(
        self, expense_id: str, *, fields: dict[str, Any],
        changed_by: Optional[str] = None,
    ) -> Expense:
        if not isinstance(fields, dict) or not fields:
            raise ValidationError("no fields to update")
        unknown = set(fields) - _ALLOWED_UPDATE_FIELDS
        if unknown:
            hint = (
                " — to mark paid/unpaid use mark_paid (expenses_mark_paid), not update"
                if {"paid", "paid_date"} & unknown else ""
            )
            raise ValidationError(f"unknown update fields: {sorted(unknown)}{hint}")
        clean: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "amount":
                clean[key] = self._validate_amount(value)
            elif key == "date":
                clean[key] = self._validate_date(value)
            elif key == "currency":
                clean[key] = self._validate_currency(value)
            else:
                clean[key] = value
        clean["updated_at"] = _utc_now_iso()
        # keys validated against the frozenset above → safe to interpolate
        set_clause = ", ".join(f"{k} = :{k}" for k in clean)
        with self.db.tx() as tx:
            cur = tx.execute(
                f"UPDATE expenses SET {set_clause} WHERE id = :expense_id",
                dict(clean, expense_id=expense_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(expense_id)
            expense = self._row_to_expense(self._fetch(tx, expense_id))
            self._write_history(tx, expense_id, "update", changed_by, expense.to_dict())
        return expense

    def mark_paid(
        self, expense_id: str, *, paid: bool,
        paid_date: Optional[str] = None, changed_by: Optional[str] = None,
    ) -> Expense:
        paid = bool(paid)
        paid_date = self._validate_date(paid_date, field="paid_date") if paid else None
        with self.db.tx() as tx:
            cur = tx.execute(
                "UPDATE expenses SET paid = :paid, paid_date = :paid_date, "
                "updated_at = :updated_at WHERE id = :expense_id",
                {
                    "paid": paid, "paid_date": paid_date,
                    "updated_at": _utc_now_iso(), "expense_id": expense_id,
                },
            )
            if cur.rowcount == 0:
                raise NotFoundError(expense_id)
            expense = self._row_to_expense(self._fetch(tx, expense_id))
            action = "mark_paid" if paid else "unmark_paid"
            self._write_history(tx, expense_id, action, changed_by, expense.to_dict())
        return expense

    def delete(self, expense_id: str, *, changed_by: Optional[str] = None) -> bool:
        with self.db.tx() as tx:
            row = self._fetch(tx, expense_id)
            if row is None:
                return False
            # A class package derives its money from this row. Deleting it
            # would leave the package with no amount, so refuse and say what to
            # do — cascading would silently destroy an attendance log that took
            # a term to accumulate.
            package = tx.query_one(
                "SELECT id, name FROM class_packages WHERE expense_id = :id",
                {"id": expense_id},
            )
            if package is not None:
                raise ValidationError(
                    f"this payment is tracked by the class package "
                    f"{package['name']!r}, which holds its attendance log. "
                    "Remove that course first — from the Classes tab in the "
                    "portal — and then delete the payment."
                )
            # history row survives the delete (pre-change snapshot)
            self._write_history(
                tx, expense_id, "delete", changed_by,
                self._row_to_expense(row).to_dict(),
            )
            tx.execute("DELETE FROM expenses WHERE id = :id", {"id": expense_id})
        return True

    # ── reads ─────────────────────────────────────────────────────────────
    def list(
        self, *, status: str = "all",
        since: Optional[str] = None, until: Optional[str] = None,
        today: Optional[str] = None,
    ) -> list[Expense]:
        # `today` is threaded in so the caller's ONE clock reading decides both
        # which rows are overdue and what date the response is labelled with.
        # Read separately, a request straddling midnight dropped a newly-overdue
        # row from the rows AND from their summarize() figures while reporting
        # the other day.
        clauses, params = ["1 = 1"], {}
        self._status_clause(status, clauses, params, today=today)
        if since:
            clauses.append("date >= :since")
            params["since"] = self._validate_date(since, field="since")
        if until:
            clauses.append("date <= :until")
            params["until"] = self._validate_date(until, field="until")
        with self.db.tx() as tx:
            rows = tx.query(
                f"SELECT {_EXPENSE_COLS} FROM expenses WHERE {' AND '.join(clauses)} "
                "ORDER BY date DESC, created_at DESC",
                params,
            )
        return [self._row_to_expense(row) for row in rows]

    @staticmethod
    def _status_clause(
        status: Optional[str], clauses: list, params: dict,
        today: Optional[str] = None,
    ) -> None:
        """Shared by list() and find(). They previously each implemented this,
        and drifted: find() silently treated 'overdue' — and any typo — as
        'all', so a query search could return paid and future rows."""
        if status == "paid":
            clauses.append("paid = :paid")
            params["paid"] = True
        elif status == "unpaid":
            clauses.append("paid = :paid")
            params["paid"] = False
        elif status == "overdue":
            clauses.append("paid = :paid")
            clauses.append("date < :today")
            params["paid"] = False
            params["today"] = today or today_str()
        elif status not in ("all", None, ""):
            raise ValidationError(
                f"invalid status filter: {status!r} — use all, paid, unpaid, "
                "or overdue (unpaid and past its due date)"
            )

    def find(
        self, query: str, *, status: str = "all", today: Optional[str] = None,
    ) -> list["Expense"]:
        """Case-insensitive substring match on description/category.

        Powers natural-language targeting from the MCP ("the football class")
        so callers don't need ids.
        """
        # escape the caller's own %/_/\ so they match literally: query='%'
        # is someone looking for a percent sign, not for the whole ledger
        needle = "%" + _LIKE_SPECIALS_RE.sub(r"\\\1", str(query or "").strip().lower()) + "%"
        clauses = [
            r"(LOWER(COALESCE(description,'')) LIKE :q ESCAPE '\' "
            r"OR LOWER(COALESCE(category,'')) LIKE :q ESCAPE '\')"
        ]
        params: dict[str, Any] = {"q": needle}
        # same reason as list(): the caller's ONE clock reading has to decide
        # which rows are overdue and what date the answer is labelled with
        self._status_clause(status, clauses, params, today=today)
        with self.db.tx() as tx:
            rows = tx.query(
                f"SELECT {_EXPENSE_COLS} FROM expenses WHERE {' AND '.join(clauses)} "
                "ORDER BY date DESC, created_at DESC",
                params,
            )
        return [self._row_to_expense(row) for row in rows]

    UPCOMING_WINDOW_DAYS = 30

    @classmethod
    def summarize(
        cls, expenses: list[Expense], *, today: Optional[str] = None
    ) -> dict[str, Any]:
        """Totals for exactly the rows handed in — one code path, no second query.

        This exists because the aggregate MUST agree with the row set beside it.
        Computing the summary with its own SQL let a filtered list be rendered
        under a whole-ledger headline: ask "what's owed this month" and get two
        rows worth ¥5,780 beneath a ¥247,780 total. Deriving both from the same
        list makes that disagreement unrepresentable.

        Buckets (see also BORROW_CATEGORY):
          total     household spending, paid or not — EXCLUDES borrow, so
                    `total == paid + unpaid` always. `count` is a row count and
                    does include borrow rows; a count is not a total
          due_now   unpaid expense, due on or before today (includes overdue)
          upcoming  unpaid expense, due within the next UPCOMING_WINDOW_DAYS —
                    a window, not "everything future": recurring costs are
                    entered a year ahead, and a card summing all of them answers
                    a question nobody asked
          borrow_*  she fronted it; owed back to her, never household spending
        """
        today = today or today_str()
        horizon = (
            datetime.strptime(today, "%Y-%m-%d") + timedelta(days=cls.UPCOMING_WINDOW_DAYS)
        ).strftime("%Y-%m-%d")

        # collected per bucket, then fsum'd: plain += is order-dependent in
        # binary floating point, so a large or extreme ledger could land a cent
        # away from the SQL aggregate this replaced.
        buckets: dict[str, list] = {
            k: [] for k in ("total", "paid", "unpaid", "due_now", "upcoming",
                            "borrow_owed", "borrow_repaid")
        }
        counts = {k: 0 for k in ("unpaid_count", "due_now_count",
                                 "upcoming_count", "borrow_owed_count")}
        for e in expenses:
            amount = float(e.amount or 0)
            if (e.category or "") == BORROW_CATEGORY:
                if e.paid:
                    buckets["borrow_repaid"].append(amount)
                else:
                    buckets["borrow_owed"].append(amount)
                    counts["borrow_owed_count"] += 1
                continue
            # after the borrow guard, never before it: `total` is a HOUSEHOLD
            # total like every other figure here (P4), and counting the loan
            # made it gross ledger flow under a name that reads as spending.
            # An agent asked "这个月花了多少" is pointed at .summary, so the
            # live ¥347,559 answer carried ¥31,100 that was never spending.
            # total == paid + unpaid now holds by construction.
            buckets["total"].append(amount)
            if e.paid:
                buckets["paid"].append(amount)
                continue
            buckets["unpaid"].append(amount)
            counts["unpaid_count"] += 1
            if e.date <= today:
                buckets["due_now"].append(amount)
                counts["due_now_count"] += 1
            elif e.date <= horizon:
                buckets["upcoming"].append(amount)
                counts["upcoming_count"] += 1
        out: dict[str, Any] = {"count": len(expenses)}
        out.update({k: round(fsum(v), 2) for k, v in buckets.items()})
        out.update(counts)
        return out

    def summary(self, *, today: Optional[str] = None) -> dict[str, Any]:
        """Whole-ledger totals. Callers showing a FILTERED list must use
        summarize() on those rows instead, or the total contradicts the list."""
        return self.summarize(self.list(status="all"), today=today)

    def history(self, expense_id: str) -> list[HistoryEntry]:
        with self.db.tx() as tx:
            rows = tx.query(
                "SELECT id, expense_id, seq, action, changed_by, changed_at, snapshot "
                "FROM expense_history WHERE expense_id = :id "
                "ORDER BY seq ASC",
                {"id": expense_id},
            )
        return [
            HistoryEntry(
                id=r["id"], expense_id=r["expense_id"], seq=r["seq"], action=r["action"],
                changed_by=r["changed_by"], changed_at=r["changed_at"],
                snapshot=json.loads(r["snapshot"]),
            )
            for r in rows
        ]

    # ── class tracker ─────────────────────────────────────────────────────
    @staticmethod
    def _validate_class_count(value: Any) -> int:
        # bool is an int subclass and 1.9 truncates to 1 — both would silently
        # become a class count nobody typed, and the count divides the money.
        # isfinite() comes first because int(nan) raises a bare ValueError that
        # the API layer does not catch, turning a bad field into a 500.
        if isinstance(value, bool):
            raise ValidationError(
                f"class_count {value!r} must be a whole number of classes, e.g. 10"
            )
        if isinstance(value, float) and (
            not isfinite(value) or value != int(value)
        ):
            raise ValidationError(
                f"class_count {value!r} must be a whole number of classes, e.g. 10"
            )
        try:
            count = int(value)
        except (TypeError, ValueError):
            raise ValidationError(
                f"class_count {value!r} not understood — pass a whole number of "
                "classes, e.g. 10"
            )
        if count < 1:
            raise ValidationError(f"class_count must be at least 1, got {count}")
        if count > 1000:
            # no course has a thousand classes, and an unbounded value reaches
            # the driver as an OverflowError, which the API layer does not
            # translate — the same 500-instead-of-400 the NaN guard closed
            raise ValidationError(
                f"class_count {count} is not a plausible number of classes "
                "(maximum 1000)"
            )
        return count

    @staticmethod
    def _validate_class_kind(value: Any) -> str:
        text = str(value or "").strip()
        if text not in CLASS_KINDS:
            raise ValidationError(
                f"kind {value!r} invalid — use 'per_class' for a pack of N "
                "classes you draw down, or 'period' for a flat month/semester "
                "fee where missed classes are owed back"
            )
        return text

    @staticmethod
    def _validate_event_kind(value: Any) -> str:
        text = str(value or "").strip()
        if text not in CLASS_EVENT_KINDS:
            raise ValidationError(
                f"kind {value!r} invalid — use 'attended' (the class happened), "
                "'missed_school' (they cancelled, so it is reclaimable) or "
                "'missed_us' (we skipped it)"
            )
        return text

    @classmethod
    def summarize_package(
        cls, package: dict[str, Any], amount: Any, events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The ONE place class-package arithmetic happens.

        Money comes in as the linked expense's ``amount`` — the package stores
        none of its own, so the tracker cannot drift from the ledger. Amounts
        are computed from the exact ratio (``amount * n / count``) rather than
        from a rounded per-class rate, so the parts still add up to the whole;
        ``rate`` is a display figure only.
        """
        count = int(package["class_count"])
        amount = float(amount or 0)
        tally = {k: 0 for k in CLASS_EVENT_KINDS}
        for event in events:
            if event["kind"] in tally:
                tally[event["kind"]] += 1
        missed = sum(tally[k] for k in _MISSED_EVENT_KINDS)

        # THE RULE, and it is not symmetric: a part is always derived from the
        # total, never the total from its parts. Summing two independently
        # rounded halves can round UP twice — which let a period package report
        # owing back ¥0.01 MORE than was ever paid, in ~0.8% of ordinary
        # splits. value(count) == total, so anything valued at count-or-fewer
        # classes is capped by construction.
        total = round(amount, 2)

        def value(n: int) -> float:
            """The exact ratio for n classes — except at the top, where it IS
            the total.

            `round(amount * count / count, 2)` is not reliably `round(amount, 2)`:
            an amount sitting on a half-cent (a third decimal of 5) crosses the
            tie differently on the multiply/divide round trip, and the whole
            package then reports a cent more than was paid. Returning `total`
            at the boundary makes the cap true by construction instead of by
            assertion — the previous version claimed exactly this in a comment
            and did not do it.
            """
            if not count:
                return 0.0
            return total if n >= count else round(amount * n / count, 2)

        out: dict[str, Any] = {
            "class_count": count,
            "amount": total,
            "rate": round(amount / count, 2) if count else 0.0,
            "attended": tally["attended"],
            "missed_school": tally["missed_school"],
            "missed_us": tally["missed_us"],
            "missed": missed,
            "logged": tally["attended"] + missed,
        }
        if package["kind"] == "per_class":
            attended = tally["attended"]
            # Money is capped at what was paid and the leftover is DERIVED by
            # subtraction, never rounded independently: two independent
            # round()s on a payment that does not divide evenly invent or lose
            # a cent, and these two numbers sit side by side on the page.
            used_amount = value(min(attended, count))
            out.update({
                "used": attended,
                "used_amount": used_amount,
                "remaining": max(0, count - attended),
                # max(0, …) because _validate_amount does not round, so an
                # amount carrying a third decimal could otherwise land on -0.0
                "remaining_amount": max(0.0, round(total - used_amount, 2)),
                # attending more classes than were bought is a real thing that
                # happens; report it rather than clamping it out of sight
                "overrun": max(0, attended - count),
            })
        else:
            # Only classes the payment actually covers carry money. Logging
            # more misses than were paid for (a wrong class_count, or a bad
            # month) must not claim back more than was handed over — "they owe
            # us more than we paid them" is a wrong total in the most visible
            # place, and the count still records what really happened.
            school = min(tally["missed_school"], count)
            # the school's share is counted first: if the log overruns, the
            # excess lands on what we forfeited rather than inflating the
            # figure we would put in front of the school
            ours = min(tally["missed_us"], count - school)
            # The same rule as above, applied to a three-way split: value the
            # school's share, then take OURS as the remainder of the pair's
            # exact value. Both reconcile with the total AND the total stays
            # the exact ratio — summing two rounded halves did neither.
            owed_amount = value(school + ours)
            reclaimable_amount = value(school)
            forfeited_amount = round(owed_amount - reclaimable_amount, 2)
            out.update({
                "owed": missed,
                "owed_amount": owed_amount,
                "reclaimable": tally["missed_school"],
                "reclaimable_amount": reclaimable_amount,
                "forfeited": tally["missed_us"],
                "forfeited_amount": forfeited_amount,
                "overrun": max(0, missed - count),
            })
        return out

    _PACKAGE_SELECT = (
        "SELECT p.id, p.expense_id, p.name, p.kind, p.class_count, "
        "p.period_label, p.archived, p.created_at, p.updated_at, "
        "e.amount AS expense_amount, e.date AS expense_date, "
        "e.description AS expense_description, e.category AS expense_category, "
        "e.paid AS expense_paid "
        "FROM class_packages p JOIN expenses e ON e.id = p.expense_id"
    )

    def create_package(
        self, *, expense_id: str, name: str, kind: str, class_count: Any,
        period_label: Optional[str] = None,
    ) -> dict[str, Any]:
        """Track an expense that paid for a course.

        The payment must already exist in the ledger — that row is where the
        money lives, and this only records what it bought.
        """
        kind = self._validate_class_kind(kind)
        class_count = self._validate_class_count(class_count)
        name = (str(name).strip() if name else "")
        if not name:
            raise ValidationError(
                "name is required — what the course is, e.g. '足球课' or 'Football'"
            )
        now = _utc_now_iso()
        package_id = generate_id()
        try:
            self._insert_package(
                package_id, str(expense_id), name, kind, class_count,
                period_label, now,
            )
        except _ConstraintLost:
            # fresh transaction: say which constraint actually fired, rather
            # than assuming "duplicate" and sending someone to look for a
            # package that does not exist
            with self.db.tx() as tx:
                duplicate = self._duplicate_package(tx, str(expense_id))
                still_there = self._fetch(tx, str(expense_id))
            if duplicate is not None:
                raise ValidationError(
                    f"that payment is already tracked by the class package "
                    f"{duplicate['name']!r} — one payment, one package, or the "
                    "same money would be counted twice"
                ) from None
            if still_there is None:
                raise NotFoundError(expense_id) from None
            raise ValidationError(
                "could not track that payment — the ledger changed while this "
                "was being saved. Call classes_list and try again."
            ) from None
        return self.package(package_id)

    def _insert_package(
        self, package_id, expense_id, name, kind, class_count, period_label, now,
    ) -> None:
        with self.db.tx() as tx:
            expense = self._fetch(tx, str(expense_id))
            if expense is None:
                raise NotFoundError(expense_id)
            existing = self._duplicate_package(tx, str(expense_id))
            if existing is not None:
                raise ValidationError(
                    f"that payment is already tracked by the class package "
                    f"{existing['name']!r} — one payment, one package, or the "
                    "same money would be counted twice"
                )
            try:
                tx.execute(
                    "INSERT INTO class_packages (id, expense_id, name, kind, "
                    "class_count, period_label, archived, created_at, updated_at) "
                    "VALUES (:id, :expense_id, :name, :kind, :class_count, "
                    ":period_label, :archived, :created_at, :updated_at)",
                    {
                        "id": package_id, "expense_id": str(expense_id), "name": name,
                        "kind": kind, "class_count": class_count,
                        "period_label": (str(period_label).strip() or None)
                        if period_label else None,
                        "archived": False, "created_at": now, "updated_at": now,
                    },
                )
            except Exception as exc:
                # The check above is not atomic with this insert, and since
                # v0.9.0 the API handlers run in a threadpool — so two taps on
                # "Add a course" can both pass it and a constraint becomes the
                # thing that fires. Doing its job must not reach the caller as
                # a 500 with a driver traceback.
                if not _is_integrity_error(exc):
                    raise
                raise _ConstraintLost from exc

    def package(self, package_id: str) -> dict[str, Any]:
        with self.db.tx() as tx:
            row = tx.query_one(
                f"{self._PACKAGE_SELECT} WHERE p.id = :id", {"id": str(package_id)}
            )
            if row is None:
                raise PackageNotFoundError(package_id)
            events = tx.query(
                "SELECT id, package_id, date, kind, note, logged_by, created_at "
                "FROM class_events WHERE package_id = :id ORDER BY date DESC, created_at DESC",
                {"id": str(package_id)},
            )
        return self._package_payload(row, events)

    @classmethod
    def _package_payload(
        cls, row: dict[str, Any], events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload = {
            "id": row["id"], "expense_id": row["expense_id"], "name": row["name"],
            "kind": row["kind"], "class_count": int(row["class_count"]),
            "period_label": row["period_label"], "archived": bool(row["archived"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "expense": {
                "id": row["expense_id"], "amount": float(row["expense_amount"]),
                "date": row["expense_date"], "description": row["expense_description"],
                "category": row["expense_category"], "paid": bool(row["expense_paid"]),
            },
            "events": [dict(e) for e in events],
        }
        payload["summary"] = cls.summarize_package(
            payload, row["expense_amount"], events
        )
        return payload

    def list_packages(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        clause = "" if include_archived else " WHERE p.archived = :archived"
        params = {} if include_archived else {"archived": False}
        with self.db.tx() as tx:
            rows = tx.query(
                f"{self._PACKAGE_SELECT}{clause} ORDER BY e.date DESC, p.created_at DESC",
                params,
            )
            # one query for every package's events rather than one per package
            events = tx.query(
                "SELECT id, package_id, date, kind, note, logged_by, created_at "
                "FROM class_events ORDER BY date DESC, created_at DESC"
            )
        by_package: dict[str, list] = {}
        for event in events:
            by_package.setdefault(event["package_id"], []).append(event)
        return [
            self._package_payload(row, by_package.get(row["id"], [])) for row in rows
        ]

    @staticmethod
    def _duplicate_package(tx, expense_id: str) -> Optional[dict[str, Any]]:
        """The friendly half of the one-package-per-payment rule.

        Its own method so a test can blind it and prove the UNIQUE index — the
        half that actually holds under concurrency — still reports a reason.
        """
        return tx.query_one(
            "SELECT id, name FROM class_packages WHERE expense_id = :eid",
            {"eid": expense_id},
        )

    def linked_expense_ids(self) -> set[str]:
        """Every expense already funding a package — ids only.

        Archived packages count: their payment is still spoken for, and
        offering it again would only produce a UNIQUE violation.
        """
        with self.db.tx() as tx:
            return {
                r["expense_id"]
                for r in tx.query("SELECT expense_id FROM class_packages")
            }

    def update_package(
        self, package_id: str, *, fields: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(fields, dict) or not fields:
            raise ValidationError("no fields to update")
        allowed = {"name", "kind", "class_count", "period_label", "archived"}
        unknown = set(fields) - allowed
        if unknown:
            hint = (
                " — the amount lives on the linked expense; edit that instead"
                if {"amount", "rate", "expense_id"} & unknown else ""
            )
            raise ValidationError(f"unknown update fields: {sorted(unknown)}{hint}")
        clean: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "class_count":
                clean[key] = self._validate_class_count(value)
            elif key == "kind":
                clean[key] = self._validate_class_kind(value)
            elif key == "archived":
                clean[key] = bool(value)
            elif key == "name":
                text = (str(value).strip() if value else "")
                if not text:
                    raise ValidationError("name cannot be empty")
                clean[key] = text
            else:
                clean[key] = (str(value).strip() or None) if value else None
        clean["updated_at"] = _utc_now_iso()
        set_clause = ", ".join(f"{k} = :{k}" for k in clean)  # keys checked above
        with self.db.tx() as tx:
            if "kind" in clean:
                current = tx.query_one(
                    "SELECT kind FROM class_packages WHERE id = :id",
                    {"id": str(package_id)},
                )
                logged = tx.query_one(
                    "SELECT COUNT(*) AS n FROM class_events WHERE package_id = :id",
                    {"id": str(package_id)},
                )
                if (current and current["kind"] != clean["kind"]
                        and logged and logged["n"]):
                    # Flipping the kind silently reinterprets every class
                    # already logged: attendances stop drawing anything down
                    # and misses turn into money owed, or the reverse. The log
                    # is the record; it does not get retconned.
                    raise ValidationError(
                        f"this course already has {logged['n']} class(es) logged, "
                        "so its type cannot be changed — the log would mean "
                        "something different. Delete it and add it again if the "
                        "type was wrong."
                    )
            cur = tx.execute(
                f"UPDATE class_packages SET {set_clause} WHERE id = :package_id",
                dict(clean, package_id=str(package_id)),
            )
            if cur.rowcount == 0:
                raise PackageNotFoundError(package_id)
        return self.package(package_id)

    def delete_package(self, package_id: str) -> bool:
        """Remove a package and its attendance log."""
        with self.db.tx() as tx:
            cur = tx.execute(
                "DELETE FROM class_packages WHERE id = :id", {"id": str(package_id)}
            )
            if cur.rowcount == 0:
                return False
            tx.execute(
                "DELETE FROM class_events WHERE package_id = :id",
                {"id": str(package_id)},
            )
        return True

    def log_class(
        self, *, package_id: str, kind: str, date: Optional[str] = None,
        note: Optional[str] = None, logged_by: Optional[str] = None,
    ) -> dict[str, Any]:
        kind = self._validate_event_kind(kind)
        date = self._validate_date(date or today_str())
        with self.db.tx() as tx:
            exists = tx.query_one(
                "SELECT id FROM class_packages WHERE id = :id", {"id": str(package_id)}
            )
            if exists is None:
                raise PackageNotFoundError(package_id)
            tx.execute(
                "INSERT INTO class_events (id, package_id, date, kind, note, "
                "logged_by, created_at) VALUES (:id, :package_id, :date, :kind, "
                ":note, :logged_by, :created_at)",
                {
                    "id": generate_id(), "package_id": str(package_id), "date": date,
                    "kind": kind, "note": note, "logged_by": logged_by,
                    "created_at": _utc_now_iso(),
                },
            )
        return self.package(package_id)

    def delete_class_event(self, event_id: str) -> bool:
        with self.db.tx() as tx:
            cur = tx.execute(
                "DELETE FROM class_events WHERE id = :id", {"id": str(event_id)}
            )
            return cur.rowcount > 0

    # ── access tokens (operator-only minting — finding M2) ───────────────
    def mint_token(
        self, *, label: Optional[str] = None, expires_days: Optional[int] = None
    ) -> dict[str, Any]:
        """Mint a link token. Default: NEVER expires (household links must not
        demand credential renewal from non-technical holders; revocation is
        the kill switch). Pass expires_days for a bounded token."""
        expires_at = None
        if expires_days is not None:
            try:
                expires_days = max(1, min(3650, int(expires_days)))
            except (TypeError, ValueError):
                expires_days = None
            if expires_days is not None:
                expires_at = (
                    datetime.now(timezone.utc) + timedelta(days=expires_days)
                ).strftime("%Y-%m-%dT%H:%M:%S")
        token = secrets.token_hex(32)
        with self.db.tx() as tx:
            tx.execute(
                "INSERT INTO access_tokens (id, token, label, expires_at, revoked, created_at, use_count) "
                "VALUES (:id, :token, :label, :expires_at, :revoked, :created_at, 0)",
                {
                    "id": generate_id(), "token": token, "label": label,
                    "expires_at": expires_at, "revoked": False,
                    "created_at": _utc_now_iso(),
                },
            )
        return {"token": token, "label": label, "expires_at": expires_at}

    def validate_token(self, token: Any) -> Optional[dict[str, Any]]:
        """Return the token row when valid; None when unknown/revoked/expired.

        Fail-closed on every path; bumps last_used_at/use_count on success.
        """
        if not token or not isinstance(token, str):
            return None
        with self.db.tx() as tx:
            row = tx.query_one(
                "SELECT id, token, label, expires_at, revoked, created_at, use_count "
                "FROM access_tokens WHERE token = :token",
                {"token": token},
            )
            if row is None or bool(row["revoked"]):
                return None
            expires_at = row["expires_at"]
            if expires_at and str(expires_at) <= _utc_now_iso():
                return None  # only bounded tokens can expire; NULL = never
            tx.execute(
                "UPDATE access_tokens SET last_used_at = :now, use_count = use_count + 1 "
                "WHERE id = :id",
                {"now": _utc_now_iso(), "id": row["id"]},
            )
        return row

    def revoke_token(self, token_or_id: str) -> bool:
        with self.db.tx() as tx:
            cur = tx.execute(
                "UPDATE access_tokens SET revoked = :revoked "
                "WHERE token = :value OR id = :value",
                {"revoked": True, "value": token_or_id},
            )
            return cur.rowcount > 0

    def list_tokens(self) -> list[dict[str, Any]]:
        with self.db.tx() as tx:
            return tx.query(
                "SELECT id, label, expires_at, revoked, created_at, last_used_at, use_count "
                "FROM access_tokens ORDER BY created_at DESC"
            )
