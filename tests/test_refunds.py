"""Refunds, the class tracker's audit trail, and the history-action migration.

Store-level. The scenario behind all of it (2026-09-05): a ¥3,600 badminton
pack of ten, five attended, half refunded. The only way to express that was
to delete the course and the payment and rebuild both — a new expense id, a
new created_at, five attendance events "created" a month after the dates
they record, and a ledger that then claimed he paid ¥1,800 on a day he paid
¥3,600. These tests replay that day and pin every figure it produced.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database  # noqa: E402
from app.models import HISTORY_ACTIONS  # noqa: E402
from app.store import Store, ValidationError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def make_store() -> Store:
    db = Database("sqlite:///:memory:")
    db.init()
    return Store(db)


# The pre-0.13.0 shape of the two tables the migration touches, verbatim from
# the schema that shipped — so the rebuild is exercised against what
# production actually has, not against a fixture that already agrees.
OLD_DDL = """
CREATE TABLE IF NOT EXISTS expenses (
  id TEXT PRIMARY KEY, date TEXT NOT NULL,
  amount REAL NOT NULL CHECK (amount > 0),
  currency TEXT NOT NULL DEFAULT 'CNY', category TEXT, description TEXT,
  paid BOOLEAN NOT NULL DEFAULT FALSE, paid_date TEXT, submitted_by TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expense_history (
  id TEXT PRIMARY KEY, expense_id TEXT NOT NULL, seq INTEGER NOT NULL,
  action TEXT NOT NULL CHECK (
    action IN ('create', 'update', 'mark_paid', 'unmark_paid', 'delete')
  ),
  changed_by TEXT, changed_at TEXT NOT NULL, snapshot TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expense_history_expense
  ON expense_history(expense_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS uq_expense_history_expense_seq
  ON expense_history (expense_id, seq);
INSERT INTO expenses VALUES ('e1','2026-08-15',3600,'CNY','aden-sports',
  'Badminton',1,'2026-08-15','Matt','2026-08-15T00:00:00','2026-08-15T00:00:00');
INSERT INTO expense_history VALUES ('h1','e1',0,'create','Matt',
  '2026-08-15T00:00:00','{"amount": 3600}');
"""


def old_database() -> str:
    """A sqlite file in the pre-0.13.0 shape, with one paid row in it."""
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_DDL)
    conn.close()
    return path


class RefundStoreTests(unittest.TestCase):
    """A refund is a second fact with its own date, never a rewrite."""

    def setUp(self):
        self.store = make_store()

    def paid(self, amount=3600, description="Badminton (8月-9月)", **kw):
        return self.store.create(
            date="2026-08-15", amount=amount, description=description,
            category="aden-sports", paid=True, paid_date="2026-08-15",
            submitted_by="Matt", **kw,
        )

    def pack(self, expense, count=10, name="羽毛球 (1:1)", kind="per_class"):
        return self.store.create_package(
            expense_id=expense.id, name=name, kind=kind, class_count=count,
            changed_by="Matt",
        )

    def test_the_badminton_day_replays_with_nothing_rebuilt(self):
        """The acceptance test from the prompt, at the store."""
        e = self.paid()
        p = self.pack(e)
        logged = self.store.log_class(
            package_id=p["id"], kind="attended", logged_by="wife",
            dates=["2026-08-17", "2026-08-21", "2026-08-28", "2026-08-31", "2026-09-02"],
        )
        before = {ev["id"]: ev["created_at"] for ev in logged["events"]}
        self.assertEqual(len(before), 5)

        out = self.store.refund(
            e.id, amount=1800, date="2026-09-05", changed_by="Matt",
            resize_package_to=5,
        )
        x = out["expense"]
        self.assertEqual(x.id, e.id)                        # same row
        self.assertEqual(x.created_at, e.created_at)        # nothing rebuilt
        self.assertEqual((x.date, x.paid_date), ("2026-08-15", "2026-08-15"))
        self.assertEqual(x.gross_amount, 3600.0)            # original intact
        self.assertEqual(x.amount, 1800.0)                  # effective
        self.assertEqual(x.refunded, 1800.0)
        s = out["package"]["summary"]
        self.assertEqual(s["class_count"], 5)
        self.assertEqual(s["rate"], 360.0)                  # not ¥180
        self.assertEqual((s["attended"], s["remaining"]), (5, 0))
        self.assertEqual(s["remaining_amount"], 0.0)
        after = {ev["id"]: ev["created_at"]
                 for ev in self.store.package(p["id"])["events"]}
        self.assertEqual(after, before, "the attendance log was touched")
        actions = [h.action for h in self.store.history(e.id)]
        self.assertIn("refund", actions)
        self.assertNotIn("update", actions, "the refund read as an amount edit")
        self.assertEqual(self.store.summary(today="2026-09-05")["total"], 1800.0)

    def test_a_refund_never_rewrites_the_row(self):
        e = self.paid()
        self.store.refund(e.id, amount=100, date="2026-09-05")
        with self.store.db.tx() as tx:
            stored = tx.query_one("SELECT amount, updated_at FROM expenses WHERE id = :id",
                                  {"id": e.id})
        self.assertEqual(float(stored["amount"]), 3600.0)
        self.assertEqual(stored["updated_at"], e.updated_at)

    def test_the_effective_amount_is_what_every_total_reads(self):
        """P4: one totals implementation, and it must see the refund without
        being told. The same for the course rate."""
        e = self.paid()
        p = self.pack(e)
        self.store.refund(e.id, amount=1800)
        rows = self.store.list(status="all")
        self.assertEqual(rows[0].amount, 1800.0)
        self.assertEqual(self.store.summarize(rows, today="2026-09-05")["paid"], 1800.0)
        self.assertEqual(self.store.summary(today="2026-09-05")["total"], 1800.0)
        self.assertEqual(self.store.package(p["id"])["summary"]["amount"], 1800.0)
        self.assertEqual(self.store.package(p["id"])["expense"]["gross_amount"], 3600.0)
        # and the search path — find() has its own SELECT
        self.assertEqual(self.store.find("Badminton")[0].amount, 1800.0)

    def test_refunds_sweep_amounts_that_do_not_divide(self):
        """LESSONS §10: ¥2,200 over 10 hides every rounding defect."""
        cases = [(3333.33, 7, 1000.01), (1999.99, 3, 0.01), (777.77, 9, 777.76),
                 (1000.124, 3, 500.05)]
        for gross, count, back in cases:
            with self.subTest(gross=gross, count=count, back=back):
                store = make_store()
                e = store.create(date="2026-08-01", amount=gross, paid=True,
                                 paid_date="2026-08-01", description="x")
                p = store.create_package(expense_id=e.id, name="c", kind="per_class",
                                         class_count=count)
                out = store.refund(e.id, amount=back)
                x = out["expense"]
                net = round(round(gross, 2) - back, 2)
                self.assertEqual(x.amount, round(gross - back, 2))
                self.assertEqual(store.summary(today="2026-08-01")["total"], net)
                s = store.package(p["id"])["summary"]
                self.assertEqual(s["amount"], round(gross - back, 2))
                self.assertEqual(s["rate"], round(x.amount / count, 2))
                self.assertEqual(round(s["used_amount"] + s["remaining_amount"], 2),
                                 s["amount"])

    def test_more_than_is_left_is_refused_cumulatively(self):
        e = self.paid()
        self.store.refund(e.id, amount=1800)
        with self.assertRaises(ValidationError) as ctx:
            self.store.refund(e.id, amount=1800.01)
        self.assertIn("¥1800.00", str(ctx.exception))
        self.assertEqual(len(self.store.list()[0].refunds), 1)

    def test_a_full_refund_nets_to_zero(self):
        e = self.paid()
        x = self.store.refund(e.id, amount=3600)["expense"]
        self.assertEqual(x.amount, 0.0)
        self.assertEqual(self.store.summary(today="2026-09-05")["paid"], 0.0)

    def test_an_unpaid_row_cannot_be_refunded_and_the_error_coaches(self):
        e = self.store.create(date="2026-08-15", amount=3600, description="x")
        with self.assertRaises(ValidationError) as ctx:
            self.store.refund(e.id, amount=100)
        text = str(ctx.exception)
        self.assertIn("expenses_update", text)
        self.assertIn("expenses_mark_paid", text)

    def test_the_amount_cannot_be_edited_below_what_came_back(self):
        """`amount` on a write is the gross figure; the guard keeps the
        effective one from going negative — and the message says which
        figure the field is, which the portal repeats in its label."""
        e = self.paid()
        self.store.refund(e.id, amount=1800)
        with self.assertRaises(ValidationError) as ctx:
            self.store.update(e.id, fields={"amount": 1000})
        self.assertIn("expenses_refund_delete", str(ctx.exception))
        # at the refunded figure it passes (net 0), and gross is what changes
        x = self.store.update(e.id, fields={"amount": 1800})
        self.assertEqual((x.gross_amount, x.amount), (1800.0, 0.0))

    def test_refund_and_resize_are_one_transaction(self):
        """A resize the shrink rule refuses must take the refund down with it.
        Applied separately there is a state where ¥1,800 sits over ten classes."""
        e = self.paid()
        p = self.pack(e)
        self.store.log_class(package_id=p["id"], kind="attended",
                             dates=["2026-08-17", "2026-08-21", "2026-08-28",
                                    "2026-08-31", "2026-09-02"])
        history_before = len(self.store.history(e.id))
        with self.assertRaises(ValidationError) as ctx:
            self.store.refund(e.id, amount=1800, resize_package_to=4)
        self.assertIn("2026-09-02", str(ctx.exception))
        x = self.store.list()[0]
        self.assertEqual((x.amount, x.refunded, x.refunds), (3600.0, 0.0, []))
        self.assertEqual(self.store.package(p["id"])["class_count"], 10)
        self.assertEqual(len(self.store.history(e.id)), history_before)

    def test_a_resize_with_no_course_is_refused_and_writes_nothing(self):
        e = self.paid()
        with self.assertRaises(ValidationError) as ctx:
            self.store.refund(e.id, amount=100, resize_package_to=5)
        self.assertIn("classes_list", str(ctx.exception))
        self.assertEqual(self.store.list()[0].refunds, [])

    def test_a_refund_writes_one_history_row_and_a_resize_one_more(self):
        e = self.paid()
        p = self.pack(e)
        n = len(self.store.history(e.id))
        self.store.refund(e.id, amount=100)
        self.assertEqual([h.action for h in self.store.history(e.id)][n:], ["refund"])
        self.store.refund(e.id, amount=100, resize_package_to=8)
        self.assertEqual([h.action for h in self.store.history(e.id)][n + 1:],
                         ["package_update", "refund"])
        last = self.store.history(e.id)[-1].snapshot
        self.assertEqual(last["refund"]["amount"], 100.0)
        self.assertEqual(last["package"]["class_count"], 8)
        self.assertEqual(last["amount"], 3400.0)
        self.assertEqual(last["gross_amount"], 3600.0)
        self.assertEqual(self.store.package(p["id"])["class_count"], 8)

    def test_deleting_a_refund_restores_the_row_and_keeps_the_record(self):
        e = self.paid()
        rid = self.store.refund(e.id, amount=1800, reason="half back")["refund"]["id"]
        out = self.store.delete_refund(rid, changed_by="Matt")
        x = out["expense"]
        self.assertEqual((x.amount, x.refunded, x.refunds), (3600.0, 0.0, []))
        self.assertIsNone(out["package"], "no course was resized, so nothing to say")
        last = self.store.history(e.id)[-1]
        self.assertEqual(last.action, "refund_delete")
        self.assertEqual(last.snapshot["refund"]["id"], rid)
        self.assertEqual(last.snapshot["refund"]["reason"], "half back")
        self.assertNotIn("package", last.snapshot)
        self.assertIsNone(self.store.delete_refund(rid))  # gone means gone

    def test_deleting_a_refund_that_resized_the_course_puts_the_count_back(self):
        """The reproduction from the live smoke session: ¥1,000 for ten, two
        attended, refund ¥500 resized to 5 (still ¥100 a class), undo → the
        pack sat at 5 classes over ¥1,000, ¥200 a class, a rate nobody chose.
        The refund and the resize are one decision; the undo reverses both."""
        e = self.paid(amount=1000, description="ZZTEST refund harness")
        p = self.pack(e, count=10, name="ZZTEST course")
        self.store.log_class(package_id=p["id"], kind="attended",
                             dates=["2026-09-01", "2026-09-02"])
        out = self.store.refund(e.id, amount=500, resize_package_to=5, reason="test")
        rid = out["refund"]["id"]
        self.assertEqual((out["refund"]["package_id"], out["refund"]["class_count_before"],
                          out["refund"]["class_count_after"]), (p["id"], 10, 5))
        self.assertEqual(out["package"]["summary"]["rate"], 100.0)
        undone = self.store.delete_refund(rid, changed_by="Matt")
        self.assertEqual(undone["expense"].amount, 1000.0)
        self.assertTrue(undone["package"]["restored"])
        self.assertEqual(undone["package"]["class_count"], 10)
        s = self.store.package(p["id"])["summary"]
        self.assertEqual((s["class_count"], s["rate"], s["attended"], s["remaining"]),
                         (10, 100.0, 2, 8))
        # both halves of the undo are on the trail, with the author
        trail = self.store.history(e.id)
        self.assertEqual([h.action for h in trail][-2:], ["package_update", "refund_delete"])
        self.assertEqual(trail[-2].snapshot["changed"], {"class_count": 10})
        self.assertEqual(trail[-2].changed_by, "Matt")
        self.assertEqual(trail[-1].snapshot["package"]["restored"], True)
        self.assertEqual(trail[-1].snapshot["refund"]["class_count_before"], 10)

    def test_the_undo_leaves_a_count_that_was_changed_since_the_refund(self):
        """A later classes_update is a later decision; the undo must not
        clobber it — and must say so."""
        e = self.paid(amount=1000)
        p = self.pack(e, count=10)
        rid = self.store.refund(e.id, amount=500, resize_package_to=5)["refund"]["id"]
        self.store.update_package(p["id"], fields={"class_count": 7})
        undone = self.store.delete_refund(rid)
        self.assertFalse(undone["package"]["restored"])
        self.assertEqual(undone["package"]["class_count"], 7)
        self.assertIn("changed to 7", undone["package"]["reason"])
        self.assertEqual(self.store.package(p["id"])["class_count"], 7)
        self.assertNotIn("package_update",
                         [h.action for h in self.store.history(e.id)][-1:])
        # a second refund that resized again is the same situation
        rid2 = self.store.refund(e.id, amount=100, resize_package_to=4)["refund"]["id"]
        self.store.refund(e.id, amount=100, resize_package_to=3)
        self.assertFalse(self.store.delete_refund(rid2)["package"]["restored"])
        self.assertEqual(self.store.package(p["id"])["class_count"], 3)

    def test_reverting_an_upward_resize_runs_under_the_shrink_rule(self):
        """A refund can resize UP; undoing that is a shrink. If classes were
        logged beyond the old count meanwhile, the count stays and the reason
        is the shrink rule's own words."""
        e = self.paid(amount=1000)
        p = self.pack(e, count=3)
        rid = self.store.refund(e.id, amount=100, resize_package_to=6)["refund"]["id"]
        self.store.log_class(package_id=p["id"], kind="attended",
                             dates=["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"])
        undone = self.store.delete_refund(rid)
        self.assertFalse(undone["package"]["restored"])
        self.assertIn("classes_log_delete", undone["package"]["reason"])
        self.assertEqual(self.store.package(p["id"])["class_count"], 6)
        self.assertEqual(self.store.list()[0].amount, 1000.0)   # the money half still undone
        # …and without the extra classes it goes back down
        e2 = self.paid(amount=1000, description="other")
        p2 = self.pack(e2, count=3, name="other")
        rid2 = self.store.refund(e2.id, amount=100, resize_package_to=6)["refund"]["id"]
        self.assertTrue(self.store.delete_refund(rid2)["package"]["restored"])
        self.assertEqual(self.store.package(p2["id"])["class_count"], 3)

    def test_a_refund_that_resized_to_the_same_count_has_nothing_to_restore(self):
        e = self.paid(amount=1000)
        p = self.pack(e, count=10)
        rid = self.store.refund(e.id, amount=100, resize_package_to=10)["refund"]["id"]
        undone = self.store.delete_refund(rid)
        self.assertFalse(undone["package"]["restored"])
        self.assertIn("did not change", undone["package"]["reason"])
        self.assertEqual(self.store.package(p["id"])["class_count"], 10)

    def test_deleting_an_expense_snapshots_its_refunds_and_removes_them(self):
        e = self.paid()
        self.store.refund(e.id, amount=100)
        self.assertTrue(self.store.delete(e.id))
        with self.store.db.tx() as tx:
            left = tx.query("SELECT id FROM expense_refunds")
        self.assertEqual(left, [])
        snap = self.store.history(e.id)[-1].snapshot
        self.assertEqual(len(snap["refunds"]), 1)
        self.assertEqual(snap["amount"], 3500.0)

    def test_refunds_ride_on_the_row_oldest_first_with_ids(self):
        e = self.paid()
        self.store.refund(e.id, amount=100, date="2026-09-05", reason=" late ")
        self.store.refund(e.id, amount=50, date="2026-09-01")
        x = self.store.list()[0]
        self.assertEqual([r["date"] for r in x.refunds], ["2026-09-01", "2026-09-05"])
        self.assertEqual(x.refunds[1]["reason"], "late")
        self.assertTrue(all(len(r["id"]) == 12 for r in x.refunds))
        # a row nobody refunded carries an empty list and reads exactly as before
        other = self.paid(description="other")
        fresh = [r for r in self.store.list() if r.id == other.id][0]
        self.assertEqual((fresh.refunds, fresh.refunded, fresh.gross_amount),
                         ([], 0.0, 3600.0))

    def test_the_refund_date_defaults_to_today_and_is_validated(self):
        e = self.paid()
        r = self.store.refund(e.id, amount=1)["refund"]
        self.assertRegex(r["date"], r"^\d{4}-\d{2}-\d{2}$")
        with self.assertRaises(ValidationError):
            self.store.refund(e.id, amount=1, date="2026-02-30")
        with self.assertRaises(ValidationError):
            self.store.refund(e.id, amount="三百")

    def test_a_borrow_row_cannot_be_refunded_on_either_surface(self):
        """The portal hides the button; the store must refuse too, or the two
        surfaces disagree about one event and the MCP can rewrite what she is
        owed back (docs/BACKLOG.md §11 is the real gap)."""
        e = self.store.create(date="2026-08-01", amount=500, category="borrow",
                              paid=True, paid_date="2026-08-02")
        with self.assertRaises(ValidationError) as ctx:
            self.store.refund(e.id, amount=100)
        self.assertIn("BACKLOG.md §11", str(ctx.exception))
        self.assertIn("expenses_mark_paid", str(ctx.exception))
        s = self.store.summary(today="2026-08-11")
        self.assertEqual((s["total"], s["borrow_repaid"]), (0.0, 500.0))

    def test_a_refunded_row_cannot_go_back_to_unpaid(self):
        """The mirror of the unpaid-row rule. Without it, 取消已付 on a ¥3,600
        row refunded ¥1,800 put ¥1,800 on the 待付 card for a bill nothing had
        moved on. Found by the semantic review."""
        e = self.paid()
        self.store.refund(e.id, amount=1800)
        with self.assertRaises(ValidationError) as ctx:
            self.store.mark_paid(e.id, paid=False)
        self.assertIn("expenses_refund_delete", str(ctx.exception))
        s = self.store.summary(today="2026-09-05")
        self.assertEqual((s["unpaid"], s["due_now"], s["paid"]), (0.0, 0.0, 1800.0))
        self.assertTrue(self.store.list()[0].paid)
        # re-marking paid (a no-op on a paid row) is still allowed
        self.assertTrue(self.store.mark_paid(e.id, paid=True, paid_date="2026-08-15").paid)
        # …and once the refund is gone, unpaying works again
        self.store.delete_refund(self.store.list()[0].refunds[0]["id"])
        self.assertFalse(self.store.mark_paid(e.id, paid=False).paid)

    def test_the_refunds_listed_on_a_row_always_sum_to_its_refunded_figure(self):
        """Refunds are rounded at the write. Stored raw, two ¥100.005 refunds
        listed as ¥100.00 each under a ¥200.01 refunded figure — parts that
        did not sum to their whole, on her phone. Found by the structural
        review."""
        e = self.paid(amount=1000)
        for raw in (100.005, 100.005, 33.333, 0.006):
            self.store.refund(e.id, amount=raw)
        x = self.store.list()[0]
        self.assertEqual(round(sum(r["amount"] for r in x.refunds), 2), x.refunded)
        self.assertEqual(x.amount, round(1000 - x.refunded, 2))
        with self.store.db.tx() as tx:
            stored = [float(r["amount"]) for r in tx.query("SELECT amount FROM expense_refunds")]
        self.assertTrue(all(v == round(v, 2) for v in stored), stored)
        with self.assertRaises(ValidationError):
            self.store.refund(e.id, amount=0.004)   # rounds to nothing

    def test_a_refund_returns_the_funded_course_whether_or_not_it_resized(self):
        """The course's figures move either way; the caller has to be able
        to show the reprice rather than leave it silent."""
        e = self.paid()
        p = self.pack(e)
        self.store.log_class(package_id=p["id"], kind="attended",
                             dates=["2026-08-17", "2026-08-21", "2026-08-28",
                                    "2026-08-31", "2026-09-02"])
        plain = self.store.refund(e.id, amount=1800)
        self.assertFalse(plain["resized"])
        self.assertEqual(plain["package"]["summary"]["rate"], 180.0)     # the silent reprice
        self.assertEqual(plain["package"]["summary"]["remaining"], 5)
        self.assertNotIn("package", self.store.history(e.id)[-1].snapshot)
        self.store.delete_refund(plain["refund"]["id"])
        sized = self.store.refund(e.id, amount=1800, resize_package_to=5)
        self.assertTrue(sized["resized"])
        self.assertEqual(sized["package"]["summary"]["rate"], 360.0)
        self.assertIn("package", self.store.history(e.id)[-1].snapshot)
        alone = self.store.refund(self.paid(description="no course").id, amount=1)
        self.assertIsNone(alone["package"])

    def test_a_settled_term_fee_has_one_consistent_end_state(self):
        """¥2,000 for 8, three cancelled, the school refunds the ¥750 it owed.
        owed_amount is derived from the missed EVENTS, so no resize alone can
        express settlement — the natural one left ¥750 owed, a smaller one
        raised it. The consistent state removes the settled classes and
        resizes to what remains; the tool's note now says so."""
        e = self.paid(amount=2000, description="游泳课 秋季")
        p = self.pack(e, count=8, name="游泳课", kind="period")
        cancelled = self.store.log_class(package_id=p["id"], kind="missed_school",
                                         dates=["2026-09-01", "2026-09-08", "2026-09-15"])
        self.assertEqual(self.store.package(p["id"])["summary"]["owed_amount"], 750.0)
        out = self.store.refund(e.id, amount=750, resize_package_to=5)
        s = out["package"]["summary"]
        self.assertEqual((s["rate"], s["owed_amount"]), (250.0, 750.0))   # still claimed
        for ev in cancelled["logged_events"]:
            self.store.delete_class_event(ev["id"])
        s = self.store.package(p["id"])["summary"]
        self.assertEqual((s["class_count"], s["rate"], s["owed"], s["owed_amount"]),
                         (5, 250.0, 0, 0.0))
        self.assertEqual(self.store.list()[0].amount, 1250.0)

    def test_refunds_sweep_amounts_that_do_not_divide_with_classes_attended(self):
        """The earlier sweep logged no class, so used_amount was 0 and the
        reconciliation was 0 + total == total (semantic review). Every n in
        1..count−1 goes through the exact ratio on a refunded net."""
        for gross, count, back in ((3333.33, 7, 1000.01), (1999.99, 3, 0.01),
                                   (777.77, 9, 77.7), (1000.124, 3, 500.05)):
            for attended in range(1, count):
                with self.subTest(gross=gross, count=count, back=back, attended=attended):
                    store = make_store()
                    e = store.create(date="2026-08-01", amount=gross, paid=True,
                                     paid_date="2026-08-01", description="x")
                    p = store.create_package(expense_id=e.id, name="c", kind="per_class",
                                             class_count=count)
                    store.log_class(package_id=p["id"], kind="attended",
                                    dates=[f"2026-08-{d:02d}" for d in range(2, 2 + attended)])
                    net = store.refund(e.id, amount=back)["expense"].amount
                    s = store.package(p["id"])["summary"]
                    self.assertEqual(s["amount"], net)
                    self.assertEqual(s["used_amount"], round(net * attended / count, 2))
                    self.assertEqual(s["remaining_amount"], round(net - s["used_amount"], 2))
                    self.assertEqual(round(s["used_amount"] + s["remaining_amount"], 2), net)
                    self.assertLessEqual(s["used_amount"], net)

    def test_refund_and_resize_on_figures_that_do_not_divide(self):
        e = self.paid(amount=3333.33)
        p = self.pack(e, count=7)
        self.store.log_class(package_id=p["id"], kind="attended",
                             dates=["2026-08-17", "2026-08-21", "2026-08-28"])
        out = self.store.refund(e.id, amount=1000.01, resize_package_to=5)
        s = out["package"]["summary"]
        self.assertEqual(out["expense"].amount, 2333.32)
        self.assertEqual(s["rate"], 466.66)
        self.assertEqual(s["used_amount"], round(2333.32 * 3 / 5, 2))
        self.assertEqual(s["remaining_amount"], round(2333.32 - s["used_amount"], 2))
        self.assertEqual((s["used"], s["remaining"]), (3, 2))

    def test_a_refund_can_resize_an_archived_course(self):
        e = self.paid()
        p = self.pack(e)
        self.store.update_package(p["id"], fields={"archived": True})
        out = self.store.refund(e.id, amount=1800, resize_package_to=5)
        self.assertTrue(out["package"]["archived"])
        self.assertEqual(out["package"]["summary"]["rate"], 360.0)
        self.assertTrue(self.store.packages_by_expense()[e.id]["archived"])


class ClassAuditTests(unittest.TestCase):
    """docs/BACKLOG.md §3: the tracker wrote no history at all."""

    def setUp(self):
        self.store = make_store()
        self.expense = self.store.create(
            date="2026-08-03", amount=2200, description="足球课",
            category="aden-sports", paid=True, paid_date="2026-08-03",
        )

    def actions(self):
        return [h.action for h in self.store.history(self.expense.id)]

    def test_every_course_mutation_writes_one_history_row_under_the_payment(self):
        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10, changed_by="wife")
        self.assertEqual(self.actions(), ["create", "package_create"])
        self.store.update_package(p["id"], fields={"name": "Football"}, changed_by="Matt")
        self.assertEqual(self.actions()[-1], "package_update")
        self.store.log_class(package_id=p["id"], kind="attended", logged_by="wife")
        self.assertEqual(self.actions()[-1], "class_log")
        batch = self.store.log_class(package_id=p["id"], kind="attended",
                                     dates=["2026-08-10", "2026-08-12", "2026-08-14"])
        self.assertEqual(self.actions()[-2:], ["class_log", "class_log"],
                         "a batch is ONE mutation, one row")
        self.assertEqual(len(self.store.history(self.expense.id)[-1].snapshot["events"]), 3)
        self.store.delete_class_event(batch["logged_events"][0]["id"], changed_by="wife")
        self.assertEqual(self.actions()[-1], "class_unlog")
        self.store.delete_package(p["id"], changed_by="Matt")
        self.assertEqual(self.actions()[-1], "package_delete")
        by = [h.changed_by for h in self.store.history(self.expense.id)]
        self.assertEqual(by[1], "wife")
        self.assertEqual(by[-1], "Matt")

    def test_deleting_a_course_keeps_every_class_in_history(self):
        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10)
        self.store.log_class(package_id=p["id"], kind="attended",
                             dates=["2026-08-05", "2026-08-12", "2026-08-19"], note="rain")
        self.store.delete_package(p["id"])
        snap = self.store.history(self.expense.id)[-1].snapshot
        self.assertEqual(sorted(e["date"] for e in snap["events"]),
                         ["2026-08-05", "2026-08-12", "2026-08-19"])
        self.assertEqual(snap["events"][0]["note"], "rain")
        self.assertEqual(snap["summary"]["used"], 3)
        with self.store.db.tx() as tx:
            self.assertEqual(tx.query("SELECT id FROM class_events"), [])
        # and the payment is untouched
        self.assertEqual(self.store.list()[0].amount, 2200.0)

    def test_shrinking_below_the_logged_classes_is_refused_and_names_them(self):
        """The regression test the prompt asked for."""
        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10)
        logged = self.store.log_class(package_id=p["id"], kind="attended",
                                      dates=["2026-08-05", "2026-08-12", "2026-08-19"])
        third = [e for e in logged["logged_events"] if e["date"] == "2026-08-19"][0]
        with self.assertRaises(ValidationError) as ctx:
            self.store.update_package(p["id"], fields={"class_count": 2})
        text = str(ctx.exception)
        self.assertIn("2026-08-19", text)
        self.assertIn(third["id"], text)
        self.assertNotIn("2026-08-05", text, "it named a class within the count")
        self.assertIn("classes_log_delete", text)
        self.assertEqual(self.store.package(p["id"])["class_count"], 10)
        self.assertNotIn("package_update", self.actions())
        # exactly the logged count passes — the refund case
        self.assertEqual(
            self.store.update_package(p["id"], fields={"class_count": 3})["summary"]["remaining"],
            0)

    def test_a_period_fee_counts_missed_classes_for_the_shrink_rule(self):
        p = self.store.create_package(expense_id=self.expense.id, name="游泳课",
                                      kind="period", class_count=8)
        self.store.log_class(package_id=p["id"], kind="attended",
                             dates=["2026-08-01", "2026-08-02", "2026-08-03"])
        self.store.log_class(package_id=p["id"], kind="missed_school",
                             dates=["2026-08-04", "2026-08-05"])
        # attended classes do not bind a period fee; the two misses do
        self.assertEqual(self.store.update_package(p["id"], fields={"class_count": 2})
                         ["class_count"], 2)
        with self.assertRaises(ValidationError) as ctx:
            self.store.update_package(p["id"], fields={"class_count": 1})
        self.assertIn("missed", str(ctx.exception))

    def test_a_batch_log_is_all_or_nothing(self):
        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10)
        with self.assertRaises(ValidationError):
            self.store.log_class(package_id=p["id"], kind="attended",
                                 dates=["2026-08-05", "2026-08-12", "2026-13-01"])
        self.assertEqual(self.store.package(p["id"])["events"], [])
        self.assertNotIn("class_log", self.actions())

    def test_a_batch_log_tolerates_a_spoken_list_and_refuses_both_forms_at_once(self):
        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10)
        out = self.store.log_class(package_id=p["id"], kind="attended",
                                   dates="2026-08-05, 2026-08-12，2026-08-19")
        self.assertEqual(len(out["logged_events"]), 3)
        with self.assertRaises(ValidationError):
            self.store.log_class(package_id=p["id"], kind="attended",
                                 date="2026-08-20", dates=["2026-08-21"])
        with self.assertRaises(ValidationError):
            self.store.log_class(package_id=p["id"], kind="attended", dates=42)

    def test_an_empty_or_repeating_batch_logs_nothing(self):
        """`dates=[]` used to log a class TODAY — a money-moving write from a
        request that named no class (structural review). And a batch is not
        the easy way to log one class twice (docs/BACKLOG.md §6)."""
        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10)
        for bad in ([], "", "2026-08-05, 2026-08-05", ["2026-08-05", "2026-08-05"]):
            with self.subTest(dates=bad):
                with self.assertRaises(ValidationError) as ctx:
                    self.store.log_class(package_id=p["id"], kind="attended", dates=bad)
                self.assertIn("dates", str(ctx.exception))
        self.assertEqual(self.store.package(p["id"])["events"], [])
        # omitting the parameter still means today
        self.assertEqual(len(self.store.log_class(package_id=p["id"], kind="attended")
                             ["logged_events"]), 1)

    def test_a_batch_is_bounded_like_a_class_count(self):
        """50,000 distinct dates wrote 50,000 rows in under a second (the
        final verifier measured it). Same ceiling as class_count."""
        from datetime import date, timedelta

        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10)
        days = [(date(2020, 1, 1) + timedelta(days=i)).isoformat() for i in range(1001)]
        with self.assertRaises(ValidationError) as ctx:
            self.store.log_class(package_id=p["id"], kind="attended", dates=days)
        self.assertIn("1001", str(ctx.exception))
        self.assertEqual(self.store.package(p["id"])["events"], [])
        # exactly the ceiling is allowed
        out = self.store.log_class(package_id=p["id"], kind="attended", dates=days[:1000])
        self.assertEqual(len(out["logged_events"]), 1000)

    def test_packages_by_expense_carries_the_counts_the_refund_box_needs(self):
        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10)
        self.store.log_class(package_id=p["id"], kind="attended",
                             dates=["2026-08-05", "2026-08-12"])
        self.store.log_class(package_id=p["id"], kind="missed_us")
        by = self.store.packages_by_expense()[self.expense.id]
        self.assertEqual((by["attended"], by["missed"]), (2, 1))

    def test_class_logging_on_an_unmigrated_database_refuses_in_her_language(self):
        """The fail-closed radius includes her daily class logging, which
        worked before this release; the string reaches her phone as a toast
        (structural review)."""
        path = old_database()
        db = Database(f"sqlite:///{path}")
        db._migrate_history_actions = lambda: None
        db.init()
        store = Store(db)
        # the package is planted directly: create_package itself would be
        # refused by the same guard, which is not the path under test
        with db.tx() as tx:
            tx.execute("INSERT INTO class_packages (id, expense_id, name, kind, class_count, "
                       "period_label, archived, created_at, updated_at) VALUES "
                       "('p1', 'e1', 'c', 'per_class', 10, NULL, 0, 't', 't')")
        with self.assertRaises(ValidationError) as ctx:
            store.log_class(package_id="p1", kind="attended", date="2026-09-01")
        text = str(ctx.exception)
        self.assertTrue(text.startswith("记录暂时保存不了"), text)
        self.assertIn("请告诉 Matt", text)
        self.assertIn("RUNBOOK", text)      # the operator detail follows
        self.assertEqual(store.package("p1")["events"], [])

    def test_unlogging_returns_the_course_and_records_the_event(self):
        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10)
        ev = self.store.log_class(package_id=p["id"], kind="attended",
                                  date="2026-08-05")["logged_events"][0]
        out = self.store.delete_class_event(ev["id"])
        self.assertEqual(out["summary"]["remaining"], 10)
        self.assertEqual(out["unlogged_event"]["date"], "2026-08-05")
        self.assertEqual(self.store.history(self.expense.id)[-1].snapshot["events"][0]["id"],
                         ev["id"])
        self.assertIsNone(self.store.delete_class_event(ev["id"]))

    def test_archiving_is_reachable_and_reversible(self):
        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10)
        self.store.update_package(p["id"], fields={"archived": True})
        self.assertEqual(self.store.list_packages(), [])
        self.assertEqual(len(self.store.list_packages(include_archived=True)), 1)
        self.store.update_package(p["id"], fields={"archived": False})
        self.assertEqual(len(self.store.list_packages()), 1)

    def test_list_packages_query_matches_name_label_and_payment(self):
        self.store.create_package(expense_id=self.expense.id, name="Football",
                                  kind="period", class_count=8, period_label="秋季")
        other = self.store.create(date="2026-08-04", amount=1000, description="Badminton")
        self.store.create_package(expense_id=other.id, name="羽毛球", kind="per_class",
                                  class_count=5)
        names = lambda q: sorted(p["name"] for p in self.store.list_packages(query=q))
        self.assertEqual(names("foot"), ["Football"])
        self.assertEqual(names("秋季"), ["Football"])
        self.assertEqual(names("badminton"), ["羽毛球"])
        self.assertEqual(names("足球"), ["Football"])   # the payment's description
        self.assertEqual(names("nope"), [])

    def test_find_matches_a_course_name_only_when_asked(self):
        """The read tool widens; the write tools that resolve a single match
        do not (docs/BACKLOG.md §5 explains why)."""
        self.store.create_package(expense_id=self.expense.id, name="Football",
                                  kind="per_class", class_count=10, period_label="秋季")
        self.assertEqual([e.id for e in self.store.find("football", match_package=True)],
                         [self.expense.id])
        self.assertEqual([e.id for e in self.store.find("秋季", match_package=True)],
                         [self.expense.id])
        self.assertEqual(self.store.find("football"), [])
        # the join cannot duplicate a row, and status still applies through it
        self.assertEqual(len(self.store.find("足球", match_package=True)), 1)
        self.assertEqual(self.store.find("football", match_package=True, status="unpaid"), [])

    def test_packages_by_expense_maps_each_payment_to_its_course(self):
        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10)
        self.store.update_package(p["id"], fields={"archived": True})
        by = self.store.packages_by_expense()
        self.assertEqual(by[self.expense.id]["name"], "足球课")
        self.assertEqual(by[self.expense.id]["class_count"], 10)
        self.assertTrue(by[self.expense.id]["archived"], "archived courses still own their payment")

    def test_the_delete_refusal_names_classes_delete_and_the_package_id(self):
        p = self.store.create_package(expense_id=self.expense.id, name="足球课",
                                      kind="per_class", class_count=10)
        with self.assertRaises(ValidationError) as ctx:
            self.store.delete(self.expense.id)
        text = str(ctx.exception)
        self.assertIn(f"classes_delete(package_id={p['id']!r})", text)
        self.assertNotIn("Classes tab", text, "it still points the agent at the portal")


class HistoryActionsTests(unittest.TestCase):
    """The CHECK on expense_history.action is the first constraint this
    project has changed on a live table. Three things must hold: the DDL and
    the code agree, an old database gets widened, and a database that was
    not widened refuses rather than half-writes."""

    def test_schema_lists_exactly_the_actions_the_store_can_write(self):
        ddl = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        table = ddl.index("CREATE TABLE IF NOT EXISTS expense_history")
        start = ddl.index("action IN (", table)
        end = ddl.index(")", start)
        listed = re.findall(r"'([a-z_]+)'", ddl[start:end])
        self.assertEqual(listed, list(HISTORY_ACTIONS))
        self.assertEqual(len(listed), 12, "the list changed; update the migration docs")

    def test_the_migration_widens_an_old_database_and_keeps_its_rows(self):
        path = old_database()
        db = Database(f"sqlite:///{path}")
        self.assertEqual(
            db.history_actions_missing(),
            [a for a in HISTORY_ACTIONS
             if a not in ("create", "update", "mark_paid", "unmark_paid", "delete")])
        db.init()
        self.assertEqual(db.history_actions_missing(), [])
        store = Store(db)
        self.assertEqual([(h.seq, h.action) for h in store.history("e1")], [(0, "create")])
        out = store.refund("e1", amount=100, date="2026-09-05")
        self.assertEqual(out["expense"].amount, 3500.0)
        self.assertEqual([h.action for h in store.history("e1")], ["create", "refund"])
        # the indexes the rebuild had to recreate are back, uniqueness included
        with db.tx() as tx:
            names = {r["name"] for r in tx.query(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'expense_history'")}
        self.assertIn("idx_expense_history_expense", names)
        self.assertIn("uq_expense_history_expense_seq", names)
        # and running init again changes nothing
        db.init()
        self.assertEqual(db.history_actions_missing(), [])
        self.assertEqual(len(store.history("e1")), 2)

    def test_an_unmigrated_database_refuses_a_refund_without_writing(self):
        path = old_database()
        db = Database(f"sqlite:///{path}")
        db._migrate_history_actions = lambda: None      # the migration did not run
        db.init()                                         # …but the rest of the schema did
        store = Store(db)
        with self.assertRaises(ValidationError) as ctx:
            store.refund("e1", amount=100)
        self.assertIn("migration", str(ctx.exception))
        self.assertIn("RUNBOOK", str(ctx.exception))
        with db.tx() as tx:
            self.assertEqual(tx.query("SELECT id FROM expense_refunds"), [])
        self.assertEqual(store.list()[0].amount, 3600.0)

    def test_the_refund_columns_are_added_to_a_table_that_shipped_without_them(self):
        """v0.13.0's expense_refunds had seven columns; v0.13.1 needs three
        more, and CREATE TABLE IF NOT EXISTS cannot add them."""
        path = old_database()
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE expense_refunds (id TEXT PRIMARY KEY, expense_id TEXT NOT NULL "
            "REFERENCES expenses(id) ON DELETE CASCADE, amount REAL NOT NULL CHECK (amount > 0), "
            "date TEXT NOT NULL, reason TEXT, changed_by TEXT, created_at TEXT NOT NULL);"
            "INSERT INTO expense_refunds VALUES ('r0', 'e1', 100, '2026-09-01', NULL, NULL, 't');"
        )
        conn.close()
        db = Database(f"sqlite:///{path}")
        self.assertEqual(db.refund_columns_missing(),
                         ["package_id", "class_count_before", "class_count_after"])
        db.init()
        self.assertEqual(db.refund_columns_missing(), [])
        db.init()   # idempotent
        store = Store(db)
        row = store.list()[0]
        self.assertEqual(row.refunds[0]["package_id"], None)   # the old row reads as unresized
        self.assertEqual(row.amount, 3500.0)
        # and a resize through the migrated table records both counts
        p = store.create_package(expense_id="e1", name="c", kind="per_class", class_count=10)
        out = store.refund("e1", amount=100, resize_package_to=5)
        self.assertEqual((out["refund"]["class_count_before"], out["refund"]["class_count_after"]),
                         (10, 5))
        self.assertTrue(store.delete_refund(out["refund"]["id"])["package"]["restored"])
        self.assertEqual(store.package(p["id"])["class_count"], 10)

    def test_write_history_refuses_an_action_it_does_not_know(self):
        store = make_store()
        e = store.create(date="2026-08-01", amount=1)
        with store.db.tx() as tx:
            with self.assertRaises(ValueError):
                store._write_history(tx, e.id, "retcon", None, {})

    def test_a_seq_collision_reads_as_a_refusal_not_a_driver_error(self):
        """Two writes to one payment in the same instant collide on
        uq_expense_history_expense_seq. With the class tracker now writing
        under the funding payment, that is reachable from her phone."""
        store = make_store()
        e = store.create(date="2026-08-01", amount=1)
        with store.db.tx() as tx:      # history has seq 0; plant seq 2 so COUNT(*) → 2
            tx.execute(
                "INSERT INTO expense_history (id, expense_id, seq, action, changed_by, "
                "changed_at, snapshot) VALUES ('x', :e, 2, 'update', NULL, 't', '{}')",
                {"e": e.id})
        with self.assertRaises(ValidationError) as ctx:
            store.update(e.id, fields={"description": "y"})
        self.assertIn("Reload", str(ctx.exception))
        self.assertIsNone(store.list()[0].description, "the update half-applied")


if __name__ == "__main__":
    unittest.main()
