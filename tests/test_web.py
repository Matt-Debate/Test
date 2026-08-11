"""Web-tier tests — full HTTP path via Starlette TestClient, sqlite-backed."""

from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient  # noqa: E402

from app.db import Database  # noqa: E402
from app.store import Store  # noqa: E402
from app.web import build_app  # noqa: E402


def make_client() -> tuple[TestClient, Store, str]:
    db = Database("sqlite:///:memory:")
    db.init()
    store = Store(db)
    token = store.mint_token(label="wife")["token"]
    return TestClient(build_app(store)), store, token


class PortalPageTests(unittest.TestCase):
    def setUp(self):
        self.client, self.store, self.token = make_client()

    def test_healthz(self):
        for path in ("/health", "/healthz"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.json(), {"ok": True})

    def test_favicon_request_is_quiet(self):
        self.assertEqual(self.client.get("/favicon.ico").status_code, 204)

    def test_portal_served_for_valid_token(self):
        r = self.client.get(f"/t/{self.token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("家庭开支", r.text)

    def test_portal_rejects_bad_token(self):
        r = self.client.get("/t/nope")
        self.assertEqual(r.status_code, 404)
        self.assertIn("链接无效", r.text)

    def test_portal_rejects_revoked_token(self):
        self.store.revoke_token(self.token)
        self.assertEqual(self.client.get(f"/t/{self.token}").status_code, 404)


class ApiAuthTests(unittest.TestCase):
    def setUp(self):
        self.client, self.store, self.token = make_client()

    def test_all_endpoints_reject_missing_token(self):
        for name in ("list", "submit", "update", "mark-paid", "delete", "history"):
            r = self.client.post(f"/api/{name}", json={})
            self.assertEqual(r.status_code, 401, name)
            self.assertFalse(r.json()["ok"])

    def test_invalid_json_is_400(self):
        r = self.client.post(
            "/api/list", content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(r.status_code, 400)


class ApiFlowTests(unittest.TestCase):
    """A1 end-to-end: submit → list → edit → mark paid → history → delete."""

    def setUp(self):
        self.client, self.store, self.token = make_client()

    def post(self, name, **body):
        body["token"] = self.token
        return self.client.post(f"/api/{name}", json=body)

    def test_full_flow(self):
        r = self.post("submit", date="2026-07-14", amount=88.8,
                      description="小提琴课", submitted_by="Wei")
        self.assertEqual(r.status_code, 200)
        eid = r.json()["expense"]["id"]

        r = self.post("list")
        self.assertEqual(r.json()["summary"]["unpaid"], 88.8)
        self.assertEqual(len(r.json()["expenses"]), 1)

        r = self.post("update", id=eid, changed_by="Matt",
                      fields={"amount": 99.9, "category": "kids"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["expense"]["amount"], 99.9)

        r = self.post("mark-paid", id=eid, paid=True,
                      paid_date="2026-07-15", changed_by="Matt")
        self.assertTrue(r.json()["expense"]["paid"])

        r = self.post("list", status="unpaid")
        self.assertEqual(r.json()["expenses"], [])

        r = self.post("history", id=eid)
        self.assertEqual(
            [h["action"] for h in r.json()["history"]],
            ["create", "update", "mark_paid"],
        )

        r = self.post("delete", id=eid, changed_by="Matt")
        self.assertEqual(r.status_code, 200)
        r = self.post("list")
        self.assertEqual(r.json()["expenses"], [])

    def test_validation_maps_to_400(self):
        r = self.post("submit", date="2026-07-14", amount=-1)
        self.assertEqual(r.status_code, 400)
        r = self.post("submit", date="bad", amount=1)
        self.assertEqual(r.status_code, 400)
        r = self.post("mark-paid", id="whatever", paid=True)  # no paid_date
        self.assertEqual(r.status_code, 400)

    def test_missing_id_maps_to_404(self):
        for name, extra in (
            ("update", {"fields": {"amount": 1}}),
            ("mark-paid", {"paid": True, "paid_date": "2026-07-15"}),
            ("delete", {}),
        ):
            r = self.post(name, id="missing", **extra)
            self.assertEqual(r.status_code, 404, name)


class PortalEscapingTests(unittest.TestCase):
    """Regression guard for the stored-XSS fix in 0.4.5.

    render() builds list rows with innerHTML, so every server-supplied value has to
    pass through esc(). `category` is deliberately free-form end-to-end (MCP callers
    pass arbitrary strings and the ledger stores them verbatim), which makes the
    render-time escape the only control standing between a planted category and a
    stolen portal token.
    """

    PORTAL = Path(__file__).resolve().parent.parent / "app" / "portal.html"

    def setUp(self):
        self.html = self.PORTAL.read_text(encoding="utf-8")

    def test_every_use_of_catlabel_is_escaped_or_a_truthiness_guard(self):
        """Account for every `catLabel` occurrence; anything left over is a raw use.

        Deliberately not a `+ catLabel` adjacency check: the original bug read
        `esc(e.description) || catLabel || t("cat")`, where the raw use sits between
        `||` operators and no such pattern matches. Nor is `assertIn("esc(catLabel)")`
        sufficient — the escaped call already existed elsewhere in render() while the
        hole was open. Subtracting the known-safe forms is what actually discriminates.
        """
        offenders = []
        for lineno, line in enumerate(self.html.splitlines(), 1):
            if "catLabel" not in line:
                continue
            residue = line.replace("var catLabel", "")
            residue = residue.replace("esc(catLabel)", "")
            residue = re.sub(r"catLabel\s*\?", "", residue)  # truthiness guard only
            if "catLabel" in residue:
                offenders.append(f"  app/portal.html:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders, [],
            "catLabel reaches innerHTML unescaped — wrap it in esc():\n"
            + "\n".join(offenders),
        )

    def test_category_is_stored_verbatim_not_sanitized_on_write(self):
        """Escaping belongs at render, not on write.

        Mangling stored categories would corrupt the ledger and break MCP round-trips,
        so the API must keep the raw bytes and the portal must escape them.
        """
        client, _store, token = make_client()
        payload = "<img src=x onerror=alert(1)>"
        r = client.post("/api/submit", json={
            "token": token, "date": "2026-07-14", "amount": 5, "category": payload,
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["expense"]["category"], payload)


class CategoryParityTests(unittest.TestCase):
    """The portal's category list and app/store.py CATEGORIES must not drift.

    They are two hand-maintained lists in two languages. If the portal offers a
    key analytics does not group by (or vice versa), spending quietly lands in a
    bucket nobody looks at — the kind of wrong that never raises an error.
    """

    PORTAL = Path(__file__).resolve().parent.parent / "app" / "portal.html"

    def test_portal_offers_exactly_the_canonical_categories(self):
        from app.store import CATEGORY_KEYS

        html = self.PORTAL.read_text(encoding="utf-8")
        block = re.search(r"var CATS = \[(.*?)\];", html, re.S)
        self.assertIsNotNone(block, "could not find the CATS array in portal.html")
        portal_keys = tuple(re.findall(r'"([^"]+)"', block.group(1)))
        self.assertEqual(portal_keys, CATEGORY_KEYS)

    def test_portal_labels_every_category_in_both_languages(self):
        from app.store import CATEGORY_KEYS

        html = self.PORTAL.read_text(encoding="utf-8")
        for lang in ("zh", "en"):
            start = html.index(f"{lang}: {{")
            cat = html.index("cat:{", start)
            labels = html[cat:html.index("act:{", cat)]
            for key in CATEGORY_KEYS:
                self.assertRegex(
                    labels, rf'"?{re.escape(key)}"?\s*:',
                    f"{lang} is missing a label for category {key!r}",
                )

    def test_both_languages_carry_the_same_top_level_keys(self):
        """`cat:` and `ev:` each had a parity guard; the ~90 keys around them
        had none. Deleting `cls_confirm_unlog` from the English table left the
        whole suite green, and the confirm dialog then reads the literal key
        `cls_confirm_unlog` — in the language she does not use, which is the
        half nobody opens to check.
        """
        html = self.PORTAL.read_text(encoding="utf-8")
        tables = {}
        for lang in ("zh", "en"):
            start = html.index(f"{lang}: {{")
            # up to the nested `cat:` map — the flat keys are all above it
            flat = html[start:html.index("cat:{", start)]
            # keys sit several per line, so anchor on the delimiter before
            # each one rather than on the start of a line
            tables[lang] = set(
                re.findall(r'(?:^|[{,])\s*(\w+)\s*:\s*"', flat, re.M))
        self.assertGreater(len(tables["zh"]), 50, "key scan found almost nothing")
        self.assertEqual(tables["zh"] - tables["en"], set(),
                         "English is missing keys the Chinese table has")
        self.assertEqual(tables["en"] - tables["zh"], set(),
                         "Chinese is missing keys the English table has")

    def test_portal_has_no_demo_backend(self):
        """A design pass arrived carrying the artifact's in-memory demo store,
        reachable via `if (!TOKEN) return demoApi(...)`. It never fired in
        production — the portal is only served at /t/<token> — but for a ledger,
        silently accepting writes into a fake is the worst failure available,
        so it must not come back on the next handoff.
        """
        html = self.PORTAL.read_text(encoding="utf-8")
        for marker in ("DEMO_EXP", "demoApi", "demoInit", "demoSummary"):
            self.assertNotIn(marker, html, f"demo scaffolding present: {marker}")

    def test_portal_always_talks_to_the_real_api(self):
        html = self.PORTAL.read_text(encoding="utf-8")
        self.assertIn('fetch("/api/"', html)
        self.assertNotRegex(html, r"if\s*\(\s*!TOKEN\s*\)\s*return")

    def test_the_class_categories_are_real_categories(self):
        """CLASS_CATEGORIES filters the Classes tab's payment dropdown. A typo
        in it ('aden-sport') offers nothing and reads as an empty ledger, with
        no error anywhere to say why."""
        from app.store import CATEGORY_KEYS, CLASS_CATEGORIES

        for key in CLASS_CATEGORIES:
            self.assertIn(key, CATEGORY_KEYS,
                          f"{key!r} is not a category the portal can even write")

    def test_borrow_is_the_category_with_arithmetic(self):
        """The portal must special-case exactly the key the store does."""
        from app.store import BORROW_CATEGORY

        html = self.PORTAL.read_text(encoding="utf-8")
        self.assertIn(f'var BORROW = "{BORROW_CATEGORY}"', html)




class AuthorIsAuthoritativeTests(unittest.TestCase):
    """Cross-model review, finding 3: _author() gave the client's value
    precedence, so a request bearing the 'wife' link could write any name into
    the audit trail. The link's label is the only author now."""

    def setUp(self):
        self.client, self.store, self.token = make_client()

    def test_client_cannot_choose_its_own_author(self):
        r = self.client.post("/api/submit", json={
            "token": self.token, "date": "2026-08-11", "amount": 10,
            "submitted_by": "Mallory",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["expense"]["submitted_by"], "wife")

    def test_edits_are_attributed_to_the_link_too(self):
        eid = self.client.post("/api/submit", json={
            "token": self.token, "date": "2026-08-11", "amount": 10,
        }).json()["expense"]["id"]
        self.client.post("/api/update", json={
            "token": self.token, "id": eid, "changed_by": "Mallory",
            "fields": {"amount": 20},
        })
        actions = [(h.action, h.changed_by) for h in self.store.history(eid)]
        self.assertEqual(actions, [("create", "wife"), ("update", "wife")])

    def test_link_label_never_leaks_into_a_response(self):
        r = self.client.post("/api/list", json={"token": self.token})
        self.assertNotIn("_link_label", r.text)


class DocumentedCountsTests(unittest.TestCase):
    """The living docs quote a test count; it drifted four times in one session.

    Scoped to the three docs that carry the runnable command — CHANGELOG and
    FIRST_DEPLOY_PLAN quote historical counts on purpose and are left alone.
    """

    ROOT = Path(__file__).resolve().parent.parent
    LIVING = ("CLAUDE.md", "README.md", "docs/RUNBOOK.md")

    def actual_test_count(self) -> int:
        return sum(
            len(re.findall(r"^    def test_", p.read_text(encoding="utf-8"), re.M))
            for p in sorted((self.ROOT / "tests").glob("test_*.py"))
        )

    def test_the_lessons_file_exists_and_is_pointed_at(self):
        """`docs/LESSONS.md` is where a failure gets turned into a rule, and it
        only works if a session reads it. A pointer that rots is a file nobody
        opens — and the whole reason it exists is that the next session starts
        with no memory of what broke here.
        """
        lessons = self.ROOT / "docs" / "LESSONS.md"
        self.assertTrue(lessons.exists(), "docs/LESSONS.md is gone")
        body = lessons.read_text(encoding="utf-8")
        self.assertGreater(len(body), 2000, "LESSONS.md has been emptied")
        claude = (self.ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("docs/LESSONS.md", claude,
                      "CLAUDE.md no longer points at the lessons file")
        # every entry must carry the failure as well as the rule; a file of
        # bare rules is what CLAUDE.md already is
        self.assertIn("**What happened.**", body)
        self.assertIn("**Rule.**", body)
        self.assertEqual(body.count("**What happened.**"), body.count("**Rule.**"),
                         "an entry states a rule with no failure behind it, or vice versa")

    def test_the_contract_api_table_matches_the_routed_endpoints(self):
        """§6 is the API contract. An endpoint missing from it is undocumented
        surface; one listed that does not exist sends the next session looking
        for something that was never built. `midnight_in` reached the table
        only because someone remembered."""
        import re as _re

        from app.api import HANDLERS

        body = (self.ROOT / "docs" / "FEATURE_CONTRACT.md").read_text(encoding="utf-8")
        documented = set(_re.findall(r"\| `/api/([a-z-]+)`", body))
        self.assertEqual(documented, set(HANDLERS),
                         "the contract's API table and the routed endpoints differ")

    def test_documented_tool_count_matches_reality(self):
        """The suite pinned the TEST count and nothing else, so the MCP tool
        count drifted instead: README advertised 10 while 13 shipped. The
        changelog records the same failure happening once before, to the
        contract and the runbook. Pin every count a living doc asserts, not
        only the one that bit you last time.
        """
        import re as _re

        source = (self.ROOT / "app" / "mcp_server.py").read_text(encoding="utf-8")
        actual = len({
            name for name in _re.findall(r"^    def (\w+)\(", source, _re.M)
            if name.startswith(("expenses_", "classes_"))
        })
        for name in self.LIVING + ("docs/FEATURE_CONTRACT.md", "docs/MCP_DESIGN.md"):
            body = (self.ROOT / name).read_text(encoding="utf-8")
            for claim in _re.findall(r"(\d+) tools", body):
                with self.subTest(doc=name, claim=claim):
                    self.assertEqual(int(claim), actual,
                                     f"{name} advertises {claim} tools, {actual} exist")

    def test_documented_test_count_matches_reality(self):
        actual = self.actual_test_count()
        for name in self.LIVING:
            text = (self.ROOT / name).read_text(encoding="utf-8")
            # any "N tests" claim, not just the one in the run command —
            # a prose mention in the status section drifted the same day this
            # guard was written
            for quoted in re.findall(r"(\d+) tests", text):
                self.assertEqual(
                    int(quoted), actual,
                    f"{name} advertises {quoted} tests; the suite has {actual}",
                )

    def test_documented_tool_count_matches_the_server(self):
        mcp_src = (self.ROOT / "app" / "mcp_server.py").read_text(encoding="utf-8")
        actual = mcp_src.count("@mcp.tool")
        claude_md = (self.ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(f"{actual} tools", claude_md)


class ServerDecidesTodayTests(unittest.TestCase):
    """Backlog (closed in v0.9.0) — the portal read the device clock for every date
    decision while the server used APP_TZ, so the two could disagree."""

    def setUp(self):
        self.client, self.store, self.token = make_client()
        self.portal = (
            Path(__file__).resolve().parent.parent / "app" / "portal.html"
        ).read_text(encoding="utf-8")

    def test_list_response_carries_the_households_today(self):
        from app.store import today_str

        r = self.client.post("/api/list", json={"token": self.token})
        self.assertEqual(r.json()["today"], today_str())

    def test_list_today_follows_app_tz_not_the_server_clock(self):
        import os

        original = os.environ.get("APP_TZ")
        try:
            os.environ["APP_TZ"] = "Pacific/Kiritimati"
            east = self.client.post("/api/list", json={"token": self.token}).json()["today"]
            os.environ["APP_TZ"] = "Pacific/Niue"
            west = self.client.post("/api/list", json={"token": self.token}).json()["today"]
        finally:
            if original is None:
                os.environ.pop("APP_TZ", None)
            else:
                os.environ["APP_TZ"] = original
        self.assertNotEqual(east, west)

    def test_portal_prefers_the_server_date_over_the_device(self):
        self.assertIn("serverToday = j.today;", self.portal)
        # only one `new Date()` may survive — the first-paint fallback inside
        # todayStr(); every other date decision must route through todayStr()
        self.assertEqual(
            self.portal.count("new Date()"), 1,
            "a date decision is still reading the phone's clock directly",
        )

    def test_returning_to_a_backgrounded_tab_resyncs(self):
        self.assertIn('document.addEventListener("visibilitychange"', self.portal)

    def test_the_clock_is_read_exactly_once_per_list(self):
        """Read twice, a request straddling midnight buckets its rows against
        one day and labels them with the next. Counting the reads is the only
        way to pin this — both values look right in isolation."""
        from app import api as api_module
        from app import store as store_module

        calls = []

        def counting_today():
            calls.append(1)
            return "2026-08-11"

        # patch the CLOCK, not today_str: `today` and `midnight_in` were read
        # through two different helpers, so counting today_str calls said "1"
        # while the wall clock had been read twice. A request straddling
        # midnight then returned yesterday beside a whole day remaining, which
        # tells the page to hold yesterday for another day.
        from datetime import datetime, timezone

        def counting_clock():
            calls.append(1)
            return datetime(2026, 8, 11, 23, 50, tzinfo=timezone.utc)

        real = store_module._household_now
        store_module._household_now = counting_clock
        try:
            r = self.client.post("/api/list", json={"token": self.token})
        finally:
            store_module._household_now = real
        body = r.json()
        self.assertEqual(body["today"], "2026-08-11")
        self.assertEqual(body["midnight_in"], 600)
        self.assertEqual(len(calls), 1, f"clock read {len(calls)} times, expected 1")

        del api_module, counting_today   # the old patch targets, now unused

    def test_the_clock_is_read_once_for_an_OVERDUE_list_too(self):
        """`status="all"` never enters the overdue branch, so the test above
        cannot prove its own claim. `Store.list` read the clock again there,
        and a request straddling midnight then dropped a newly-overdue row from
        the rows AND their totals while labelling the response the other day.
        """
        from app import store as store_module

        calls = []
        from datetime import datetime, timezone

        def counting_clock():
            calls.append(1)
            return datetime(2026, 8, 11, 23, 59, 59, tzinfo=timezone.utc)

        real = store_module._household_now
        store_module._household_now = counting_clock
        try:
            r = self.client.post("/api/list",
                                 json={"token": self.token, "status": "overdue"})
        finally:
            store_module._household_now = real
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["today"], "2026-08-11")
        self.assertEqual(len(calls), 1, f"clock read {len(calls)} times, expected 1")

    def test_the_response_says_how_much_of_today_is_left(self):
        """Without it the page rolls the date over 24h after the response
        instead of at midnight — a page opened at 23:50 offers yesterday all
        the next day. Both endpoints that carry `today` must carry it."""
        for endpoint in ("list", "classes-list"):
            with self.subTest(endpoint=endpoint):
                body = self.client.post(f"/api/{endpoint}",
                                        json={"token": self.token}).json()
                self.assertIn("midnight_in", body)
                self.assertIsInstance(body["midnight_in"], int)
                self.assertGreater(body["midnight_in"], 0)
                self.assertLessEqual(body["midnight_in"], 86400)

    def test_history_shows_when_an_already_paid_row_was_paid(self):
        """Collapsing create+mark_paid into one entry hid the payment date the
        surviving row carries — the trail stopped saying when money moved."""
        self.assertIn("(snap.paid && snap.paid_date)", self.portal)
        self.assertIn("esc(snap.paid_date)", self.portal)  # P6: never interpolate raw


@unittest.skipUnless(shutil.which("node"), "node not available to run the portal's JS")
class PortalDateArithmeticTests(unittest.TestCase):
    """Executes the portal's own date functions instead of grepping for them.

    Every other guarantee about `portal.html` is pinned by a source-string
    assertion, which is how the first version of this feature shipped a
    regression: it cached the server's date forever, so a tab left open across
    midnight kept offering yesterday as the payment date — and an accepted
    default writes a wrong date into the ledger. A string check cannot observe
    time passing. This runs the real code.
    """

    PORTAL = Path(__file__).resolve().parent.parent / "app" / "portal.html"

    def date_fns(self) -> str:
        """addDays + todayStr, lifted verbatim from the page."""
        src = self.PORTAL.read_text(encoding="utf-8")
        start = src.index("  function addDays(ymd, n) {")
        end = src.index("  function daysBetween(")
        block = src[start:end]
        self.assertIn("function todayStr()", block, "todayStr moved; fix the slice")
        return block

    def run_js(self, setup: str, expr: str) -> str:
        script = (
            "var serverToday = null, serverTodayAt = 0, resyncing = false;\n"
            # null = the pre-v0.11.0 behaviour (roll over on elapsed hours);
            # a test that cares about midnight sets it in `setup`
            "var serverMidnightIn = null;\n"
            "var refresh = function () { throw new Error('unexpected refetch'); };\n"
            + self.date_fns()
            + "\n" + setup + "\nconsole.log(String(" + expr + "));\n"
        )
        out = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_the_date_rolls_over_at_midnight_not_24h_after_the_response(self):
        """The page loads at 23:50 and she logs a class ten minutes later. On
        elapsed hours alone that is still "yesterday" — and stays yesterday
        until 23:50 the NEXT day. A date says nothing about how much of it is
        left, so the server sends the seconds remaining with it.
        """
        setup = (
            'serverToday = "2026-08-31"; serverTodayAt = 1000000;\n'
            "serverMidnightIn = 600;\n"                       # 23:50 in Shanghai
            "Date.now = function () { return 1000000 + 601 * 1000; };"
        )
        self.assertEqual(self.run_js(setup, "todayStr()"), "2026-09-01")

    def test_midnight_itself_is_already_the_next_day(self):
        """The boundary, pinned explicitly: at exactly the remaining seconds
        the day HAS turned. Off by one here is a whole day of classes filed
        under yesterday."""
        setup = (
            'serverToday = "2026-08-31"; serverTodayAt = 1000000;\n'
            "serverMidnightIn = 600;\n"
            "Date.now = function () { return 1000000 + 600 * 1000; };"
        )
        self.assertEqual(self.run_js(setup, "todayStr()"), "2026-09-01")

    def test_the_date_holds_right_up_to_midnight(self):
        """The other side of it: one second early is still today, or every
        evening would file its classes under tomorrow."""
        setup = (
            'serverToday = "2026-08-31"; serverTodayAt = 1000000;\n'
            "serverMidnightIn = 600;\n"
            "Date.now = function () { return 1000000 + 599 * 1000; };"
        )
        self.assertEqual(self.run_js(setup, "todayStr()"), "2026-08-31")

    def test_a_long_evening_session_still_caps_at_one_day(self):
        """The ±1-day cap is what stops a wrong device clock writing a date
        into the future; the midnight offset must not slip past it."""
        setup = (
            'serverToday = "2026-08-31"; serverTodayAt = 1000000;\n'
            "serverMidnightIn = 600;\n"
            "var refetched = 0; refresh = function () { refetched++; };\n"
            "Date.now = function () { return 1000000 + (600 + 5 * 86400) * 1000; };"
        )
        self.assertEqual(self.run_js(setup, "todayStr()"), "2026-09-01")

    def test_the_date_advances_while_the_page_stays_open(self):
        """The regression this class exists for: 25 hours later must be the
        next day, not the day the page happened to load."""
        setup = (
            'serverToday = "2026-08-31"; serverTodayAt = 1000000;\n'
            "Date.now = function () { return 1000000 + 25 * 3600 * 1000; };"
        )
        self.assertEqual(self.run_js(setup, "todayStr()"), "2026-09-01")

    def test_the_date_holds_within_the_same_day(self):
        setup = (
            'serverToday = "2026-08-31"; serverTodayAt = 1000000;\n'
            "Date.now = function () { return 1000000 + 23 * 3600 * 1000; };"
        )
        self.assertEqual(self.run_js(setup, "todayStr()"), "2026-08-31")

    def test_a_forward_clock_jump_cannot_push_the_date_into_the_future(self):
        """Elapsed time is only a proxy for a date: an NTP correction or a
        manual clock change mid-session reads as time passing. Unbounded, that
        writes a payment date in a month that has not happened yet."""
        for jump_days in (3, 40, 400):
            with self.subTest(jump_days=jump_days):
                setup = (
                    'serverToday = "2026-08-31"; serverTodayAt = 0;\n'
                    "refresh = function () {};\n"
                    "Date.now = function () { return %d * 86400000 + 3600000; };"
                    % jump_days
                )
                self.assertEqual(self.run_js(setup, "todayStr()"), "2026-09-01")

    def test_exceeding_the_cap_refetches_exactly_once(self):
        """The clamp keeps the date safe; the refetch is what makes it correct
        again. Removing the refetch left the whole suite green, so it needed
        its own guard — and the one-shot latch is what stops a render storm."""
        setup = (
            'serverToday = "2026-08-31"; serverTodayAt = 0;\n'
            "var calls = 0; refresh = function () { calls++; };\n"
            "Date.now = function () { return 9 * 86400000; };\n"
            "todayStr(); todayStr(); todayStr();"
        )
        self.assertEqual(self.run_js(setup, "calls"), "1")

    def test_the_date_rolls_over_a_year_boundary(self):
        setup = (
            'serverToday = "2026-12-31"; serverTodayAt = 0;\n'
            "Date.now = function () { return 26 * 3600 * 1000; };"
        )
        self.assertEqual(self.run_js(setup, "todayStr()"), "2027-01-01")

    def test_add_days_is_dst_proof(self):
        """Parsed and formatted in UTC on purpose: a local-time round trip
        across a DST boundary lands on the wrong calendar day."""
        for tz in ("America/New_York", "Europe/London", "Asia/Shanghai"):
            with self.subTest(tz=tz):
                out = subprocess.run(
                    ["node", "-e",
                     "var serverToday=null, serverTodayAt=0;\n" + self.date_fns()
                     + '\nconsole.log(addDays("2026-03-07", 1), addDays("2026-11-01", 1));'],
                    capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": tz},
                )
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(out.stdout.strip(), "2026-03-08 2026-11-02")

    def test_without_a_server_date_it_falls_back_to_the_device(self):
        """`new Date()` does not route through `Date.now` in V8, so stubbing
        the clock proves nothing here — compare against the real device date."""
        import datetime

        out = self.run_js("", "todayStr()")
        self.assertEqual(out, datetime.date.today().strftime("%Y-%m-%d"))


class HandlersRunOffTheEventLoopTests(unittest.TestCase):
    """Backlog (closed in v0.9.0) — every API handler is synchronous and hits the
    database; awaiting them inline blocked the loop for the whole round trip
    to Neon, so one slow query stalled every other request in the process."""

    def test_blocking_store_calls_are_dispatched_to_a_thread(self):
        src = (
            Path(__file__).resolve().parent.parent / "app" / "web.py"
        ).read_text(encoding="utf-8")
        self.assertIn("await run_in_threadpool(handler, store, body)", src)
        self.assertIn("await run_in_threadpool(store.validate_token, token)", src)
        self.assertNotIn("status, payload = handler(store, body)", src)

    def test_the_api_still_works_through_the_threadpool(self):
        client, store, token = make_client()
        r = client.post("/api/submit", json={
            "token": token, "date": "2026-08-01", "amount": 42, "description": "水电",
        })
        self.assertEqual(r.status_code, 200, r.text)
        listed = client.post("/api/list", json={"token": token}).json()
        self.assertEqual(len(listed["expenses"]), 1)
        self.assertEqual(listed["summary"]["unpaid"], 42.0)


class ConstraintHardeningTests(unittest.TestCase):
    """Backlog (closed in v0.9.0) — the seq uniqueness constraint must apply, but must
    never be able to stop a live portal from starting."""

    def test_constraint_applies_on_a_fresh_database(self):
        from app.db import Database

        db = Database("sqlite:///:memory:")
        db.init()
        self.assertEqual(db._apply_hardening(), [], "constraint failed to apply")

    def test_unappliable_constraint_degrades_to_a_warning(self):
        """Data that already violates it must not take the service down."""
        import io
        from contextlib import redirect_stderr

        from app.db import Database
        from app.store import Store

        db = Database("sqlite:///:memory:")
        db.init()
        store = Store(db)
        exp = store.create(date="2026-08-01", amount=10)
        # forge the duplicate the constraint exists to prevent, behind its back
        with db.tx() as tx:
            tx.execute("DROP INDEX uq_expense_history_expense_seq")
            tx.execute(
                "INSERT INTO expense_history "
                "(id, expense_id, seq, action, changed_by, changed_at, snapshot) "
                "VALUES ('dup', :eid, 0, 'update', NULL, '2026-08-01T00:00:00', '{}')",
                {"eid": exp.id},
            )
        buf = io.StringIO()
        with redirect_stderr(buf):
            failed = db._apply_hardening()
        # name the statement, not just the count — otherwise this passes for
        # the wrong reason the moment hardening.sql gains a second entry
        self.assertEqual(len(failed), 1)
        self.assertIn("uq_expense_history_expense_seq", failed[0])
        self.assertIn("WARNING", buf.getvalue())
        # and the app still serves
        self.assertEqual(len(store.list()), 1)


class ClassTrackerApiTests(unittest.TestCase):
    """Tab 4 over the real HTTP path."""

    def setUp(self):
        self.client, self.store, self.token = make_client()

    def post(self, _endpoint, **body):
        # underscored: this feature's bodies carry a "name" field, which would
        # otherwise bind to the positional and raise TypeError
        body["token"] = self.token
        return self.client.post(f"/api/{_endpoint}", json=body)

    def a_payment(self, amount=2200, description="足球课"):
        return self.post("submit", date="2026-08-03", amount=amount,
                         description=description, category="aden-sports"
                         ).json()["expense"]["id"]

    def test_full_flow(self):
        eid = self.a_payment()
        r = self.post("classes-add", expense_id=eid, name="足球课",
                      kind="per_class", class_count=10, period_label="8月")
        self.assertEqual(r.status_code, 200, r.text)
        pid = r.json()["package"]["id"]

        r = self.post("classes-log", package_id=pid, kind="attended", date="2026-08-05")
        s = r.json()["package"]["summary"]
        self.assertEqual((s["remaining"], s["remaining_amount"]), (9, 1980.0))

        listed = self.post("classes-list").json()
        self.assertEqual(len(listed["packages"]), 1)
        self.assertTrue(listed["today"])

        event_id = listed["packages"][0]["events"][0]["id"]
        self.assertEqual(self.post("classes-unlog", event_id=event_id).status_code, 200)
        self.assertEqual(
            self.post("classes-list").json()["packages"][0]["summary"]["remaining"], 10
        )

        self.assertEqual(self.post("classes-delete", id=pid).status_code, 200)
        self.assertEqual(self.post("classes-list").json()["packages"], [])

    def test_every_field_the_form_sends_survives_the_round_trip(self):
        """The store pins these; the HTTP layer was trusted to pass them
        through. Dropping `date`, `period_label` or `note` in app/api.py left
        all 210 tests green while silently discarding what she typed."""
        eid = self.a_payment()
        pid = self.post("classes-add", expense_id=eid, name="足球课",
                        kind="per_class", class_count=10,
                        period_label="8月").json()["package"]["id"]
        r = self.post("classes-log", package_id=pid, kind="missed_school",
                      date="2026-08-05", note="下雨停课")
        package = r.json()["package"]
        self.assertEqual(package["period_label"], "8月")
        self.assertEqual(package["name"], "足球课")
        self.assertEqual(package["class_count"], 10)
        event = package["events"][0]
        self.assertEqual(event["date"], "2026-08-05")
        self.assertEqual(event["note"], "下雨停课")
        self.assertEqual(event["kind"], "missed_school")

    def test_archiving_is_respected_at_the_http_boundary(self):
        eid = self.a_payment()
        pid = self.post("classes-add", expense_id=eid, name="足球课",
                        kind="per_class", class_count=10).json()["package"]["id"]
        self.post("classes-update", id=pid, fields={"archived": True})
        self.assertEqual(self.post("classes-list").json()["packages"], [])
        widened = self.post("classes-list", include_archived=True).json()
        self.assertEqual([p["archived"] for p in widened["packages"]], [True])
        # and the payment stays spoken for, so it is not offered again
        self.assertNotIn(eid, [c["id"] for c in widened["candidates"]])

    def test_candidates_exclude_payments_already_tracked(self):
        tracked = self.a_payment(description="足球课")
        free = self.a_payment(amount=99, description="游泳课")
        self.post("classes-add", expense_id=tracked, name="足球课",
                  kind="per_class", class_count=10)
        ids = [c["id"] for c in self.post("classes-list").json()["candidates"]]
        self.assertEqual(ids, [free], "a tracked payment must not be offered again")

    def test_candidates_offer_only_course_categories(self):
        """Unfiltered, the dropdown was every expense, newest date first — and
        the live ledger carries twelve monthly living-expense rows dated out to
        2027-07-31, which buried the four payments that were actually courses.

        `a_payment` already posts aden-sports, so a test built only on it stays
        green whether the filter exists or not; the rent row is the whole point.
        """
        rent = self.post("submit", date="2027-07-31", amount=22000,
                         description="Living expenses (2027 8月)",
                         category="living").json()["expense"]["id"]
        piano = self.post("submit", date="2026-08-20", amount=3800,
                          description="Piano", category="aden-edu"
                          ).json()["expense"]["id"]
        football = self.a_payment()  # aden-sports
        ids = [c["id"] for c in self.post("classes-list").json()["candidates"]]
        self.assertNotIn(rent, ids, "a living-expense row was offered as a course")
        self.assertEqual(sorted(ids), sorted([piano, football]),
                         "both course categories must be offered, and only those")

    def test_candidate_categories_are_matched_case_insensitively(self):
        """`category` is free text and the MCP drifts from the canonical keys —
        the live ledger holds rows written as 'living expenses'. A payment that
        silently fails to appear in the dropdown cannot explain itself."""
        odd = self.post("submit", date="2026-08-20", amount=500,
                        description="Swim", category=" Aden-Sports "
                        ).json()["expense"]["id"]
        ids = [c["id"] for c in self.post("classes-list").json()["candidates"]]
        self.assertIn(odd, ids)

    def test_portal_write_is_attributed_from_the_link_not_the_client(self):
        """Same rule as expenses: the label on the token wins (A8/P4)."""
        eid = self.a_payment()
        pid = self.post("classes-add", expense_id=eid, name="足球课",
                        kind="per_class", class_count=10).json()["package"]["id"]
        r = self.post("classes-log", package_id=pid, kind="attended",
                      logged_by="Mallory")
        self.assertEqual(r.json()["package"]["events"][0]["logged_by"], "wife")

    def test_errors_map_to_status_codes(self):
        self.assertEqual(
            self.post("classes-add", expense_id="nope", name="x",
                      kind="per_class", class_count=1).status_code, 404)
        eid = self.a_payment()
        self.assertEqual(
            self.post("classes-add", expense_id=eid, name="x",
                      kind="weekly", class_count=1).status_code, 400)
        self.assertEqual(self.post("classes-delete", id="nope").status_code, 404)
        self.assertEqual(self.post("classes-unlog", event_id="nope").status_code, 404)

    def test_deleting_a_tracked_payment_is_refused_with_a_reason(self):
        eid = self.a_payment()
        self.post("classes-add", expense_id=eid, name="足球课",
                  kind="per_class", class_count=10)
        r = self.post("delete", id=eid)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Remove that course first", r.json()["error"])


@unittest.skipUnless(shutil.which("node"), "node not available to run the portal's JS")
class ClassLineRenderingTests(unittest.TestCase):
    """Runs the portal's own `clsLine` instead of grepping for it.

    Mutation testing found six ways to corrupt this tab's money display that
    the whole suite let through — showing `owed_amount` where the reclaimable
    half belongs, rendering a period package through the per_class branch,
    dropping the cents. Every one lived in code no test executed.
    """

    PORTAL = Path(__file__).resolve().parent.parent / "app" / "portal.html"

    def cls_line(self, package: dict) -> dict:
        import json

        src = self.PORTAL.read_text(encoding="utf-8")
        start = src.index("  function clsLine(p) {")
        end = src.index("  function renderClasses() {")
        block = src[start:end]
        self.assertIn("function clsLine", block, "block markers moved")
        script = (
            # stubs: the labels are i18n keys, the money format is the portal's
            'function t(k) { return k; }\n'
            'function money(n) { return "¥" + Number(n).toFixed(2); }\n'
            + block
            + f"\nconsole.log(JSON.stringify(clsLine({json.dumps(package)})));\n"
        )
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_a_per_class_pack_shows_what_is_left(self):
        line = self.cls_line({"kind": "per_class", "summary": {
            "remaining": 7, "class_count": 10, "rate": 220.0, "used": 3,
            "overrun": 0, "remaining_amount": 1540.0,
        }})
        self.assertEqual(line["big"], "7/10")
        self.assertIn("¥1540.00", line["amt"])   # cents, not ¥1540 rounded
        self.assertIn("¥220.00", line["sub"])

    def test_a_period_package_shows_what_is_owed_and_the_split(self):
        line = self.cls_line({"kind": "period", "summary": {
            "owed": 3, "owed_amount": 750.0, "rate": 250.0,
            "reclaimable": 2, "reclaimable_amount": 500.0,
            "forfeited": 1, "forfeited_amount": 250.0,
        }})
        self.assertEqual(line["big"], "3")
        self.assertIn("¥750.00", line["amt"])
        # the two halves must be their own figures — swapping in owed_amount
        # here was a silent money corruption no test noticed
        self.assertIn("¥500.00", line["sub"])
        self.assertIn("¥250.00", line["sub"])
        self.assertNotIn("¥750.00", line["sub"])
        self.assertNotIn("undefined", line["sub"] + line["amt"] + line["big"])

    def test_the_two_kinds_do_not_render_through_each_other(self):
        """Inverting the branch made a period package print 'undefined/8'."""
        for package in (
            {"kind": "period", "summary": {
                "owed": 0, "owed_amount": 0.0, "rate": 250.0,
                "reclaimable": 0, "reclaimable_amount": 0.0,
                "forfeited": 0, "forfeited_amount": 0.0}},
            {"kind": "per_class", "summary": {
                "remaining": 0, "class_count": 4, "rate": 100.0, "used": 4,
                "overrun": 0, "remaining_amount": 0.0}},
        ):
            with self.subTest(kind=package["kind"]):
                line = self.cls_line(package)
                self.assertNotIn("undefined", "".join(line.values()))
                self.assertNotIn("NaN", "".join(line.values()))

    def test_cents_are_never_rounded_away(self):
        """The server says ¥666.67; whole-yuan rounding showed ¥667 on the tab
        whose job is telling a school what it owes."""
        line = self.cls_line({"kind": "period", "summary": {
            "owed": 2, "owed_amount": 666.67, "rate": 333.33,
            "reclaimable": 1, "reclaimable_amount": 333.33,
            "forfeited": 1, "forfeited_amount": 333.34,
        }})
        self.assertIn("¥666.67", line["amt"])
        self.assertIn("¥333.33", line["sub"])

    def test_an_overrun_is_surfaced_not_swallowed(self):
        line = self.cls_line({"kind": "per_class", "summary": {
            "remaining": 0, "class_count": 2, "rate": 100.0, "used": 3,
            "overrun": 1, "remaining_amount": 0.0,
        }})
        self.assertIn("cls_over", line["sub"])

    def test_a_capped_period_row_says_that_it_is_capped(self):
        """The counts are uncapped and the money is not, so '(5)' beside
        ¥0.00 reads as a bug unless the row says why."""
        line = self.cls_line({"kind": "period", "summary": {
            "owed": 6, "owed_amount": 800.0, "rate": 400.0,
            "reclaimable": 5, "reclaimable_amount": 800.0,
            "forfeited": 1, "forfeited_amount": 0.0, "overrun": 4,
        }})
        self.assertIn("cls_capped", line["sub"])

    def test_the_period_row_shows_three_distinct_figures(self):
        """Earlier fixtures used rate == reclaimable == forfeited, so swapping
        any one for another still passed every assertion."""
        line = self.cls_line({"kind": "period", "summary": {
            "owed": 3, "owed_amount": 1200.0, "rate": 400.0,
            "reclaimable": 2, "reclaimable_amount": 800.0,
            "forfeited": 1, "forfeited_amount": 100.0, "overrun": 0,
        }})
        self.assertIn("¥400.00", line["sub"])   # the rate, its own value
        self.assertIn("¥800.00", line["sub"])   # reclaimable
        self.assertIn("¥100.00", line["sub"])   # forfeited
        self.assertIn("¥1200.00", line["amt"])  # the total, only in the total


@unittest.skipUnless(shutil.which("node"), "node not available to run the portal's JS")
class ClassRowRenderingTests(unittest.TestCase):
    """Runs the real `renderClasses` and inspects the HTML it produces.

    The click handler is covered separately, but the RENDER half was not:
    forcing `isOpen` to false — so every course row shows no buttons and no
    class log, permanently — left the whole suite green.
    """

    PORTAL = Path(__file__).resolve().parent.parent / "app" / "portal.html"

    def render(self, open_ids: list, seed: str = "", read: str = None) -> str:
        import json

        src = self.PORTAL.read_text(encoding="utf-8")
        block = src[src.index("  function clsLine(p) {"):src.index("  function render() {")]
        self.assertIn("function renderClasses()", block, "block markers moved")
        package = {
            "id": "p1", "name": "足球课", "period_label": "8月",
            "kind": "per_class", "events": [
                {"id": "e1", "date": "2026-08-05", "kind": "attended",
                 "logged_by": "wife", "note": None},
            ],
            # every real payload carries this (INNER JOIN in _PACKAGE_SELECT);
            # without it a fixture passes only because `period_label ||` never
            # evaluates the right-hand side
            "expense": {"id": "x1", "date": "2026-08-20", "amount": 2200.0,
                        "description": "足球课", "category": "aden-sports",
                        "paid": False},
            "summary": {"remaining": 9, "class_count": 10, "rate": 220.0,
                        "used": 1, "overrun": 0, "remaining_amount": 1980.0},
        }
        harness = f"""
var packages = [{json.dumps(package)}];
var candidates = [{{"id":"x1","description":"足球课","amount":2200,"date":"2026-08-03","category":"aden-sports"}}];
var openPkgs = {json.dumps({i: True for i in open_ids})};
var clsDates = {{}}, clsBusy = {{}};
function todayStr() {{ return "2026-08-11"; }}
var lang = "zh";
var STR = {{zh: {{ev: {{attended:"上了", missed_school:"停课", missed_us:"没去"}}}}}};
var out = {{}};
function $(id) {{ return {{ innerHTML: "", set: null, value: "",
  get selectedOptions() {{ return []; }},
  textContent: "" }}; }}
var nodes = {{}};
$ = function (id) {{ if (!nodes[id]) nodes[id] = {{innerHTML:"", value:"", textContent:""}};
  return nodes[id]; }};
{seed}
function esc(s) {{ return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {{
  return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]; }}); }}
function t(k) {{ return k; }}
function money(n) {{ return "¥" + Number(n).toFixed(2); }}
function money0(n) {{ return "¥" + Math.round(Number(n)); }}
function categoryLabel(e) {{ return e.category || ""; }}
"""
        read = read or 'nodes["classesBody"].innerHTML'
        script = (harness + block
                  + "\nrenderClasses();\nconsole.log(" + read + ");\n")
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_a_closed_row_hides_its_controls_and_log(self):
        html = self.render(open_ids=[])
        self.assertIn('class="btns" hidden=""', html)
        self.assertIn('class="histbox" hidden=""', html)
        self.assertNotIn('class="item open"', html)

    def test_an_open_row_shows_its_controls_and_log(self):
        """Forcing isOpen to false here made the entire tab inert."""
        html = self.render(open_ids=["p1"])
        self.assertIn('class="item open"', html)
        self.assertIn('class="btns">', html)      # no hidden attribute
        self.assertIn('class="histbox">', html)
        self.assertIn("2026-08-05", html)         # the class log is rendered
        self.assertIn("data-unlog=", html)        # ...with its remove control

    def test_an_open_row_offers_a_date_picker_starting_at_today(self):
        """Logging a class used to open prompt() and ask a phone user to type
        "2026-08-05" by hand — under a label that said 到期日 (due date)."""
        html = self.render(open_ids=["p1"])
        self.assertIn('class="c-date" id="c-date-p1" value="2026-08-11"', html)
        self.assertIn('<label for="c-date-p1">', html)   # tappable label
        self.assertIn("cls_date", html)   # its own label, not t("due")

    def test_an_empty_dropdown_offers_nothing_selectable(self):
        """This branch was rendered by no test, and the category filter made it
        the normal state for a ledger with no untracked course payment. An
        option carrying any non-empty value passes the submit guard and posts a
        bogus expense_id — a 404 in place of the coaching message."""
        html = self.render(open_ids=[], seed="candidates = [];",
                           read='nodes["clsExpense"].innerHTML')
        self.assertIn('<option value="">', html)
        self.assertIn("cls_no_payment", html)

    def test_a_re_render_moves_an_untouched_row_on_to_the_new_day(self):
        """An earlier fix PINNED today into clsDates when an open row rendered.
        The box and the tap did then agree — by freezing: a row opened at 23:50
        showed and logged yesterday for the rest of the page's life, and no
        refresh could correct it. The default has to be recomputed."""
        html = self.render(
            open_ids=["p1"],
            # render once before midnight — which is what seeded the pin — then
            # let the day turn and render again. One render cannot show this.
            read='(function () { todayStr = function () { return "2026-09-01"; };'
                 ' renderClasses(); return nodes["classesBody"].innerHTML; })()')
        self.assertIn('class="c-date" id="c-date-p1" value="2026-09-01"', html)

    def test_a_re_render_does_not_move_a_date_she_chose(self):
        """The other half: a pick survives the re-render another row's tap
        causes, which is the only reason the map exists."""
        html = self.render(open_ids=["p1"], seed="""
clsDates = {p1: "2026-07-02"};
todayStr = function () { return "2026-09-01"; };
""")
        self.assertIn('class="c-date" id="c-date-p1" value="2026-07-02"', html)

    def test_rendering_records_nothing_of_its_own(self):
        """renderClasses() is a repaint. When it also wrote to clsDates, the
        write outlived every later correction."""
        state = self.render(open_ids=["p1"], read="JSON.stringify(clsDates)")
        self.assertEqual(json.loads(state), {})

    def test_courses_whose_payments_also_read_alike_get_a_handle(self):
        """The description fallback fails when it too matches — two terms
        bought in one sitting share a date, a description AND an amount, and on
        day one their figures are identical as well. The id is the only handle
        unique by construction; it appears only where the text collides.
        """
        html = self.render(open_ids=[], seed="""
packages[0].period_label = null;
packages[1] = JSON.parse(JSON.stringify(packages[0]));
packages[1].id = "p2deadbeef";
""")
        titles = re.findall(r'<div class="ex-desc">(.*?)</div>', html)
        self.assertEqual(len(titles), 2, html)
        self.assertNotEqual(titles[0], titles[1], "two courses still read alike")

    def test_a_course_with_no_twin_carries_no_id_noise(self):
        """The handle is a last resort, not decoration — one course must not
        wear a hex fragment for no reason."""
        html = self.render(open_ids=[], seed="packages[0].period_label = null;")
        titles = re.findall(r'<div class="ex-desc">(.*?)</div>', html)
        self.assertNotIn("p1", titles[0].split("<span")[0])

    def test_identical_payments_are_told_apart_in_the_dropdown(self):
        """Picking the wrong option links the course to the wrong money, and
        that amount is what the whole tracker divides."""
        html = self.render(
            open_ids=[],
            seed="""
candidates = [
  {"id":"aaaa1111","description":"足球课","amount":2200,"date":"2026-08-03","category":"aden-sports"},
  {"id":"bbbb2222","description":"足球课","amount":2200,"date":"2026-08-03","category":"aden-sports"}
];""",
            read='nodes["clsExpense"].innerHTML')
        options = re.findall(r"<option[^>]*>(.*?)</option>", html)
        self.assertEqual(len(options), 2)
        self.assertNotEqual(options[0], options[1],
                            "two payments render the same option text")

    def test_two_courses_whose_ids_share_a_prefix_still_differ(self):
        """The handle was four characters of a twelve-character id, so two ids
        sharing a prefix produced the same label again — the wrong-course log
        this exists to prevent, reachable through the fix for it."""
        html = self.render(open_ids=[], seed="""
packages[0].period_label = null;
packages[0].id = "abcd11111111";
packages[1] = JSON.parse(JSON.stringify(packages[0]));
packages[1].id = "abcd22222222";
""")
        titles = re.findall(r'<div class="ex-desc">(.*?)</div>', html)
        self.assertEqual(len(titles), 2, html)
        self.assertNotEqual(titles[0], titles[1], "a shared id prefix still collides")
        # inequality alone would also pass for any two-character suffix; the
        # guarantee is the WHOLE id, which is the handle the MCP falls back to
        self.assertIn("abcd11111111", titles[0])
        self.assertIn("abcd22222222", titles[1])

    def test_two_same_named_courses_do_not_render_identically(self):
        """The shape this release now produces exclusively: the portal stopped
        asking a per-class pack for a period label, and that label was the row's
        only disambiguator. Two terms of 足球课 rendering byte for byte the same
        means a class logged against the wrong one — which moves both rows'
        figures while both still look right.

        Both packs here are funded by payments dated the SAME day, because two
        terms bought in one sitting are, which is why the payment date alone is
        not enough. The description is where she writes the month.
        """
        html = self.render(open_ids=[], seed="""
packages[0].period_label = null;
packages[0].expense.description = "Football (8月, 10课)";
packages[1] = JSON.parse(JSON.stringify(packages[0]));
packages[1].id = "p2";
packages[1].expense.description = "Football (9月, 10课)";
""")
        titles = re.findall(r'<div class="ex-desc">(.*?)</div>', html)
        self.assertEqual(len(titles), 2, html)
        self.assertNotEqual(titles[0], titles[1],
                            "nothing in the row tells the two courses apart")
        self.assertIn("8月", titles[0])
        self.assertIn("9月", titles[1])

    def test_a_row_with_a_write_in_flight_renders_its_picker_disabled(self):
        """A repaint would otherwise draw a live-looking date box over a course
        whose log is still in the air — inviting exactly the edit the disabled
        state exists to prevent."""
        html = self.render(open_ids=["p1"], seed="clsBusy = {p1: true};")
        self.assertIn('id="c-date-p1" value="2026-08-11" disabled=""', html)

    def test_an_idle_row_renders_its_picker_live(self):
        html = self.render(open_ids=["p1"])
        self.assertNotIn("disabled", html)

    def test_a_closed_row_hides_the_date_picker_with_its_buttons(self):
        html = self.render(open_ids=[])
        self.assertIn('class="clsdate" hidden=""', html)

    def test_the_picker_keeps_the_date_she_already_chose(self):
        """Every log re-renders the tab. Resetting to today here means
        backfilling three missed classes from last month is three date picks,
        and the third one silently lands on today if she forgets."""
        html = self.render(open_ids=["p1"], seed='clsDates = {p1: "2026-07-02"};')
        self.assertIn('value="2026-07-02"', html)

    def test_removing_a_class_record_carries_what_it_would_remove(self):
        """The confirm names the row; without this attribute it would ask about
        nothing in particular, which is not a check at all."""
        html = self.render(open_ids=["p1"])
        self.assertIn('data-when="2026-08-05 · 上了"', html)

    def test_the_row_renders_the_course_and_its_figures(self):
        html = self.render(open_ids=["p1"])
        self.assertIn("足球课", html)
        self.assertIn("8月", html)
        self.assertIn("9/10", html)
        self.assertIn("¥1980.00", html)

    def test_a_course_with_no_classes_logged_shows_a_placeholder(self):
        """Was a grep for the literal — which also appears in the History tab,
        so deleting the class-log branch entirely left it passing."""
        html = self.render(
            open_ids=["p1"],
            seed='packages[0].events = [];',
        )
        self.assertIn('<div class="hist">–</div>', html)

    # A <select> is not a plain object: replacing innerHTML resets it to the
    # first option, and assigning a value that is not an option is ignored.
    # Modelling that is the whole point — with a bare `{value: ""}` stub, and a
    # fixture holding one candidate, "keep her pick" and "take the first one"
    # are the same string and the test cannot fail.
    SELECT_STUB = """
function makeSelect() {
  var self = {_html: "", _value: "", _options: []};
  Object.defineProperty(self, "innerHTML", {
    get: function () { return self._html; },
    set: function (v) {
      self._html = v;
      self._options = (v.match(/value="([^"]*)"/g) || []).map(function (m) {
        return m.slice(7, -1); });
      self._value = self._options.length ? self._options[0] : "";
    }});
  Object.defineProperty(self, "value", {
    get: function () { return self._value; },
    set: function (v) { if (self._options.indexOf(v) >= 0) self._value = v; }});
  return self;
}
nodes["clsExpense"] = makeSelect();
"""

    def test_the_add_form_keeps_the_payment_she_picked(self):
        """Every row tap re-renders the tab and rebuilds this select. Losing
        the selection silently links the next course to whichever payment
        happens to be listed first — and that payment is the amount the whole
        tracker divides."""
        html = self.render(
            open_ids=[],
            seed=self.SELECT_STUB + """
candidates = [
  {"id":"x1","description":"足球课","amount":2200,"date":"2026-08-03","category":"aden-sports"},
  {"id":"x2","description":"游泳课","amount":1000,"date":"2026-08-04","category":"aden-sports"}
];
// a prior render, then her pick: the SECOND payment, not the first
nodes["clsExpense"].innerHTML = '<option value="x1"></option><option value="x2"></option>';
nodes["clsExpense"].value = "x2";
""",
            read='nodes["clsExpense"].value',
        )
        self.assertEqual(html.strip(), "x2",
                         "the re-render reverted her pick to the first payment")

    def test_a_payment_that_is_no_longer_offered_leaves_the_picker_clean(self):
        """Her previous pick may have just been linked to a package, so it is
        gone from candidates. Restoring it must not fabricate a selection."""
        html = self.render(
            open_ids=[],
            seed=self.SELECT_STUB + """
nodes["clsExpense"].innerHTML = '<option value="gone"></option>';
nodes["clsExpense"].value = "gone";
""",
            read='JSON.stringify(nodes["clsExpense"].value)',
        )
        # falls back to whatever the rebuilt list offers, never to "gone"
        self.assertNotIn("gone", html)

    def test_the_rate_hint_divides_the_payment_by_the_class_count(self):
        """The figure she reads when deciding whether the course is priced
        right. Nothing referenced clsRateHint from the suite at all."""
        html = self.render(
            open_ids=[],
            seed=('nodes["clsExpense"] = {innerHTML:"", value:"x1", textContent:""};'
                  '\nnodes["clsCount"] = {innerHTML:"", value:"10", textContent:""};'),
            read='nodes["clsRateHint"].textContent',
        )
        self.assertIn("¥220.00", html)     # 2200 / 10
        self.assertNotIn("¥22000", html)


@unittest.skipUnless(shutil.which("node"), "node not available to run the portal's JS")
class ClassRefreshOrderingTests(unittest.TestCase):
    """`refreshClasses` tags each fetch with a generation number.

    Without it the older of two in-flight responses lands last and repaints
    pre-log totals over post-log ones: she logs a class, watches the count go
    down, then watches it go back up — and logs it again.
    """

    PORTAL = Path(__file__).resolve().parent.parent / "app" / "portal.html"

    def test_a_slow_earlier_response_cannot_overwrite_a_newer_one(self):
        import json

        src = self.PORTAL.read_text(encoding="utf-8")
        # start at the counter's declaration, not the function, so the slice
        # carries the real `var clsGen` rather than a stub of it
        block = src[src.index("  var clsGen = 0;"):
                    src.index('  $("clsExpense").addEventListener')]
        self.assertIn("function refreshClasses()", block, "block markers moved")
        script = """
var packages = [], candidates = [], serverToday = null, serverTodayAt = 0;
var rendered = [];
var resolvers = [];
function renderClasses() { rendered.push(packages.length); }
function toast() {}
function api() {
  return { then: function (f) { resolvers.push(f); return this; },
           catch: function () { return this; } };
}
""" + block + """
refreshClasses();                      // generation 1 (the slow one)
refreshClasses();                      // generation 2 (the fresh one)
resolvers[1]({packages: [1, 2], candidates: []});   // newer answers first
resolvers[0]({packages: [1, 2, 3, 4], candidates: []});  // older lands late
console.log(JSON.stringify({rendered: rendered, finalCount: packages.length}));
"""
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        state = json.loads(out.stdout)
        self.assertEqual(state["finalCount"], 2,
                         "the stale response overwrote the newer one")
        self.assertEqual(state["rendered"], [2],
                         "the stale response should not have rendered at all")


@unittest.skipUnless(shutil.which("node"), "node not available to run the portal's JS")
class ClassTabInteractionTests(unittest.TestCase):
    """Executes the Classes tab's click handler against a stub DOM.

    Mutation testing found that inverting a single boolean in this handler
    makes the entire tab inert — no buttons, no class log, forever — while all
    210 tests stayed green, because every portal guarantee here was a string
    match. Same for the double-tap guard and the stale-response counter: the
    tokens simply did not appear in the test suite.
    """

    PORTAL = Path(__file__).resolve().parent.parent / "app" / "portal.html"

    def run_handler(self, driver: str) -> dict:
        """Run the real classesBody handler body with stubbed globals."""
        import json

        src = self.PORTAL.read_text(encoding="utf-8")
        start = src.index('  $("classesBody").addEventListener("click", function (ev) {')
        end = src.index("  // ---- init ----")
        handler = src[start:end]
        self.assertIn("openPkgs[id]", handler, "handler markers moved")
        harness = """
var openPkgs = {}, logged = [], rendered = 0, apiCalls = [];
// clsDates only carries a pick across a re-render; the BOX (dateEl) is what
// the handler reads, so a driver states the date by setting dateEl.value
var clsDates = {};
var clsBusy = {};
var timers = [];
function setTimeout(fn, ms) { timers.push({fn: fn, ms: ms}); return timers.length; }
function fireTimers() { var ts = timers; timers = []; ts.forEach(function (x) { x.fn(); }); }
var PKG = "p1";
// the log handler re-finds the live picker by id, because a re-render has
// already replaced the node it captured when the request began
var document = {getElementById: function (i) {
  return i === "c-date-" + PKG ? dateEl : null; }};
function renderClasses() { rendered++; }
function refreshClasses() { rendered++; }
var toasts = [];
function toast(m) { toasts.push(String(m)); }
function t(k) { return k; }
function todayStr() { return "2026-08-11"; }
function confirm(msg) { confirms.push(msg); return confirmed; }
var pending = false;   // set by a driver to model a request still in flight
var rejectWith = null; // an Error to fail with: .answered = the server replied
var held = [];
function resolveLog() {   // let a held request come back, later than its context
  var fns = held; held = [];
  fns.forEach(function (f) { f({}); });
}
function api(name, body) {
  apiCalls.push({name: name, body: body});
  if (pending) return { then: function (f) { held.push(f); return this; },
                        catch: function () { return this; } };
  if (rejectWith) return { then: function () { return this; },
                           catch: function (f) { f(rejectWith); return this; } };
  return { then: function (f) { f({}); return this; },
           catch: function () { return this; } };
}
var button = {disabled: false, getAttribute: function (a) {
  return a === "data-c" ? this._c : null; }, _c: null};
// The row's date picker. Its value reaches the log handler ONLY through
// clsDates, which the change listener writes — so a driver states a pick by
// setting clsDates, exactly as the real listener would. A stub that let a test
// set dateEl.value alone could express a DOM state the app cannot reach.
var dateEl = {value: "2026-08-11"};
var itemEl = {getAttribute: function (a) { return a === "data-pkg" ? PKG : null; },
  querySelector: function (sel) { return sel === ".c-date" ? dateEl : null; }};
var confirmed = true, confirms = [];
var $ = function () { return {addEventListener: function () {}}; };
"""
        # the handler is registered via $("classesBody").addEventListener —
        # capture the callback instead of running it against a real DOM
        harness += """
var handlerFn = null;
$ = function () { return {addEventListener: function (_e, fn) { handlerFn = fn; }}; };
"""
        script = (harness + handler
                  + "\n" + driver
                  + "\nconsole.log(JSON.stringify({openPkgs: openPkgs, "
                    "rendered: rendered, apiCalls: apiCalls, "
                    "confirms: confirms, clsDates: clsDates, toasts: toasts, "
                    "picker: dateEl.value, busy: clsBusy, "
                    "pickerDisabled: !!dateEl.disabled, "
                    "disabled: button.disabled}));\n")
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    TAP_ROW = """
var ev = {target: {closest: function (sel) {
  if (sel === "[data-pkg]") return itemEl;
  return null; }}, stopPropagation: function () {}};
handlerFn(ev);
"""

    def test_tapping_a_row_opens_it(self):
        """Inverting the open flag here left every course row with no buttons
        and no class log, permanently."""
        state = self.run_handler(self.TAP_ROW)
        self.assertEqual(state["openPkgs"], {"p1": True})
        self.assertGreaterEqual(state["rendered"], 1)

    def test_tapping_an_open_row_closes_it(self):
        state = self.run_handler(self.TAP_ROW + self.TAP_ROW)
        self.assertEqual(state["openPkgs"], {"p1": False})

    def test_a_double_tapped_log_button_only_logs_once(self):
        """One thumb, a laggy connection — on a period package each spurious
        tap claims another class back from the school. `pending` holds the
        first request open, which is the state the guard exists for."""
        state = self.run_handler("pending = true;\n" + self.LOG_TAP + self.LOG_TAP)
        self.assertEqual(len(state["apiCalls"]), 1,
                         "the in-flight guard did not stop the second tap")
        self.assertEqual(state["apiCalls"][0]["name"], "classes-log")
        self.assertEqual(state["apiCalls"][0]["body"]["date"], "2026-08-11")

    def test_a_re_render_mid_flight_does_not_unlock_the_course(self):
        """The old guard was `b.disabled` — a property of one button node. Any
        re-render replaces that node with an enabled one, so a second tap got
        through and logged the class twice. The lock has to outlive the DOM.
        """
        state = self.run_handler("""
pending = true;
""" + self.LOG_TAP + """
button.disabled = false;   // as a re-render leaves the replacement button
""" + self.LOG_TAP)
        self.assertEqual(len(state["apiCalls"]), 1,
                         "a re-render re-armed the button and logged twice")

    def test_a_sibling_button_cannot_log_during_an_in_flight_log(self):
        """`b.disabled` covered only the button tapped. Tapping 停课 while ✓上了
        was in flight wrote two events — and on a period package the second one
        claims another class back from the school."""
        state = self.run_handler("pending = true;\n" + self.log_tap("attended")
                                 + self.log_tap("missed_school"))
        self.assertEqual([c["body"]["kind"] for c in state["apiCalls"]],
                         ["attended"], "two events from one course at once")

    @staticmethod
    def log_tap(kind: str = "attended") -> str:
        """A tap on one of the three log buttons. Parameterised because a fixed
        `button._c = "attended"` silently overwrote any kind a driver set
        before it, so the sibling-button test tapped ✓上了 twice."""
        return """
button._c = "%s";
var ev = {target: {closest: function (sel) {
  if (sel === "[data-pkg]") return itemEl;
  if (sel === "button[data-c]") return button;
  return null; }}, stopPropagation: function () {}};
handlerFn(ev);
""" % kind

    LOG_TAP = """
button._c = "attended";
var ev = {target: {closest: function (sel) {
  if (sel === "[data-pkg]") return itemEl;
  if (sel === "button[data-c]") return button;
  return null; }}, stopPropagation: function () {}};
handlerFn(ev);
"""

    def test_the_logged_date_is_the_one_in_the_box(self):
        """The box is what she can see, so it is what must be written."""
        state = self.run_handler('dateEl.value = "2026-07-02";\n' + self.LOG_TAP)
        self.assertEqual(state["apiCalls"][0]["body"]["date"], "2026-07-02")

    def test_a_cleared_box_falls_back_to_today(self):
        """A date input cleared by hand reads "" — posting that is a 400, and
        on a phone an empty box is easy to leave behind."""
        state = self.run_handler('dateEl.value = "";\n' + self.LOG_TAP)
        self.assertEqual(state["apiCalls"][0]["body"]["date"], "2026-08-11")

    def test_it_writes_the_date_on_screen_even_when_no_change_event_fired(self):
        """`<input type="date">` fires NO change event when you re-pick the
        value it already shows, so a rule that reads a stored pick discards
        that choice and writes something else. Reading the box cannot: the
        date she sees is the date that goes in, with no event required.
        """
        state = self.run_handler("""
dateEl.value = "2026-08-10";   // on screen, and deliberately re-picked
clsDates = {};                 // …so nothing was ever recorded
""" + self.LOG_TAP)
        self.assertEqual(state["apiCalls"][0]["body"]["date"], "2026-08-10")

    def test_a_stored_pick_never_overrides_what_the_box_shows(self):
        """The two drifted apart in every earlier version of this. Whichever
        way they disagree, the box wins — she cannot see the other one."""
        state = self.run_handler("""
dateEl.value = "2026-07-02";
clsDates[PKG] = "2026-09-30";   // a stale copy, from any of the ways they drift
""" + self.LOG_TAP)
        self.assertEqual(state["apiCalls"][0]["body"]["date"], "2026-07-02")

    def test_a_second_tap_before_the_refresh_lands_does_not_reuse_the_date(self):
        """Clearing the memory only changes what the NEXT render seeds. If the
        handler reads the input instead, the picked date is still sitting in the
        DOM — and the button re-enables before the refresh arrives, so a second
        tap reuses it. If that refresh fails, every later tap does.
        """
        state = self.run_handler('dateEl.value = "2026-07-02";\n'
                                 + self.LOG_TAP + self.LOG_TAP)
        dates = [c["body"]["date"] for c in state["apiCalls"]]
        self.assertEqual(dates, ["2026-07-02", "2026-08-11"],
                         "the backfill date survived its own log")
        self.assertEqual(state["picker"], "2026-08-11")

    def test_the_box_returns_to_today_after_a_log(self):
        """refreshClasses() repaints only inside its own `.then`, so on a flaky
        link nothing else resets the row — and a backfill date left sitting in
        the box is the next class filed under last month. The GFW is why this
        app has no CDN; a request that does not come back is the normal case.
        """
        state = self.run_handler("""
clsDates[PKG] = "2026-07-02";
dateEl.value = "2026-07-02";   // as her pick left the box
""" + self.LOG_TAP)
        self.assertEqual(state["apiCalls"][0]["body"]["date"], "2026-07-02")
        self.assertEqual(state["picker"], "2026-08-11",
                         "the row still offers the date it just used")
        self.assertEqual(state["clsDates"], {}, "the pick outlived its log")

    def test_closing_a_row_drops_a_date_she_did_not_use(self):
        """The picker is hidden while the row is closed, so an abandoned pick
        waits out of sight: open, pick 7月2日, close, reopen tomorrow, tap 停课
        — and it files under 7月2日. The memory is for a re-render caused by
        ANOTHER row, which closing this one is not.
        """
        state = self.run_handler("""
openPkgs[PKG] = true;
clsDates[PKG] = "2026-07-02";
""" + self.TAP_ROW)          # tapping the row itself closes it
        self.assertEqual(state["openPkgs"], {"p1": False})
        self.assertEqual(state["clsDates"], {})

    def test_closing_a_row_leaves_the_other_rows_dates_alone(self):
        """Dropping the intent means dropping THIS row's — the memory exists so
        a pick survives the re-render that opening another row causes, which is
        the one job it has."""
        state = self.run_handler("""
openPkgs[PKG] = true;
clsDates[PKG] = "2026-07-02";
clsDates["p2"] = "2026-07-09";
""" + self.TAP_ROW)
        self.assertEqual(state["clsDates"], {"p2": "2026-07-09"})

    def test_a_date_picked_while_a_log_is_in_flight_survives_it(self):
        """A slow request outlives its own context. She logs 7月2日, and while
        it is still going picks 7月9日 for the next one — the reset that
        follows the first log must not take her newer choice with it."""
        state = self.run_handler("""
pending = true;
dateEl.value = "2026-07-02";
""" + self.LOG_TAP + """
dateEl.value = "2026-07-09";   // her next pick, while the first is in flight
resolveLog();
""")
        self.assertEqual(state["apiCalls"][0]["body"]["date"], "2026-07-02")
        self.assertEqual(state["picker"], "2026-07-09",
                         "the in-flight log reset a date she picked after it")

    def test_a_refusal_from_the_server_releases_the_course(self):
        """A 400 means the write did not happen — nothing is in doubt, so
        holding the course would strand her over a typo."""
        state = self.run_handler(
            'rejectWith = new Error("bad date"); rejectWith.answered = true;\n'
            + self.LOG_TAP + self.LOG_TAP)
        self.assertEqual(len(state["apiCalls"]), 2, "a refused log locked the course")

    def test_a_network_failure_does_NOT_release_the_course(self):
        """The dangerous half: a rejection with no answer is ambiguous — the
        insert may have committed and the reply been lost. class_events has no
        idempotency constraint (BACKLOG §6), so the retry an open lock permits
        writes a second row and summarize_package counts both. Staying locked
        costs a reload and is visible; the duplicate is neither.
        """
        state = self.run_handler(
            'rejectWith = new Error("network");\n' + self.LOG_TAP + self.LOG_TAP)
        self.assertEqual(len(state["apiCalls"]), 1,
                         "an ambiguous failure allowed a retry that can double-log")
        self.assertEqual(state["busy"], {"p1": True})

    def test_the_date_cannot_be_changed_while_its_write_is_in_flight(self):
        """Rather than trying to DETECT a pick made during the request — which
        a same-value re-pick makes undetectable, by event or by value — the box
        is closed for the duration."""
        state = self.run_handler("pending = true;\n" + self.LOG_TAP)
        self.assertTrue(state["pickerDisabled"], "she can still edit under the write")

    def test_the_picker_comes_back_when_the_log_succeeds(self):
        """Disabling it is only half a lifecycle. A success normally repaints
        the row, which replaces the input — but refreshClasses() repaints only
        if its own request lands, so on a flaky link the box stays dead."""
        state = self.run_handler(self.LOG_TAP)
        self.assertFalse(state["pickerDisabled"], "the date box never came back")

    def test_the_picker_comes_back_when_the_server_refuses(self):
        """A refusal requests no repaint at all, so nothing else can restore
        it — she is left with a dead box after mistyping a date."""
        state = self.run_handler(
            'rejectWith = new Error("bad date"); rejectWith.answered = true;\n'
            + self.LOG_TAP)
        self.assertFalse(state["pickerDisabled"])

    def test_the_picker_stays_dead_while_the_outcome_is_unknown(self):
        """The other half: an ambiguous failure holds the whole course, and a
        live-looking date box would invite the retry that can double-log."""
        state = self.run_handler(
            'rejectWith = new Error("network");\n' + self.LOG_TAP)
        self.assertTrue(state["pickerDisabled"])
        self.assertEqual(state["busy"], {"p1": True})

    def test_the_reset_asks_the_box_not_an_event_counter(self):
        """A counter of `change` events cannot see a re-pick of the value
        already shown — `<input type="date">` fires nothing when the value does
        not change. The box can: it is the same question the tap asks."""
        state = self.run_handler("""
pending = true;
dateEl.value = "2026-07-02";
""" + self.LOG_TAP + """
dateEl.value = "2026-07-09";   // changed with no event of any kind
resolveLog();
""")
        self.assertEqual(state["picker"], "2026-07-09")

    def test_nothing_on_a_timer_releases_the_lock(self):
        """A 30s release was tried here and is a WORSE bug than the deadlock it
        fixed: the request it gives up on may already have committed, so the
        retry it permits writes a second class_events row — and both are
        counted by summarize_package, which moves money. A course held until
        reload is visible and recoverable; a duplicate class is neither.
        """
        state = self.run_handler("pending = true;\n" + self.LOG_TAP
                                 + "fireTimers();\n" + self.LOG_TAP)
        self.assertEqual(len(state["apiCalls"]), 1,
                         "a timer unlocked the course and allowed a retry")
        self.assertEqual(state["busy"], {"p1": True})

    def test_the_toast_says_which_date_was_logged(self):
        """The one action here with no confirmation step. Naming the date is
        what makes a wrong one visible at the moment it happens."""
        state = self.run_handler('dateEl.value = "2026-07-02";\n' + self.LOG_TAP)
        self.assertIn("2026-07-02", state["toasts"][-1])

    def test_opening_the_date_picker_does_not_close_the_row(self):
        """The picker lives inside the row, and the row's own tap handler
        toggles it shut — so tapping the date box collapsed the controls
        underneath her thumb before the picker could open."""
        state = self.run_handler("""
openPkgs[PKG] = true;
var ev = {target: {closest: function (sel) {
  if (sel === "[data-pkg]") return itemEl;
  if (sel === ".clsdate") return {};
  return null; }}, stopPropagation: function () {}};
handlerFn(ev);
""")
        self.assertEqual(state["openPkgs"], {"p1": True}, "the row closed itself")

    UNLOG_TAP = """
var unlogEl = {getAttribute: function (a) {
  if (a === "data-unlog") return "e1";
  if (a === "data-when") return "2026-08-05 · 上了";
  return null; }};
var ev = {target: {closest: function (sel) {
  if (sel === "[data-pkg]") return itemEl;
  if (sel === "[data-unlog]") return unlogEl;
  return null; }}, stopPropagation: function () {}};
handlerFn(ev);
"""

    def remember_date(self, driver: str) -> dict:
        """Run the real classesBody `change` listener — the half of the date
        memory that no test executed. A key/value swap there keeps every token
        the wiring guard greps for and silently makes the memory never work.
        """
        import json

        src = self.PORTAL.read_text(encoding="utf-8")
        start = src.index("  // the picker's date outlives the re-render")
        block = src[start:src.index('  $("addClsForm")')]
        self.assertIn("clsDates[", block, "block markers moved")
        script = ("var clsDates = {}, handlerFn = null;\n"
                  'function todayStr() { return "2026-08-11"; }\n'
                  'var $ = function () { return {addEventListener:'
                  " function (_e, fn) { handlerFn = fn; }}; };\n"
                  + block + "\n" + driver
                  + "\nconsole.log(JSON.stringify({dates: clsDates}));\n")
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_picking_a_date_records_it_against_that_package(self):
        state = self.remember_date("""
var dateEl = {value: "2026-07-02"};
var itemEl = {getAttribute: function (a) { return a === "data-pkg" ? "p1" : null; }};
handlerFn({target: {closest: function (sel) {
  if (sel === ".c-date") return dateEl;
  if (sel === "[data-pkg]") return itemEl;
  return null; }}});
""")
        self.assertEqual(state["dates"], {"p1": "2026-07-02"})

    def test_emptying_the_box_puts_today_back_in_it(self):
        """A cleared date input reads "". Storing that leaves the box blank
        while the tap falls through to today — the box then shows one thing and
        writes another, which is the class of bug this release keeps making."""
        state = self.remember_date("""
var dateEl = {value: ""};
var itemEl = {getAttribute: function (a) { return a === "data-pkg" ? "p1" : null; }};
handlerFn({target: {closest: function (sel) {
  if (sel === ".c-date") return dateEl;
  if (sel === "[data-pkg]") return itemEl;
  return null; }}});
""")
        self.assertEqual(state["dates"], {"p1": "2026-08-11"})

    def test_a_change_somewhere_else_in_the_row_is_ignored(self):
        """The listener is on the whole tab body, so every input in every course
        row reaches it — only the date picker may write here."""
        state = self.remember_date("""
var itemEl = {getAttribute: function (a) { return a === "data-pkg" ? "p1" : null; }};
handlerFn({target: {closest: function (sel) {
  if (sel === "[data-pkg]") return itemEl;
  return null; }}});
""")
        self.assertEqual(state["dates"], {})

    def test_removing_a_class_record_asks_first(self):
        """A 12px × beside the class log, one mis-tap from erasing attendance —
        and on a period package, from handing the school back a class it owed."""
        state = self.run_handler(self.UNLOG_TAP)
        self.assertEqual(len(state["confirms"]), 1, "it deleted without asking")
        self.assertTrue(state["confirms"][0].startswith("cls_confirm_unlog"),
                        f"wrong question asked: {state['confirms'][0]!r}")
        self.assertIn("2026-08-05", state["confirms"][0],
                      "the question must name the record it would remove")
        self.assertEqual(state["apiCalls"][0]["name"], "classes-unlog")

    def test_declining_that_question_removes_nothing(self):
        state = self.run_handler("confirmed = false;\n" + self.UNLOG_TAP)
        self.assertEqual(state["apiCalls"], [], "Cancel still deleted the record")

    DELPKG_TAP = """
button._c = "delpkg";
var ev = {target: {closest: function (sel) {
  if (sel === "[data-pkg]") return itemEl;
  if (sel === "button[data-c]") return button;
  return null; }}, stopPropagation: function () {}};
handlerFn(ev);
"""

    def test_deleting_a_whole_course_asks_first(self):
        """The most destructive control on the tab — it takes the package and
        every class ever logged against it. Only the endpoint was tested; the
        confirm in front of it was executed by nothing."""
        state = self.run_handler(self.DELPKG_TAP)
        self.assertEqual(len(state["confirms"]), 1, "it deleted without asking")
        self.assertTrue(state["confirms"][0].startswith("cls_confirm_del"),
                        f"wrong question asked: {state['confirms'][0]!r}")
        self.assertEqual(state["apiCalls"][0]["name"], "classes-delete")

    def test_declining_the_course_delete_removes_nothing(self):
        state = self.run_handler("confirmed = false;\n" + self.DELPKG_TAP)
        self.assertEqual(state["apiCalls"], [], "Cancel still deleted the course")


@unittest.skipUnless(shutil.which("node"), "node not available to run the portal's JS")
class ApiErrorShapeTests(unittest.TestCase):
    """Runs the real `api()` helper.

    The class log releases its per-course lock only for a failure the SERVER
    answered — an unanswered one may have committed and lost its reply, and a
    retry would double-log. That distinction is made here, by a flag on the
    error, and the handler tests stub `api()` out entirely: they supply the
    flag whose production path is the thing in question.
    """

    PORTAL = Path(__file__).resolve().parent.parent / "app" / "portal.html"

    def call_api(self, fetch_js: str) -> dict:
        src = self.PORTAL.read_text(encoding="utf-8")
        block = src[src.index("  function api(name, body) {"):
                    src.index("  // ---- i18n ----")]
        self.assertIn("err.answered", block, "block markers moved")
        script = f"""
var TOKEN = "tok";
function t(k) {{ return k; }}
var fetch = {fetch_js};
{block}
api("classes-log", {{}}).then(
  function () {{ console.log(JSON.stringify({{outcome: "resolved"}})); }},
  function (e) {{ console.log(JSON.stringify(
      {{outcome: "rejected", answered: !!e.answered, msg: String(e.message)}})); }}
);
"""
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_a_server_refusal_is_marked_answered(self):
        result = self.call_api(
            'function () { return Promise.resolve({ok: false, json: function () {'
            ' return Promise.resolve({ok: false, error: "bad date"}); }}); }')
        self.assertEqual(result["outcome"], "rejected")
        self.assertTrue(result["answered"], "a refusal the server sent is not in doubt")
        self.assertEqual(result["msg"], "bad date")

    def test_a_200_carrying_ok_false_is_marked_answered(self):
        """The API answers 200 with {ok: false} for a validation refusal, so a
        fixture that sets BOTH r.ok=false and j.ok=false cannot tell the two
        halves of the check apart — dropping `|| !j.ok` would survive it."""
        result = self.call_api(
            'function () { return Promise.resolve({ok: true, json: function () {'
            ' return Promise.resolve({ok: false, error: "amount must be > 0"}); }}); }')
        self.assertEqual(result["outcome"], "rejected")
        self.assertTrue(result["answered"])
        self.assertEqual(result["msg"], "amount must be > 0")

    def test_a_network_failure_is_not_marked_answered(self):
        """The whole point: this one may have committed."""
        result = self.call_api(
            'function () { return Promise.reject(new Error("network down")); }')
        self.assertEqual(result["outcome"], "rejected")
        self.assertFalse(result["answered"],
                         "an unanswered failure was treated as a definite one")

    def test_a_body_that_is_not_json_is_not_marked_answered(self):
        """A proxy's HTML 502 can follow a write that landed."""
        result = self.call_api(
            'function () { return Promise.resolve({ok: false, json: function () {'
            ' return Promise.reject(new SyntaxError("Unexpected token <")); }}); }')
        self.assertEqual(result["outcome"], "rejected")
        self.assertFalse(result["answered"])


@unittest.skipUnless(shutil.which("node"), "node not available to run the portal's JS")
class ClassPeriodFieldTests(unittest.TestCase):
    """A pack of N classes is counted in classes, not in months, so the Period
    box has nothing to label there and is hidden — and cleared, because a hidden
    field that still submits what she typed is exactly the shape of bug this
    file keeps finding.
    """

    PORTAL = Path(__file__).resolve().parent.parent / "app" / "portal.html"

    def state(self, kind: str, typed: str = "8月") -> dict:
        import json

        src = self.PORTAL.read_text(encoding="utf-8")
        block = src[src.index("  function clsKindFields() {"):
                    src.index("  function clsRateHint() {")]
        self.assertIn("clsPeriodWrap", block, "block markers moved")
        script = f"""
var nodes = {{clsKind: {{value: {json.dumps(kind)}}},
  clsPeriodWrap: {{hidden: false}},
  clsPeriod: {{value: {json.dumps(typed)}}}}};
function $(id) {{ return nodes[id]; }}
{block}
clsKindFields();
console.log(JSON.stringify({{hidden: nodes.clsPeriodWrap.hidden,
  period: nodes.clsPeriod.value}}));
"""
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_a_per_class_pack_is_offered_no_period_box(self):
        self.assertTrue(self.state("per_class")["hidden"])

    def test_a_monthly_or_semester_fee_keeps_it(self):
        """The period IS the thing being bought there — hiding it too would
        leave 一月 and 二月 as two rows called the same name."""
        self.assertFalse(self.state("period")["hidden"])

    def test_switching_to_per_class_clears_what_she_already_typed(self):
        self.assertEqual(self.state("per_class")["period"], "")

    def test_a_visible_period_box_is_left_alone(self):
        self.assertEqual(self.state("period")["period"], "8月")


@unittest.skipUnless(shutil.which("node"), "node not available to run the portal's JS")
class ClassAddFormTests(unittest.TestCase):
    """Runs the real addClsForm submit handler.

    Filed as BACKLOG §4 and unexecuted by any test since v0.10.0, while four
    mutations of it each produce a visibly wrong figure: hard-coding `kind`,
    `class_count + 1`, taking `expense_id` from the wrong place, swapping name
    and period. This release also changed the meaning of the field it reads —
    `clsPeriod` is now hidden and cleared for a per-class pack.
    """

    PORTAL = Path(__file__).resolve().parent.parent / "app" / "portal.html"
    HANDLER = ('  $("addClsForm").addEventListener("submit"',
               '  $("classesBody").addEventListener("click"')
    ENDPOINT = "classes-add"

    def submit(self, fields: dict, response: dict | None = None) -> dict:
        """`response` is what the server answers with — the add handler now
        reads it back to build its confirmation, so a test that left it empty
        could not tell an echo of the response from an echo of the form."""
        import json

        src = self.PORTAL.read_text(encoding="utf-8")
        start, end = self.HANDLER
        block = src[src.index(start):src.index(end)]
        self.assertIn(self.ENDPOINT, block, "block markers moved")
        script = f"""
var nodes = {json.dumps(fields)}, sent = null, toasts = [], handlerFn = null;
for (var k in nodes) nodes[k] = {{value: nodes[k]}};
nodes.addClsWrap = {{open: true}}; nodes.addWrap = {{open: true}};
function refresh() {{}}
function todayStr() {{ return "2026-08-11"; }}
function $(id) {{ if (!nodes[id]) nodes[id] = {{value: ""}};
  nodes[id].addEventListener = function (_e, fn) {{ handlerFn = fn; }};
  return nodes[id]; }}
function t(k) {{ return k; }}
function toast(m) {{ toasts.push(String(m)); }}
function refreshClasses() {{}}
function api(name, body) {{
  sent = {{name: name, body: body}};
  return {{then: function (f) {{ f({json.dumps(response or {})}); return this; }},
           catch: function () {{ return this; }}}};
}}
function money(n) {{ return "\\u00a5" + Number(n || 0).toFixed(2); }}
function categoryLabel(e) {{ return e.category || ""; }}
{block}
handlerFn({{preventDefault: function () {{}}}});
console.log(JSON.stringify({{sent: sent, toasts: toasts}}));
"""
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    A_PACK = {"clsExpense": "x1", "clsName": " 足球课 ", "clsKind": "per_class",
              "clsCount": "10", "clsPeriod": ""}

    def test_the_form_sends_exactly_what_she_typed(self):
        sent = self.submit(dict(self.A_PACK))["sent"]
        self.assertEqual(sent["name"], "classes-add")
        self.assertEqual(sent["body"], {
            "expense_id": "x1", "name": "足球课", "kind": "per_class",
            "class_count": 10, "period_label": None,
        })

    def test_the_period_label_reaches_the_server_when_the_kind_uses_one(self):
        sent = self.submit(dict(self.A_PACK, clsKind="period",
                                clsPeriod=" 8月 "))["sent"]
        self.assertEqual(sent["body"]["kind"], "period")
        self.assertEqual(sent["body"]["period_label"], "8月")

    def test_the_class_count_is_sent_as_the_number_she_typed(self):
        """It divides the payment. `class_count + 1` turns a ¥220 rate into
        ¥200 and drags every remaining and owed figure with it."""
        sent = self.submit(dict(self.A_PACK, clsCount="8"))["sent"]
        self.assertEqual(sent["body"]["class_count"], 8)

    def test_no_payment_selected_sends_nothing(self):
        result = self.submit(dict(self.A_PACK, clsExpense=""))
        self.assertIsNone(result["sent"], "it posted an empty expense_id")
        self.assertEqual(result["toasts"], ["cls_no_payment"])


@unittest.skipUnless(shutil.which("node"), "node not available to run the portal's JS")
class ExpenseAddFormTests(ClassAddFormTests):
    """The other half of BACKLOG §4: the expense form, live since v0.1 and the
    one that writes money directly, was executed by no test at all.

    Same harness, different slice — it is the older and more-used of the two
    submit handlers, and every field it sends lands in a total.
    """

    # starts at addedMsg so the confirmation builder under test is the SHIPPING
    # one, not a stub — it is the whole subject of the toast tests below
    HANDLER = ("  function addedMsg(e) {",
               "  // ---- per-item actions ----")
    ENDPOINT = "submit"

    A_ROW = {"date": "2026-08-20", "amount": "2200", "category": "aden-sports",
             "desc": "  足球课 8月  "}

    def test_the_form_sends_exactly_what_she_typed(self):
        sent = self.submit(dict(self.A_ROW))["sent"]
        self.assertEqual(sent["name"], "submit")
        self.assertEqual(sent["body"], {
            "date": "2026-08-20", "amount": 2200.0,
            "category": "aden-sports", "description": "足球课 8月",
        })

    def test_an_empty_date_falls_back_to_the_household_today(self):
        """`date` is the DUE date and the field is prefilled, but she can clear
        it. Sending "" is a 400; sending the browser's idea of today is a day
        out in China for most of the working day."""
        sent = self.submit(dict(self.A_ROW, date=""))["sent"]
        self.assertEqual(sent["body"]["date"], "2026-08-11")

    def test_an_empty_description_is_null_not_a_blank_string(self):
        sent = self.submit(dict(self.A_ROW, desc="   "))["sent"]
        self.assertIsNone(sent["body"]["description"])

    def test_the_amount_is_sent_as_the_number_she_typed(self):
        """It is the money. A parseInt here silently drops the fen off every
        amount with one."""
        sent = self.submit(dict(self.A_ROW, amount="220.55"))["sent"]
        self.assertEqual(sent["body"]["amount"], 220.55)

    STORED = {"ok": True, "expense": {
        "id": "3dc9ed78d440", "date": "2026-09-30", "amount": 1980.0,
        "description": "football （10月）", "category": "aden-sports",
        "paid": False,
    }}

    def test_the_confirmation_names_the_row_the_server_stored(self):
        """She added the same ¥1,980 course twice because "已添加" flashed for
        1.7s over a list that did not move. The confirmation has to say what
        landed — and say it from the RESPONSE, so a value the server normalised
        is confirmed as stored rather than as typed (LESSONS §5)."""
        toast = self.submit(dict(self.A_ROW), response=self.STORED)["toasts"][0]
        self.assertIn("football （10月）", toast)
        self.assertIn("2026-09-30", toast)
        self.assertIn("1980", toast.replace(",", ""))

    def test_the_confirmation_does_not_echo_the_form(self):
        """The discriminating case: she typed one date and amount, the server
        stored another. A confirmation built from the form fields is a
        confirmation of a write that did not happen that way — and it would
        pass every assertion above."""
        typed = dict(self.A_ROW, date="2026-08-20", amount="2200",
                     desc="typed description")
        toast = self.submit(typed, response=self.STORED)["toasts"][0]
        self.assertNotIn("2026-08-20", toast)
        self.assertNotIn("2200", toast.replace(",", ""))
        self.assertNotIn("typed description", toast)

    def test_a_response_without_a_row_still_confirms(self):
        """Degrade to the old one-word toast rather than printing "undefined"
        across her ledger if the response shape ever changes."""
        toast = self.submit(dict(self.A_ROW), response={"ok": True})["toasts"][0]
        self.assertEqual(toast, "added")

    # not applicable — this form has no payment selector
    test_the_period_label_reaches_the_server_when_the_kind_uses_one = None
    test_the_class_count_is_sent_as_the_number_she_typed = None
    test_no_payment_selected_sends_nothing = None


@unittest.skipUnless(shutil.which("node"), "node not available to run the portal's JS")
class DueTabVisibilityTests(unittest.TestCase):
    """Every unpaid row must be VISIBLE on the tab she adds from.

    The Due tab has rendered only `daysBetween(today, date) <= 30` since the
    v0.6.0 redesign, and no test ever executed `renderNow`. It went unnoticed
    while rows were near-term. On 2026-08-11 she entered five months of course
    fees at once; the first row due in 50 days appeared in no list and no card,
    the success toast was a 1.7s flash of a constant, and the page underneath
    did not move — so she added the same ¥1,980 course a second time. Both rows
    are in production.

    The sweep is the point. A test that asserts "the Later section exists"
    passes on a section rendered with the wrong predicate, or on one nothing
    puts rows into (LESSONS §8); this one fails unless every row in a 460-day
    span actually reaches the markup.
    """

    PORTAL = Path(__file__).resolve().parent.parent / "app" / "portal.html"
    TODAY = "2026-08-11"

    def render_now(self, rows: list) -> str:
        """Run the portal's real renderNow() over `rows`; return #nowBody.

        Deliberately takes the block from `var CATS` so esc(), money(),
        daysBetween(), isBorrow(), categoryLabel() and stateOf() are the
        SHIPPING ones. Stubbing them is how a renderer bug hides: a permissive
        stub is a test that cannot fail (LESSONS §3).
        """
        import json

        src = self.PORTAL.read_text(encoding="utf-8")
        block = src[src.index("  var CATS = ["):src.index("  // ---- tab 3: stats ----")]
        for marker in ("function renderNow", "function stateOf", "function esc"):
            self.assertIn(marker, block, "block markers moved")
        script = f"""
var _nodes = {{}}, localStorage = {{getItem: function () {{ return "zh"; }}}};
var document = {{
  getElementById: function (id) {{
    if (!_nodes[id]) _nodes[id] = {{innerHTML: "", addEventListener: function () {{}}}};
    return _nodes[id];
  }},
  addEventListener: function () {{}},
}};
{block}
expenses = {json.dumps(rows)};
serverToday = {json.dumps(self.TODAY)};
serverTodayAt = Date.now();
serverMidnightIn = 43200;
renderNow();
console.log(JSON.stringify(_nodes.nowBody.innerHTML));
"""
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        import json as _json

        return _json.loads(out.stdout)

    def render_cards_and_now(self, rows: list, summary: dict) -> tuple:
        """Both halves of the Due tab from ONE render, so the card and the
        section header below it can be compared as she sees them."""
        import json

        src = self.PORTAL.read_text(encoding="utf-8")
        block = src[src.index("  var CATS = ["):src.index("  // ---- tab 3: stats ----")]
        self.assertIn("function renderCards", block, "block markers moved")
        script = f"""
var _nodes = {{}}, localStorage = {{getItem: function () {{ return "zh"; }}}};
var document = {{
  getElementById: function (id) {{
    if (!_nodes[id]) _nodes[id] = {{innerHTML: "", addEventListener: function () {{}}}};
    return _nodes[id];
  }},
  addEventListener: function () {{}},
}};
{block}
expenses = {json.dumps(rows)};
summary = {json.dumps(summary)};
serverToday = {json.dumps(self.TODAY)};
serverTodayAt = Date.now();
serverMidnightIn = 43200;
renderCards();
renderNow();
console.log(JSON.stringify([_nodes.cards.innerHTML, _nodes.nowBody.innerHTML]));
"""
        out = subprocess.run(["node", "-e", script], capture_output=True,
                             text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        import json as _json

        return tuple(_json.loads(out.stdout))

    A_MONTH = [
        {"id": "rent", "date": "2026-08-31", "amount": 22000.0,
         "category": "living", "description": "Living expenses",
         "paid": True, "paid_date": "2026-08-05"},
        {"id": "net", "date": "2026-08-10", "amount": 399.0,
         "category": "utilities", "description": "Internet",
         "paid": True, "paid_date": "2026-08-10"},
        # the row that made the two figures disagree: money she fronted,
        # repaid this month. ¥31,100 is the live amount.
        {"id": "office", "date": "2026-07-30", "amount": 31100.0,
         "category": "borrow", "description": "Borrowed — office",
         "paid": True, "paid_date": "2026-08-05"},
    ]

    def paid_figures(self) -> tuple:
        import re

        cards, body = self.render_cards_and_now(self.A_MONTH, {
            "due_now": 0, "due_now_count": 0, "upcoming": 0,
            "upcoming_count": 0, "borrow_owed": 0, "borrow_owed_count": 0,
        })
        card = re.search(r'本月已付</div><div class="v">([^<]+)', cards)
        sec = re.search(r'本月已付</h2><span class="tot">([^<]+)', body)
        self.assertIsNotNone(card, f"paid card not rendered: {cards}")
        self.assertIsNotNone(sec, f"paid section not rendered: {body}")
        return card.group(1), sec.group(1), body

    def test_the_paid_card_and_the_paid_section_agree(self):
        """They read the same two words over different numbers: ¥24,399 on the
        card, ¥55,499 on the section header. The card excluded borrow (P4); the
        section totalled whatever rows it was handed."""
        card, sec, _ = self.paid_figures()
        self.assertEqual(card, sec)

    def test_household_spending_excludes_what_she_fronted(self):
        """Not just equal — equal to the RIGHT figure. Both agreeing on ¥55,499
        would satisfy the test above and still count a repayment as spending."""
        card, sec, _ = self.paid_figures()
        self.assertEqual(card, "¥22,399")

    def test_a_repayment_is_still_shown_somewhere(self):
        """The complement. Excluding borrow from 本月已付 without giving it a
        home is the same defect as the 30-day filter: 待还我 carries only what
        is still owed, so a repaid row would leave the tab entirely."""
        _, _, body = self.paid_figures()
        self.assertIn('data-id="office"', body)
        self.assertIn("本月已还我", body)

    def a_sweep(self) -> list:
        """One unpaid row per day-offset across a 460-day span.

        Amounts are distinct and non-round so a row cannot be confused with its
        neighbour, and the span deliberately runs past the 30-day window on both
        sides — the boundary is the whole subject.
        """
        from datetime import datetime, timedelta

        base = datetime.strptime(self.TODAY, "%Y-%m-%d")
        rows = []
        for i, offset in enumerate(range(-60, 400)):
            rows.append({
                "id": f"row{i:04d}",
                "date": (base + timedelta(days=offset)).strftime("%Y-%m-%d"),
                "amount": 100 + i + 0.37,
                "category": "aden-sports",
                "description": f"row {i} at {offset:+d}d",
                "paid": False, "paid_date": None,
            })
        return rows

    def test_no_unpaid_row_is_invisible_on_the_due_tab(self):
        rows = self.a_sweep()
        html = self.render_now(rows)
        missing = [r["id"] for r in rows if f'data-id="{r["id"]}"' not in html]
        self.assertEqual(
            missing, [],
            f"{len(missing)} of {len(rows)} unpaid rows render nowhere on the "
            f"Due tab — she cannot see what she just added. First few: "
            f"{missing[:5]}",
        )

    def test_the_row_she_actually_added_is_on_the_due_tab(self):
        """The production case, by id and description. 2026-09-30 is 50 days
        after 2026-08-11: outside the 30-day window that hid it."""
        html = self.render_now([{
            "id": "3dc9ed78d440", "date": "2026-09-30", "amount": 1980,
            "category": "aden-sports", "description": "football （10月）",
            "paid": False, "paid_date": None,
        }])
        self.assertIn('data-id="3dc9ed78d440"', html)
        self.assertIn("football", html)

    def test_a_borrow_row_stays_in_its_own_section(self):
        """P4: money she fronted is never household spending. Surfacing the
        far-future rows must not sweep borrow into the due list."""
        html = self.render_now([{
            "id": "lent1", "date": "2027-01-01", "amount": 500,
            "category": "borrow", "description": "fronted it",
            "paid": False, "paid_date": None,
        }])
        self.assertIn('data-id="lent1"', html)
        self.assertIn("st lend", html, "a borrow row lost its lent styling")


class ClassKindParityTests(unittest.TestCase):
    """The portal hard-codes the kind strings the store validates against.

    They are two hand-maintained lists, exactly like the category keys — and a
    silent mismatch here means a button that always errors, or a package the
    portal renders as the wrong shape.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def setUp(self):
        self.portal = (self.ROOT / "app" / "portal.html").read_text(encoding="utf-8")

    def test_package_kinds_match_the_store(self):
        from app.store import CLASS_KINDS

        for kind in CLASS_KINDS:
            self.assertIn(f'value="{kind}"', self.portal,
                          f"portal offers no option for package kind {kind!r}")

    def test_the_period_box_and_the_date_picker_are_wired_up(self):
        """Both behaviours above are functions, and a function nothing calls is
        a no-op the node harness cannot see: clsKindFields would then only run
        on a language switch, and a chosen date would never be remembered."""
        self.assertIn('$("clsKind").addEventListener("change", clsKindFields)',
                      self.portal)
        self.assertRegex(  # …and once at startup, so a fresh page opens right
            self.portal, r"if \(keepKind\) ks\.value = keepKind;\s*\n\s*clsKindFields\(\);")
        self.assertRegex(  # (?s) inline — assertRegex's third arg is the message
            self.portal,
            r'(?s)\$\("classesBody"\)\.addEventListener\("change".*?clsDates\[')

    def test_a_hidden_field_inside_a_form_row_is_actually_hidden(self):
        """`.row > *` sets flex, and an author rule beat the UA sheet's
        [hidden]{display:none} once already — every class row showed its
        Delete button while marked hidden. The Period box sits in a .row."""
        self.assertIn(".row > [hidden]", self.portal)
        self.assertRegex(self.portal, r"\.row > \[hidden\][^{]*\{[^}]*display:none")
        # the rule is keyed on that parent, so the box has to still be in one.
        # Anchored on the sibling field rather than a `.*?` reach from the first
        # <div class="row"> in the file — that one matched no matter where the
        # box moved to.
        self.assertRegex(
            self.portal,
            r'<div class="row">\s*<div>\s*<label for="clsKind"[^>]*></label>'
            r'\s*<select id="clsKind"></select>\s*</div>'
            r'\s*<div id="clsPeriodWrap">')

    def test_event_kinds_match_the_store(self):
        from app.store import CLASS_EVENT_KINDS

        for kind in CLASS_EVENT_KINDS:
            self.assertIn(f'data-c="{kind}"', self.portal,
                          f"portal has no button that logs {kind!r}")

    def test_both_languages_label_every_event_kind(self):
        """A bare `"{kind}:" in portal` check was satisfied by the Chinese
        table alone — deleting only the English label went unnoticed, as did
        replacing the whole map with a comment that happened to name the keys."""
        from app.store import CLASS_EVENT_KINDS

        blocks = re.findall(r"\bev:\s*\{(.*?)\}", self.portal, re.S)
        self.assertEqual(len(blocks), 2, "expected one ev: map per language")
        for lang, block in zip(("zh", "en"), blocks):
            for kind in CLASS_EVENT_KINDS:
                with self.subTest(lang=lang, kind=kind):
                    self.assertRegex(
                        block, rf"{kind}\s*:\s*[\"']",
                        f"{lang} has no label for event kind {kind!r}",
                    )

    def test_the_draw_down_button_belongs_to_per_class_packs_only(self):
        """Inverting this conditional took the '✓ Attended' button away from
        the packs that need it and gave it to period fees, where attending
        means nothing. The strings all still existed, so a grep passed."""
        block = self.portal[
            self.portal.index("  function renderClasses() {"):
            self.portal.index("  function clsEvents(p) {")
        ]
        self.assertRegex(
            block,
            r'p\.kind === "per_class"\s*\?\s*\'<button class="pay" data-c="attended"',
            "the attended button is no longer gated on a per_class package",
        )

    def test_the_expense_row_handler_ignores_class_rows(self):
        """Class rows reuse `.ex-hd`. A document-level handler bound to it ran
        `toggleItem(null)` and re-rendered, closing the row the class handler
        had just opened — tapping a course did nothing at all."""
        for match in re.finditer(r"toggleItem\(item\.getAttribute\(\"data-id\"\)\)",
                                 self.portal):
            line_start = self.portal.rfind("\n", 0, match.start())
            line = self.portal[line_start:match.end()]
            self.assertIn(
                'item.getAttribute("data-id")', line.split("toggleItem")[0],
                "toggleItem is called without first checking the row is an "
                "expense; class rows carry data-pkg and would pass null",
            )

    def test_hidden_row_controls_are_actually_hidden(self):
        """`.btns { display:flex }` is an author rule and out-ranks the UA
        sheet's `[hidden] { display:none }`, so every course row showed its
        action buttons — Delete included — whether open or not."""
        self.assertRegex(
            self.portal,
            r"\.btns\[hidden\][^{]*\{[^}]*display\s*:\s*none",
            "a hidden .btns will still render; add an explicit display:none",
        )

    def test_a_course_row_renders_its_class_log(self):
        """Dropping clsEvents(p) from the row removed the log and the only
        control that can take a mis-logged class back."""
        block = self.portal[
            self.portal.index("  function renderClasses() {"):
            self.portal.index("  function clsEvents(p) {")
        ]
        self.assertIn("clsEvents(p)", block)
        self.assertIn("data-unlog=", self.portal)

    def test_every_class_value_reaching_innerhtml_is_escaped(self):
        """P6/XSS: a course name is free text and the rows are built with
        innerHTML. Account for each interpolation of the package fields."""
        # Scoped to the class-tracker block: `e` means an *expense* everywhere
        # else in this file, so a whole-file scan only rediscovers those.
        start = self.portal.index("  function clsLine(p) {")
        end = self.portal.index("  function render() {")
        block = self.portal[start:end]
        self.assertIn("function renderClasses()", block, "block markers moved")
        base = self.portal[:start].count("\n")
        offenders = []
        for offset, line in enumerate(block.splitlines()):
            for expr in ("p.name", "p.period_label", "e.note", "e.logged_by",
                         "e.date", "p.id", "e.id", "e.description"):
                if expr not in line:
                    continue
                # subtract the known-safe forms, then see what is left — the
                # same shape as PortalEscapingTests, because `esc(x || y)` is
                # safe and a naive `esc(x)` match does not recognise it
                residue = line.replace(f"esc({expr}", "")
                # a property read, a truthiness guard or an object-key lookup
                # (`openPkgs[p.id]`) is not an interpolation into HTML
                residue = re.sub(
                    re.escape(expr) + r"\s*(\?|\)|\]|\.|,|;|=|$)", "", residue
                )
                if expr in residue:
                    offenders.append(
                        f"  app/portal.html:{base + offset + 1}: {line.strip()}"
                    )
        self.assertEqual(offenders, [], "unescaped class field in innerHTML:\n"
                         + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
