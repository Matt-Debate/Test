"""Framework-free API handlers.

Each handler takes ``(store, body)`` and returns ``(status, payload)`` —
testable without any web framework; ``app/web.py`` wires them to routes.

Every handler revalidates the access token (fail-closed, contract A5).
Error mapping: invalid token → 401, validation → 400, missing id → 404.
"""

from __future__ import annotations

from typing import Any

from .store import (
    NotFoundError, Store, ValidationError, is_class_category, today_and_midnight,
)


_LINK_LABEL = "_link_label"  # set by _guard from the validated token, never the client


def _authenticate(store: Store, body: dict) -> bool:
    """Validate the link and stamp its label onto the request.

    Portal writes carry no author — the form deliberately does not ask, since
    only one person types into it. But once more than one link is live, "an
    expense exists" and "who logged it" are different facts, and the audit trail
    needs the second. The label is taken from the validated token row and
    overwrites anything the client sent, so it cannot be spoofed.
    """
    link = store.validate_token(body.get("token"))
    body[_LINK_LABEL] = (link or {}).get("label")
    return link is not None


def _author(body: dict, explicit_key: str) -> Any:
    """The validated link's label, authoritatively.

    A client-supplied author is ignored outright. The portal form does not ask
    for a name, so anything arriving in the body is either noise or a spoof —
    a request bearing the "wife" link with submitted_by="Mallory" must not be
    able to write "Mallory" into the audit trail. MCP callers do not pass
    through here; they name the speaker via the store directly.
    """
    del explicit_key  # kept for call-site readability; deliberately unused
    return body.get(_LINK_LABEL)


def _err(status: int, message: str) -> tuple[int, dict]:
    return status, {"ok": False, "error": message}


def _guard(handler):
    """Auth + error-translation wrapper shared by all handlers."""

    def wrapped(store: Store, body: Any) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return _err(400, "invalid request body")
        if not _authenticate(store, body):
            return _err(401, "invalid or expired link")
        try:
            return handler(store, body)
        except ValidationError as exc:
            return _err(400, str(exc))
        except KeyError:
            return _err(404, "expense not found")

    return wrapped


@_guard
def api_list(store: Store, body: dict) -> tuple[int, dict]:
    # read the clock ONCE, BEFORE the query: computing it separately for the
    # rows, the summary and the response let a request straddling midnight
    # bucket its rows against one day and label them with the next. The
    # overdue filter inside store.list() reads it too, which is why `today` is
    # passed down rather than left to default. The remaining-seconds figure
    # comes from the same reading — yesterday's date beside a whole day left
    # tells the page to hold yesterday for another day.
    today, midnight_in = today_and_midnight()
    expenses = store.list(
        status=body.get("status") or "all",
        since=body.get("since"),
        until=body.get("until"),
        today=today,
    )
    # which course a payment funds, so a row can say so and the refund box
    # can offer to resize it in the same call. One cheap query, merged here
    # rather than carried on the Expense: the MCP answers that question with
    # classes_list, and a snapshot need not repeat the course on every edit
    packages = store.packages_by_expense()
    rows = []
    for e in expenses:
        row = e.to_dict()
        row["package"] = packages.get(e.id)
        rows.append(row)
    return 200, {
        "ok": True,
        "expenses": rows,
        # summarize the rows we are actually returning: a filtered list under a
        # whole-ledger headline is a wrong number in the most visible place
        "summary": store.summarize(expenses, today=today),
        # the household's today (APP_TZ), so the page stops asking the phone.
        # The server already decides overdue-ness this way; when the two
        # disagreed — a travelling phone, a wrong clock — a row moved between
        # Due and Upcoming depending on who you asked.
        "today": today,
        # …and how much of it is left, so the page can roll the date over AT
        # midnight rather than 24h after this response. A date alone says
        # nothing about how much of the day remains.
        "midnight_in": midnight_in,
    }


@_guard
def api_submit(store: Store, body: dict) -> tuple[int, dict]:
    expense = store.create(
        date=body.get("date"),
        amount=body.get("amount"),
        currency=body.get("currency") or "CNY",
        category=body.get("category"),
        description=body.get("description"),
        submitted_by=_author(body, "submitted_by"),
    )
    return 200, {"ok": True, "expense": expense.to_dict()}


@_guard
def api_update(store: Store, body: dict) -> tuple[int, dict]:
    fields = body.get("fields")
    if not isinstance(fields, dict):
        raise ValidationError("fields object is required")
    expense = store.update(
        str(body.get("id")), fields=fields, changed_by=_author(body, "changed_by")
    )
    return 200, {"ok": True, "expense": expense.to_dict()}


@_guard
def api_mark_paid(store: Store, body: dict) -> tuple[int, dict]:
    expense = store.mark_paid(
        str(body.get("id")),
        paid=bool(body.get("paid")),
        paid_date=body.get("paid_date"),
        changed_by=_author(body, "changed_by"),
    )
    return 200, {"ok": True, "expense": expense.to_dict()}


@_guard
def api_delete(store: Store, body: dict) -> tuple[int, dict]:
    deleted = store.delete(str(body.get("id")), changed_by=_author(body, "changed_by"))
    if not deleted:
        raise NotFoundError(body.get("id"))
    return 200, {"ok": True}


@_guard
def api_history(store: Store, body: dict) -> tuple[int, dict]:
    entries = store.history(str(body.get("id")))
    return 200, {"ok": True, "history": [h.to_dict() for h in entries]}


@_guard
def api_refund(store: Store, body: dict) -> tuple[int, dict]:
    """Money that came back on a payment — recorded, never written down.

    ``resize_package_to`` resizes the funded course in the same transaction.
    The portal sends it only when the row funds a per-class pack and she
    changed the figure; an absent or blank value records the refund alone.
    """
    resize = body.get("resize_package_to")
    if resize in ("", None):
        resize = None
    result = store.refund(
        str(body.get("id")),
        amount=body.get("amount"),
        date=body.get("date"),
        reason=body.get("reason"),
        changed_by=_author(body, "changed_by"),
        resize_package_to=resize,
    )
    return 200, {
        "ok": True,
        "expense": result["expense"].to_dict(),
        "refund": result["refund"],
        # the funded course, resized or not, so the confirmation can quote
        # the rate the refund left it with
        "package": result["package"],
        "resized": result["resized"],
    }


@_guard
def api_refund_delete(store: Store, body: dict) -> tuple[int, dict]:
    result = store.delete_refund(
        str(body.get("refund_id")), changed_by=_author(body, "changed_by")
    )
    if result is None:
        raise NotFoundError(body.get("refund_id"))
    package = result["package"]
    return 200, {
        "ok": True,
        "expense": result["expense"].to_dict(),
        # what the undo did to the course — restored, or left and why — so
        # the confirmation can say so; the payload itself is not needed here
        "package": {k: v for k, v in package.items() if k != "payload"} if package else None,
    }


@_guard
def api_classes_list(store: Store, body: dict) -> tuple[int, dict]:
    packages = store.list_packages(
        include_archived=bool(body.get("include_archived"))
    )
    # The course payments not yet tracked, so the add form can offer them
    # instead of asking her to retype an amount the ledger already holds. Taken
    # from a cheap id-only query: calling list_packages again would re-run the
    # join, re-scan every class event and re-summarize, all to read one column.
    #
    # Narrowed to CLASS_CATEGORIES: unfiltered and date-ordered, twelve
    # future-dated living-expense rows sat on top of the four payments that
    # were actually courses. No date bound on top of that — a course payment
    # falls due in the future all the time, and that is the one most likely to
    # be tracked next.
    cls_today, cls_midnight_in = today_and_midnight()   # one reading, as above
    linked = store.linked_expense_ids()
    candidates = [
        {"id": e.id, "date": e.date, "amount": e.amount,
         "description": e.description, "category": e.category, "paid": e.paid}
        for e in store.list(status="all")
        if e.id not in linked and is_class_category(e.category)
    ]
    return 200, {
        "ok": True, "packages": packages, "candidates": candidates,
        "today": cls_today, "midnight_in": cls_midnight_in,
    }


@_guard
def api_classes_add(store: Store, body: dict) -> tuple[int, dict]:
    package = store.create_package(
        expense_id=str(body.get("expense_id")),
        name=body.get("name"),
        kind=body.get("kind"),
        class_count=body.get("class_count"),
        period_label=body.get("period_label"),
        changed_by=_author(body, "changed_by"),
    )
    return 200, {"ok": True, "package": package}


@_guard
def api_classes_log(store: Store, body: dict) -> tuple[int, dict]:
    package = store.log_class(
        package_id=str(body.get("package_id")),
        kind=body.get("kind"),
        date=body.get("date"),
        # the batch form too: dropped here, a body carrying `dates` logged ONE
        # class for today — a silently ignored parameter is a wrong write
        dates=body.get("dates"),
        note=body.get("note"),
        logged_by=_author(body, "logged_by"),
    )
    return 200, {"ok": True, "package": package}


@_guard
def api_classes_unlog(store: Store, body: dict) -> tuple[int, dict]:
    package = store.delete_class_event(
        str(body.get("event_id")), changed_by=_author(body, "changed_by")
    )
    if package is None:
        raise NotFoundError(body.get("event_id"))
    return 200, {"ok": True, "package": package}


@_guard
def api_classes_update(store: Store, body: dict) -> tuple[int, dict]:
    fields = body.get("fields")
    if not isinstance(fields, dict):
        raise ValidationError("fields object is required")
    package = store.update_package(
        str(body.get("id")), fields=fields, changed_by=_author(body, "changed_by")
    )
    return 200, {"ok": True, "package": package}


@_guard
def api_classes_delete(store: Store, body: dict) -> tuple[int, dict]:
    if not store.delete_package(
        str(body.get("id")), changed_by=_author(body, "changed_by")
    ):
        raise NotFoundError(body.get("id"))
    return 200, {"ok": True}


HANDLERS = {
    "list": api_list,
    "classes-list": api_classes_list,
    "classes-add": api_classes_add,
    "classes-log": api_classes_log,
    "classes-unlog": api_classes_unlog,
    "classes-update": api_classes_update,
    "classes-delete": api_classes_delete,
    "submit": api_submit,
    "update": api_update,
    "mark-paid": api_mark_paid,
    "delete": api_delete,
    "history": api_history,
    "refund": api_refund,
    "refund-delete": api_refund_delete,
}
