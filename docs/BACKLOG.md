# Backlog — deferred work

Known, deliberately-not-done items. Nothing here blocks daily use — the ledger
is live and in use by two people since 2026-08-11. Each entry says why it was
deferred, so a future session can judge whether that reasoning still holds.

**Cleared 2026-08-11 (v0.9.0):** every code defect that was filed here is fixed
with a regression test, and the two operational unknowns turned out to need no
action. See `docs/CHANGELOG.md` [0.9.0] for what each one actually cost. What
survives below is one item the owner deliberately deferred, one found by the
review of that release, and one that cannot be verified from inside this repo.

---

## 1. Move the portal to its own Auth0 tenant

**Filed 2026-08-11. Deferred by the owner the same day** ("forget the logo and
app name, file it as a long term improvement"). Priority: low. Do not do this
without asking — it is the one item here that costs her something.

**Symptom.** The portal login page shows the WorkOS logo and labeling, which is
wrong for a household expense app.

**Why it can't just be fixed.** Branding is tenant-level. `/branding` on
`work-os.jp.auth0.com` carries `logo_url: https://matt-sd.netlify.app/work-os-logo.png`
and is shared by every application in the tenant — changing it would rebrand the
WorkOS admin login too. The tenant `friendly_name` behaves the same way, which is
where any remaining "WorkOS" *text* comes from. The only isolated lever is the
application's own `logo_uri`, and the tenant is on New Universal Login, which does
not reliably honor per-application logos.

**The real reason to do it.** The contract calls this project's isolation from
`work-dashboards` "structural (own repo, own database, own services)". Adding
portal OAuth in v0.5.0 put a shared **Auth0 tenant** underneath both. (The GCP
project is shared too — `work-dashboards`, 693424932326 — which the contract
never claimed otherwise, but it is worth knowing when reasoning about
isolation.) Branding is the visible symptom; the coupling is the actual issue.

**What it involves.** New tenant (e.g. `family-expenses.jp.auth0.com`), new
Regular Web Application, recreate the one user, own branding, then swap
`AUTH0_DOMAIN` / `AUTH0_CLIENT_ID` and the client-secret Secret Manager entry.
No application code changes — `app/auth.py` reads all of it from env.

**A8 cost — the free window has closed.** Changing tenants invalidates existing
sessions: anyone signed in is signed out once and must log in again against the
new tenant. This was free before onboarding. **She was onboarded on 2026-08-11**
and is now using the portal daily, so doing this costs her one forced re-login
with a new password — exactly the friction §5.1 exists to prevent. Not fatal,
but it must be scheduled and explained to her rather than done quietly. Weigh it
against the fact that the only symptom is a logo.

---

## 2. MCP tool calls still block the event loop

**Filed 2026-08-11**, during the adversarial review of v0.9.0. Priority: low at
two users; it is the unfinished half of a fix that shipped.

v0.9.0 moved the `/api/*` handlers off the event loop with `run_in_threadpool`.
The ten MCP tools in `app/mcp_server.py` are all plain `def` and were left alone
on the strength of a claim — written in the old backlog — that "FastMCP uses a
threadpool". **That claim is false.** A reviewer read the installed `mcp` 1.26.0:
`FuncMetadata.call_fn_with_arg_validation` ends in `return fn(**arguments)` for
synchronous functions, with no `to_thread`, and confirmed it empirically by
observing `Store.list` execute on `MainThread` during an `expenses_list` call.

**Consequence.** An owner MCP query against a cold Neon compute blocks the loop
for the whole round trip, stalling every concurrent `/api/*` request and
`/health` — the exact failure the API-side fix was meant to remove. At two users
this is a latency wart, not an outage.

**Fix.** Make each tool `async def` and `await run_in_threadpool(...)` around the
store calls, or wrap the bodies in `anyio.to_thread.run_sync`. Ten small edits;
none of them touch the `/mcp` mount path, its no-auth posture, or any tool
signature, so §5.1 is not engaged. Worth doing next time the MCP surface is open
for other reasons rather than on its own.

## 3. The class tracker has no audit trail, and no way to retire a course

**Filed 2026-08-11** during the review of v0.10.0. Priority: low, but it is a
real asymmetry with how the rest of this app treats money.

Every expense mutation writes an `expense_history` row in the same transaction.
Class packages write none: `create_package`, `update_package`, `delete_package`,
`log_class` and `delete_class_event` leave no record, and there is no
`class_history` table. `class_events` is the log of what happened in the
classes, not of who edited the tracker. Changing `class_count` — which divides
the money — is unrecorded, and deleting a package destroys a term of attendance
with nothing remembering it.

Related, from the same review:
- **`archived` is unreachable.** It is threaded through the store, the API and
  `classes_list(include_archived=…)`, but nothing can set it: the portal never
  calls `/api/classes-update` and there is no MCP tool. Retiring a finished
  course therefore means deleting it, which destroys the log. Either wire up
  archiving or drop the flag.
- **No `classes_delete` / `classes_update` MCP tool.** The owner works through
  MCP, so removing a course is portal-only. The delete-refusal on a funding
  payment now says "from the Classes tab in the portal" rather than naming a
  tool that does not exist, but it is still a dead end from the MCP side.
- **Server error strings are English only.** The portal surfaces them verbatim
  in a toast, and its primary user reads the Chinese UI. True of every error in
  the app, not just the class ones, which is why it is filed rather than
  half-fixed here.
- **A borrow-funded package is coherent but unexplained.** Nothing stops a
  package being funded by a `category='borrow'` row, so the same ¥2,200 can read
  as "owed back to me" on the Due tab and "remaining" on the Classes tab. No
  total is corrupted — `Store.summarize` is untouched — but the two views
  describe one payment two ways with nothing reconciling them.

## 4. ~~Form-submit handlers are an untested layer, project-wide~~ — CLOSED

**Filed 2026-08-11** by the ninth review round of v0.10.0. **Closed 2026-08-11**
in v0.11.0, when a review round pointed out that the release had changed the
meaning of a field one of those handlers reads.

Both handlers now run under node against stub form fields —
`ClassAddFormTests` (`addClsForm`) and `ExpenseAddFormTests` (`addForm`, live
since v0.1 and the one that writes money directly). The four mutations this
entry named as surviving the whole suite — `kind` hard-coded to `per_class`,
`class_count + 1`, `expense_id` taken from `candidates[0]`, name and period
label swapped — are each caught now, as are four on the expense side:
`parseFloat`→`parseInt` on the amount, a cleared date sent blank instead of
defaulting to the household's today, a blank description sent as `""` rather
than NULL, and the category read from the wrong field.

## 5. `store.find` matches the category column, so a query can hit a row that never mentions it

**Filed 2026-08-11** by the third review round of v0.11.0. Priority: medium —
it is the same defect class that round fixed on the class side, sitting on the
tools that move money directly. **Pre-existing and untouched by that release**
(`store.find` and `_resolve` are unchanged across `57033b8..HEAD`).

`Store.find` matches the query against `description` **or** `category`, and
`_resolve` in `app/mcp_server.py` resolves on a single match with no tiering
and no signal about which column produced it. A reviewer demonstrated
`expenses_mark_paid(query='aden-edu')` marking a 水电 expense paid — the query
word appears nowhere in its description. Every targeting tool routes through
it: `expenses_mark_paid`, `expenses_update`, `expenses_delete`, `classes_add`.

**Why it is filed rather than fixed.** v0.11.0 fixed exactly this shape in
`_resolve_package` — description matches became a weaker tier that never
resolves alone. The same tiering is the obvious fix here (`description` is the
primary signal, `category` the fallback, and a category-only match asks). But
these are the tools that mark paid, edit amounts and delete rows, and this
release has already had two fix waves whose own fixes were defective. Changing
the targeting logic of every money tool as the last edit before a deploy is the
pattern that caused those. It wants its own change and its own review round.

**Watch for:** the fix must keep `expenses_list(query=…)` matching categories —
searching by bucket is a legitimate read. Only the single-match *resolution*
used by write tools needs the tier.

## 6. `classes_log` has no idempotency, so a retry cannot be made safe

**Filed 2026-08-11** by the third cross-model review of v0.11.0. Priority:
medium — it bounds what the portal is allowed to do about a lost response.

`class_events` has no uniqueness constraint (`db/schema.sql`), and
`Store.log_class` inserts unconditionally. A request that commits server-side
but whose response is lost therefore cannot be retried safely: the retry writes
a second row, both are counted by `summarize_package`, and the money moves.

**What this already cost.** v0.11.0 added a per-course in-flight lock, then a
30-second timer to release it if a request never settled. The timer was a worse
bug than the deadlock it fixed — it is precisely the blind retry described
above. It was removed. A course whose request never settles now stays locked
until the page is reloaded, which is visible (the buttons stay disabled) and
recoverable, and that is the deliberate trade.

**Related, accepted for now.** A tab left VISIBLE across midnight repaints
nothing, so an untouched class-date box keeps showing yesterday and — the box
being what gets written — logs yesterday. A `setTimeout` armed for the
household's midnight was tried and reverted: it re-armed against its own
expired deadline and became a one-second render loop, refused to arm at all
when a DST fall-back made the countdown exceed 24h, and misbehaved on a
backward clock change. The reviewer's judgement, which was taken: the target
bug is narrow and VISIBLE (the date is on screen and in the success toast),
`visibilitychange` already covers a phone, and the timer as written was more
dangerous than the behaviour it corrected. A one-shot midnight callback that
simply calls `refresh()` and is re-armed only by a fresh server response would
be the right shape — with a test that actually fires the callback, which the
reverted one lacked.

**What a real fix looks like.** Either a client-generated event id carried on
the request and made UNIQUE in the table, so a retry collapses onto the same
row; or a reconciliation read (`classes_list`) before permitting the retry.
The first is additive schema plus one column and is the better shape. Until one
exists, **do not add any automatic retry or timed unlock to the class log** —
that is the whole reason this entry is here.

## 7. Twelve live rows carry a category that is not a category

**Filed 2026-08-11** while building the v0.11.0 dropdown filter. Priority: low —
no total is wrong — but it is invisible from inside the code.

`category` is free text by design (the MCP can write anything), and the twelve
monthly living-expense rows in production are categorised **`living expenses`**,
not the canonical `living`. Nothing errors: they count in every total, and the
Stats tab charts them under the literal string, i.e. in a bucket beside the
`生活费` one rather than in it. It is exactly the failure `CategoryParityTests`
was written to prevent between the portal and the store, happening instead
between an agent and the store.

`is_class_category()` is deliberately forgiving about case and whitespace for
this reason, but it cannot rescue a genuinely different word. Two options: a
one-off `UPDATE expenses SET category='living' WHERE category='living expenses'`
(12 rows, a real write against production, so it needs the owner's go-ahead), or
accept free text and stop pretending the canonical list is closed. Do not
"fix" it by making the store reject unknown categories — that would break the
MCP's documented behaviour of accepting anything.

## 8. Neon's restore window has never been recorded or rehearsed

**Filed 2026-08-11.** Priority: medium — this is the only item here that could
cost real data.

`FIRST_DEPLOY_PLAN.md` "Operations after launch" calls for confirming the
point-in-time-restore window and rehearsing a restore. Neither happened.
Backups are currently an assumption, not a verified capability, and this is
19 rows of real money with an append-only audit trail that only exists in one
place.

**Why it is still open.** It cannot be checked from inside this repo: there is
no Neon CLI installed and no API key in the environment, so the retention window
is only visible in the Neon console. Rehearsing a restore also creates a Neon
branch — an action against live infrastructure that needs the owner's
go-ahead rather than an agent's initiative.

**What to do, in order.**
1. Read the retention window: Neon console → project → Settings → *Restore
   window* (free tier has historically been 24h; paid tiers 7–30 days). Record
   the actual number here.
2. Rehearse: create a branch from a timestamp ~1 hour ago, point a throwaway
   `DATABASE_URL` at it, and run
   `python3 -c "import os,psycopg;print(psycopg.connect(os.environ['DATABASE_URL']).execute('select count(*) from expenses').fetchone())"`.
   Delete the branch afterwards. Nothing touches the primary.
3. A cheaper standing backstop, if the window turns out to be short: a periodic
   `pg_dump` to local storage. Note that the dump contains real household
   financial data and every live portal token — treat it like a secret (P9).

## 9. ~~"本月已付" is two different numbers on the same tab~~ — CLOSED

**Filed and closed 2026-08-11**, both in v0.12.0: filed while rendering the Due
tab against live data, then fixed the same day at the owner's direction rather
than deferred.

**Closed by** excluding borrow from the paid list — so the card and the section
agree at ¥24,399 — *and* giving the repayment a `本月已还我` section, which is
what the entry below said the real fix had to do. Three guards:
`test_the_paid_card_and_the_paid_section_agree`,
`test_household_spending_excludes_what_she_fronted` (equality alone is
satisfiable at the wrong figure) and `test_a_repayment_is_still_shown_somewhere`
(the complement). All three mutation-checked.

The original entry follows, because the reasoning about *why the obvious fix was
wrong* is the part worth keeping.

The summary **card** headed 本月已付 excludes borrow (`renderCards` filters
`!isBorrow(e)`, correct — money she fronted is not household spending). The
**section** headed 本月已付 immediately below it does not: `section()` totals
whatever rows it is given, and the `paid` list has no borrow filter. With the
live ledger that is **¥24,399 on the card and ¥55,499 on the section header** —
the same two words over a ¥31,100 gap, which is her 陈美霖 office repayment.

Reproduced by running the shipping `renderCards`/`renderNow` under node against
the 26 production rows; no stored figure is affected and `Store.summarize` is
not involved (both are portal-side sums).

**Why it is filed rather than fixed.** The obvious fix — drop borrow from the
`paid` list — recreates the bug v0.12.0 just fixed: a repaid borrow would then
appear in no section of the Due tab at all, because `sec_lent` only carries
**unpaid** borrow. That is "a filter with no complement" again (LESSONS §12).
Excluding it from the total while leaving the row listed is worse: the total
would stop describing the rows under it, which is the one thing P4 says a
summary must never do.

**What a real fix looks like.** Give 待还我 a repaid half — the section already
exists and already means "borrow" — so the row stays visible and the household
total stays honest. That is a UI decision about how she wants repayments shown,
so it needs the owner, not an agent's initiative. It was deliberately kept out
of the v0.12.0 hotfix rather than widening a production fix.
