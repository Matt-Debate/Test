"""Store test suite — runs entirely on sqlite (no DB server), stdlib-only.

Run:  python3 -m unittest discover -s tests -v   (or pytest)
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database  # noqa: E402
from math import isfinite  # noqa: E402

from app.store import Store, ValidationError  # noqa: E402


def make_store() -> Store:
    db = Database("sqlite:///:memory:")
    db.init()
    return Store(db)


class CreateAndReadTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_create_and_list(self):
        exp = self.store.create(
            date="2026-07-14", amount=125.5, description="课外书",
            category="Kids", submitted_by="Wei",
        )
        self.assertEqual(exp.currency, "CNY")
        self.assertFalse(exp.paid)
        listed = self.store.list()
        self.assertEqual([e.id for e in listed], [exp.id])
        self.assertEqual(listed[0].description, "课外书")

    def test_list_filters_and_order(self):
        a = self.store.create(date="2026-07-01", amount=10)
        b = self.store.create(date="2026-07-10", amount=20)
        self.store.mark_paid(a.id, paid=True, paid_date="2026-07-02")
        self.assertEqual([e.id for e in self.store.list()], [b.id, a.id])  # newest first
        self.assertEqual([e.id for e in self.store.list(status="paid")], [a.id])
        self.assertEqual([e.id for e in self.store.list(status="unpaid")], [b.id])
        self.assertEqual([e.id for e in self.store.list(since="2026-07-05")], [b.id])
        self.assertEqual([e.id for e in self.store.list(until="2026-07-05")], [a.id])
        with self.assertRaises(ValidationError):
            self.store.list(status="bogus")

    def test_summary(self):
        a = self.store.create(date="2026-07-01", amount=10.25)
        self.store.create(date="2026-07-02", amount=5.75)
        self.store.mark_paid(a.id, paid=True, paid_date="2026-07-03")
        s = self.store.summary(today="2026-07-31")
        self.assertEqual(s, {
            "count": 2, "total": 16.0, "paid": 10.25,
            "unpaid": 5.75, "unpaid_count": 1,
            "due_now": 5.75, "due_now_count": 1,
            "upcoming": 0.0, "upcoming_count": 0,
            "borrow_owed": 0.0, "borrow_owed_count": 0, "borrow_repaid": 0.0,
        })

    def test_summary_splits_due_from_upcoming(self):
        """Recurring costs are entered months ahead; a bare unpaid total would
        report a year of rent as owed today."""
        self.store.create(date="2026-07-02", amount=245, category="utilities")
        self.store.create(date="2026-08-10", amount=22000, category="living")
        s = self.store.summary(today="2026-07-31")
        self.assertEqual(s["due_now"], 245.0)
        self.assertEqual(s["upcoming"], 22000.0)
        self.assertEqual(s["unpaid"], 22245.0)  # both, for anything wanting the whole

    def test_upcoming_is_a_window_not_everything_future(self):
        """A year of living payments loaded in advance must not pile into the
        'upcoming' card — it answers 'what is coming', not 'what exists'."""
        self.store.create(date="2026-08-05", amount=100, category="food")     # +5d
        self.store.create(date="2026-08-29", amount=200, category="food")     # +29d
        self.store.create(date="2026-09-30", amount=22000, category="living")  # +61d
        s = self.store.summary(today="2026-07-31")
        self.assertEqual(s["upcoming"], 300.0)
        self.assertEqual(s["upcoming_count"], 2)
        # still counted as owed overall — it is real money, just not imminent
        self.assertEqual(s["unpaid"], 22300.0)

    def test_summary_keeps_borrow_out_of_every_expense_figure(self):
        """She fronted the money — repaying her is not household spending."""
        self.store.create(date="2026-07-02", amount=800, category="borrow")
        repaid = self.store.create(date="2026-07-01", amount=500, category="borrow")
        self.store.mark_paid(repaid.id, paid=True, paid_date="2026-07-05")
        spent = self.store.create(date="2026-07-01", amount=300, category="aden-sports")
        self.store.mark_paid(spent.id, paid=True, paid_date="2026-07-01")
        s = self.store.summary(today="2026-07-31")
        self.assertEqual(s["paid"], 300.0)      # NOT 800
        self.assertEqual(s["unpaid"], 0.0)      # the outstanding borrow is not an expense
        self.assertEqual(s["due_now"], 0.0)
        self.assertEqual(s["borrow_owed"], 800.0)
        self.assertEqual(s["borrow_repaid"], 500.0)
        # "every figure" has to include `total`, and for four releases it did
        # not: this read 1600 — the ¥300 actually spent plus ¥1,300 of loan —
        # under the one key an agent asked "how much did we spend" reaches for.
        self.assertEqual(s["total"], 300.0)
        # …and `count` is deliberately different: a row count, not a total
        self.assertEqual(s["count"], 3)

    def test_a_row_is_summed_with_the_cents_it_displays(self):
        """`money()` shows two decimals, so a row of 1000.124 reads ¥1,000.12.
        Summed raw, four of them put ¥4,000.50 under ¥4,000.48 of visible
        items — a total that does not add up on screen, which is the one thing
        a ledger must never do.

        This is what the cent-rounding in summarize() buys. The
        total == paid + unpaid invariant below is guarded twice (rounding AND
        deriving), so neither mutation kills it alone; this one has only the
        rounding behind it.
        """
        for i in range(4):
            self.store.create(date="2026-07-0%d" % (i + 1), amount="1000.124",
                              category="living")
        s = self.store.summary(today="2026-07-31")
        displayed = sum(round(e.amount, 2) for e in self.store.list())
        self.assertEqual(s["total"], round(displayed, 2))
        self.assertEqual(s["total"], 4000.48)

    def test_total_is_always_paid_plus_unpaid(self):
        """The invariant that makes the bucket unambiguous, swept rather than
        asserted at one point (LESSONS §10).

        The first version of this test used 7.77 / 13.13 / 1039.31 under a
        docstring claiming "primes and thirds". They are exact two-decimal
        values, which is the one class where `round(fsum(...), 2)` is an
        identity — so the invariant held for reasons unrelated to the code and
        the test could not fail. `_validate_amount` accepts a third decimal and
        never rounds; four rows of 1000.124 gave total ¥4000.50 against
        paid + unpaid ¥4000.49. These amounts have third decimals now, and
        1000.124 is the exact case that broke it.
        """
        from itertools import product

        for i, (amount, category, paid) in enumerate(product(
            (100.035, 33.333, 1000.124, 2.225), ("living", "borrow", None),
            (True, False),
        )):
            self.store.create(
                date="2026-07-%02d" % (i + 1), amount=amount, category=category,
                paid=paid, paid_date="2026-07-05" if paid else None,
            )
            s = self.store.summary(today="2026-07-31")
            self.assertEqual(s["total"], round(s["paid"] + s["unpaid"], 2),
                             f"after {i + 1} rows")

    def test_summary_counts_uncategorised_rows(self):
        """`category <> 'borrow'` is NULL for a NULL category — a naive filter
        would silently drop every uncategorised expense from the totals."""
        self.store.create(date="2026-07-02", amount=42)  # no category at all
        s = self.store.summary(today="2026-07-31")
        self.assertEqual(s["unpaid"], 42.0)
        self.assertEqual(s["due_now"], 42.0)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_amount_must_be_positive_number(self):
        for bad in (0, -5, "abc", None):
            with self.assertRaises(ValidationError):
                self.store.create(date="2026-07-14", amount=bad)

    def test_an_infinite_amount_cannot_lock_her_out_of_the_portal(self):
        """`inf > 0` is True, so it used to commit — and JSONResponse
        serialises with allow_nan=False, so EVERY later /api/list returned 500.
        One bad write, and the only way back was database access."""
        for bad in (float("inf"), float("-inf"), float("nan"), "inf", "nan"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.store.create(date="2026-07-14", amount=bad)
        self.assertEqual(self.store.list(), [], "a bad amount was persisted")

    def test_an_absurd_amount_cannot_overflow_the_totals(self):
        """Two rows near the float ceiling make fsum() return inf, and every
        total then serialises to a 500 — the same lockout one layer up."""
        with self.assertRaises(ValidationError):
            self.store.create(date="2026-07-14", amount=1e308)
        self.store.create(date="2026-07-14", amount=999_999_999_999)  # still fine
        self.assertTrue(isfinite(self.store.summary(today="2026-07-14")["total"]))

    def test_a_date_no_calendar_has_is_refused(self):
        """The regex only pinned the SHAPE, so '2026-13-01' was stored happily.

        It is not a tidiness problem, and the two cases fail differently —
        which is why both must be refused here rather than handled downstream.
        '2026-13-01' makes `Date.parse` return NaN, and NaN is neither `<= 30`
        nor `> 30`, so the portal's Due tab rendered it in no section at all —
        v0.12.0's own defect, reachable by writing a bad date through the MCP.
        '2026-02-30' is worse in a quieter way: it normalises to early March
        and renders, sorts and totals as a row due on a day she never entered.
        No renderer can detect the second; only refusing the write can.

        Found by cross-model review.
        """
        for bad in ("2026-13-01", "2026-02-30", "2026-00-10", "2026-01-32"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.store.create(date=bad, amount=10)
        self.assertEqual(self.store.list(), [], "an impossible date was stored")
        # …and the leap-year boundary is not collateral damage
        self.store.create(date="2028-02-29", amount=10)
        with self.assertRaises(ValidationError):
            self.store.create(date="2027-02-29", amount=10)

    def test_paid_date_is_checked_the_same_way(self):
        """It goes through the same validator, and it decides which month a
        payment counts in.

        NOT the NaN hazard — this docstring said so and was wrong. The
        paid-this-month code only slices `"2026-02"` off the front, so an
        impossible day never reaches arithmetic here. It is the quieter
        failure: `2026-02-31` is a payment filed under a day that does not
        exist, and if the month ever rolled (`2026-12-32`) it would land in the
        wrong month's 本月已付 with nothing to show for it.
        """
        with self.assertRaises(ValidationError):
            self.store.create(date="2026-07-14", amount=10,
                              paid=True, paid_date="2026-02-31")
        # …and it was refused BEFORE the insert. Asserting only the raise lets
        # the validation move below the INSERT and still pass, which would
        # leave the bad row in the ledger.
        self.assertEqual(self.store.list(), [], "the row was written anyway")

    def test_date_format(self):
        for bad in ("", None, "14/07/2026", "2026-7-4"):
            with self.assertRaises(ValidationError):
                self.store.create(date=bad, amount=1)

    def test_update_rejects_unknown_fields(self):
        exp = self.store.create(date="2026-07-14", amount=1)
        with self.assertRaises(ValidationError):
            self.store.update(exp.id, fields={"paid": True})  # must use mark_paid
        with self.assertRaises(ValidationError):
            self.store.update(exp.id, fields={})

    def test_update_missing_id_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.store.update("nope", fields={"amount": 2})

    def test_mark_paid_requires_paid_date(self):
        exp = self.store.create(date="2026-07-14", amount=1)
        with self.assertRaises(ValidationError):
            self.store.mark_paid(exp.id, paid=True)  # no date
        got = self.store.mark_paid(exp.id, paid=True, paid_date="2026-07-15")
        self.assertTrue(got.paid)
        got = self.store.mark_paid(exp.id, paid=False)  # unmark clears date
        self.assertFalse(got.paid)
        self.assertIsNone(got.paid_date)

    def test_db_check_rejects_paid_without_date(self):
        # A4: the constraint holds even if application validation is bypassed.
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db.tx() as tx:
                tx.execute(
                    "INSERT INTO expenses (id, date, amount, paid, created_at, updated_at) "
                    "VALUES ('x1', '2026-07-14', 1.0, TRUE, 'now', 'now')"
                )


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_every_mutation_writes_one_history_row(self):
        exp = self.store.create(date="2026-07-14", amount=9, submitted_by="Wei")
        self.store.update(exp.id, fields={"amount": 11}, changed_by="Matt")
        self.store.mark_paid(exp.id, paid=True, paid_date="2026-07-15", changed_by="Matt")
        self.store.mark_paid(exp.id, paid=False, changed_by="Wei")
        self.store.delete(exp.id, changed_by="Matt")
        actions = [h.action for h in self.store.history(exp.id)]
        self.assertEqual(actions, ["create", "update", "mark_paid", "unmark_paid", "delete"])

    def test_snapshots_capture_state(self):
        exp = self.store.create(date="2026-07-14", amount=9)
        self.store.update(exp.id, fields={"amount": 11})
        entries = self.store.history(exp.id)
        self.assertEqual(entries[0].snapshot["amount"], 9)
        self.assertEqual(entries[1].snapshot["amount"], 11)
        self.assertEqual(entries[0].changed_by, None)

    def test_delete_snapshot_is_pre_change_and_survives(self):
        exp = self.store.create(date="2026-07-14", amount=42, description="旧沙发")
        self.assertTrue(self.store.delete(exp.id))
        self.assertEqual(self.store.list(), [])
        entries = self.store.history(exp.id)
        self.assertEqual(entries[-1].action, "delete")
        self.assertEqual(entries[-1].snapshot["description"], "旧沙发")

    def test_delete_missing_returns_false_without_history(self):
        self.assertFalse(self.store.delete("nope"))
        self.assertEqual(self.store.history("nope"), [])


class AtomicityTests(unittest.TestCase):
    """M3: primary write and history row commit together or not at all."""

    def setUp(self):
        self.store = make_store()

    def test_history_failure_rolls_back_create(self):
        original = self.store._write_history

        def boom(*args, **kwargs):
            raise RuntimeError("forced history failure")

        self.store._write_history = boom
        with self.assertRaises(RuntimeError):
            self.store.create(date="2026-07-14", amount=5)
        self.store._write_history = original
        self.assertEqual(self.store.list(), [])          # expense rolled back
        self.assertEqual(self.store.summary()["count"], 0)

    def test_history_failure_rolls_back_update(self):
        exp = self.store.create(date="2026-07-14", amount=5)
        original = self.store._write_history
        self.store._write_history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError)
        with self.assertRaises(RuntimeError):
            self.store.update(exp.id, fields={"amount": 99})
        self.store._write_history = original
        self.assertEqual(self.store.list()[0].amount, 5)  # value unchanged


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_mint_and_validate(self):
        minted = self.store.mint_token(label="wife", expires_days=30)
        self.assertEqual(len(minted["token"]), 64)
        row = self.store.validate_token(minted["token"])
        self.assertIsNotNone(row)
        self.assertEqual(row["label"], "wife")

    def test_default_mint_never_expires(self):
        minted = self.store.mint_token(label="wife")
        self.assertIsNone(minted["expires_at"])
        self.assertIsNotNone(self.store.validate_token(minted["token"]))

    def test_validate_bumps_usage(self):
        minted = self.store.mint_token()
        self.store.validate_token(minted["token"])
        self.store.validate_token(minted["token"])
        tokens = self.store.list_tokens()
        self.assertEqual(tokens[0]["use_count"], 2)
        self.assertIsNotNone(tokens[0]["last_used_at"])

    def test_unknown_and_garbage_tokens_rejected(self):
        self.assertIsNone(self.store.validate_token("deadbeef" * 8))
        self.assertIsNone(self.store.validate_token(""))
        self.assertIsNone(self.store.validate_token(None))
        self.assertIsNone(self.store.validate_token(123))

    def test_revoked_token_rejected(self):
        minted = self.store.mint_token()
        self.assertTrue(self.store.revoke_token(minted["token"]))
        self.assertIsNone(self.store.validate_token(minted["token"]))

    def test_expired_token_rejected(self):
        minted = self.store.mint_token(expires_days=1)
        with self.store.db.tx() as tx:  # force expiry into the past
            tx.execute(
                "UPDATE access_tokens SET expires_at = '2000-01-01T00:00:00' "
                "WHERE token = :t", {"t": minted["token"]},
            )
        self.assertIsNone(self.store.validate_token(minted["token"]))

    def test_expires_days_clamped(self):
        minted = self.store.mint_token(expires_days=99999)
        self.assertIsNotNone(minted["expires_at"])  # bounded when requested


class NaturalInputTests(unittest.TestCase):
    """Spoken/pasted forms the MCP path must tolerate."""

    def setUp(self):
        self.store = make_store()

    def test_amount_accepts_decorated_strings(self):
        for raw, expect in (("¥300", 300.0), ("300块", 300.0),
                            ("1,200元", 1200.0), (" 88.8 rmb ", 88.8)):
            exp = self.store.create(date="2026-07-14", amount=raw)
            self.assertEqual(exp.amount, expect)

    def test_amount_garbage_still_rejected(self):
        for bad in ("三百", "¥", ""):
            with self.assertRaises(ValidationError):
                self.store.create(date="2026-07-14", amount=bad)

    def test_find_matches_chinese_and_english(self):
        a = self.store.create(date="2026-07-01", amount=300, description="足球课")
        b = self.store.create(date="2026-07-02", amount=200, description="Piano lesson")
        self.store.create(date="2026-07-03", amount=50, category="food")
        self.assertEqual([e.id for e in self.store.find("足球")], [a.id])
        self.assertEqual([e.id for e in self.store.find("piano")], [b.id])  # case-insensitive
        self.assertEqual(len(self.store.find("课")), 1)
        self.assertEqual(len(self.store.find("food")), 1)  # category matches too
        self.assertEqual(self.store.find("nothing-like-this"), [])

    def test_find_respects_status_filter(self):
        a = self.store.create(date="2026-07-01", amount=300, description="足球课")
        self.store.create(date="2026-07-02", amount=300, description="足球装备")
        self.store.mark_paid(a.id, paid=True, paid_date="2026-07-02")
        self.assertEqual(len(self.store.find("足球")), 2)
        self.assertEqual(len(self.store.find("足球", status="unpaid")), 1)




class SummaryMatchesTheRowsTests(unittest.TestCase):
    """The aggregate must describe the rows beside it.

    A filtered list under a whole-ledger headline is a wrong number in the most
    visible place on the page: two rows worth ¥5,780 beneath a ¥247,780 total.
    """

    def setUp(self):
        self.store = make_store()
        self.store.create(date="2026-08-20", amount=3800, category="aden-edu")
        self.store.create(date="2026-08-20", amount=1980, category="aden-sports")
        for month in ("09", "10", "11"):
            self.store.create(date=f"2026-{month}-01", amount=22000, category="living")

    def test_summarize_describes_only_the_rows_given(self):
        rows = self.store.list(status="unpaid", since="2026-08-01", until="2026-08-31")
        s = self.store.summarize(rows, today="2026-08-11")
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["total"], 5780.0)
        self.assertEqual(s["unpaid"], 5780.0)
        self.assertEqual(s["unpaid_count"], 2)

    def test_whole_ledger_summary_still_sees_everything(self):
        s = self.store.summary(today="2026-08-11")
        self.assertEqual(s["count"], 5)
        self.assertEqual(s["unpaid"], 71780.0)

    def test_overdue_status_filter(self):
        self.store.create(date="2026-07-01", amount=500, category="utilities")
        overdue = self.store.list(status="overdue")
        self.assertEqual([e.date for e in overdue], ["2026-07-01"])

    def test_invalid_status_coaches_the_caller(self):
        with self.assertRaises(ValidationError) as ctx:
            self.store.list(status="nope")
        self.assertIn("overdue", str(ctx.exception))


class CodexVerifyRegressionTests(unittest.TestCase):
    """Findings from the cross-model review of v0.8.0 — each one a real defect
    the existing suite let through."""

    def setUp(self):
        self.store = make_store()

    def test_find_honours_overdue_like_list_does(self):
        """find() and list() each hand-rolled the status filter and drifted:
        find() silently treated 'overdue' as 'all', so a query search could
        return paid and future rows."""
        old = self.store.create(date="2026-01-01", amount=10, description="tennis")
        paid = self.store.create(date="2026-01-02", amount=20, description="tennis")
        self.store.mark_paid(paid.id, paid=True, paid_date="2026-01-03")
        self.store.create(date="2099-01-01", amount=30, description="tennis")
        found = self.store.find("tennis", status="overdue")
        self.assertEqual([e.id for e in found], [old.id])

    def test_find_rejects_an_unknown_status_instead_of_ignoring_it(self):
        with self.assertRaises(ValidationError):
            self.store.find("anything", status="bogus")

    def test_upcoming_horizon_boundary_is_exactly_30_days(self):
        """The decisive case the first pass never tested: day 30 in, day 31 out."""
        self.store.create(date="2026-08-30", amount=1, category="food")   # +30
        self.store.create(date="2026-08-31", amount=2, category="food")   # +31
        s = self.store.summary(today="2026-07-31")
        self.assertEqual(s["upcoming"], 1.0)
        self.assertEqual(s["upcoming_count"], 1)
        self.assertEqual(s["unpaid"], 3.0)  # the +31 row is still owed, just not imminent

    def test_totals_do_not_drift_with_row_order(self):
        """Plain += is order-dependent in binary floating point; fsum is not."""
        amounts = [0.1, 0.2, 1e9, 0.3, 1e-9, 4.44, 0.7]
        for a in amounts:
            self.store.create(date="2026-08-01", amount=a, category="food")
        forward = self.store.summarize(self.store.list(), today="2026-08-11")
        backward = self.store.summarize(list(reversed(self.store.list())), today="2026-08-11")
        self.assertEqual(forward["unpaid"], backward["unpaid"])
        self.assertEqual(forward["total"], backward["total"])


class HouseholdMidnightTests(unittest.TestCase):
    """The portal extrapolates `today` from elapsed time while a page is open.

    Elapsed time alone rolls the date over 24h after the response rather than
    at midnight, so a page loaded at 23:50 kept offering yesterday for almost a
    whole day. It cannot work that out from a date, so the server sends how
    much of the day is left.
    """

    def test_it_is_a_slice_of_one_day_not_a_whole_one(self):
        from app.store import seconds_until_midnight

        left = seconds_until_midnight()
        self.assertGreater(left, 0)
        self.assertLessEqual(left, 86400)

    def test_it_lands_on_tomorrow_and_not_before(self):
        """The property that matters: adding it to now crosses into the next
        household day, and one second less does not."""
        from datetime import timedelta

        from app.store import _household_now, seconds_until_midnight

        now = _household_now()
        left = seconds_until_midnight()
        self.assertEqual(
            (now + timedelta(seconds=left)).strftime("%Y-%m-%d"),
            (now + timedelta(days=1)).strftime("%Y-%m-%d"),
        )
        self.assertEqual(
            (now + timedelta(seconds=left - 1)).strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"),
        )

    def test_it_is_real_elapsed_time_across_a_DST_change(self):
        """Subtracting two aware datetimes carrying the SAME ZoneInfo does
        WALL-CLOCK arithmetic, which is an hour wrong either side of a DST
        transition. Shanghai has no DST, but APP_TZ is configurable and a
        helper correct in exactly one zone is a trap for whoever changes it.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.store import _seconds_until_midnight_from

        ny = ZoneInfo("America/New_York")
        for label, now, expected in [
            # 2026-03-08: clocks go FORWARD at 02:00, so this day is 23h long
            ("spring forward", datetime(2026, 3, 8, 12, 0, tzinfo=ny), 12 * 3600),
            # 2026-11-01: clocks go BACK at 02:00, so this day is 25h long
            ("fall back", datetime(2026, 11, 1, 12, 0, tzinfo=ny), 12 * 3600),
        ]:
            with self.subTest(label):
                # noon → next midnight is 12h of REAL time on both days: the
                # transition happened before noon in each case
                self.assertEqual(_seconds_until_midnight_from(now), expected)

    def test_it_loses_the_hour_when_the_day_is_shorter(self):
        """The pre-transition window is the only one that exposes wall-clock
        arithmetic — the noon cases above sit AFTER the change and pass either
        way. Spring forward: 23h30m on the clock, 22h30m of real time."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.store import _seconds_until_midnight_from

        ny = ZoneInfo("America/New_York")
        # 2026-03-08 00:30 EST, before the 02:00 spring-forward
        now = datetime(2026, 3, 8, 0, 30, tzinfo=ny)
        self.assertEqual(_seconds_until_midnight_from(now), 22 * 3600 + 1800)

    def test_it_counts_the_extra_hour_when_the_day_is_longer(self):
        """The transition AHEAD of the remaining window is where wall-clock
        arithmetic actually differs — 25 real hours read as 24."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.store import _seconds_until_midnight_from

        ny = ZoneInfo("America/New_York")
        # 2026-11-01 00:30 EDT, before the 02:00 fall-back: 24h30m of real time
        # remain until midnight, not 23h30m
        now = datetime(2026, 11, 1, 0, 30, tzinfo=ny, fold=0)
        self.assertEqual(_seconds_until_midnight_from(now), 24 * 3600 + 1800)

    def test_it_follows_APP_TZ(self):
        """A UTC server answering a China household is the reason any of this
        exists — the remaining seconds must be the household's, not the box's."""
        import os

        from app.store import seconds_until_midnight

        original = os.environ.get("APP_TZ")
        try:
            os.environ["APP_TZ"] = "Asia/Shanghai"
            shanghai = seconds_until_midnight()
            os.environ["APP_TZ"] = "America/New_York"
            new_york = seconds_until_midnight()
        finally:
            if original is None:
                os.environ.pop("APP_TZ", None)
            else:
                os.environ["APP_TZ"] = original
        # the two zones are never on the same clock, so the counts differ
        self.assertNotEqual(shanghai, new_york)


class BacklogRegressionTests(unittest.TestCase):
    """Items filed in docs/BACKLOG.md as known-and-deferred, now closed in v0.9.0.

    Each test was written against the unfixed code first and observed to fail;
    the entries in the backlog say what each defect actually costs.
    """

    def setUp(self):
        self.store = make_store()

    # §4 — find() built its LIKE pattern without escaping
    def test_like_wildcards_in_a_query_are_literal(self):
        """query='%' matched every row, so an agent searching for a literal
        percent sign got the whole ledger back and could act on the wrong one."""
        self.store.create(date="2026-08-01", amount=10, description="足球课")
        self.store.create(date="2026-08-02", amount=20, description="100% cotton")
        self.store.create(date="2026-08-03", amount=30, description="a_b test")
        # a row that genuinely contains a backslash: without it, find("\\")
        # returning [] is satisfied just as well by an escape clause that
        # matches nothing at all
        self.store.create(date="2026-08-04", amount=40, description=r"C:\receipts")
        self.assertEqual(
            [e.description for e in self.store.find("%")], ["100% cotton"]
        )
        self.assertEqual([e.description for e in self.store.find("_")], ["a_b test"])
        self.assertEqual(
            [e.description for e in self.store.find("\\")], [r"C:\receipts"]
        )
        self.assertEqual(
            [e.description for e in self.store.find(r"C:\re")], [r"C:\receipts"]
        )
        # the ordinary case must keep working
        self.assertEqual(len(self.store.find("足球")), 1)
        self.assertEqual(len(self.store.find("")), 4)  # empty query still matches all

    # §4 — a bare KeyError reached MCP callers as just the id
    def test_missing_expense_raises_a_coaching_error(self):
        for call in (
            lambda: self.store.update("nope", fields={"amount": 5}),
            lambda: self.store.mark_paid("nope", paid=True, paid_date="2026-08-01"),
        ):
            with self.assertRaises(KeyError) as ctx:  # api.py maps KeyError → 404
                call()
            message = str(ctx.exception)
            self.assertIn("nope", message)
            self.assertIn("query", message, "error must name the way to retry")
            self.assertNotEqual(message, repr("nope"), "bare KeyError repr leaked")

    # §4 — expenses_add(paid=True) spanned two transactions
    def test_creating_an_already_paid_expense_is_one_transaction(self):
        exp = self.store.create(
            date="2026-08-01", amount=300, description="足球课",
            paid=True, paid_date="2026-08-02", submitted_by="Wei",
        )
        self.assertTrue(exp.paid)
        self.assertEqual(exp.paid_date, "2026-08-02")
        history = self.store.history(exp.id)
        self.assertEqual([h.action for h in history], ["create"])
        self.assertTrue(history[0].snapshot["paid"])

    def test_created_paid_without_a_date_is_paid_today_not_when_due(self):
        """Defaulting to the DUE date would record a payment in the future for
        any bill entered ahead of time, filing it in the wrong month's total."""
        from app.store import today_str

        exp = self.store.create(date="2099-12-01", amount=5, paid=True)
        self.assertEqual(exp.paid_date, today_str())

    def test_only_the_store_decides_the_default_payment_date(self):
        """The MCP used to apply its own today-default on top of the store's,
        so the two could disagree and the store's rule was unreachable."""
        src = (
            Path(__file__).resolve().parent.parent / "app" / "mcp_server.py"
        ).read_text(encoding="utf-8")
        self.assertIn("paid=paid, paid_date=paid_date,", src)
        self.assertNotIn("(paid_date or today_str())", src)

    # §4 — expense_history.seq had no uniqueness constraint
    def test_history_seq_is_unique_per_expense(self):
        exp = self.store.create(date="2026-08-01", amount=10)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db.tx() as tx:
                tx.execute(
                    "INSERT INTO expense_history "
                    "(id, expense_id, seq, action, changed_by, changed_at, snapshot) "
                    "VALUES ('dup', :eid, 0, 'update', NULL, '2026-08-01T00:00:00', '{}')",
                    {"eid": exp.id},
                )

    # §3 — currencies were summed without conversion
    def test_non_cny_currency_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self.store.create(date="2026-08-01", amount=10, currency="USD")
        self.assertIn("CNY", str(ctx.exception))
        exp = self.store.create(date="2026-08-01", amount=10)
        with self.assertRaises(ValidationError):
            self.store.update(exp.id, fields={"currency": "usd"})

    def test_editing_other_fields_never_trips_the_currency_guard(self):
        exp = self.store.create(date="2026-08-01", amount=10, description="x")
        self.assertEqual(
            self.store.update(exp.id, fields={"description": "y"}).currency, "CNY"
        )
        self.assertEqual(self.store.update(exp.id, fields={"currency": "cny"}).currency, "CNY")

    # §4 — APP_TZ was unpinned by any test
    def test_today_str_follows_app_tz(self):
        """Every date assertion elsewhere is a shape-only regex, so a silent
        fallback to UTC — an image without tzdata — passed the whole suite.
        These two zones are 25 hours apart: they can never share a date."""
        import os

        from app.store import today_str

        original = os.environ.get("APP_TZ")
        try:
            os.environ["APP_TZ"] = "Pacific/Kiritimati"   # UTC+14
            east = today_str()
            os.environ["APP_TZ"] = "Pacific/Niue"         # UTC-11
            west = today_str()
        finally:
            if original is None:
                os.environ.pop("APP_TZ", None)
            else:
                os.environ["APP_TZ"] = original
        self.assertNotEqual(east, west, "APP_TZ is being ignored")

    def test_the_households_timezone_data_ships_with_the_image(self):
        from zoneinfo import ZoneInfo

        ZoneInfo("Asia/Shanghai")  # raises if tzdata is missing

    def test_the_default_timezone_is_the_households(self):
        """Nothing else pins the default: switching it to UTC left the suite
        green while putting the family a day behind after 08:00 CST."""
        src = (
            Path(__file__).resolve().parent.parent / "app" / "store.py"
        ).read_text(encoding="utf-8")
        self.assertIn('os.environ.get("APP_TZ", "Asia/Shanghai")', src)

    def test_an_unusable_timezone_falls_back_loudly(self):
        """The fallback is correct for a live service but must not be silent —
        a silent UTC fallback is exactly how this went unnoticed."""
        import io
        import os
        from contextlib import redirect_stderr

        from app.store import today_str

        original = os.environ.get("APP_TZ")
        buf = io.StringIO()
        try:
            os.environ["APP_TZ"] = "Not/AZone"
            with redirect_stderr(buf):
                fallback = today_str()
        finally:
            if original is None:
                os.environ.pop("APP_TZ", None)
            else:
                os.environ["APP_TZ"] = original
        self.assertRegex(fallback, r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("WARNING", buf.getvalue())
        self.assertIn("Not/AZone", buf.getvalue())


class ClassMoneyInvariantTests(unittest.TestCase):
    """A sweep, not examples.

    Every money defect this feature has shipped survived a suite full of
    hand-picked cases — ¥2,200/10 and ¥2,000/8 both divide evenly, so rounding
    could never bite. Twice the fix for one rounding bug introduced another in
    the opposite direction. These assert the INVARIANTS over thousands of
    combinations instead, which is the only thing that has actually held.

    Pure arithmetic on Store.summarize_package — no database, no fixtures.
    """

    # amounts chosen to be hostile: odd cents, half-cent boundaries, values
    # that cannot be represented exactly in binary floating point
    AMOUNTS = (
        0.01, 0.07, 1.0, 9.99, 99.95, 100.0, 333.33, 999.99, 1000.0, 1000.01,
        1234.57, 2000.0, 2200.01, 2727.46, 4238.31, 5000.05, 12345.67, 99999.99,
        # third decimals — the shape that breaks the cap, and which the first
        # version of this sweep called "half-cent boundaries" while containing
        # none. `_validate_amount` accepts them and never rounds, so the MCP
        # and a direct /api/* call can both produce one.
        100.035, 0.005, 1000.999, 33.333, 2.225, 987.6545,
    )
    COUNTS = (1, 2, 3, 4, 5, 7, 8, 10, 12)

    def summarize(self, kind, amount, count, attended=0, school=0, ours=0):
        events = (
            [{"kind": "attended"}] * attended
            + [{"kind": "missed_school"}] * school
            + [{"kind": "missed_us"}] * ours
        )
        return Store.summarize_package(
            {"class_count": count, "kind": kind}, amount, events
        )

    def test_per_class_parts_always_sum_to_the_payment(self):
        checked = 0
        for amount in self.AMOUNTS:
            for count in self.COUNTS:
                for attended in range(0, count + 3):
                    s = self.summarize("per_class", amount, count, attended=attended)
                    total = s["amount"]
                    with self.subTest(amount=amount, count=count, attended=attended):
                        self.assertEqual(
                            round(s["used_amount"] + s["remaining_amount"], 2), total
                        )
                        self.assertLessEqual(s["used_amount"], total)
                        self.assertGreaterEqual(s["remaining_amount"], 0.0)
                        self.assertGreaterEqual(s["used_amount"], 0.0)
                    checked += 1
        self.assertGreater(checked, 1000)

    def test_period_split_always_reconciles_and_never_exceeds_the_payment(self):
        """The regression that made this class necessary: summing two
        independently rounded halves rounds up twice, so a package could report
        owing back a cent MORE than was ever handed over."""
        checked = 0
        for amount in self.AMOUNTS:
            for count in self.COUNTS:
                for school in range(0, count + 2):
                    for ours in range(0, count + 2 - school):
                        s = self.summarize("period", amount, count,
                                           school=school, ours=ours)
                        total = s["amount"]
                        with self.subTest(amount=amount, count=count,
                                          school=school, ours=ours):
                            self.assertEqual(
                                round(s["reclaimable_amount"]
                                      + s["forfeited_amount"], 2),
                                s["owed_amount"],
                                "the split must reconcile with its own total",
                            )
                            self.assertLessEqual(
                                s["owed_amount"], total,
                                "cannot be owed back more than was paid",
                            )
                            # each PART bounded too: deriving the total from
                            # the parts can leave a correct total sitting over
                            # two garbage halves — ¥2,000 reclaimable and
                            # -¥1,200 forfeited still sum to ¥800
                            for key in ("reclaimable_amount", "forfeited_amount"):
                                self.assertGreaterEqual(s[key], 0.0, key)
                                self.assertLessEqual(s[key], total, key)
                        checked += 1
        self.assertGreater(checked, 1000)

    def test_the_owed_total_is_the_exact_ratio_while_it_fits(self):
        """Reconciling must not cost accuracy: below the cap the total is still
        amount x n / count, not a sum of rounded pieces.

        At n == count it is the PAYMENT, not the ratio. Those differ for an
        amount on a half-cent — `round(amount*count/count, 2)` can land a cent
        above `round(amount, 2)` — and that gap is exactly how a package came
        to report owing back more than was handed over.
        """
        for amount in self.AMOUNTS:
            for count in self.COUNTS:
                for school in range(0, count + 1):
                    for ours in range(0, count + 1 - school):
                        s = self.summarize("period", amount, count,
                                           school=school, ours=ours)
                        missed = school + ours
                        expected = (
                            round(amount, 2) if missed >= count
                            else round(amount * missed / count, 2)
                        )
                        with self.subTest(amount=amount, count=count,
                                          school=school, ours=ours):
                            self.assertEqual(s["owed_amount"], expected)

    def test_no_figure_is_ever_negative_or_a_negative_zero(self):
        """An amount carrying a third decimal made `remaining_amount` -0.0,
        which the portal hid (falsy) and the MCP printed as ¥-0.00."""
        for amount in (1000.999, 0.005, 33.333, 1.0e-3):
            for count in (1, 3, 7):
                for kind, extra in (("per_class", {"attended": count}),
                                    ("period", {"school": count})):
                    s = self.summarize(kind, amount, count, **extra)
                    with self.subTest(amount=amount, count=count, kind=kind):
                        for key, val in s.items():
                            if key.endswith("_amount") or key in ("amount", "rate"):
                                self.assertGreaterEqual(val, 0.0, key)
                                self.assertEqual(
                                    val, abs(val), f"{key} is a negative zero"
                                )

    def test_the_school_is_charged_first_when_the_log_overruns(self):
        """Attribution, not arithmetic. Flipping the allocation to us-first
        keeps every total correct and reconciling — it just hands the school
        back the money she was going to claim from them. No invariant can see
        that, so it has to be asserted directionally."""
        # ¥1,000 for 2 classes; 2 cancelled by them, 2 skipped by us
        s = self.summarize("period", 1000.0, 2, school=2, ours=2)
        self.assertEqual(s["owed_amount"], 1000.0)
        self.assertEqual(s["reclaimable_amount"], 1000.0,
                         "the school's share must be valued first and in full")
        self.assertEqual(s["forfeited_amount"], 0.0,
                         "the overrun must land on what we forfeited")
        # and the other way round: when we are the overrun, the school's real
        # share is still exactly its own
        s = self.summarize("period", 1000.0, 2, school=1, ours=3)
        self.assertEqual(s["reclaimable_amount"], 500.0)
        self.assertEqual(s["forfeited_amount"], 500.0)

    def test_the_counts_report_what_happened_even_when_the_money_is_capped(self):
        """Money stops at the payment; the log does not. Capping the counts too
        would quietly under-report how many classes the school cancelled."""
        s = self.summarize("period", 800.0, 2, school=5, ours=1)
        self.assertEqual(s["reclaimable"], 5)   # not min(5, 2)
        self.assertEqual(s["forfeited"], 1)
        self.assertEqual(s["owed"], 6)
        self.assertEqual(s["reclaimable"] + s["forfeited"], s["owed"])
        self.assertEqual(s["overrun"], 4)
        self.assertEqual(s["owed_amount"], 800.0)   # capped

    def test_per_class_money_is_the_exact_ratio_at_every_n(self):
        """A rounded rate x n agrees with the exact ratio at n=1, which is the
        only case the first test of this pinned."""
        for amount, count in ((1000.0, 3), (2000.0, 7), (0.07, 3), (12345.67, 9)):
            for used in range(0, count + 1):
                s = self.summarize("per_class", amount, count, attended=used)
                with self.subTest(amount=amount, count=count, used=used):
                    self.assertEqual(
                        s["used_amount"], round(amount * used / count, 2)
                    )

    def test_attendance_never_affects_a_period_package_s_money(self):
        for amount in self.AMOUNTS[:6]:
            for count in self.COUNTS[:5]:
                bare = self.summarize("period", amount, count, school=1)
                with_attendance = self.summarize(
                    "period", amount, count, school=1, attended=count
                )
                with self.subTest(amount=amount, count=count):
                    self.assertEqual(bare["owed_amount"],
                                     with_attendance["owed_amount"])


class ClassTrackerTests(unittest.TestCase):
    """The class tracker holds no money of its own — every figure is derived
    from the linked payment, so the ledger and the tracker cannot disagree."""

    def setUp(self):
        self.store = make_store()

    def pack(self, *, amount=2200, count=10, kind="per_class", label="8月"):
        expense = self.store.create(
            date="2026-08-03", amount=amount, description="足球课",
            category="aden-sports",
        )
        package = self.store.create_package(
            expense_id=expense.id, name="足球课", kind=kind,
            class_count=count, period_label=label,
        )
        return expense, package

    # ── (a) per-class packs draw down ────────────────────────────────────
    def test_per_class_pack_reports_classes_and_money_remaining(self):
        _expense, package = self.pack()
        self.assertEqual(package["summary"]["rate"], 220.0)
        self.assertEqual(package["summary"]["remaining"], 10)
        self.assertEqual(package["summary"]["remaining_amount"], 2200.0)
        for day in ("2026-08-05", "2026-08-12", "2026-08-19"):
            package = self.store.log_class(
                package_id=package["id"], kind="attended", date=day
            )
        s = package["summary"]
        self.assertEqual((s["used"], s["remaining"]), (3, 7))
        self.assertEqual(s["remaining_amount"], 1540.0)
        self.assertEqual(s["used_amount"], 660.0)

    def test_attending_more_than_were_bought_is_reported_not_hidden(self):
        _expense, package = self.pack(count=2)
        for _ in range(3):
            package = self.store.log_class(package_id=package["id"], kind="attended")
        s = package["summary"]
        self.assertEqual(s["remaining"], 0)          # never negative
        self.assertEqual(s["remaining_amount"], 0.0)
        self.assertEqual(s["overrun"], 1)            # but the fact survives

    def test_a_missed_class_does_not_consume_a_prepaid_credit(self):
        _expense, package = self.pack()
        package = self.store.log_class(package_id=package["id"], kind="missed_school")
        self.assertEqual(package["summary"]["remaining"], 10)

    # ── (b) period fees owe back what did not happen ─────────────────────
    def test_period_package_splits_owed_into_reclaimable_and_forfeited(self):
        """The example from the spec: ¥2,000 for 8 classes, 3 missed."""
        _expense, package = self.pack(amount=2000, count=8, kind="period", label="9月")
        for kind in ("missed_school", "missed_school", "missed_us"):
            package = self.store.log_class(
                package_id=package["id"], kind=kind, date="2026-09-05"
            )
        s = package["summary"]
        self.assertEqual(s["rate"], 250.0)
        self.assertEqual((s["owed"], s["owed_amount"]), (3, 750.0))
        self.assertEqual((s["reclaimable"], s["reclaimable_amount"]), (2, 500.0))
        self.assertEqual((s["forfeited"], s["forfeited_amount"]), (1, 250.0))
        # owed is the whole of it; the split is only for the argument
        self.assertEqual(s["reclaimable_amount"] + s["forfeited_amount"], s["owed_amount"])

    # ── money correctness ────────────────────────────────────────────────
    def test_amounts_are_exact_ratios_not_a_rounded_rate_times_n(self):
        """¥1,000 over 3 classes: a rounded ¥333.33 rate would lose a cent per
        class and the parts would stop adding up to the payment."""
        _expense, package = self.pack(amount=1000, count=3)
        self.assertEqual(package["summary"]["rate"], 333.33)      # display only
        self.assertEqual(package["summary"]["remaining_amount"], 1000.0)
        package = self.store.log_class(package_id=package["id"], kind="attended")
        s = package["summary"]
        self.assertEqual(s["used_amount"] + s["remaining_amount"], 1000.0)

    def test_the_rate_follows_the_payment_because_it_is_never_stored(self):
        expense, package = self.pack(amount=2200, count=10)
        self.assertEqual(package["summary"]["rate"], 220.0)
        self.store.update(expense.id, fields={"amount": 2500})  # she was told wrong
        self.assertEqual(self.store.package(package["id"])["summary"]["rate"], 250.0)

    def test_one_payment_can_only_fund_one_package(self):
        expense, _package = self.pack()
        with self.assertRaises(ValidationError) as ctx:
            self.store.create_package(
                expense_id=expense.id, name="again", kind="per_class", class_count=5
            )
        self.assertIn("already tracked", str(ctx.exception))

    def test_a_tracked_payment_cannot_be_deleted_out_from_under_its_package(self):
        expense, package = self.pack()
        self.store.log_class(package_id=package["id"], kind="attended")
        with self.assertRaises(ValidationError) as ctx:
            self.store.delete(expense.id)
        self.assertIn("Remove that course first", str(ctx.exception))
        self.assertEqual(len(self.store.list()), 1)          # nothing was removed
        self.assertEqual(len(self.store.package(package["id"])["events"]), 1)
        # and it works once the package is gone
        self.assertTrue(self.store.delete_package(package["id"]))
        self.assertTrue(self.store.delete(expense.id))

    def test_the_class_tracker_never_moves_an_expense_total(self):
        """P4: Store.summarize is the one totals implementation and knows
        nothing about classes. Consumption is not spending."""
        _expense, package = self.pack()
        before = self.store.summary(today="2026-08-11")
        for _ in range(4):
            self.store.log_class(package_id=package["id"], kind="attended")
        self.assertEqual(self.store.summary(today="2026-08-11"), before)

    def test_deleting_a_package_takes_its_class_log_with_it(self):
        _expense, package = self.pack()
        self.store.log_class(package_id=package["id"], kind="attended")
        self.assertTrue(self.store.delete_package(package["id"]))
        with self.store.db.tx() as tx:
            left = tx.query("SELECT id FROM class_events WHERE package_id = :p",
                            {"p": package["id"]})
        self.assertEqual(left, [])

    def test_one_logged_class_can_be_taken_back(self):
        _expense, package = self.pack()
        package = self.store.log_class(package_id=package["id"], kind="attended")
        event_id = package["events"][0]["id"]
        self.assertTrue(self.store.delete_class_event(event_id))
        self.assertEqual(self.store.package(package["id"])["summary"]["used"], 0)
        self.assertFalse(self.store.delete_class_event(event_id))  # already gone

    # ── validation, all of it coaching ───────────────────────────────────
    def test_validation_rejects_and_explains(self):
        expense = self.store.create(date="2026-08-03", amount=100)
        for fields, expected in (
            ({"kind": "monthly"}, "per_class"),
            ({"class_count": 0}, "at least 1"),
            ({"class_count": "ten"}, "whole number"),
            ({"name": "  "}, "name is required"),
        ):
            args = dict(expense_id=expense.id, name="足球课",
                        kind="per_class", class_count=10)
            args.update(fields)
            with self.assertRaises(ValidationError) as ctx:
                self.store.create_package(**args)
            self.assertIn(expected, str(ctx.exception))

    def test_a_package_needs_a_payment_that_exists(self):
        with self.assertRaises(KeyError) as ctx:
            self.store.create_package(
                expense_id="nope", name="足球课", kind="per_class", class_count=4
            )
        self.assertIn("expenses_list", str(ctx.exception))

    def test_logging_rejects_an_unknown_event_kind(self):
        _expense, package = self.pack()
        with self.assertRaises(ValidationError) as ctx:
            self.store.log_class(package_id=package["id"], kind="cancelled")
        self.assertIn("missed_school", str(ctx.exception))

    def test_the_price_cannot_be_edited_on_the_package(self):
        """The money lives on the expense; two places to edit it is two places
        for it to be wrong."""
        _expense, package = self.pack()
        with self.assertRaises(ValidationError) as ctx:
            self.store.update_package(package["id"], fields={"amount": 999})
        self.assertIn("lives on the linked expense", str(ctx.exception))

    def test_logging_defaults_to_today(self):
        from app.store import today_str

        _expense, package = self.pack()
        package = self.store.log_class(package_id=package["id"], kind="attended")
        self.assertEqual(package["events"][0]["date"], today_str())

    # ── gaps the adversarial review found: every one of these mutations
    #    survived the first version of this suite ────────────────────────
    def test_attending_a_period_class_is_not_owing_it(self):
        """Adding `attended` to the missed tally reported the WHOLE payment as
        a debt owed back, and nothing caught it — no test had ever logged an
        attendance against a period package."""
        _expense, package = self.pack(amount=2000, count=8, kind="period")
        for _ in range(8):
            package = self.store.log_class(package_id=package["id"], kind="attended")
        s = package["summary"]
        self.assertEqual((s["owed"], s["owed_amount"]), (0, 0.0))
        package = self.store.log_class(package_id=package["id"], kind="missed_school")
        self.assertEqual(package["summary"]["owed"], 1)   # only the miss counts

    def test_you_cannot_be_owed_back_more_than_you_paid(self):
        """A wrong class_count or a bad month could log more misses than the
        payment covers; the money must stay capped even though the count is
        reported honestly."""
        _expense, package = self.pack(amount=2000, count=8, kind="period")
        for _ in range(11):
            package = self.store.log_class(
                package_id=package["id"], kind="missed_school"
            )
        s = package["summary"]
        self.assertEqual(s["owed"], 11)            # what really happened
        self.assertEqual(s["owed_amount"], 2000.0)  # never more than was paid
        self.assertEqual(s["overrun"], 3)           # and the excess is visible

    def test_the_period_split_reconciles_when_the_price_does_not_divide(self):
        """¥1,000 over 3: three independent round()s gave owed ¥666.67 against
        parts of ¥333.33 + ¥333.33 — a visible ¥0.01 hole in the figure the
        school is being shown."""
        _expense, package = self.pack(amount=1000, count=3, kind="period")
        for kind in ("missed_school", "missed_us"):
            package = self.store.log_class(package_id=package["id"], kind=kind)
        s = package["summary"]
        self.assertEqual(
            round(s["reclaimable_amount"] + s["forfeited_amount"], 2), s["owed_amount"]
        )

    def test_used_and_remaining_always_sum_to_the_payment(self):
        """Independent rounding invented or lost a cent on odd payments."""
        for amount, count, attend in (
            (999.99, 2, 1), (1234.57, 2, 1), (2200.01, 10, 5), (1000, 3, 1), (0.07, 3, 2)
        ):
            with self.subTest(amount=amount, count=count):
                store = make_store()
                expense = store.create(date="2026-08-03", amount=amount)
                package = store.create_package(
                    expense_id=expense.id, name="x", kind="per_class",
                    class_count=count,
                )
                for _ in range(attend):
                    package = store.log_class(
                        package_id=package["id"], kind="attended"
                    )
                s = package["summary"]
                self.assertEqual(
                    round(s["used_amount"] + s["remaining_amount"], 2), s["amount"]
                )

    def test_used_amount_never_exceeds_the_payment(self):
        _expense, package = self.pack(amount=2200, count=10)
        for _ in range(12):
            package = self.store.log_class(package_id=package["id"], kind="attended")
        self.assertEqual(package["summary"]["used_amount"], 2200.0)
        self.assertEqual(package["summary"]["overrun"], 2)

    def test_neither_missed_kind_burns_a_prepaid_credit(self):
        """Only `missed_school` was covered; making `missed_us` consume a class
        went unnoticed, and that is a money-visible policy choice."""
        for kind in ("missed_school", "missed_us"):
            with self.subTest(kind=kind):
                store = make_store()
                expense = store.create(date="2026-08-03", amount=2200)
                package = store.create_package(
                    expense_id=expense.id, name="x", kind="per_class", class_count=10
                )
                package = store.log_class(package_id=package["id"], kind=kind)
                self.assertEqual(package["summary"]["remaining"], 10)
                self.assertEqual(package["summary"]["remaining_amount"], 2200.0)

    def test_the_supplied_date_is_the_one_recorded(self):
        """Only the omitted-date default was pinned, so a store that ignored
        the date entirely and always stamped today passed."""
        _expense, package = self.pack()
        package = self.store.log_class(
            package_id=package["id"], kind="attended", date="2026-08-05"
        )
        self.assertEqual(package["events"][0]["date"], "2026-08-05")

    def test_the_period_label_survives_the_round_trip(self):
        _expense, package = self.pack(label="8月")
        self.assertEqual(self.store.package(package["id"])["period_label"], "8月")

    def test_the_database_refuses_a_second_package_on_one_payment(self):
        """The Python pre-check was tested; the constraint behind it was not."""
        expense, _package = self.pack()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db.tx() as tx:
                tx.execute(
                    "INSERT INTO class_packages (id, expense_id, name, kind, "
                    "class_count, archived, created_at, updated_at) VALUES "
                    "('dup', :eid, 'x', 'per_class', 1, FALSE, 'now', 'now')",
                    {"eid": expense.id},
                )

    def test_a_lost_race_reads_as_a_reason_not_a_crash(self):
        """The duplicate pre-check is not atomic with the insert, and since
        v0.9.0 the handlers run in a threadpool — so the UNIQUE index can be
        what fires. The caller must still get the coaching, not a 500 carrying
        a driver traceback. Simulated by blinding the pre-check, which is
        exactly what a concurrent transaction does to it."""
        expense, package = self.pack()
        real = Store._duplicate_package
        calls = []

        def blind_once(tx, expense_id):
            # invisible to the pre-check (as a concurrent uncommitted insert
            # is), present by the time the constraint has fired and we ask why
            calls.append(expense_id)
            return None if len(calls) == 1 else real(tx, expense_id)

        self.store._duplicate_package = blind_once
        with self.assertRaises(ValidationError) as ctx:
            self.store.create_package(
                expense_id=expense.id, name="again", kind="per_class", class_count=5
            )
        self.assertGreaterEqual(len(calls), 2, "the constraint path was not taken")
        self.assertIn("already tracked", str(ctx.exception))
        self.assertIn(package["name"], str(ctx.exception))

    def test_a_vanished_payment_is_not_reported_as_a_duplicate(self):
        """The constraint that fires might be the foreign key, not UNIQUE.
        Saying 'already tracked' then sends someone looking for a package that
        does not exist."""
        expense = self.store.create(date="2026-08-03", amount=100)
        real_fetch = self.store._fetch

        def vanish(tx, expense_id):
            row = real_fetch(tx, expense_id)
            self.store._fetch = real_fetch          # only for the pre-check
            with self.store.db.tx() as other:
                other.execute("DELETE FROM expenses WHERE id = :i", {"i": expense_id})
            return row

        self.store._fetch = vanish
        with self.assertRaises(KeyError) as ctx:
            self.store.create_package(
                expense_id=expense.id, name="足球课", kind="per_class", class_count=5
            )
        self.assertIn("no expense with id", str(ctx.exception))
        self.assertNotIn("already tracked", str(ctx.exception))

    def test_the_type_cannot_change_once_classes_are_logged(self):
        """Flipping kind silently reinterprets the whole log: attendances stop
        drawing down and misses become money owed."""
        _expense, package = self.pack()
        self.store.log_class(package_id=package["id"], kind="attended")
        with self.assertRaises(ValidationError) as ctx:
            self.store.update_package(package["id"], fields={"kind": "period"})
        self.assertIn("cannot be changed", str(ctx.exception))
        # ...but it is fine before anything is logged
        _e2, fresh = self.pack(amount=500, label="9月")
        self.assertEqual(
            self.store.update_package(fresh["id"], fields={"kind": "period"})["kind"],
            "period",
        )

    def test_a_fractional_class_count_is_refused(self):
        """int(1.9) is 1, and the count divides the money."""
        expense = self.store.create(date="2026-08-03", amount=100)
        for bad in (1.9, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.store.create_package(
                        expense_id=expense.id, name="x", kind="per_class",
                        class_count=bad,
                    )

    def test_a_missing_package_says_where_package_ids_come_from(self):
        """The inherited message pointed at expenses_list, which cannot
        produce a package id (P3)."""
        with self.assertRaises(KeyError) as ctx:
            self.store.package("nope")
        self.assertIn("classes_list", str(ctx.exception))
        self.assertNotIn("expenses_list", str(ctx.exception))

    def test_every_package_entry_point_says_where_package_ids_come_from(self):
        """The fix landed in three call sites and only one was pinned."""
        for call in (
            lambda: self.store.package("nope"),
            lambda: self.store.update_package("nope", fields={"name": "x"}),
            lambda: self.store.log_class(package_id="nope", kind="attended"),
        ):
            with self.assertRaises(KeyError) as ctx:
                call()
            self.assertIn("classes_list", str(ctx.exception))
            self.assertNotIn("expenses_list", str(ctx.exception))

    def test_a_note_on_a_logged_class_is_kept(self):
        """The reason a class was missed is what decides reclaimable versus
        forfeited in an argument with the school."""
        _expense, package = self.pack()
        package = self.store.log_class(
            package_id=package["id"], kind="missed_school", note="下雨停课"
        )
        self.assertEqual(package["events"][0]["note"], "下雨停课")

    def test_a_malformed_class_date_is_refused(self):
        _expense, package = self.pack()
        with self.assertRaises(ValidationError):
            self.store.log_class(
                package_id=package["id"], kind="attended", date="tomorrow-ish"
            )

    def test_the_class_log_reads_newest_first(self):
        _expense, package = self.pack()
        for day in ("2026-08-05", "2026-08-19", "2026-08-12"):
            package = self.store.log_class(
                package_id=package["id"], kind="attended", date=day
            )
        self.assertEqual(
            [e["date"] for e in package["events"]],
            ["2026-08-19", "2026-08-12", "2026-08-05"],
        )

    def test_the_archived_flag_is_reported_not_just_acted_on(self):
        """An agent asked 'which courses have I archived' reads the flag."""
        _expense, package = self.pack()
        self.store.update_package(package["id"], fields={"archived": True})
        listed = self.store.list_packages(include_archived=True)
        self.assertEqual([p["archived"] for p in listed], [True])

    def test_an_archived_package_still_owns_its_payment(self):
        """Otherwise the payment is offered again and the add fails on a
        package she can no longer see."""
        expense, package = self.pack()
        self.store.update_package(package["id"], fields={"archived": True})
        self.assertIn(expense.id, self.store.linked_expense_ids())

    def test_a_non_integrity_failure_is_not_laundered_into_a_reason(self):
        """Rewriting every insert failure as 'already tracked' turns a dropped
        connection into a confident false statement, and the write is lost.

        The failure is injected at the DB call so the REAL `_insert_package`
        and the REAL `if not _is_integrity_error(exc): raise` both run —
        stubbing `_insert_package` wholesale proved only that the outer
        `except` was narrow, and left the guard itself unpinned.
        """
        import contextlib

        expense = self.store.create(date="2026-08-03", amount=100)
        boom = RuntimeError("connection reset by peer")
        real_tx = self.store.db.tx

        @contextlib.contextmanager
        def failing_tx():
            with real_tx() as tx:
                class Proxy:
                    def __getattr__(self, name):
                        return getattr(tx, name)

                    def execute(self, sql, params=None):
                        if "INSERT INTO class_packages" in sql:
                            raise boom
                        return tx.execute(sql, params)

                yield Proxy()

        self.store.db.tx = failing_tx
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self.store.create_package(
                    expense_id=expense.id, name="足球课", kind="per_class",
                    class_count=5,
                )
        finally:
            self.store.db.tx = real_tx
        self.assertIs(ctx.exception, boom)

    def test_a_class_count_that_is_not_a_real_number_is_refused(self):
        """int(nan) raises a bare ValueError the API does not catch, so this
        was a 500 rather than a 400. Same for an unbounded count, which
        reaches the driver as an OverflowError."""
        expense = self.store.create(date="2026-08-03", amount=100)
        for bad in (float("nan"), float("inf"), float("-inf"), 10 ** 20, 2 ** 63):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    self.store.create_package(
                        expense_id=expense.id, name="足球课", kind="per_class",
                        class_count=bad,
                    )

    def test_the_database_refuses_to_orphan_a_package(self):
        """The app-level guard makes this unreachable normally; the FK is the
        backstop for the race where the check and the delete are not atomic."""
        expense, _package = self.pack()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db.tx() as tx:
                tx.execute("DELETE FROM expenses WHERE id = :i", {"i": expense.id})

    def test_the_database_refuses_nonsense_a_bypass_could_write(self):
        expense = self.store.create(date="2026-08-03", amount=100)
        for values, why in (
            ("('p1', :eid, 'x', 'per_class', 0, FALSE, 'n', 'n')", "class_count > 0"),
            ("('p2', :eid, 'x', 'weekly', 1, FALSE, 'n', 'n')", "kind allow-list"),
        ):
            with self.subTest(why=why):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.store.db.tx() as tx:
                        tx.execute(
                            "INSERT INTO class_packages (id, expense_id, name, "
                            "kind, class_count, archived, created_at, updated_at) "
                            f"VALUES {values}", {"eid": expense.id},
                        )
        _e, package = self.pack(label="9月")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.db.tx() as tx:
                tx.execute(
                    "INSERT INTO class_events (id, package_id, date, kind, "
                    "created_at) VALUES ('e1', :p, '2026-08-01', 'junk', 'n')",
                    {"p": package["id"]},
                )

    def test_each_course_is_summarised_from_its_own_classes(self):
        """list_packages loads every event in one query and groups them in
        Python. Handing the whole set to each package makes a course she has
        never attended report classes consumed — and every existing test had
        either one package or no events, so nothing could see it."""
        football_expense = self.store.create(
            date="2026-08-03", amount=2200, description="足球课"
        )
        football = self.store.create_package(
            expense_id=football_expense.id, name="足球课", kind="per_class",
            class_count=10,
        )
        swim_expense = self.store.create(
            date="2026-08-04", amount=1000, description="游泳课"
        )
        swim = self.store.create_package(
            expense_id=swim_expense.id, name="游泳课", kind="per_class",
            class_count=5,
        )
        for _ in range(3):
            self.store.log_class(package_id=football["id"], kind="attended")

        by_id = {p["id"]: p for p in self.store.list_packages()}
        self.assertEqual(by_id[football["id"]]["summary"]["used"], 3)
        self.assertEqual(by_id[swim["id"]]["summary"]["used"], 0,
                         "a course with no classes logged reported some")
        self.assertEqual(by_id[swim["id"]]["summary"]["remaining"], 5)
        self.assertEqual(by_id[swim["id"]]["summary"]["remaining_amount"], 1000.0)
        self.assertEqual(by_id[swim["id"]]["events"], [])
        # and the single-package read agrees with the list
        self.assertEqual(
            self.store.package(swim["id"])["summary"],
            by_id[swim["id"]]["summary"],
        )

    def test_archived_packages_are_hidden_unless_asked_for(self):
        _expense, package = self.pack()
        self.store.update_package(package["id"], fields={"archived": True})
        self.assertEqual(self.store.list_packages(), [])
        self.assertEqual(len(self.store.list_packages(include_archived=True)), 1)


if __name__ == "__main__":
    unittest.main()
