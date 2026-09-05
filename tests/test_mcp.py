"""MCP-tier tests — tool registration/behavior in-process, bearer auth via HTTP."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.db import Database  # noqa: E402
from app.mcp_server import McpBearerMiddleware, build_mcp  # noqa: E402
from app.store import Store  # noqa: E402

EXPECTED_TOOLS = {
    "expenses_help", "expenses_list", "expenses_add", "expenses_mark_paid",
    "expenses_update", "expenses_delete", "expenses_history",
    "expenses_refund", "expenses_refund_delete",
    "expenses_mint_link", "expenses_revoke_link", "expenses_list_links",
    "classes_list", "classes_add", "classes_log", "classes_log_delete",
    "classes_update", "classes_delete",
}

_TEST_LOOP = asyncio.new_event_loop()


def make_store() -> Store:
    db = Database("sqlite:///:memory:")
    db.init()
    return Store(db)


def run(coro):
    return _TEST_LOOP.run_until_complete(coro)


def tearDownModule():
    _TEST_LOOP.close()


def tool_payload(result) -> dict | list:
    """Extract the JSON payload from a FastMCP call_tool result."""
    content, structured = result
    if structured is not None:
        return structured.get("result", structured) if isinstance(structured, dict) else structured
    return json.loads(content[0].text)


class McpToolTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.mcp = build_mcp(self.store)

    def test_expected_tool_set(self):
        tools = run(self.mcp.list_tools())
        self.assertEqual({t.name for t in tools}, EXPECTED_TOOLS)

    def test_add_list_mark_paid_history(self):
        added = tool_payload(run(self.mcp.call_tool("expenses_add", {
            "date": "2026-07-14", "amount": "200", "description": "电费",
        })))
        eid = added["id"]

        listed = tool_payload(run(self.mcp.call_tool("expenses_list", {"status": "unpaid"})))
        self.assertEqual(len(listed["expenses"]), 1)
        self.assertEqual(listed["summary"]["unpaid"], 200.0)

        paid = tool_payload(run(self.mcp.call_tool("expenses_mark_paid", {
            "expense_id": eid, "paid": True, "paid_date": "2026-07-15",
        })))
        self.assertTrue(paid["paid"])

        hist = tool_payload(run(self.mcp.call_tool("expenses_history", {"expense_id": eid})))
        self.assertEqual([h["action"] for h in hist["history"]], ["create", "mark_paid"])

        summary = tool_payload(run(self.mcp.call_tool("expenses_list", {})))["summary"]
        self.assertEqual(summary["paid"], 200.0)

    def test_mint_and_revoke_link(self):
        minted = tool_payload(run(self.mcp.call_tool("expenses_mint_link", {
            "label": "wife", "expires_days": 365,
        })))
        self.assertEqual(len(minted["token"]), 64)
        self.assertIsNotNone(self.store.validate_token(minted["token"]))
        revoked = tool_payload(run(self.mcp.call_tool("expenses_revoke_link", {
            "token_or_id": minted["token"],
        })))
        self.assertTrue(revoked["revoked"])
        self.assertIsNone(self.store.validate_token(minted["token"]))

    def test_list_links_shows_ids_and_usage_but_never_tokens(self):
        minted = tool_payload(run(self.mcp.call_tool("expenses_mint_link", {
            "label": "wife",
        })))
        result = tool_payload(run(self.mcp.call_tool("expenses_list_links", {})))
        self.assertEqual(len(result["links"]), 1)
        link = result["links"][0]
        self.assertEqual(link["label"], "wife")
        self.assertEqual(link["status"], "active")
        self.assertEqual(link["expires_at"], "never")
        self.assertEqual(link["last_used_at"], "never opened")

        # The whole point: an id you can revoke with, and no token anywhere.
        self.assertEqual(link["id"], self.store.list_tokens()[0]["id"])
        self.assertNotIn(minted["token"], json.dumps(result))

    def test_list_links_id_round_trips_into_revoke(self):
        """The gap this tool closes: revoking without the operator holding a token."""
        minted = tool_payload(run(self.mcp.call_tool("expenses_mint_link", {
            "label": "wife",
        })))
        listed = tool_payload(run(self.mcp.call_tool("expenses_list_links", {})))
        revoked = tool_payload(run(self.mcp.call_tool("expenses_revoke_link", {
            "token_or_id": listed["links"][0]["id"],
        })))
        self.assertTrue(revoked["revoked"])
        self.assertIsNone(self.store.validate_token(minted["token"]))

    def test_list_links_hides_revoked_unless_asked(self):
        minted = tool_payload(run(self.mcp.call_tool("expenses_mint_link", {
            "label": "old-phone",
        })))
        run(self.mcp.call_tool("expenses_revoke_link", {"token_or_id": minted["token"]}))

        default = tool_payload(run(self.mcp.call_tool("expenses_list_links", {})))
        self.assertEqual(default["links"], [])

        widened = tool_payload(run(self.mcp.call_tool("expenses_list_links", {
            "include_revoked": True,
        })))
        self.assertEqual([x["status"] for x in widened["links"]], ["revoked"])

    def test_list_links_marks_expired_separately_from_revoked(self):
        self.store.mint_token(label="stale", expires_days=1)
        with self.store.db.tx() as tx:  # age it past expiry without touching revoked
            tx.execute("UPDATE access_tokens SET expires_at = :e",
                       {"e": "2000-01-01T00:00:00Z"})
        listed = tool_payload(run(self.mcp.call_tool("expenses_list_links", {})))
        self.assertEqual([x["status"] for x in listed["links"]], ["expired"])

    def test_validation_errors_propagate(self):
        from mcp.server.fastmcp.exceptions import ToolError
        with self.assertRaises(ToolError):
            run(self.mcp.call_tool("expenses_add", {"date": "2026-07-14", "amount": "-5"}))


class NaturalSpeechTests(unittest.TestCase):
    """'足球课付了' must work without ids, dates, or clean numbers."""

    def setUp(self):
        self.store = make_store()
        self.mcp = build_mcp(self.store)

    def call(self, tool, **args):
        return tool_payload(run(self.mcp.call_tool(tool, args)))

    def test_add_with_spoken_amount_and_no_date(self):
        added = self.call("expenses_add", amount="¥300", description="足球课")
        self.assertEqual(added["amount"], 300.0)
        self.assertRegex(added["date"], r"^\d{4}-\d{2}-\d{2}$")  # defaulted to today

    def test_mark_paid_by_query_defaults_today(self):
        self.call("expenses_add", amount="300", description="足球课")
        result = self.call("expenses_mark_paid", query="足球")
        self.assertTrue(result["paid"])
        self.assertRegex(result["paid_date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_mark_paid_prefers_the_unpaid_match(self):
        old = self.call("expenses_add", amount="300", description="足球课")
        self.call("expenses_mark_paid", expense_id=old["id"], paid_date="2026-07-01")
        self.call("expenses_add", amount="350", description="足球课")
        result = self.call("expenses_mark_paid", query="足球")  # two matches, one unpaid
        self.assertTrue(result["paid"])
        self.assertNotEqual(result["id"], old["id"])

    def test_ambiguous_query_returns_candidates(self):
        self.call("expenses_add", amount="300", description="足球课")
        self.call("expenses_add", amount="200", description="足球装备")
        result = self.call("expenses_delete", query="足球")
        self.assertEqual(result["matched"], 2)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIn("expense_id", result["hint"])

    def test_no_match_returns_hint_not_error(self):
        result = self.call("expenses_mark_paid", query="不存在的东西")
        self.assertEqual(result["matched"], 0)
        self.assertIn("expenses_list", result["hint"])

    def test_update_by_query_with_spoken_amount(self):
        self.call("expenses_add", amount="300", description="钢琴课")
        result = self.call("expenses_update", query="钢琴", amount="350块")
        self.assertEqual(result["amount"], 350.0)

    def test_delete_by_query_keeps_history(self):
        added = self.call("expenses_add", amount="300", description="旧课程")
        result = self.call("expenses_delete", query="旧课程")
        self.assertTrue(result["deleted"])
        hist = self.call("expenses_history", expense_id=added["id"])
        self.assertEqual([h["action"] for h in hist["history"]], ["create", "delete"])

    def test_list_with_query_filter(self):
        self.call("expenses_add", amount="300", description="足球课")
        self.call("expenses_add", amount="50", description="水果")
        listed = self.call("expenses_list", query="足球")
        self.assertEqual(len(listed["expenses"]), 1)

    def test_mint_link_never_expires_by_default(self):
        minted = self.call("expenses_mint_link", label="wife")
        self.assertIsNone(minted["expires_at"])
        self.assertIsNotNone(self.store.validate_token(minted["token"]))


class AgentErgonomicsTests(unittest.TestCase):
    """The channels agents actually read: descriptions, results, errors,
    prompts, annotations (docs/MCP_DESIGN.md)."""

    def setUp(self):
        self.store = make_store()
        self.mcp = build_mcp(self.store)

    def call(self, tool, **args):
        return tool_payload(run(self.mcp.call_tool(tool, args)))

    def test_help_tool_returns_playbook(self):
        text = self.call("expenses_help")
        for anchor in ("expenses_mark_paid", "足球课", "today", "candidates"):
            self.assertIn(anchor, text)

    def test_descriptions_carry_bilingual_triggers(self):
        # Whitespace-normalized: docstrings wrap, and a trigger phrase split
        # across a line break still reads fine to the agent. Assert on meaning,
        # not on where the source happens to break.
        desc = {
            t.name: " ".join((t.description or "").split())
            for t in run(self.mcp.list_tools())
        }
        self.assertIn("付了", desc["expenses_mark_paid"])
        self.assertIn("paid", desc["expenses_mark_paid"])
        self.assertIn("300块", desc["expenses_add"])
        self.assertIn("expenses_update", desc["expenses_add"])  # cross-ref
        self.assertIn("expenses_mark_paid", desc["expenses_delete"])
        # link tools must point at each other, not at a CLI the agent cannot run
        self.assertIn("谁有链接", desc["expenses_list_links"])
        self.assertIn("list the links", desc["expenses_list_links"])
        self.assertIn("expenses_revoke_link", desc["expenses_list_links"])
        self.assertIn("expenses_mint_link", desc["expenses_list_links"])
        self.assertIn("expenses_list_links", desc["expenses_revoke_link"])
        self.assertNotIn("CLI", desc["expenses_revoke_link"])

    def test_the_summary_says_which_rows_its_totals_cover(self):
        """P3. Since v0.12.0 `total` excludes borrow while `count` counts every
        row, so a list containing a borrow row has items summing to MORE than
        its own total. That is correct and it is surprising, which is exactly
        the combination that has to be stated where the agent reads — not only
        in a Python docstring. The 对账 persona previously told the agent to
        present the rows "with the total" and nothing warned it the two would
        not reconcile.
        """
        desc = " ".join(
            (t.description or "") for t in run(self.mcp.list_tools())
            if t.name == "expenses_list"
        ).split()
        desc = " ".join(desc)
        self.assertIn("borrow", desc)
        self.assertIn("borrow_owed", desc)
        self.assertIn("count", desc)
        # the surprising part, in words, not just the key names
        self.assertIn("more than", desc.lower())

        settle = run(self.mcp.get_prompt("duizhang"))
        text = " ".join(
            m.content.text for m in settle.messages if hasattr(m.content, "text")
        )
        self.assertIn("borrow_owed", text)
        self.assertIn("owed", text.lower())

    def test_the_summary_semantics_are_true_of_the_store(self):
        """The claim above is guidance an agent will ACT on, so it has to match
        what summarize() does — a wrong tool description causes actions, not
        just confusion (LESSONS §9)."""
        self.store.create(date="2026-07-01", amount=300, category="aden-sports")
        self.store.create(date="2026-07-02", amount=800, category="borrow")
        out = self.call("expenses_list", status="all")
        s = out["summary"]
        self.assertEqual(s["count"], 2)          # counts every row
        self.assertEqual(s["total"], 300.0)      # …but totals only spending
        self.assertEqual(s["borrow_owed"], 800.0)
        listed = sum(e["amount"] for e in out["expenses"])
        self.assertGreater(listed, s["total"],
                           "the description promises this can happen")
        self.assertEqual(round(s["total"] + s["borrow_owed"], 2), listed)

    def test_help_lists_the_canonical_categories_and_flags_borrow(self):
        """The agent invented 'loan repayment' because nothing ever told it the
        keys existed. The list has to live where the agent actually reads."""
        from app.store import BORROW_CATEGORY, CATEGORY_KEYS

        text = self.call("expenses_help")
        for key in CATEGORY_KEYS:
            self.assertIn(key, text, f"category {key!r} missing from the playbook")
        self.assertIn("垫付", text)
        self.assertIn(BORROW_CATEGORY, text)

    def test_write_tools_point_at_the_category_keys(self):
        desc = {
            t.name: " ".join((t.description or "").split())
            for t in run(self.mcp.list_tools())
        }
        for tool in ("expenses_add", "expenses_update"):
            self.assertIn("borrow", desc[tool], tool)
            self.assertIn("expenses_help", desc[tool], tool)

    def test_offbook_category_is_coached_in_the_result(self):
        """Saves, but says so — silent mis-bucketing is the failure mode: the
        row looks fine and the money lands where nobody looks."""
        result = self.call("expenses_add", amount="100", category="loan repayment")
        self.assertEqual(result["category"], "loan repayment")  # stored verbatim
        self.assertIn("loan repayment", result["note"])
        self.assertIn("borrow", result["note"])

    def test_canonical_category_gets_no_warning(self):
        result = self.call("expenses_add", amount="100", category="borrow")
        self.assertNotIn("NOTE", result["note"])

    def test_category_note_survives_on_update_too(self):
        added = self.call("expenses_add", amount="100", description="office")
        out = self.call("expenses_update", expense_id=added["id"], category="reimbursement")
        self.assertIn("reimbursement", out["note"])

    def test_annotations_read_vs_destructive(self):
        tools = {t.name: t for t in run(self.mcp.list_tools())}
        self.assertTrue(tools["expenses_list"].annotations.readOnlyHint)
        self.assertTrue(tools["expenses_help"].annotations.readOnlyHint)
        self.assertTrue(tools["expenses_delete"].annotations.destructiveHint)
        self.assertFalse(tools["expenses_add"].annotations.readOnlyHint)
        self.assertTrue(tools["expenses_list_links"].annotations.readOnlyHint)
        self.assertTrue(tools["expenses_revoke_link"].annotations.destructiveHint)

    def test_personas_registered(self):
        prompts = {p.name for p in run(self.mcp.list_prompts())}
        self.assertEqual(prompts, {"jizhang", "duizhang", "xiufu"})

    def test_numeric_amount_accepted(self):
        # agents often pass numbers, not strings — must not be rejected
        added = self.call("expenses_add", amount=300, description="足球课")
        self.assertEqual(added["amount"], 300.0)

    def test_add_already_paid_in_one_call(self):
        """One call, and now one transaction: the row is inserted already paid.

        It used to insert then mark_paid, leaving two history rows and a window
        where a failed second write reported an error over a row that was in
        fact saved (unpaid). One 'create' entry describing the paid row is both
        atomic and a truer account of what happened.
        """
        added = self.call("expenses_add", amount="300", description="足球课",
                          paid=True, submitted_by="Wei")
        self.assertTrue(added["paid"])
        self.assertRegex(added["paid_date"], r"^\d{4}-\d{2}-\d{2}$")
        hist = self.call("expenses_history", expense_id=added["id"])
        self.assertEqual([h["action"] for h in hist["history"]], ["create"])
        self.assertTrue(hist["history"][0]["snapshot"]["paid"])

    def test_a_missing_id_coaches_the_agent_at_the_mcp_boundary(self):
        """docs/BACKLOG.md filed this as reaching *MCP callers* as a bare id.

        The store-level test proves the message exists; only this one proves it
        survives to the agent. The HTTP path deliberately discards it (404
        'expense not found'), so nothing else covers this boundary.
        """
        from mcp.server.fastmcp.exceptions import ToolError

        for tool, args in (
            ("expenses_update", {"expense_id": "nope", "amount": "5"}),
            ("expenses_mark_paid", {"expense_id": "nope"}),
        ):
            with self.assertRaises(ToolError) as ctx:
                run(self.mcp.call_tool(tool, args))
            message = str(ctx.exception)
            self.assertIn("nope", message)
            self.assertIn("expenses_list", message)   # where ids come from
            self.assertIn("query", message)           # the other way to target
            self.assertNotIn("KeyError", message)

    def test_class_tracker_speaks_in_whole_answers(self):
        """The note is the channel the agent reads back to the user, so it has
        to carry the answer — classes AND money — not just confirm the write."""
        self.call("expenses_add", amount="2200", description="足球课 8月")
        added = self.call("classes_add", name="足球课", class_count="10",
                          query="足球", period_label="8月")
        self.assertIn("¥220.00", added["note"])
        self.assertIn("classes_log", added["note"])

        logged = self.call("classes_log", kind="attended", query="足球")
        self.assertIn("9 of 10", logged["note"])
        self.assertIn("¥1980.00", logged["note"])

        listed = self.call("classes_list")
        self.assertIn("9/10", listed["note"])

    def test_period_package_note_names_what_is_reclaimable(self):
        self.call("expenses_add", amount="2000", description="游泳课 9月")
        package = self.call("classes_add", name="游泳课", class_count=8,
                            kind="period", query="游泳", period_label="9月")
        for kind in ("missed_school", "missed_school", "missed_us"):
            note = self.call("classes_log", kind=kind,
                             package_id=package["id"])["note"]
        self.assertIn("¥750.00", note)      # owed in total
        self.assertIn("¥500.00", note)      # the reclaimable half
        self.assertIn("reclaimable", note)

    def test_an_ambiguous_course_returns_candidates_rather_than_guessing(self):
        for month in ("8月", "9月"):
            self.call("expenses_add", amount="1000", description=f"足球课 {month}")
            self.call("classes_add", name="足球课", class_count=5,
                      query=month, period_label=month)
        result = self.call("classes_log", kind="attended", query="足球")
        self.assertEqual(result["matched"], 2)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIn("package_id", result["candidates"][0])

    def test_a_course_can_be_targeted_by_its_period(self):
        """Two terms of the same course share a name — the month is the only
        thing that tells them apart, so dropping it from the matcher leaves the
        agent in an ambiguity loop it cannot exit."""
        for month in ("8月", "9月"):
            self.call("expenses_add", amount="1000", description=f"足球课 {month}")
            self.call("classes_add", name="足球课", class_count=5,
                      query=month, period_label=month)
        result = self.call("classes_log", kind="attended", query="9月")
        self.assertEqual(result["period_label"], "9月")

    def test_two_same_named_packs_are_told_apart_by_their_payment(self):
        """Since v0.11.0 the portal does not ask a per_class pack for a period
        label, so two terms of 足球课 created there are both called 足球课 with
        nothing else on them. Candidates carrying only name/period_label/kind
        would be three identical rows, and the disambiguation question the agent
        is told to ask the user would have no answer.
        """
        for month in ("8月", "9月"):
            self.call("expenses_add", amount="1000", description=f"足球课 {month}")
            self.call("classes_add", name="足球课", class_count=5, query=month)

        result = self.call("classes_log", kind="attended", query="足球课")
        self.assertEqual(result["matched"], 2)
        payments = [c["payment"] for c in result["candidates"]]
        self.assertEqual(len(set(payments)), 2, f"indistinguishable: {payments}")
        self.assertTrue(any("8月" in p for p in payments))

    def test_a_pack_with_no_period_label_is_reachable_by_its_payment(self):
        """'8月' has nowhere else to match once the portal stops asking for a
        period label — it lives in the payment, "足球课 8月". It surfaces as a
        question rather than a write: see the wrong-course test below."""
        for month in ("8月", "9月"):
            self.call("expenses_add", amount="1000", description=f"足球课 {month}")
            self.call("classes_add", name="足球课", class_count=5, query=month)

        result = self.call("classes_log", kind="attended", query="9月")
        self.assertEqual(result["matched"], 1)
        self.assertIn("9月", result["candidates"][0]["payment"])

    def test_a_payment_match_never_logs_against_a_course_on_its_own(self):
        """Matching the funding description is what makes '8月' resolvable at
        all, but it is weak evidence: the payment for 游泳课 may well read
        "足球课 8月 (转游泳)". Acting on it drew a class off swimming when the
        owner said football — a wrong write with nothing to show for it.
        """
        self.call("expenses_add", amount="1000", description="足球课 8月 (转游泳)")
        self.call("classes_add", name="游泳课", class_count=5, query="转游泳")

        result = self.call("classes_log", kind="attended", query="足球课")
        self.assertNotIn("id", result, "it logged against 游泳课 without asking")
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["candidates"][0]["name"], "游泳课")
        self.assertIn("PAYMENT", result["hint"],
                      "the agent is not told this match is the weaker kind")

    def test_a_course_name_still_beats_a_payment_that_mentions_another(self):
        """The tiering has to cut the right way: with a real 足球课 course in
        the ledger, a swimming payment that happens to say 足球课 must not make
        the football course ambiguous."""
        self.call("expenses_add", amount="1000", description="足球课 8月 (转游泳)")
        self.call("classes_add", name="游泳课", class_count=5, query="转游泳")
        self.call("expenses_add", amount="2200", description="Football 9月")
        self.call("classes_add", name="足球课", class_count=10, query="Football")

        result = self.call("classes_log", kind="attended", query="足球课")
        self.assertEqual(result["name"], "足球课")

    def test_two_packs_bought_in_one_sitting_are_still_distinguishable(self):
        """The normal MCP entry path: expenses_add defaults the date to today,
        so two terms bought in one sitting share a date, a description AND an
        amount. `payment` then prints identically for both, and the hint still
        tells the agent to ask the user to choose between two identical rows —
        which is the exact failure the payment field was added to fix.
        """
        paid = [self.call("expenses_add", amount="2200", description="足球课")["id"]
                for _ in range(2)]
        for expense_id in paid:
            self.call("classes_add", name="足球课", class_count=10,
                      expense_id=expense_id)

        result = self.call("classes_log", kind="attended", query="足球课")
        self.assertEqual(result["matched"], 2)
        rows = [
            {k: v for k, v in c.items() if k != "package_id"}
            for c in result["candidates"]
        ]
        # These two really are indistinguishable — same payment date, same
        # description, same amount, no classes logged, created in the same
        # second. What must hold is that the hint says so instead of telling
        # the agent to ask the user to pick between two identical rows.
        self.assertEqual(rows[0], rows[1], "fixture no longer reproduces the case")
        # not a bare `assertIn("package_id", ...)` — the hint's generic tail
        # already says "call again with that package_id", so that passes with
        # the guidance for THIS case removed
        self.assertIn("only `package_id` can separate", result["hint"],
                      f"the hint still says to ask the user to pick: {result['hint']}")
        self.assertNotEqual(result["candidates"][0]["package_id"],
                            result["candidates"][1]["package_id"])

    def test_the_extra_candidate_fields_carry_real_data(self):
        """When two same-named packs are NOT identical, `classes_logged` is what
        separates them — and the sibling test above cannot see it, because its
        fixture is the case where every field matches by construction."""
        paid = [self.call("expenses_add", amount="2200", description="足球课")["id"]
                for _ in range(2)]
        first = self.call("classes_add", name="足球课", class_count=10,
                          expense_id=paid[0])["id"]
        self.call("classes_log", kind="attended", package_id=first)
        self.call("classes_add", name="足球课", class_count=10, expense_id=paid[1])

        result = self.call("classes_log", kind="attended", query="足球课")
        by_id = {c["package_id"]: c for c in result["candidates"]}
        self.assertEqual(by_id[first]["classes_logged"], 1)
        self.assertEqual(
            [c["classes_logged"] for c in result["candidates"] if c["package_id"] != first],
            [0])
        for candidate in result["candidates"]:
            self.assertRegex(candidate["started"], r"^\d{4}-\d{2}-\d{2}T")

    def test_the_summary_note_names_a_pack_that_has_no_period_label(self):
        """P3: the note is the channel the agent reads back to the owner. Two
        label-less packs both reading "足球课 (—)" answers 还剩几节课 with a
        figure the owner cannot attach to a course."""
        for month in ("8月", "9月"):
            self.call("expenses_add", amount="1000", description=f"足球课 {month}")
            self.call("classes_add", name="足球课", class_count=5, query=month)

        # asserting the placeholder is gone would pass for any filler, including
        # one that reads the same for both packs — what matters is that the two
        # lines differ, since the note is how the agent names them back
        lines = sorted(self.call("classes_list")["note"].split("; "))
        self.assertEqual(len(lines), 2, lines)
        self.assertNotEqual(lines[0], lines[1], f"both packs read alike: {lines}")
        self.assertNotIn("—", lines[0])

    def test_an_overdue_list_reads_the_clock_once(self):
        """`expenses_list(status='overdue')` selected rows through find()/list()
        — each reading the clock itself — and only then read `today` for the
        totals. A call straddling midnight dropped a newly-overdue row from the
        rows AND from the summary while labelling the answer the other day.
        This is the owner's path; /api/list was fixed first and this was not.
        """
        from datetime import datetime, timezone

        from app import store as store_module

        calls = []

        def counting_clock():
            calls.append(1)
            return datetime(2026, 8, 11, 23, 59, 59, tzinfo=timezone.utc)

        real = store_module._household_now
        store_module._household_now = counting_clock
        try:
            for status in ("overdue",):
                calls.clear()
                self.call("expenses_list", status=status)
                self.assertEqual(len(calls), 1,
                                 f"{status}: clock read {len(calls)} times")
                calls.clear()
                self.call("expenses_list", status=status, query="足球")
                self.assertEqual(len(calls), 1,
                                 f"{status} by query: clock read {len(calls)} times")
        finally:
            store_module._household_now = real

    def test_a_retired_course_does_not_take_a_live_one_s_class(self):
        """An archived course is a finished one, and it matched by name like
        any other — so 足球课 retired last term won outright over the running
        one. The write was then unverifiable: classes_list hides archived by
        default, so the note the agent reads back to the owner does not contain
        the class it just logged.
        """
        self.call("expenses_add", amount="2200", description="足球课 8月")
        old = self.call("classes_add", name="足球课", class_count=10, query="8月")
        self.store.update_package(old["id"], fields={"archived": True})
        self.call("expenses_add", amount="2200", description="足球课 9月")
        live = self.call("classes_add", name="足球课", class_count=10, query="9月")

        result = self.call("classes_log", kind="attended", query="足球课")
        self.assertEqual(result.get("id"), live["id"],
                         "it logged against the course that finished last term")

    def test_two_live_courses_of_one_name_are_still_a_question(self):
        """Preferring the live one must not become "pick any live one"."""
        for month in ("8月", "9月"):
            self.call("expenses_add", amount="2200", description=f"足球课 {month}")
            self.call("classes_add", name="足球课", class_count=10, query=month)

        result = self.call("classes_log", kind="attended", query="足球课")
        self.assertEqual(result["matched"], 2)
        self.assertEqual([c["archived"] for c in result["candidates"]],
                         [False, False])

    def test_the_candidates_say_which_course_has_been_retired(self):
        """Without it the agent shows the owner two rows and cannot tell him
        one of them is last term's."""
        self.call("expenses_add", amount="2200", description="足球课 7月")
        old = self.call("classes_add", name="足球课", class_count=10, query="7月")
        self.store.update_package(old["id"], fields={"archived": True})
        for month in ("8月", "9月"):  # two live ones, so it has to ask
            self.call("expenses_add", amount="2200", description=f"足球课 {month}")
            self.call("classes_add", name="足球课", class_count=10, query=month)

        result = self.call("classes_log", kind="attended", query="足球课")
        self.assertEqual(result["matched"], 3)
        self.assertEqual(
            sorted(c["archived"] for c in result["candidates"]),
            [False, False, True])

    def test_an_archived_course_is_still_reachable_by_the_agent(self):
        """classes_list hides it by default; that must not make it impossible
        to correct a class logged against it."""
        self.call("expenses_add", amount="1000", description="足球课")
        package = self.call("classes_add", name="足球课", class_count=5, query="足球")
        self.store.update_package(package["id"], fields={"archived": True})
        logged = self.call("classes_log", kind="attended", query="足球")
        self.assertEqual(logged["id"], package["id"])

    def test_the_period_note_reports_both_counts_correctly(self):
        self.call("expenses_add", amount="1000", description="游泳课")
        package = self.call("classes_add", name="游泳课", class_count=10,
                            kind="period", query="游泳")
        self.call("classes_log", kind="missed_school", package_id=package["id"])
        for _ in range(2):
            note = self.call("classes_log", kind="missed_us",
                             package_id=package["id"])["note"]
        self.assertIn("1 cancelled by them", note)
        self.assertIn("2 skipped by us", note)

    def test_class_tools_coach_when_the_payment_is_missing(self):
        from mcp.server.fastmcp.exceptions import ToolError

        result = self.call("classes_add", name="足球课", class_count=5, query="足球")
        self.assertEqual(result["matched"], 0)
        self.assertIn("expenses_list", result["hint"])
        with self.assertRaises(ToolError) as ctx:
            run(self.mcp.call_tool("classes_log", {"kind": "nope", "query": "x"}))
        self.assertIn("missed_school", str(ctx.exception))

    def test_class_tool_descriptions_carry_bilingual_triggers(self):
        desc = {
            t.name: " ".join((t.description or "").split())
            for t in run(self.mcp.list_tools())
        }
        self.assertIn("还剩几节课", desc["classes_list"])
        self.assertIn("how many classes left", desc["classes_list"])
        self.assertIn("今天上了足球课", desc["classes_log"])
        # P3: a cross-reference is only guidance if it names something callable
        self.assertIn("classes_add", desc["classes_list"])
        self.assertIn("classes_log", desc["classes_list"])
        self.assertIn("expenses_add", desc["classes_add"])
        # the rate is derived, and the agent must not try to pass one
        self.assertIn("do not pass a rate", desc["classes_add"])

    def test_help_routes_class_questions(self):
        text = self.call("expenses_help")
        for anchor in ("classes_list", "classes_add", "classes_log",
                       "missed_school", "per_class"):
            self.assertIn(anchor, text)

    def test_write_results_carry_unpaid_total_note(self):
        added = self.call("expenses_add", amount="300", description="足球课")
        self.assertIn("unpaid total", added["note"])
        paid = self.call("expenses_mark_paid", expense_id=added["id"])
        self.assertIn("¥0.00", paid["note"])

    def test_error_strings_coach_the_agent(self):
        from mcp.server.fastmcp.exceptions import ToolError
        with self.assertRaises(ToolError) as ctx:
            run(self.mcp.call_tool("expenses_add", {"amount": "三百", "description": "x"}))
        self.assertIn("300块", str(ctx.exception))  # tells the agent what works
        with self.assertRaises(ToolError) as ctx:
            run(self.mcp.call_tool("expenses_add", {"amount": "300", "date": "昨天"}))
        self.assertIn("omit", str(ctx.exception))  # tells it dates can be omitted
        self.call("expenses_add", amount="300", description="足球课")
        with self.assertRaises(ToolError) as ctx:
            run(self.mcp.call_tool("expenses_update", {"query": "足球"}))
        self.assertIn("expenses_mark_paid", str(ctx.exception))  # redirects


class RefundAndCourseToolTests(unittest.TestCase):
    """v0.13.0: the five tools a real day needed and did not have, and the
    portal host the agent could not name. See tests/test_refunds.py for the
    store; these prove the same things survive the tool boundary, where the
    description, the note and the error string are all the agent reads."""

    def setUp(self):
        self.store = make_store()
        self.mcp = build_mcp(self.store)

    def call(self, tool, **args):
        return tool_payload(run(self.mcp.call_tool(tool, args)))

    def badminton(self):
        """The 2026-09-05 fixture: ¥3,600 for ten, five attended."""
        e = self.call("expenses_add", amount="3600", description="Badminton (8月-9月)",
                      date="2026-08-15", category="aden-sports", paid=True,
                      paid_date="2026-08-15", submitted_by="Matt")
        p = self.call("classes_add", name="羽毛球 (1:1)", class_count=10,
                      query="Badminton", changed_by="Matt")
        logged = self.call("classes_log", kind="attended", query="羽毛球",
                           dates=["2026-08-17", "2026-08-21", "2026-08-28",
                                  "2026-08-31", "2026-09-02"], logged_by="wife")
        return e, p, logged

    def test_the_badminton_day_replays_with_no_rebuild_and_no_portal(self):
        """The acceptance test, verbatim from the prompt."""
        e, p, logged = self.badminton()
        before = {ev["id"]: ev["created_at"] for ev in logged["logged_events"]}
        r = self.call("expenses_refund", query="Badminton (8月-9月)", amount=1800,
                      date="2026-09-05", resize_package_to=5, changed_by="Matt")
        self.assertEqual(r["id"], e["id"])
        self.assertEqual((r["gross_amount"], r["amount"], r["refunded"]),
                         (3600.0, 1800.0, 1800.0))
        s = r["package"]["summary"]
        self.assertEqual((s["class_count"], s["rate"], s["attended"], s["remaining"]),
                         (5, 360.0, 5, 0))
        self.assertIn("¥360.00", r["note"])
        self.assertIn(r["refund"]["id"], r["note"])
        after = {ev["id"]: ev["created_at"]
                 for ev in self.call("classes_list", verbose=True)["packages"][0]["events"]}
        self.assertEqual(after, before)
        # step 2 of the acceptance test, unchanged
        self.call("expenses_add", amount="1800", description="Badminton group 10 classes",
                  date="2026-09-04", category="aden-sports", submitted_by="wife")
        self.call("expenses_mark_paid", query="group", paid_date="2026-09-05")
        self.call("classes_add", name="羽毛球 (group)", class_count=10, query="group")
        # step 3: net ¥3,600 across the two, and the refund is an event
        total = self.call("expenses_list", query="badminton")["summary"]
        self.assertEqual((total["total"], total["paid"], total["count"]), (3600.0, 3600.0, 2))
        actions = [h["action"] for h in
                   self.call("expenses_history", expense_id=e["id"])["history"]]
        self.assertIn("refund", actions)
        self.assertNotIn("update", actions)
        self.assertEqual(actions[0], "create")

    def test_deleting_a_payment_with_a_live_course_names_the_tool_and_the_id(self):
        """The regression test the prompt asked for: the old error pointed at
        the portal's Classes tab, a surface the agent cannot reach."""
        from mcp.server.fastmcp.exceptions import ToolError

        _e, p, _logged = self.badminton()
        with self.assertRaises(ToolError) as ctx:
            run(self.mcp.call_tool("expenses_delete", {"query": "Badminton"}))
        text = str(ctx.exception)
        self.assertIn("classes_delete(package_id=", text)
        self.assertIn(p["id"], text)
        self.assertNotIn("Classes tab", text)
        self.assertEqual(len(self.call("expenses_list")["expenses"]), 1)

    def test_shrinking_a_course_below_its_log_is_refused_at_the_tool_boundary(self):
        from mcp.server.fastmcp.exceptions import ToolError

        _e, p, logged = self.badminton()
        last = [ev for ev in logged["logged_events"] if ev["date"] == "2026-09-02"][0]
        with self.assertRaises(ToolError) as ctx:
            run(self.mcp.call_tool("classes_update", {"query": "羽毛球", "class_count": 4}))
        text = str(ctx.exception)
        self.assertIn("classes_log_delete", text)
        self.assertIn(last["id"], text)
        self.assertEqual(self.call("classes_list")["packages"][0]["class_count"], 10)

    def test_a_refund_prefers_the_paid_match(self):
        self.call("expenses_add", amount="3600", description="Badminton", paid=True)
        self.call("expenses_add", amount="300", description="Badminton court")
        r = self.call("expenses_refund", query="Badminton", amount=100)
        self.assertEqual(r["gross_amount"], 3600.0)

    def test_a_refund_can_be_taken_back_by_the_id_in_its_result(self):
        e, _p, _l = self.badminton()
        r = self.call("expenses_refund", expense_id=e["id"], amount=1800)
        back = self.call("expenses_refund_delete", refund_id=r["refund"]["id"])
        self.assertEqual((back["amount"], back["refunded"]), (3600.0, 0.0))
        self.assertIn("¥3600.00", back["note"])
        from mcp.server.fastmcp.exceptions import ToolError
        with self.assertRaises(ToolError) as ctx:
            run(self.mcp.call_tool("expenses_refund_delete", {"refund_id": "nope"}))
        self.assertIn("expenses_history", str(ctx.exception))

    def test_a_refund_without_a_resize_says_the_reprice_out_loud(self):
        """The note is what the agent reads back. Leaving a pack at ten
        classes after a half refund is a real choice, never a silent one."""
        e, _p, _l = self.badminton()
        r = self.call("expenses_refund", expense_id=e["id"], amount=1800)
        self.assertFalse(r["package"]["resized"])
        self.assertIn("still 10 classes, now ¥180.00 each", r["note"])
        self.assertIn("resize_package_to", r["note"])
        self.assertIn("5 left", r["note"])

    def test_a_refund_on_a_term_fee_names_the_classes_it_may_have_settled(self):
        self.call("expenses_add", amount="2000", description="游泳课 秋季", paid=True,
                  category="aden-sports")
        p = self.call("classes_add", name="游泳课", class_count=8, kind="period", query="游泳")
        logged = self.call("classes_log", kind="missed_school", package_id=p["id"],
                           dates=["2026-09-01", "2026-09-08", "2026-09-15"])
        r = self.call("expenses_refund", query="游泳", amount=750, resize_package_to=5)
        note = r["note"]
        self.assertIn("term fee", note)
        self.assertIn("classes_log_delete", note)
        for ev in logged["logged_events"]:
            self.assertIn(ev["id"], note)
        self.assertIn("owed back = ¥750.00", note)   # honest about what it still claims
        desc = {t.name: " ".join((t.description or "").split())
                for t in run(self.mcp.list_tools())}
        self.assertIn("period", desc["expenses_refund"])
        self.assertIn("classes_log_delete", desc["expenses_refund"])

    def test_unpaying_a_refunded_row_is_refused_at_the_boundary(self):
        from mcp.server.fastmcp.exceptions import ToolError
        e, _p, _l = self.badminton()
        self.call("expenses_refund", expense_id=e["id"], amount=1800)
        with self.assertRaises(ToolError) as ctx:
            run(self.mcp.call_tool("expenses_mark_paid", {"expense_id": e["id"], "paid": False}))
        self.assertIn("expenses_refund_delete", str(ctx.exception))
        self.assertEqual(self.call("expenses_list")["summary"]["unpaid"], 0.0)
        desc = {t.name: " ".join((t.description or "").split())
                for t in run(self.mcp.list_tools())}
        self.assertIn("expenses_refund_delete", desc["expenses_mark_paid"])

    def test_the_course_notes_quote_the_gross_figure_under_paid(self):
        """"¥1800.00 paid" on a ¥3,600 payment is the sentence the refund
        table exists to stop (LESSONS §15)."""
        e = self.call("expenses_add", amount="3600", description="Badminton", paid=True)
        self.call("expenses_refund", expense_id=e["id"], amount=1800)
        added = self.call("classes_add", name="羽毛球", class_count=5, expense_id=e["id"])
        self.assertIn("¥3600.00 paid", added["note"])
        self.assertIn("¥1800.00 refunded", added["note"])
        self.assertNotIn("¥1800.00 paid", added["note"])
        gone = self.call("classes_delete", package_id=added["id"])
        self.assertIn("¥3600.00", gone["note"])
        self.assertIn("¥1800.00 refunded", gone["note"])

    def test_a_spoken_date_list_reaches_the_store_through_the_tool(self):
        """Typed as a list alone, pydantic refused the string with its own
        error before the store's tolerance could be reached."""
        self.call("expenses_add", amount="2200", description="足球课")
        p = self.call("classes_add", name="足球课", class_count=10, query="足球")
        out = self.call("classes_log", kind="attended", package_id=p["id"],
                        dates="2026-08-17, 2026-08-21")
        self.assertEqual(len(out["logged_events"]), 2)
        from mcp.server.fastmcp.exceptions import ToolError
        with self.assertRaises(ToolError) as ctx:
            run(self.mcp.call_tool("classes_log", {"kind": "attended", "package_id": p["id"],
                                                   "dates": []}))
        self.assertIn("omit dates", str(ctx.exception))
        self.assertEqual(self.call("classes_list")["packages"][0]["events_count"], 2)

    def test_a_query_search_still_honours_since_and_until_through_the_join(self):
        self.badminton()                                   # 2026-08-15, course 羽毛球
        self.call("expenses_add", amount="300", description="Badminton court",
                  date="2026-09-10")
        by_course = self.call("expenses_list", query="羽毛球")
        self.assertEqual([e["date"] for e in by_course["expenses"]], ["2026-08-15"])
        self.assertEqual(self.call("expenses_list", query="羽毛球", since="2026-09-01")
                         ["expenses"], [])
        both = self.call("expenses_list", query="badminton", until="2026-08-31")
        self.assertEqual([e["date"] for e in both["expenses"]], ["2026-08-15"])

    def test_undoing_a_resizing_refund_says_what_happened_to_the_course(self):
        """The live smoke session's finding: the undo reversed the money and
        left the pack at a rate nobody chose, and the note said nothing."""
        e = self.call("expenses_add", amount="1000", description="ZZTEST refund harness",
                      category="other", paid=True)
        p = self.call("classes_add", name="ZZTEST course", class_count=10, query="ZZTEST")
        self.call("classes_log", kind="attended", query="ZZTEST",
                  dates=["2026-09-01", "2026-09-02"])
        r = self.call("expenses_refund", query="ZZTEST", amount=500, resize_package_to=5)
        self.assertEqual(r["package"]["summary"]["rate"], 100.0)
        back = self.call("expenses_refund_delete", refund_id=r["refund"]["id"])
        self.assertEqual(back["amount"], 1000.0)
        self.assertTrue(back["package"]["restored"])
        self.assertIn("restored to 10 classes at ¥100.00 each", back["note"])
        self.assertIn("had set it to 5", back["note"])
        listed = self.call("classes_list")["packages"][0]
        self.assertEqual((listed["class_count"], listed["summary"]["rate"]), (10, 100.0))
        # the guarded case is said out loud too
        r2 = self.call("expenses_refund", expense_id=e["id"], amount=200, resize_package_to=5)
        self.call("classes_update", package_id=p["id"], class_count=7)
        left = self.call("expenses_refund_delete", refund_id=r2["refund"]["id"])
        self.assertFalse(left["package"]["restored"])
        self.assertIn("left at 7 classes", left["note"])
        self.assertIn("changed to 7", left["note"])
        self.assertIn("classes_update", left["note"])
        # and a refund that resized nothing keeps the old, shorter note
        r3 = self.call("expenses_refund", expense_id=e["id"], amount=1)
        plain = self.call("expenses_refund_delete", refund_id=r3["refund"]["id"])
        self.assertNotIn("package", plain)
        self.assertNotIn("classes", plain["note"])
        desc = {t.name: " ".join((t.description or "").split())
                for t in run(self.mcp.list_tools())}
        self.assertIn("class count it had before", desc["expenses_refund_delete"])
        self.assertNotIn("NOT resized back", desc["expenses_refund_delete"])
        self.assertIn("previous class count", " ".join(self.call("expenses_help").split()))

    def test_a_refund_on_an_unpaid_row_coaches_at_the_boundary(self):
        from mcp.server.fastmcp.exceptions import ToolError
        self.call("expenses_add", amount="300", description="足球课")
        with self.assertRaises(ToolError) as ctx:
            run(self.mcp.call_tool("expenses_refund", {"query": "足球", "amount": "50"}))
        self.assertIn("expenses_mark_paid", str(ctx.exception))

    def test_an_update_on_a_refunded_row_says_which_figure_it_changed(self):
        e, _p, _l = self.badminton()
        self.call("expenses_refund", expense_id=e["id"], amount=1800)
        out = self.call("expenses_update", expense_id=e["id"], amount="3500")
        self.assertEqual((out["gross_amount"], out["amount"]), (3500.0, 1700.0))
        self.assertIn("¥1700.00", out["note"])
        self.assertIn("¥1800.00", out["note"])

    def test_classes_list_is_light_by_default_and_full_when_asked(self):
        self.badminton()
        light = self.call("classes_list")
        self.assertNotIn("events", light["packages"][0])
        self.assertEqual(light["packages"][0]["events_count"], 5)
        self.assertIn("2026-09-02", light["packages"][0]["last_event"])
        self.assertIn("verbose=true", light["note"])
        full = self.call("classes_list", verbose=True)
        self.assertEqual(len(full["packages"][0]["events"]), 5)
        self.assertEqual(self.call("classes_list", query="nothing")["packages"], [])
        self.assertEqual(len(self.call("classes_list", query="羽毛")["packages"]), 1)

    def test_a_batch_log_says_how_many_it_wrote_and_returns_their_ids(self):
        _e, _p, logged = self.badminton()
        self.assertIn("logged 5 classes", logged["note"])
        self.assertIn("2026-08-17", logged["note"])
        self.assertEqual(len(logged["logged_events"]), 5)
        removed = self.call("classes_log_delete", event_id=logged["logged_events"][0]["id"])
        self.assertIn("removed 2026-08-17", removed["note"])
        self.assertEqual(removed["summary"]["remaining"], 6)

    def test_a_course_can_be_retired_and_removed_from_the_mcp(self):
        e, p, _l = self.badminton()
        retired = self.call("classes_update", query="羽毛球", archived=True)
        self.assertIn("ARCHIVED", retired["note"])
        self.assertEqual(self.call("classes_list")["packages"], [])
        self.assertEqual(len(self.call("classes_list", include_archived=True)["packages"]), 1)
        gone = self.call("classes_delete", package_id=p["id"], changed_by="Matt")
        self.assertTrue(gone["deleted"])
        self.assertIn("5 logged", gone["note"])
        self.assertIn(e["id"], gone["note"])
        self.assertEqual(self.call("expenses_history", expense_id=e["id"])["history"][-1]
                         ["action"], "package_delete")
        # and now the payment can go
        self.assertTrue(self.call("expenses_delete", expense_id=e["id"])["deleted"])

    def test_a_search_finds_a_payment_by_the_course_it_funds(self):
        """'Badminton' in the ledger, 羽毛球 in her head."""
        self.badminton()
        self.assertEqual(len(self.call("expenses_list", query="羽毛球")["expenses"]), 1)
        # …but a write tool still resolves on the payment's own words only
        self.assertEqual(self.call("expenses_mark_paid", query="羽毛球")["matched"], 0)

    def test_the_new_descriptions_carry_triggers_and_cross_references(self):
        desc = {
            t.name: " ".join((t.description or "").split())
            for t in run(self.mcp.list_tools())
        }
        self.assertIn("退了1800", desc["expenses_refund"])
        self.assertIn("refunded", desc["expenses_refund"])
        self.assertIn("resize_package_to", desc["expenses_refund"])
        self.assertIn("expenses_refund_delete", desc["expenses_refund"])
        self.assertIn("expenses_refund", desc["expenses_update"])
        self.assertIn("expenses_refund", desc["expenses_delete"])
        self.assertIn("classes_delete", desc["expenses_delete"])
        self.assertIn("课时改成5", desc["classes_update"])
        self.assertIn("archive", desc["classes_update"])
        self.assertIn("expenses_refund", desc["classes_update"])
        self.assertIn("classes_log_delete", desc["classes_log"])
        self.assertIn("dates=", desc["classes_log"])
        for name in ("classes_update", "classes_delete"):
            self.assertIn(name, desc["classes_list"])
        self.assertIn("verbose=true", desc["classes_list"])
        # the destructive ones say to confirm — that text is the only thing
        # an assistant reads before calling
        for name in ("expenses_refund_delete", "classes_delete", "classes_log_delete"):
            self.assertIn("confirm", desc[name].lower(), name)
        self.assertIn(".url", desc["expenses_mint_link"])
        self.assertNotIn("<this service>", desc["expenses_mint_link"])

    def test_the_new_annotations(self):
        tools = {t.name: t for t in run(self.mcp.list_tools())}
        self.assertFalse(tools["expenses_refund"].annotations.destructiveHint)
        self.assertFalse(tools["expenses_refund"].annotations.readOnlyHint)
        self.assertFalse(tools["classes_update"].annotations.destructiveHint)
        for name in ("expenses_refund_delete", "classes_delete", "classes_log_delete"):
            self.assertTrue(tools[name].annotations.destructiveHint, name)

    def test_help_routes_refunds_and_course_edits(self):
        text = self.call("expenses_help")
        for anchor in ("expenses_refund(", "退了1800", "resize_package_to",
                       "expenses_refund_delete", "classes_update(", "archived=true",
                       "classes_delete(", "classes_log_delete(", "dates=[",
                       "gross_amount", "EFFECTIVE"):
            self.assertIn(anchor, text, anchor)
        fix = run(self.mcp.get_prompt("xiufu"))
        prompt = " ".join(m.content.text for m in fix.messages if hasattr(m.content, "text"))
        for anchor in ("expenses_refund", "expenses_refund_delete", "classes_update",
                       "classes_log_delete"):
            self.assertIn(anchor, prompt, anchor)

    def test_the_portal_host_reaches_every_channel_when_configured(self):
        os.environ["PORTAL_BASE_URL"] = "https://family-expenses-test.a.run.app/"
        try:
            mcp = build_mcp(make_store())
            call = lambda tool, **a: tool_payload(run(mcp.call_tool(tool, a)))
            minted = call("expenses_mint_link", label="wife")
            self.assertEqual(
                minted["url"],
                f"https://family-expenses-test.a.run.app/t/{minted['token']}")
            self.assertNotIn("PORTAL_BASE_URL", minted["note"])
            links = call("expenses_list_links")
            self.assertEqual(links["portal"], "https://family-expenses-test.a.run.app/t/<token>")
            self.assertIn("family-expenses-test.a.run.app", links["note"])
            self.assertNotIn(minted["token"], json.dumps(links))
            self.assertIn("family-expenses-test.a.run.app/t/<token>", call("expenses_help"))
        finally:
            os.environ.pop("PORTAL_BASE_URL", None)

    def test_without_a_host_the_tools_say_so_instead_of_pretending(self):
        os.environ.pop("PORTAL_BASE_URL", None)
        mcp = build_mcp(make_store())
        call = lambda tool, **a: tool_payload(run(mcp.call_tool(tool, a)))
        minted = call("expenses_mint_link", label="wife")
        self.assertIn("PORTAL_BASE_URL", minted["url"])
        self.assertIn("PORTAL_BASE_URL", minted["note"])
        self.assertIsNone(call("expenses_list_links")["portal"])
        self.assertIn("PORTAL_BASE_URL is not set", call("expenses_help"))


class BearerMiddlewareTests(unittest.TestCase):
    """/mcp: open when MCP_SECRET unset (owner's accepted threat model),
    enforced when set. Other paths always untouched."""

    def make_client(self) -> TestClient:
        async def open_ok(request):
            return JSONResponse({"ok": True})

        inner = Starlette(routes=[
            Route("/healthz", open_ok, methods=["GET"]),
            Route("/mcp", open_ok, methods=["GET"]),
        ])
        return TestClient(McpBearerMiddleware(inner))

    def test_no_secret_means_open(self):
        os.environ.pop("MCP_SECRET", None)
        client = self.make_client()
        self.assertEqual(client.get("/mcp").status_code, 200)
        self.assertEqual(client.get("/healthz").status_code, 200)

    def test_secret_set_enforces_bearer(self):
        os.environ["MCP_SECRET"] = "s3cret"
        try:
            client = self.make_client()
            self.assertEqual(client.get("/mcp").status_code, 401)
            self.assertEqual(
                client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code, 401)
            self.assertEqual(
                client.get("/mcp", headers={"Authorization": "Bearer s3cret"}).status_code, 200)
            self.assertEqual(client.get("/healthz").status_code, 200)  # portal unaffected
        finally:
            os.environ.pop("MCP_SECRET", None)


class CompatibilityContractTests(unittest.TestCase):
    """FEATURE_CONTRACT §5.1 / A8: nothing may ever force a connected family
    member to reconnect. These pin the frozen surface — if one of these fails,
    the change would break her bookmark or connector."""

    def test_mcp_mount_path_is_frozen(self):
        self.assertEqual(McpBearerMiddleware(lambda s, r, w: None).prefix, "/mcp")

    def test_portal_path_shape_is_frozen(self):
        from app.web import build_routes
        paths = {r.path for r in build_routes(make_store())}
        self.assertIn("/t/{token}", paths)
        self.assertIn("/health", paths)
        self.assertIn("/healthz", paths)
        for name in ("list", "submit", "update", "mark-paid", "delete", "history"):
            self.assertIn(f"/api/{name}", paths)

    def test_default_posture_needs_no_credentials(self):
        # unset MCP_SECRET = open; minted tokens have no expiry
        os.environ.pop("MCP_SECRET", None)
        store = make_store()
        minted = store.mint_token(label="wife")
        self.assertIsNone(minted["expires_at"])

    def test_revocation_is_explicit_never_automatic(self):
        store = make_store()
        token = store.mint_token(label="wife")["token"]
        for _ in range(50):  # heavy use must never invalidate a link
            self.assertIsNotNone(store.validate_token(token))


class CombinedAppTests(unittest.TestCase):
    """build_asgi_app wires MCP + portal into one service."""

    def test_portal_routes_present_on_combined_app(self):
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
        os.environ.pop("MCP_SECRET", None)
        try:
            from app.main import build_asgi_app
            # context manager runs the MCP session-manager lifespan
            with TestClient(build_asgi_app()) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/t/badtoken").status_code, 404)
                # MCP open when no secret configured: transport answers (405
                # for plain GET without SSE accept), not 401/503 gatekeeping.
                self.assertNotIn(client.get("/mcp").status_code, (401, 503))
        finally:
            os.environ.pop("DATABASE_URL", None)

    def test_cloud_run_host_completes_initialize_and_tools_list(self):
        os.environ["DATABASE_URL"] = "sqlite:///:memory:"
        os.environ.pop("MCP_SECRET", None)
        try:
            from app.main import build_asgi_app

            headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            initialize = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "cloud-run-gate", "version": "1"},
                },
            }
            tools_list = {
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
            }
            initialized_notification = {
                "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}
            }
            with TestClient(
                build_asgi_app(),
                base_url="https://family-expenses-test.asia-southeast1.run.app",
            ) as client:
                initialized = client.post("/mcp", headers=headers, json=initialize)
                notified = client.post(
                    "/mcp", headers=headers, json=initialized_notification
                )
                listed = client.post("/mcp", headers=headers, json=tools_list)

            self.assertEqual(initialized.status_code, 200, initialized.text)
            self.assertEqual(notified.status_code, 202, notified.text)
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertIn("serverInfo", initialized.json()["result"])
            self.assertEqual(
                {tool["name"] for tool in listed.json()["result"]["tools"]},
                EXPECTED_TOOLS,
            )
        finally:
            os.environ.pop("DATABASE_URL", None)


if __name__ == "__main__":
    unittest.main()
