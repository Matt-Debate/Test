"""Family MCP surface — engineered for what LLM agents ACTUALLY read.

Channel priority (see docs/MCP_DESIGN.md): agents reliably see (1) tool
names/descriptions/param schemas and (2) tool results & error strings.
Server ``instructions`` and resources are inconsistently surfaced across
clients, and prompts are user-invoked. Therefore:

  * trigger phrases (中文 + EN) live IN the tool descriptions — that is what
    drives correct tool selection;
  * every error string is coaching: it says what to call instead, so a wrong
    call self-corrects in one round trip;
  * results carry the running unpaid total so the agent can confirm naturally;
  * ``expenses_help`` returns the full playbook — works even on clients that
    never show instructions;
  * three personas ship as MCP prompts (记账 / 对账 / 修复) for clients that
    expose prompt templates.

Auth (owner's accepted threat model): links never expire; ``/mcp`` is open
unless ``MCP_SECRET`` is set.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Optional, Union

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import portal_base_url, portal_link
from .store import (
    BORROW_CATEGORY, CATEGORY_KEYS, Store, ValidationError, _utc_now_iso,
    today_str,
)

_READ = ToolAnnotations(readOnlyHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

_HELP = """\
家庭开支 Family Expenses — playbook for assistants.

WHAT THIS IS: one household's simple ledger of expenses that still need to be
paid (待付) or were paid (已付). Amounts are CNY (¥). Users speak casually,
Chinese or English. Always reply in the user's language.

INTENT → TOOL:
- "我还要付什么 / what do I owe / 有什么没付" → expenses_list(status="unpaid")
- "足球课300块 / football 300" → expenses_add(amount="300", description="足球课")
- "昨天交了300的足球课(已经付了)" → expenses_add(..., paid=true, paid_date=...)
- "足球课付了 / paid the football class / 交了" → expenses_mark_paid(query="足球课")
- "足球课改成350 / actually it was 350" → expenses_update(query="足球课", amount="350")
- "删掉/不用了 delete the swim class" → expenses_delete(query="游泳课") — confirm first
- "这个月花了多少 / totals" → expenses_list() and read .summary
- "这条是谁改的 / what happened to X" → expenses_history
- "给我老婆做个链接" → expenses_mint_link(label="wife") — link never expires
- "哪些链接还在用 / who has a link / list the links" → expenses_list_links
- "足球还剩几节课 / how many classes left / 还有几次" → classes_list
- "足球课交了2200，10节课 / paid for 10 classes" → classes_add (the payment must
  be in the ledger first — expenses_add, then classes_add(query=…))
- "今天上了足球课 / went today" → classes_log(kind="attended")
- "今天的课取消了 / they cancelled" → classes_log(kind="missed_school")
- "今天没去 / we skipped" → classes_log(kind="missed_us")
- "8月17、21、28都上了 / log these three dates" → classes_log(kind=…, dates=[…])
  — one call, all or nothing
- "那天没上，记错了 / undo that class / 删掉那条上课记录" →
  classes_log_delete(event_id=…) — ids from classes_list(verbose=true) or the
  classes_log result
- "退了1800 / 退款 / they refunded us 1800 / got money back" →
  expenses_refund(query=…, amount="1800"). The payment KEEPS its original
  amount; every total reads amount − refunds. A refund on a course usually
  means fewer classes: pass resize_package_to=<new class_count> in the SAME
  call so the per-class rate stays honest (¥3600 for 10 refunded ¥1800 is
  5 classes at ¥360, not 10 at ¥180)
- "那个退款记错了 / undo the refund" → expenses_refund_delete(refund_id=…) —
  confirm first. A course resized with the refund goes back to its previous
  class count too (unless something changed it since — the note says)
- "课时改成5 / 改成5节 / the pack is 5 classes now / rename the course /
  改名" → classes_update(query=…, class_count=5)
- "这个课结束了 / 上完了 / archive the course / retire it" →
  classes_update(query=…, archived=true) — keeps the log, hides it from lists
- "删掉这个课程 / stop tracking the course / remove the course" →
  classes_delete(query=…) — confirm first; its class log stays in the
  payment's expenses_history

CLASS TRACKER — two shapes, and they answer different questions:
- kind="per_class": a pack of N classes. Attending draws one down. Answers
  "how many classes and how much money is LEFT".
- kind="period": a flat month/semester fee. Nothing is drawn down; the classes
  that did NOT happen are owed back. Answers "what do they owe us" — split into
  reclaimable (missed_school: they cancelled) and forfeited (missed_us: we
  skipped). Both count toward the total owed; the cause is what you argue with.
A package carries NO money of its own: the rate is the linked payment's amount
÷ class_count. To correct the price, edit the EXPENSE, not the package.

CATEGORIES — prefer these EXACT keys. Anything else is accepted and counted as
an ordinary household expense (and charted under whatever string you sent), so
an invented key does not vanish — it just is not one of the household's buckets:
  living · aden-edu · aden-sports · aden-clothes · aden-other · food · home ·
  utilities · internet · mobile · transport · travel · entertainment ·
  clothes · medical · borrow · other

- "borrow" is the ONE category with arithmetic behind it: it means she paid out
  of her own pocket (or the company's) and is owed the money BACK. Only the
  exact string "borrow" does this — it is kept out of every household expense
  total and reported on its own. A synonym like "loan repayment" or
  "reimbursement" is NOT recognised: it counts as ordinary household spending
  and inflates the paid/unpaid totals instead. Use it for
  "垫付/她先付的/borrowed from her/she fronted it/I lent".
- "living" is the recurring monthly household payment (生活费).

RULES OF THUMB:
- Dates/paid dates: omit them — the server defaults to today in China time.
- Amounts: pass what the user said — "¥300", "300块", "1,200元" all parse.
- Every row's `amount` is the EFFECTIVE figure (after refunds); `gross_amount`
  and `refunded` say what was paid and what came back. expenses_update(amount=)
  sets the ORIGINAL figure — to change what came back, use the refund tools.
- Keep the user's own words as the description (don't translate it).
- query matching: substring on description/category — and for expenses_list
  also the linked course's name, so "羽毛球" finds a payment described
  "Badminton". If a tool returns matched>1 with candidates, show them briefly
  and ask which; then call again with expense_id. Never guess.
- Pass the speaker's name as submitted_by / changed_by when you know it —
  the family reads the edit history.
"""


def _help_text() -> str:
    """The playbook, with the portal host filled in at build time.

    An assistant that cannot name the portal cannot help anyone reach it:
    a minted link used to come back as "https://<this service>/t/<token>",
    which could be neither opened nor forwarded.
    """
    base = portal_base_url()
    portal = (
        f"PORTAL: the family's phone page is at {base}/t/<token>. "
        "expenses_mint_link returns the full link; expenses_list_links never "
        "shows token values."
        if base else
        "PORTAL: the phone page is at https://<host>/t/<token>, but "
        "PORTAL_BASE_URL is not set on this service, so the host cannot be "
        "named here — ask the owner for it."
    )
    return _HELP + "\n" + portal + "\n"


def build_mcp(store: Store) -> FastMCP:
    help_text = _help_text()
    mcp = FastMCP(
        "family-expenses",
        instructions=help_text,  # bonus for clients that surface it
        stateless_http=True,
        json_response=True,
        host=os.environ.get("HOST", "0.0.0.0"),
    )

    # ── helpers ───────────────────────────────────────────────────────────
    def _category_note(category) -> str:
        """Flag an off-list category.

        Says what actually happens, not what would be tidier: the row counts as
        an ordinary household expense. The dangerous case is a borrow-synonym,
        which inflates household spending instead of the money-owed-back figure.
        """
        text = (str(category).strip() if category else "")
        if not text or text in CATEGORY_KEYS:
            return ""
        return (
            f" · NOTE: {text!r} is not one of the household's category keys, so "
            "this counts as ordinary household spending. If it is money that "
            f"must be paid back, use category={BORROW_CATEGORY!r} exactly — "
            "no synonym is recognised. See expenses_help for the list."
        )

    def _summary_note() -> str:
        s = store.summary()
        return f"unpaid total now ¥{s['unpaid']:.2f} across {s['unpaid_count']} item(s)"

    def _candidates(matches) -> list[dict[str, Any]]:
        return [
            {
                "expense_id": e.id, "description": e.description,
                "amount": e.amount, "date": e.date, "paid": e.paid,
                "category": e.category,
            }
            for e in matches[:8]
        ]

    def _resolve(
        expense_id: Optional[str], query: Optional[str], *,
        prefer_unpaid: bool = False, prefer_paid: bool = False,
    ):
        if expense_id:
            return expense_id, None
        if not query or not str(query).strip():
            raise ValidationError(
                "target missing: pass expense_id, or query with a word from the "
                "expense's description (e.g. query='足球课')"
            )
        matches = store.find(query)
        if prefer_unpaid and len(matches) > 1:
            unpaid = [e for e in matches if not e.paid]
            if len(unpaid) == 1:
                return unpaid[0].id, None
        if prefer_paid and len(matches) > 1:
            # the mirror image, for refunds: money comes back on a payment
            # that went out, so with one paid match among several it is the
            # one meant
            paid = [e for e in matches if e.paid]
            if len(paid) == 1:
                return paid[0].id, None
        if len(matches) == 1:
            return matches[0].id, None
        if not matches:
            return None, {
                "matched": 0, "candidates": [],
                "hint": (f"nothing matches {query!r} — call "
                         "expenses_list(status='all') and look for it, or ask the user"),
            }
        return None, {
            "matched": len(matches), "candidates": _candidates(matches),
            "hint": ("several matches — show these to the user, ask which one, "
                     "then call again with that expense_id"),
        }

    # ── help ──────────────────────────────────────────────────────────────
    @mcp.tool(annotations=_READ)
    def expenses_help() -> str:
        """START HERE when unsure. Returns the playbook: which tool for which
        user phrase (中文/EN), defaults, how to resolve ambiguity, and the
        portal's address."""
        return help_text

    # ── reads ─────────────────────────────────────────────────────────────
    @mcp.tool(annotations=_READ)
    def expenses_list(
        status: str = "all",
        query: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> dict[str, Any]:
        """List expenses AND totals. Use for: '我还要付什么/what do I owe'
        (status='unpaid'), '这个月花了多少/how much did we spend' (read
        .summary), or finding an item ('那个足球的' → query='足球'). query
        matches the description, the category AND the name of the course a
        payment funds, so '羽毛球' finds a payment described 'Badminton'.
        status: all|paid|unpaid|overdue. since/until: YYYY-MM-DD.
        Each row's amount is the EFFECTIVE figure after refunds; gross_amount
        and refunded say what was paid and what came back, .refunds lists each
        refund with its id (for expenses_refund_delete).
        .summary describes exactly the rows returned; when a filter is applied
        .ledger_total carries the whole-ledger figures for context.
        IN .summary: total/paid/unpaid are HOUSEHOLD SPENDING and leave out
        category='borrow' (money she fronted); those rows are in .borrow_owed
        and .borrow_repaid instead. .count counts EVERY returned row, borrow
        included — it is a count, not a total. So when the list contains a
        borrow row the items add up to MORE than .total, by design: quote
        .total for '花了多少', and add .borrow_owed for '她垫了多少'. Never
        sum the rows yourself to check .total — they are answering two
        different questions."""
        # read the clock BEFORE selecting rows: the overdue filter inside
        # find()/list() reads it too, and a call straddling midnight then omits
        # a newly-overdue row from the rows AND from the summary figure while
        # labelling the answer with the other day
        today = today_str()
        if query and str(query).strip():
            # validate before comparing: Store.list() validates these, and a
            # malformed date must coach the caller rather than silently
            # producing an arbitrary slice
            if since:
                since = store._validate_date(since, field="since")
            if until:
                until = store._validate_date(until, field="until")
            expenses = store.find(query, status=status, today=today,
                                  match_package=True)
            # find() has no date support; applying the range here keeps
            # since/until meaningful instead of silently ignored
            if since:
                expenses = [e for e in expenses if e.date >= since]
            if until:
                expenses = [e for e in expenses if e.date <= until]
        else:
            expenses = store.list(status=status, since=since, until=until,
                                  today=today)
        return {
            "expenses": [e.to_dict() for e in expenses],
            # totals for THESE rows — a filtered list beside a whole-ledger
            # total is a wrong answer to the question that was asked
            "summary": store.summarize(expenses, today=today),
            "ledger_total": store.summary(today=today) if (query or since or until
                                                           or status not in ("all", None, ""))
            else None,
        }

    @mcp.tool(annotations=_READ)
    def expenses_history(expense_id: str) -> dict[str, Any]:
        """Audit trail for ONE expense: every add/edit/paid/delete with who and
        when. Use for: '谁改的/这条怎么回事/what happened to this one'.
        Needs the expense_id (find it via expenses_list first)."""
        return {"history": [h.to_dict() for h in store.history(expense_id)]}

    # ── writes ────────────────────────────────────────────────────────────
    @mcp.tool(annotations=_WRITE)
    def expenses_add(
        amount: Union[str, float],
        description: Optional[str] = None,
        date: Optional[str] = None,
        category: Optional[str] = None,
        submitted_by: Optional[str] = None,
        paid: bool = False,
        paid_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record an expense. Use for: '足球课300块', 'football class 300',
        '要交300的班费'. amount accepts 300, '¥300', '300块'. Omit date =
        today (China time). Keep the user's own words as description. If they
        say it's ALREADY paid ('昨天交了...'), pass paid=true (paid_date
        defaults to today). To change an EXISTING expense use expenses_update;
        to pay one off use expenses_mark_paid. category: use an exact key from expenses_help — and for money someone fronted and is owed back, category='borrow' (never a synonym)."""
        # one transaction, even when it arrives already paid: this used to
        # create the row and then mark it paid separately, and a failure in
        # between left the expense unpaid while the tool reported an error
        expense = store.create(
            date=date or today_str(), amount=amount, description=description,
            category=category, submitted_by=submitted_by,
            paid=paid, paid_date=paid_date,
        )
        result = expense.to_dict()
        result["note"] = _summary_note() + _category_note(category)
        return result

    @mcp.tool(annotations=_WRITE)
    def expenses_mark_paid(
        expense_id: Optional[str] = None,
        query: Optional[str] = None,
        paid: bool = True,
        paid_date: Optional[str] = None,
        changed_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """Check an expense off as paid. Use for: '足球课付了', '交了', 'paid
        the football class', 'settled it'. Target by query (a word from its
        description — unpaid items are preferred) or expense_id. Omit
        paid_date = today. paid=false undoes a mistaken check-off — refused
        on a row with a refund recorded (money came back on it; remove the
        refund first with expenses_refund_delete). To change
        amount/description instead, use expenses_update."""
        eid, ambiguous = _resolve(expense_id, query, prefer_unpaid=True)
        if ambiguous:
            return ambiguous
        if paid and not paid_date:
            paid_date = today_str()
        result = store.mark_paid(
            eid, paid=paid, paid_date=paid_date, changed_by=changed_by
        ).to_dict()
        result["note"] = _summary_note()
        return result

    @mcp.tool(annotations=_WRITE)
    def expenses_update(
        expense_id: Optional[str] = None,
        query: Optional[str] = None,
        amount: Optional[Union[str, float]] = None,
        description: Optional[str] = None,
        date: Optional[str] = None,
        category: Optional[str] = None,
        changed_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """Correct an existing expense. Use for: '改成350', 'actually it was
        350', '不是足球是篮球', wrong date. Target by query or expense_id;
        pass ONLY the fields that change. amount is the ORIGINAL figure
        (before any refund) — if money came BACK, use expenses_refund instead
        of lowering the amount. To mark paid/unpaid use expenses_mark_paid
        (this tool cannot set paid). category must be an exact key from
        expenses_help; 'borrow' means owed back to whoever paid."""
        eid, ambiguous = _resolve(expense_id, query, prefer_unpaid=True)
        if ambiguous:
            return ambiguous
        fields = {
            k: v
            for k, v in {
                "amount": amount, "description": description,
                "date": date, "category": category,
            }.items()
            if v is not None
        }
        if not fields:
            raise ValidationError(
                "nothing to change — pass amount, description, date or category; "
                "for paid status use expenses_mark_paid"
            )
        expense = store.update(eid, fields=fields, changed_by=changed_by)
        result = expense.to_dict()
        result["note"] = _summary_note() + _category_note(fields.get("category"))
        if expense.refunded:
            result["note"] += (
                f" · NOTE: ¥{expense.refunded:.2f} has been refunded on this "
                f"row, so its effective amount is ¥{expense.amount:.2f} "
                f"(original ¥{expense.gross_amount:.2f})"
            )
        return result

    @mcp.tool(annotations=_DESTRUCTIVE)
    def expenses_delete(
        expense_id: Optional[str] = None,
        query: Optional[str] = None,
        changed_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """Remove an expense entirely. Use ONLY for '删掉/delete/不用了 it was
        entered by mistake' — and confirm with the user first. If the expense
        was simply paid, use expenses_mark_paid instead; if money came back,
        expenses_refund. The audit history is kept. Target by query or
        expense_id. A payment that funds a course is refused until the course
        is removed with classes_delete (the error names its package_id)."""
        eid, ambiguous = _resolve(expense_id, query, prefer_unpaid=False)
        if ambiguous:
            return ambiguous
        return {"deleted": store.delete(eid, changed_by=changed_by),
                "note": _summary_note()}

    # ── refunds ───────────────────────────────────────────────────────────
    def _refund_note(expense) -> str:
        return (
            f"effective amount now ¥{expense.amount:.2f} "
            f"(¥{expense.gross_amount:.2f} paid, ¥{expense.refunded:.2f} back)"
        )

    @mcp.tool(annotations=_WRITE)
    def expenses_refund(
        amount: Union[str, float],
        expense_id: Optional[str] = None,
        query: Optional[str] = None,
        date: Optional[str] = None,
        reason: Optional[str] = None,
        changed_by: Optional[str] = None,
        resize_package_to: Optional[Union[str, int]] = None,
    ) -> dict[str, Any]:
        """Record money that came BACK on a paid expense. Use for: '退了1800',
        '退款', 'they refunded us 1800', 'got 1800 back', '退了一半'. The
        original row keeps its amount and dates; the refund is a separate
        event with its own date (omit date = today), and every total reads
        amount − refunds. NEVER express a refund by lowering the amount with
        expenses_update. Target by query (a word from the description — paid
        rows are preferred) or expense_id. A course funded by the payment is
        repriced by the refund, so READ THE NOTE: on a per_class pack, pass
        resize_package_to=<new class_count> when the refund means fewer
        classes (¥3600 for 10 refunded ¥1800 is 5 at ¥360, not 10 at ¥180) —
        same transaction. On a period (term) fee a refund usually SETTLES
        classes the school owed back: record it, then remove those missed
        classes with classes_log_delete and resize the term to what remains,
        or 'owed' keeps claiming them. To undo: expenses_refund_delete(
        refund_id=…) with the id in this result."""
        eid, ambiguous = _resolve(expense_id, query, prefer_paid=True)
        if ambiguous:
            return ambiguous
        outcome = store.refund(
            eid, amount=amount, date=date, reason=reason, changed_by=changed_by,
            resize_package_to=resize_package_to,
        )
        expense = outcome["expense"]
        result = expense.to_dict()
        result["refund"] = outcome["refund"]
        note = (
            f"refund ¥{outcome['refund']['amount']:.2f} recorded on "
            f"{outcome['refund']['date']} (refund_id={outcome['refund']['id']}) · "
            + _refund_note(expense)
        )
        package = outcome["package"]
        if package is not None:
            s = package["summary"]
            result["package"] = {
                "id": package["id"], "name": package["name"],
                "kind": package["kind"], "summary": s,
                "resized": outcome["resized"],
            }
            # the course's figures moved whether or not it was resized — the
            # reprice must never be silent, on either surface
            if package["kind"] == "per_class":
                note += (
                    f" · {package['name']} "
                    + ("resized to" if outcome["resized"] else "still")
                    + f" {s['class_count']} classes, now ¥{s['rate']:.2f} each, "
                    f"{s['attended']} attended, {s['remaining']} left"
                    + ("" if outcome["resized"] else
                       " — if the refund means FEWER classes, call again with "
                       "resize_package_to (or classes_update) so the rate is "
                       "honest; ¥{:.2f} over {} classes is what the tracker now "
                       "says a class cost".format(s["amount"], s["class_count"]))
                )
            else:
                settled = [e for e in package["events"]
                           if e["kind"] in ("missed_school", "missed_us")]
                note += (
                    f" · {package['name']} is a term fee: "
                    + ("resized to" if outcome["resized"] else "still")
                    + f" {s['class_count']} classes at ¥{s['rate']:.2f} each, "
                    f"{s['owed']} owed back = ¥{s['owed_amount']:.2f}"
                    + ((" — NOTE: if this refund SETTLES those missed classes, "
                        "remove them with classes_log_delete(event_id=…) "
                        "so 'owed' stops claiming them: "
                        + ", ".join(f"{e['date']} {e['kind']} ({e['id']})"
                                    for e in settled[:8])
                        + (", …" if len(settled) > 8 else "")
                        + "; and resize the term to the classes that remain")
                       if settled else "")
                )
        result["note"] = note
        return result

    @mcp.tool(annotations=_DESTRUCTIVE)
    def expenses_refund_delete(
        refund_id: str, changed_by: Optional[str] = None
    ) -> dict[str, Any]:
        """Take back a refund that was recorded by mistake ('那个退款记错了',
        'undo the refund', 'they did not actually refund it') — confirm with
        the user first. The refund row is removed and kept in the history;
        the expense's effective amount goes back up. If the refund resized
        the course (resize_package_to), the course goes back to the class
        count it had before — the two were one decision — unless something
        changed the count since, in which case it is left alone and the note
        says so. refund_id comes from the expenses_refund result, from
        .refunds on an expenses_list row, or from expenses_history (action
        'refund'). Read the note for what happened to the course."""
        outcome = store.delete_refund(refund_id, changed_by=changed_by)
        if outcome is None:
            raise ValidationError(
                f"no refund with id {refund_id!r} — refund ids are in "
                "expenses_history (action 'refund') and on each row's .refunds "
                "in expenses_list"
            )
        expense = outcome["expense"]
        result = expense.to_dict()
        note = "refund removed · " + _refund_note(expense)
        package = outcome["package"]
        if package is not None:
            result["package"] = {k: v for k, v in package.items() if k != "payload"}
            if package.get("restored"):
                s = package["payload"]["summary"]
                note += (
                    f" · {package['name']} restored to {s['class_count']} classes "
                    f"at ¥{s['rate']:.2f} each (the refund had set it to "
                    f"{package['class_count_after']})"
                )
            else:
                note += (
                    f" · {package.get('name') or 'the course'} left at "
                    f"{package.get('class_count', '?')} classes — {package['reason']}"
                    + ("; classes_update(class_count=…) if that is wrong"
                       if package.get("class_count") is not None else "")
                )
        result["note"] = note
        return result

    # ── class tracker ─────────────────────────────────────────────────────
    def _resolve_package(package_id: Optional[str], query: Optional[str]):
        packages = store.list_packages(include_archived=True)
        if package_id:
            return str(package_id), None
        text = str(query or "").strip().lower()
        if not text:
            raise ValidationError(
                "target missing: pass package_id, or query with a word from the "
                "course name (e.g. query='足球')"
            )
        named = [
            p for p in packages
            if text in (p["name"] or "").lower()
            or text in (p["period_label"] or "").lower()
        ]
        # An archived course is a finished one, and it is a name match like any
        # other — so 足球课 retired last term beat the live 足球课 and took the
        # write, invisibly: classes_list hides archived by default, so the note
        # the agent reads back does not contain the class it just logged. Same
        # shape as prefer_unpaid on the expense side.
        live = [p for p in named if not p["archived"]]
        if len(live) == 1:
            return live[0]["id"], None
        if not live and len(named) == 1:
            return named[0]["id"], None  # only a retired course matches: still reachable
        # The funding payment is the weaker signal, and only consulted when the
        # course names miss: a per_class pack carries no period label since
        # v0.11.0, so '8月' has nowhere else to match — it lives in
        # "Football (8月, 10课)". It never resolves on its own, because the
        # payment for 游泳课 may well say "足球课 8月 (转游泳)", and drawing a
        # class off swimming when the owner said football is a wrong write with
        # nothing to show for it.
        by_payment = not named
        matches = named or [
            p for p in packages
            if text in (p["expense"]["description"] or "").lower()
        ]
        if not matches:
            return None, {
                "matched": 0, "candidates": [],
                "hint": (f"no class package matches {query!r} — call classes_list "
                         "to see them, or classes_add to start one"),
            }
        return None, {
            "matched": len(matches),
            # name + period_label alone made two per_class packs called
            # 'Football' indistinguishable, so the disambiguation question had no
            # answer. The payment that funds each one is what tells them apart.
            "candidates": [
                {"package_id": p["id"], "name": p["name"],
                 "period_label": p["period_label"], "kind": p["kind"],
                 "payment": f"{p['expense']['date']} · "
                            f"{p['expense']['description'] or '–'} · "
                            f"¥{p['expense']['amount']:.2f}",
                 # two terms bought in one sitting can share a date, a
                 # description AND an amount, so `payment` alone can print
                 # twice; `created_at` is second-granular, so `started` can too.
                 # `package_id` is the only handle unique by construction.
                 "classes_logged": len(p["events"]),
                 "started": p["created_at"],
                 # the one field that separates a finished course from a
                 # running one — without it the agent shows the owner two rows
                 # and cannot say "one of these is last term"
                 "archived": p["archived"]}
                for p in matches[:8]
            ],
            "hint": (
                (f"nothing is CALLED {query!r} — these are courses whose "
                 "PAYMENT says so, which is a weaker match. Confirm with the "
                 "user before logging against one, then call again with its "
                 "package_id. "
                 if by_payment else
                 "several courses match — show these to the user, ask which, "
                 "then call again with that package_id. ")
                + "`payment` is usually what tells two same-named courses "
                  "apart; if these rows read alike, they are courses only "
                  "`package_id` can separate — say so rather than guessing"),
        }

    @mcp.tool(annotations=_READ)
    def classes_list(
        query: Optional[str] = None,
        include_archived: bool = False,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Prepaid courses and what is left of them. Use for: '还剩几节课/
        how many classes left', '足球还有几次', '这个月缺了几节/how many did we
        miss', 'what do they owe us'. Returns每 package with classes remaining
        and money remaining (per_class), or classes owed back split into
        reclaimable vs forfeited (period). query narrows by course name,
        period label or the payment's description. By default each package
        carries counts and its last class, not the whole log; pass
        verbose=true for every event with its event_id (needed for
        classes_log_delete). To start tracking a course use classes_add; to
        record a class use classes_log; to edit or retire one, classes_update;
        to remove one, classes_delete."""
        packages = store.list_packages(include_archived=include_archived, query=query)
        if not verbose:
            for p in packages:
                events = p.pop("events")
                p["events_count"] = len(events)
                p["last_event"] = (
                    f"{events[0]['date']} · {events[0]['kind']}" if events else None
                )
        lines = []
        for p in packages:
            s = p["summary"]
            # the period label, or the payment behind it: a per_class package
            # created from the portal has no label since v0.11.0, and two terms
            # of one course would otherwise both read "足球课 (—)" here. The
            # payment's DESCRIPTION, not its date — two terms bought in one
            # sitting share a date, and she writes the month in the description.
            tag = (p["period_label"] or p["expense"]["description"]
                   or p["expense"]["date"])
            if p["kind"] == "per_class":
                lines.append(
                    f"{p['name']} ({tag}): "
                    f"{s['remaining']}/{s['class_count']} classes left, "
                    f"¥{s['remaining_amount']:.2f}"
                )
            else:
                lines.append(
                    f"{p['name']} ({tag}): "
                    f"{s['owed']} owed back = ¥{s['owed_amount']:.2f} "
                    f"({s['reclaimable']} theirs / {s['forfeited']} ours)"
                )
        return {
            "packages": packages,
            "note": ("; ".join(lines) if lines else
                     ("no class packages match" if query else "no class packages yet")
                     + " — classes_add starts one from a payment already in the ledger")
                    + ("" if verbose else
                       " · pass verbose=true for the class log with event ids"),
        }

    @mcp.tool(annotations=_WRITE)
    def classes_add(
        name: str,
        class_count: Union[str, int],
        kind: str = "per_class",
        expense_id: Optional[str] = None,
        query: Optional[str] = None,
        period_label: Optional[str] = None,
        changed_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """Start tracking a prepaid course, FROM a payment already recorded.
        Use for: '足球课交了2200，10节课', 'paid for 10 football classes',
        '报了8月的课'. Target the payment by query (a word from its
        description) or expense_id — record it with expenses_add FIRST if it is
        not in the ledger yet. kind='per_class' for a pack of N classes drawn
        down one at a time; kind='period' for a flat month/semester fee where
        MISSED classes are owed back. The per-class rate is derived from the
        payment (amount ÷ class_count) — do not pass a rate. period_label is
        free text like '8月' or '秋季学期'. NOTE: the portal's Classes tab can
        only START a course from a payment categorised 'aden-edu' or
        'aden-sports' — its add form lists no others. A course started HERE from
        any other category shows up in her tab normally and she can log classes
        against it, so there is nothing to correct; do not change a payment's
        category to make one appear."""
        eid, ambiguous = _resolve(expense_id, query, prefer_unpaid=False)
        if ambiguous:
            return ambiguous
        package = store.create_package(
            expense_id=eid, name=name, kind=kind,
            class_count=class_count, period_label=period_label,
            changed_by=changed_by,
        )
        s = package["summary"]
        x = package["expense"]
        package["note"] = (
            f"tracking {s['class_count']} classes at ¥{s['rate']:.2f} each "
            # the GROSS figure under the word "paid": the effective amount
            # is what funds the course, but "¥1800 paid" on a ¥3600 payment
            # is the exact sentence the refund table exists to stop
            f"(¥{x['gross_amount']:.2f} paid"
            + (f", ¥{x['refunded']:.2f} refunded, ¥{s['amount']:.2f} effective"
               if x["refunded"] else "")
            + "). "
            + ("Log each class with classes_log(kind='attended')."
               if package["kind"] == "per_class" else
               "Log the ones that do NOT happen with classes_log("
               "kind='missed_school') or kind='missed_us'.")
        )
        return package

    def _class_note(package) -> str:
        s = package["summary"]
        return (
            f"{s['remaining']} of {s['class_count']} classes left "
            f"(¥{s['remaining_amount']:.2f})"
            + (f" — NOTE: {s['overrun']} more attended than were paid for"
               if s.get("overrun") else "")
            if package["kind"] == "per_class" else
            f"{s['owed']} class(es) owed back = ¥{s['owed_amount']:.2f} "
            f"({s['reclaimable']} cancelled by them = ¥{s['reclaimable_amount']:.2f} "
            f"reclaimable, {s['forfeited']} skipped by us)"
        )

    @mcp.tool(annotations=_WRITE)
    def classes_log(
        kind: str,
        package_id: Optional[str] = None,
        query: Optional[str] = None,
        date: Optional[str] = None,
        # a list, or one string the store splits ("8-17, 8-21" is what speech
        # produces) — typed as a list alone, pydantic refused the string with
        # its own error before the store's coaching could be reached
        dates: Optional[Union[list[str], str]] = None,
        note: Optional[str] = None,
        logged_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """Record one class — or several dates at once — against a course.
        Use for: '今天上了足球课/went to football today' (kind='attended'),
        '今天的课取消了/they cancelled' (kind='missed_school'), '今天没去/we
        skipped it' (kind='missed_us'), '8月17、21、28都上了/log these dates'
        (dates=['2026-08-17', …] — one call, all or nothing). Target by query
        (a word from the course name) or package_id. Omit date = today. On a
        per_class pack only 'attended' draws a class down; on a period package
        the missed ones are what is owed back, and the cause decides whether
        it is reclaimable ('missed_school') or forfeited ('missed_us'). Read
        the result's note for what is left; .logged_events carries each
        event's id, and classes_log_delete(event_id=…) takes one back."""
        # validate the kind BEFORE resolving the course: it is wrong no matter
        # which package the agent meant, and reporting "no such course" first
        # would cost a round trip to discover the real mistake
        kind = store._validate_event_kind(kind)
        pid, ambiguous = _resolve_package(package_id, query)
        if ambiguous:
            return ambiguous
        package = store.log_class(
            package_id=pid, kind=kind, date=date, dates=dates, note=note,
            logged_by=logged_by,
        )
        written = package["logged_events"]
        package["note"] = (
            (f"logged {len(written)} classes ({', '.join(e['date'] for e in written)}) · "
             if len(written) > 1 else "")
            + _class_note(package)
        )
        return package

    @mcp.tool(annotations=_DESTRUCTIVE)
    def classes_log_delete(
        event_id: str, changed_by: Optional[str] = None
    ) -> dict[str, Any]:
        """Take back ONE logged class ('那天没上，记错了', 'undo today's
        class', 'delete that class record') — confirm with the user first.
        event_id comes from the classes_log result (.logged_events) or from
        classes_list(verbose=true). The removed event stays in the funding
        payment's expenses_history. To remove a whole course use
        classes_delete instead."""
        package = store.delete_class_event(event_id, changed_by=changed_by)
        if package is None:
            raise ValidationError(
                f"no class event with id {event_id!r} — event ids come from "
                "classes_list(verbose=true) or the result of classes_log"
            )
        ev = package["unlogged_event"]
        package["note"] = (
            f"removed {ev['date']} · {ev['kind']} from {package['name']} · "
            + _class_note(package)
        )
        return package

    @mcp.tool(annotations=_WRITE)
    def classes_update(
        package_id: Optional[str] = None,
        query: Optional[str] = None,
        class_count: Optional[Union[str, int]] = None,
        name: Optional[str] = None,
        kind: Optional[str] = None,
        period_label: Optional[str] = None,
        archived: Optional[bool] = None,
        changed_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """Edit a course — never its money. Use for: '课时改成5/改成5节/the
        pack is 5 classes now' (class_count), '改名/rename it' (name),
        '这个课结束了/上完了/archive the course/retire it' (archived=true;
        keeps the log, hides it from classes_list unless include_archived),
        or a period label (period_label='' clears it). Target by query (a
        word from the course name) or package_id; pass ONLY the fields that
        change. The per-class rate is
        recomputed from the payment ÷ class_count — to change the money, edit
        the payment (expenses_update) or record a refund (expenses_refund,
        which can resize in the same call). Shrinking below the classes
        already logged is refused and the error names them. kind cannot change
        once anything is logged."""
        pid, ambiguous = _resolve_package(package_id, query)
        if ambiguous:
            return ambiguous
        fields = {
            k: v
            for k, v in {
                "class_count": class_count, "name": name, "kind": kind,
                "period_label": period_label, "archived": archived,
            }.items()
            if v is not None
        }
        if not fields:
            raise ValidationError(
                "nothing to change — pass class_count, name, kind, period_label "
                "or archived; the money lives on the payment (expenses_update)"
            )
        package = store.update_package(pid, fields=fields, changed_by=changed_by)
        s = package["summary"]
        package["note"] = (
            f"{package['name']}: {s['class_count']} classes at ¥{s['rate']:.2f} each"
            + (" · ARCHIVED (hidden from classes_list unless include_archived=true)"
               if package["archived"] else "")
            + " · " + _class_note(package)
        )
        return package

    @mcp.tool(annotations=_DESTRUCTIVE)
    def classes_delete(
        package_id: Optional[str] = None,
        query: Optional[str] = None,
        changed_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """Stop tracking a course entirely ('删掉这个课程', 'remove the
        course', 'stop tracking it') — confirm with the user first, naming
        the course and how many classes are logged. The class log is kept in
        the funding payment's expenses_history, and the payment itself stays
        in the ledger. If the course merely finished, prefer
        classes_update(archived=true). Target by query or package_id."""
        pid, ambiguous = _resolve_package(package_id, query)
        if ambiguous:
            return ambiguous
        package = store.package(pid)
        deleted = store.delete_package(pid, changed_by=changed_by)
        return {
            "deleted": deleted, "package_id": pid, "name": package["name"],
            "expense_id": package["expense_id"],
            "note": (
                f"{package['name']} removed with {len(package['events'])} logged "
                f"class(es) — all kept in expenses_history(expense_id="
                f"{package['expense_id']!r}). The payment "
                f"¥{package['expense']['gross_amount']:.2f}"
                + (f" (¥{package['expense']['refunded']:.2f} refunded)"
                   if package["expense"]["refunded"] else "")
                + " is still in the ledger."
            ),
        }

    # ── link management ───────────────────────────────────────────────────
    @mcp.tool(annotations=_READ)
    def expenses_list_links(include_revoked: bool = False) -> dict[str, Any]:
        """List the portal links that exist and how they are being used. Use
        for: '谁有链接/哪些链接还在用/有几个链接', 'who has a link', 'list the
        links', and ALWAYS before revoking, so you know which one to kill.
        Returns each link's id, label and usage — NOT the token itself, so
        nothing permanent leaks into the chat. Pass an id straight to
        expenses_revoke_link. To create a link use expenses_mint_link."""
        rows = store.list_tokens()
        links = []
        for r in rows:
            if r["revoked"]:
                status = "revoked"
            elif r["expires_at"] and str(r["expires_at"]) <= _utc_now_iso():
                status = "expired"
            else:
                status = "active"
            if status == "revoked" and not include_revoked:
                continue
            links.append({
                "id": r["id"], "label": r["label"], "status": status,
                "expires_at": r["expires_at"] or "never",
                "use_count": r["use_count"],
                "last_used_at": r["last_used_at"] or "never opened",
                "created_at": r["created_at"],
            })
        active = sum(1 for x in links if x["status"] == "active")
        base = portal_base_url()
        return {
            "links": links,
            "portal": f"{base}/t/<token>" if base else None,
            "note": (
                f"{active} active link(s). Token values are never listed — "
                "revoke with expenses_revoke_link(token_or_id=<the id above>). "
                + (f"The portal is at {base}/t/<token>. " if base else
                   "PORTAL_BASE_URL is not set, so the host cannot be named here. ")
                + ("Revoked links hidden; pass include_revoked=true to see them."
                   if not include_revoked else "")
            ),
        }

    @mcp.tool(annotations=_WRITE)
    def expenses_mint_link(
        label: Optional[str] = None, expires_days: Optional[int] = None
    ) -> dict[str, Any]:
        """Create a portal link for a family member ('给我老婆做个链接' /
        'make a link for my wife'). Never expires unless expires_days is set.
        Returns .url, the full https://<host>/t/<token> link to hand over —
        it is a permanent credential, so only mint when asked. To see or
        revoke existing links use expenses_list_links / expenses_revoke_link."""
        minted = store.mint_token(label=label, expires_days=expires_days)
        minted["url"] = portal_link(minted["token"])
        minted["note"] = (
            "send .url to the family member; it never expires"
            + (" (expires_days was set, so it does)" if minted["expires_at"] else "")
            + ("" if portal_base_url() else
               " — PORTAL_BASE_URL is not set on this service, so .url carries a "
               "placeholder host; ask the owner for the real one")
        )
        return minted

    @mcp.tool(annotations=_DESTRUCTIVE)
    def expenses_revoke_link(token_or_id: str) -> dict[str, Any]:
        """Kill a portal link (lost phone, leaked URL). Takes the token or its
        id — call expenses_list_links first to see the ids. Revoking is
        permanent and the family member loses access immediately, so confirm
        with the user which link before calling."""
        return {"revoked": store.revoke_token(token_or_id)}

    # ── personas (MCP prompts — user-invocable in clients that show them) ─
    @mcp.prompt(name="jizhang", title="记账 Quick add")
    def quick_add(said: str = "") -> str:
        """快速记一笔 — paste or say what needs paying."""
        return (
            "You are the family bookkeeper (家庭记账员). The user will dictate "
            "expenses casually, possibly several in one message, Chinese or "
            "English. For each: call expenses_add keeping their exact wording "
            "as description; omit dates (defaults to today); pass amounts "
            "verbatim ('300块' is fine). If they said it's already paid, set "
            "paid=true. Confirm each item back in ONE short line in their "
            "language, ending with the unpaid total from the result's note. "
            "Ask at most one question, and only if the amount is missing."
            + (f"\n\nThe user said: {said}" if said else "")
        )

    @mcp.prompt(name="duizhang", title="对账 Settle up")
    def settle_up() -> str:
        """过一遍待付的，付了的打勾。"""
        return (
            "You are helping settle the family ledger (对账). Call "
            "expenses_list(status='unpaid') and present a short numbered list "
            "in the user's language with amounts, then the total from "
            ".summary.unpaid. That total leaves OUT any category='borrow' row "
            "in the list — she fronted that money and is owed it back, so it "
            "is not something to pay. If the list has one, say so on its own "
            "line using .summary.borrow_owed rather than folding it in; the "
            "numbered items will otherwise appear to add up to more than the "
            "total. Then walk through it: for each item they say is paid, call "
            "expenses_mark_paid (today's date unless they say otherwise). "
            "Finish by reporting what's still unpaid."
        )

    @mcp.prompt(name="xiufu", title="修复 Fix a mistake")
    def fix_mistake(problem: str = "") -> str:
        """记错了/改不动了/找不到 — troubleshooting persona."""
        return (
            "You are troubleshooting the family ledger (修复记录). Something "
            "was recorded wrongly or can't be found. Steps: (1) call "
            "expenses_list(status='all') — or with query=<word the user "
            "used> — and locate the item(s); show what you found; (2) if "
            "unclear which item, ask, showing the candidates; (3) apply the "
            "fix: wrong amount/text/date → expenses_update; wrongly marked "
            "paid → expenses_mark_paid(paid=false); money came back → "
            "expenses_refund (never lower the amount for a refund); a refund "
            "recorded wrongly → expenses_refund_delete; a course with the "
            "wrong class count → classes_update; a class logged by mistake → "
            "classes_log_delete; duplicate/mistake → expenses_delete after "
            "explicit confirmation; (4) if the user disputes what happened, "
            "call expenses_history for that item and explain who changed "
            "what, when — it also lists the course's own changes. Never "
            "delete without asking."
            + (f"\n\nThe problem: {problem}" if problem else "")
        )

    return mcp


class McpBearerMiddleware:
    """Optional bearer gate on the MCP mount.

    ``MCP_SECRET`` set → require ``Authorization: Bearer $MCP_SECRET`` (401
    otherwise). Unset → /mcp is open, per the owner's accepted threat model
    (obscure URL, low-stakes ledger). Portal and API paths are never touched.
    """

    def __init__(self, app: ASGIApp, protected_prefix: str = "/mcp"):
        self.app = app
        self.prefix = protected_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        secret = os.environ.get("MCP_SECRET", "")
        if secret:
            headers = dict(scope.get("headers") or [])
            supplied = (headers.get(b"authorization") or b"").decode()
            if not hmac.compare_digest(supplied, f"Bearer {secret}"):
                response = JSONResponse(
                    {"ok": False, "error": "unauthorized"}, status_code=401
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
