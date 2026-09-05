#!/usr/bin/env python3
"""Post-deploy live smoke — run from your PC right after `gcloud run deploy`.

Proves the two things the sqlite test suite cannot:
  1. db/schema.sql applies cleanly to the real Neon Postgres;
  2. the deployed service works end-to-end over the public URL.

Usage:
  DATABASE_URL='postgres://…' python3 scripts/smoke_live.py \
      --base-url https://family-expenses-xxxx.a.run.app

Creates a temporary link + expense, exercises the full flow, then deletes the
expense and revokes the link. Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database  # noqa: E402
from app.store import Store  # noqa: E402

FAILED = False

# One list, used by both the assertion and the line the operator reads — the
# label said "10 tools" for a release in which the assertion checked 13.
EXPECTED_MCP_TOOLS = {
    "expenses_help", "expenses_list", "expenses_add", "expenses_mark_paid",
    "expenses_update", "expenses_delete", "expenses_history",
    "expenses_refund", "expenses_refund_delete",
    "expenses_mint_link", "expenses_revoke_link", "expenses_list_links",
    "classes_list", "classes_add", "classes_log", "classes_log_delete",
    "classes_update", "classes_delete",
}


def is_neon_pooled_url(url: str) -> bool:
    """Return whether a Postgres URL names Neon's pooled endpoint."""
    hostname = urlparse(url).hostname or ""
    return "-pooler." in hostname and hostname.endswith("neon.tech")


def exercise_pooled_token_gate(store, token: str, repetitions: int = 6) -> bool:
    """Cross psycopg's default prepare threshold on the runtime query shape."""
    if repetitions < 6:
        raise ValueError("pooled token gate requires at least six repetitions")
    return all(store.validate_token(token) is not None for _ in range(repetitions))


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(f"  {'✓' if ok else '✗ FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILED = True


def get(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.status, r.read().decode()


_TOKEN_RE = re.compile(r"[0-9a-f]{32,}")


def redact_tokens(text: str) -> str:
    """Never let a portal token reach stdout — it is a bearer credential and
    this script's output gets pasted into transcripts and CI logs."""
    return _TOKEN_RE.sub("<redacted>", text or "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def get_no_redirect(url: str) -> tuple[int, str, str]:
    """(status, Location, body) without following the redirect.

    The portal sits behind Auth0 since v0.5.0, so a valid link answers 302 →
    /login. Following that lands on Auth0's own login page, which answers 400
    to a scripted request — and this script read that as a dead deployment.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=30) as r:
            return r.status, r.headers.get("Location", ""), r.read().decode()
    except urllib.error.HTTPError as exc:
        body = ""
        if exc.code not in (301, 302, 303, 307, 308):
            try:
                body = exc.read().decode()
            except Exception:
                body = ""
        return exc.code, exc.headers.get("Location", ""), body


def post(base: str, name: str, **body):
    req = urllib.request.Request(
        f"{base}/api/{name}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _tool_payload(result):
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured.get("result", structured)
    return json.loads(result.content[0].text)


def exercise_portal_api(base: str, token: str) -> None:
    """The full portal flow over /api/*. Only reachable when the portal is NOT
    behind Auth0 — with login on, these endpoints answer 401 to a script and
    the MCP flow below is what proves the write path end to end."""
    out = post(base, "submit", token=token, date="2026-01-01",
               amount=1.23, description="[smoke] delete me", submitted_by="smoke")
    eid = out["expense"]["id"]
    check("submit", out["ok"])

    out = post(base, "list", token=token, status="unpaid")
    check("list shows it", any(e["id"] == eid for e in out["expenses"]))
    check("list carries the household's today", bool(out.get("today")), str(out.get("today")))

    out = post(base, "update", token=token, id=eid, changed_by="smoke",
               fields={"amount": 2.34, "description": "[smoke] updated"})
    check("update", out["expense"]["amount"] == 2.34 and
          out["expense"]["description"] == "[smoke] updated")

    out = post(base, "mark-paid", token=token, id=eid,
               paid=True, paid_date="2026-01-02", changed_by="smoke")
    check("mark paid", out["expense"]["paid"])

    out = post(base, "mark-paid", token=token, id=eid,
               paid=False, changed_by="smoke")
    check("unmark paid", not out["expense"]["paid"] and
          out["expense"]["paid_date"] is None)

    out = post(base, "history", token=token, id=eid)
    actions = [h["action"] for h in out["history"]]
    check("history atomic trail", actions == [
        "create", "update", "mark_paid", "unmark_paid"
    ], str(actions))

    out = post(base, "delete", token=token, id=eid, changed_by="smoke")
    check("delete (cleanup)", out["ok"])


async def exercise_public_mcp(base: str) -> None:
    """Use the real MCP client stack for inventory and conversational flows."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    created_ids: list[str] = []
    package_ids: list[str] = []   # removed FIRST in cleanup: a funded payment refuses delete
    async with streamablehttp_client(f"{base}/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            prompts = await session.list_prompts()
            assert {tool.name for tool in tools.tools} == EXPECTED_MCP_TOOLS
            assert {prompt.name for prompt in prompts.prompts} == {
                "jizhang", "duizhang", "xiufu",
            }
            try:
                chinese = _tool_payload(await session.call_tool("expenses_add", {
                    "amount": "¥13.57", "description": "[smoke-mcp] 足球课",
                    "submitted_by": "smoke-mcp",
                }))
                created_ids.append(chinese["id"])
                english = _tool_payload(await session.call_tool("expenses_add", {
                    "amount": "9.43 rmb", "description": "[smoke-mcp] football gear",
                    "submitted_by": "smoke-mcp",
                }))
                created_ids.append(english["id"])

                listed = _tool_payload(await session.call_tool(
                    "expenses_list", {"status": "unpaid", "query": "smoke-mcp"}
                ))
                assert {e["id"] for e in listed["expenses"]} == set(created_ids)

                ambiguous = _tool_payload(await session.call_tool(
                    "expenses_delete", {"query": "smoke-mcp"}
                ))
                assert ambiguous["matched"] == 2 and len(ambiguous["candidates"]) == 2

                corrected = _tool_payload(await session.call_tool(
                    "expenses_update", {"expense_id": chinese["id"], "amount": "14.25块"}
                ))
                assert corrected["amount"] == 14.25
                paid = _tool_payload(await session.call_tool(
                    "expenses_mark_paid", {"query": "足球课"}
                ))
                assert paid["id"] == chinese["id"] and paid["paid"]
                history = _tool_payload(await session.call_tool(
                    "expenses_history", {"expense_id": chinese["id"]}
                ))
                assert [h["action"] for h in history["history"]] == [
                    "create", "update", "mark_paid",
                ]

                # The 30-day boundary, against real Postgres. The Due tab
                # partitions unpaid rows on it, so a row past the horizon must
                # be unpaid-but-not-upcoming — if those two ever stop being
                # different questions, one of the portal's two sections is
                # empty for the wrong reason.
                #
                # Being straight about what this does NOT cover: from v0.6.0 to
                # v0.12.0 the portal hid every such row, and this layer stayed
                # green throughout — /api/list was correct the whole time and
                # the loss was in rendering. DueTabVisibilityTests is the guard
                # for that; this one pins the semantics it relies on.
                far_date = (date.today() + timedelta(days=200)).isoformat()
                far = _tool_payload(await session.call_tool("expenses_add", {
                    "amount": "1980", "date": far_date,
                    "description": "[smoke-mcp] far-future 足球课",
                    "category": "aden-sports", "submitted_by": "smoke-mcp",
                }))
                created_ids.append(far["id"])
                horizon = _tool_payload(await session.call_tool(
                    "expenses_list", {"status": "unpaid", "query": "far-future"}
                ))
                assert far["id"] in {e["id"] for e in horizon["expenses"]}, horizon
                s = horizon["summary"]
                assert s["unpaid_count"] == 1 and s["upcoming_count"] == 0, s

                # The class tracker, end to end. Until v0.11.0 these three
                # tools appeared in this script only as names in the inventory
                # assertion, so a deploy whose Classes tab was entirely broken
                # still printed PASS. ¥1000 over 3 is the split that does not
                # divide evenly — the case every class-money defect survived.
                course = _tool_payload(await session.call_tool("expenses_add", {
                    "amount": "1000", "description": "[smoke-mcp] 钢琴课",
                    "category": "aden-edu", "submitted_by": "smoke-mcp",
                }))
                # …and it is cleaned up. Until v0.13.0 this id was never
                # appended, so every smoke run left a ¥1,000 unpaid row in her
                # 待付 tab and a live course in her Classes tab.
                created_ids.append(course["id"])
                package = _tool_payload(await session.call_tool("classes_add", {
                    "name": "[smoke] 钢琴课", "class_count": "3",
                    "expense_id": course["id"],
                }))
                package_ids.append(package["id"])
                assert package["summary"]["rate"] == 333.33, package["summary"]
                logged = _tool_payload(await session.call_tool("classes_log", {
                    "package_id": package["id"], "kind": "attended",
                }))
                s = logged["summary"]
                assert s["remaining"] == 2, s
                # the invariant the whole feature rests on: a part is derived
                # from the total, so the parts cannot sum past what was paid
                assert round(s["used_amount"] + s["remaining_amount"], 2) == 1000.00, s
                listed = _tool_payload(await session.call_tool("classes_list", {}))
                assert any(p["id"] == package["id"] for p in listed["packages"])

                # Refunds, end to end against real Postgres. The widened
                # expense_history.action constraint is v0.13.0's largest new
                # risk and this is the one place it meets production: a refund
                # writes a 'refund' history row, so if the startup migration
                # did not apply, this is where it shows. ¥100.01 so the
                # effective figure does not divide by three either.
                _tool_payload(await session.call_tool("expenses_mark_paid", {
                    "expense_id": course["id"], "changed_by": "smoke-mcp",
                }))
                refunded = _tool_payload(await session.call_tool("expenses_refund", {
                    "expense_id": course["id"], "amount": "100.01",
                    "reason": "[smoke-mcp]", "changed_by": "smoke-mcp",
                }))
                assert refunded["amount"] == 899.99, refunded
                assert refunded["gross_amount"] == 1000.0, refunded
                assert refunded["package"]["summary"]["amount"] == 899.99, refunded["package"]
                restored = _tool_payload(await session.call_tool("expenses_refund_delete", {
                    "refund_id": refunded["refund"]["id"], "changed_by": "smoke-mcp",
                }))
                assert restored["amount"] == 1000.0 and restored["refunds"] == [], restored
                history = _tool_payload(await session.call_tool("expenses_history", {
                    "expense_id": course["id"],
                }))
                assert [h["action"] for h in history["history"]][-2:] == [
                    "refund", "refund_delete",
                ], history
            finally:
                for package_id in package_ids:
                    await session.call_tool(
                        "classes_delete",
                        {"package_id": package_id, "changed_by": "smoke-mcp"},
                    )
                for expense_id in created_ids:
                    await session.call_tool(
                        "expenses_delete",
                        {"expense_id": expense_id, "changed_by": "smoke-mcp"},
                    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="deployed service URL")
    base = parser.parse_args().base_url.rstrip("/")

    print("1. Postgres schema (direct via DATABASE_URL)")
    store = Store(Database())  # requires DATABASE_URL env
    if not store.db.is_pg:
        print("  ✗ DATABASE_URL is not set to a postgres:// URL"); return 1
    if not is_neon_pooled_url(store.db.url):
        print("  ✗ DATABASE_URL must be the Neon pooled URI for the runtime gate")
        return 1
    store.db.init()  # idempotent — proves the portable DDL on real PG
    check("schema.sql applied idempotently to Postgres", True)
    # v0.13.0 widened expense_history.action in place at startup. If that
    # did not take, every refund, course edit and class log is refused —
    # and the only other sign is one WARNING line in the Cloud Run log.
    missing = store.db.history_actions_missing()
    check("expense_history accepts every action (v0.13.0 migration applied)",
          not missing, f"still rejected: {missing}")
    # v0.13.1 added three columns to expense_refunds in place; without them
    # every refund INSERT fails
    gaps = store.db.refund_columns_missing()
    check("expense_refunds has its resize columns (v0.13.1 migration applied)",
          not gaps, f"missing: {gaps}")

    minted = store.mint_token(label="smoke-test")
    token = minted["token"]
    check("temporary smoke link minted", len(token) == 64)
    check(
        "pooled token validation repeated 6x (prepared-statement threshold)",
        exercise_pooled_token_gate(store, token),
    )

    eid = None
    try:
        print("2. Deployed service over the public URL")
        status, _ = get(f"{base}/health")
        check("GET /health", status == 200)

        status, location, html = get_no_redirect(f"{base}/t/{token}")
        portal_behind_login = status == 302 and location.startswith("/login")
        check(
            "portal accepts the link"
            + (" (302 → /login, Auth0 on)" if portal_behind_login else ""),
            portal_behind_login or (status == 200 and "家庭开支" in html),
            # the Location carries ?next=/t/<token>; printing it would put a
            # live portal token in a terminal, a CI log or a pasted transcript
            f"status={status} location={redact_tokens(location) or '-'}",
        )

        status, _location, _body = get_no_redirect(f"{base}/t/definitely-wrong-token")
        # 404 before any login round trip: a bad link must not send someone to
        # Auth0 only to be rejected at the end of it
        check("bad token rejected (404, without a login round trip)", status == 404,
              f"status={status}")

        if portal_behind_login:
            print("3. Portal API is guarded; mutations verified over MCP in step 4")
            # /api/* is session-guarded, so this script cannot drive it. That the
            # guard HOLDS is the assertion worth making here.
            for name in ("list", "submit"):
                try:
                    post(base, name, token=token)
                    check(f"/api/{name} refuses an unauthenticated caller", False,
                          "expected 401")
                except urllib.error.HTTPError as exc:
                    check(f"/api/{name} refuses an unauthenticated caller",
                          exc.code == 401, f"status={exc.code}")
            eid = None
        else:
            print("3. Full expense flow (temporary data, cleaned up below)")
            exercise_portal_api(base, token)

        status, _ = get(f"{base}/health")  # still alive after the workout
        check("service healthy after flow", status == 200)

        print("4. Public MCP client + bilingual conversational flow")
        try:
            asyncio.run(exercise_public_mcp(base))
            check("initialize, %d tools, 3 prompts, bilingual writes/reads/cleanup"
            % len(EXPECTED_MCP_TOOLS), True)
        except Exception as exc:
            check("public MCP gate", False, f"{type(exc).__name__}: {exc}")
    finally:
        print("5. Cleanup")
        # packages first: a payment that funds one cannot be deleted, so the
        # expense sweep below would refuse the course row and leave both behind
        for package in store.list_packages(include_archived=True):
            if str(package["name"] or "").startswith("[smoke"):
                try:
                    store.delete_package(package["id"])
                except Exception:
                    print("  ! could not delete smoke class package — remove it manually")
        if eid:
            try:
                post(base, "delete", token=token, id=eid, changed_by="smoke")
            except Exception:
                print("  ! could not delete smoke expense — remove '[smoke]' row manually")
        for expense in store.find("smoke-mcp"):
            try:
                store.delete(expense.id, changed_by="smoke-cleanup")
            except Exception:
                print("  ! could not delete MCP smoke expense — remove it manually")
        check("smoke link revoked", store.revoke_token(token))

    print("\nRESULT:", "FAIL — see ✗ above" if FAILED else
          "PASS — deployment verified end-to-end.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
