# Feature Contract — Family Expenses

**Status:** ACTIVE (v0.11.0 deployed as `family-expenses-00014-zm5` —
in daily household use since 2026-08-11)
**Owner:** matt-debate
**Repo:** `Matt-Debate/family-expenses`
**Default branch:** `main`
**Date:** 2026-07-15

## 1. Purpose

A dead-simple web portal where a household member (the operator's spouse) can
**submit and edit personal expenses the operator needs to pay**, replacing
free-text WeChat messages. Plus an MCP surface so the operator can query the
ledger from Claude / ChatGPT.

## 2. Architecture decision (v0.2.0 pivot)

Originally planned as a feature *inside* `work-dashboards`, reusing its parent
portal. **Superseded by owner direction (2026-07-14):**

- All code lives in **this standalone repo**. `work-dashboards` is
  reference-only — its *patterns* are reused (portal-token links, Neon
  Postgres, Cloud Run streamable-HTTP MCP), never its code, tables, or deploys.
- **Isolation is structural:** separate repo, separate database, separate
  services. Nothing to hide from the business app because nothing touches it.
  (This retires prior invariants I1–I3 by construction.)
- **MCP hosting: Cloud Run** streamable-HTTP (same stack as the existing
  `work-dashboards` cloud MCP: Python `mcp` SDK ≥1.12, Dockerfile + Cloud
  Build), per owner: "Cloud Run already works; no need to introduce new tech."
- Push to the designated branch after every completed chunk.

## 3. Requirements (owner spec)

- **Sa** — No approval workflow. Spouse submits; anyone with the link can edit;
  **every change is kept in an append-only history**.
- **Sb** — Simple model: add an expense; edit it if something changes; a way to
  **check it off as paid on a date**. Not a business system.
- **Sc** — An **MCP** so the operator can query via Anthropic/OpenAI clients.
- **Sd** — Proper documentation, versioning, and testing despite trivial scope.

### Non-goals
No approval flow, no user accounts/roles, no multi-currency conversion (single
default CNY, code stored), no receipts/recurring/budgets in v1.

## 4. Data model (single-tenant, portable SQL)

The app is single-household — no tenant column. SQL is written to run on both
**Postgres (Neon, production)** and **sqlite (tests)**: application-managed
ISO-8601 UTC timestamps (no DB triggers), `snapshot` stored as JSON **TEXT**,
no arrays, no PG-only expressions.

### `expenses`
| column | type | notes |
|---|---|---|
| `id` | TEXT PK | 12-hex app-generated |
| `date` | TEXT NOT NULL | `YYYY-MM-DD` — the **due** date: when it must be paid |
| `amount` | REAL NOT NULL CHECK > 0 | the **gross** figure. Every read exposes the **effective** amount (gross − refunds) under the name `amount`, with `gross_amount` and `refunded` beside it, so no total or course rate can miss a refund; `update(amount=…)` and the portal edit box write the gross figure and refuse to go below what came back |
| `currency` | TEXT NOT NULL DEFAULT 'CNY' | **CNY only, enforced** — every total adds `amount` without conversion, so one foreign row would falsify all of them |
| `category` | TEXT | free text; canonical keys in `store.CATEGORIES`. **`borrow` is arithmetic-bearing** — money she fronted, owed back, excluded from every household total |
| `description` | TEXT | what it was |
| `paid` | BOOLEAN NOT NULL DEFAULT FALSE | |
| `paid_date` | TEXT | required iff `paid` (CHECK) |
| `submitted_by` | TEXT | attribution, not auth. Portal writes are stamped server-side with the link's label and ignore any client value; MCP callers name the speaker |
| `created_at` / `updated_at` | TEXT NOT NULL | app-managed UTC ISO |

### `expense_history` (append-only)
`id` PK, `expense_id`, `action` CHECK in
(`create`,`update`,`mark_paid`,`unmark_paid`,`delete`,`refund`,`refund_delete`,
`package_create`,`package_update`,`package_delete`,`class_log`,`class_unlog`),
`changed_by`, `changed_at`, `snapshot` TEXT (JSON; post-change state,
pre-change for the deletes). Never updated or deleted by application code.
Since v0.13.0 the class tracker's own mutations are recorded here **under the
payment that funds the course** (a package is 1:1 with its expense and never
re-pointed), and a `package_delete` snapshot carries every logged class — so
removing a course destroys nothing. The action list is mirrored in
`app/models.py HISTORY_ACTIONS` (parity-tested); widening the CHECK on a live
database is done by `Database._migrate_history_actions()` at startup.

### `expense_refunds` (v0.13.0)
`id` PK, `expense_id` FK → `expenses` ON DELETE CASCADE, `amount` CHECK > 0,
`date`, `reason`, `changed_by`, `created_at`. **Money that came back on a paid
row, as its own dated fact.** The payment keeps its amount and dates; the
refund cannot exceed what is left; an unpaid row cannot be refunded (that is a
price change, `update`); a `borrow` row cannot be refunded (a repayment is
`mark_paid`; a partial one has no primitive — `docs/BACKLOG.md` §11); and a
refunded row cannot go back to unpaid (`mark_paid(paid=False)` refuses, the
mirror of the same rule). Amounts are rounded to cents at the write, so the
refunds listed on a row always sum to its `refunded`. A refund reduces its
row **in the row's own month** —
the refund date is recorded and shown, but it does not create a credit in the
month it arrived, because every tab buckets by row and a dated credit would be
a negative row, which the schema forbids. Deleting the payment snapshots its
refunds into history and cascades them away.

### `class_packages` / `class_events` (v0.10.0)

A **package** is a prepaid course funded by exactly one `expenses` row
(`expense_id` UNIQUE — two packages on one payment would each claim the whole
amount and double-count it). It stores **no money of its own**: the per-class
rate is `expenses.amount / class_count` — the **effective** amount, after
refunds — derived at read time, so correcting the payment or refunding part of
it corrects the tracker and the two cannot disagree. `kind` is `per_class`
(a pack of N classes drawn down by attending) or `period` (a flat month/semester
fee where the classes that did NOT happen are owed back). An expense that funds
a package cannot be deleted until the package is — the refusal names
`classes_delete(package_id=…)` — and cascading is deliberately not offered.
`class_count` can be edited (`classes_update`, the portal's course editor, or
`expenses_refund(resize_package_to=…)` in the same transaction as the refund),
but never below the classes already logged: attended ones for a pack, missed
ones for a period fee. The refusal names the conflicting classes.

**`class_events`** — one row per class: `attended`, `missed_school` (they
cancelled, so it is reclaimable) or `missed_us` (we skipped, so it is
forfeited). Both missed kinds count toward what is owed; the cause is reported
separately because only one of them is worth arguing about.
`Store.summarize_package` is the ONE implementation of this arithmetic.
Amounts come from the exact ratio `amount * n / count`, never a rounded
per-class rate, and **the figures that sit side by side are derived from each
other rather than rounded independently**: `remaining_amount` is
`amount - used_amount`, and `forfeited_amount` is `owed_amount - reclaimable_amount`.
The identity `owed = reclaimable + forfeited` holds, but it is a consequence,
not the derivation — computing the total from two rounded parts is exactly how
it came to exceed the payment. Three
independent `round()` calls do not reconcile — ¥1,000 over 3 classes gave an
owed total a cent adrift from its own halves. Money is also capped at the
payment: over-logging is reported as an `overrun` count, never as owing back
more than was handed over. `rate` is a display figure only.

### `access_tokens`
`id` PK, `token` TEXT UNIQUE (`secrets.token_hex(32)`), `label`,
`expires_at` TEXT, `revoked` BOOLEAN DEFAULT FALSE, `created_at`,
`last_used_at`, `use_count`. Mirrors the `work-dashboards` portal-token
pattern, minus tenancy/scoping.

## 5. Access model

- One bookmarkable link per token: `/t/<token>`, minted via the MCP tool
  `expenses_mint_link` or `scripts/mint_link.py` (M2). No self-serve minting.
- **Links never expire by default** (owner decision 2026-07-14: the holder is
  non-technical and will not renew credentials; revocation is the kill
  switch). Bounded expiry remains available per token.
- Every API request revalidates the token (revoked + expiry-if-set,
  fail-closed on unknown/garbage tokens).
- **MCP gate is optional and OFF**: `MCP_SECRET` unset → open. Owner's risk
  ranking (final, 2026-07-14): the dominant risk is a non-technical family
  member being forced to reconnect/re-auth after a change — the app falls
  into disuse and the build is blamed. Unauthorized ledger edits are a
  lesser, recoverable risk (audit history + revocation); auth gets added
  only if abuse actually happens, as a conscious trade.

### 5.1 Compatibility contract (highest-priority invariant)
No change may require the portal link or a connected MCP client to be
reconfigured. Frozen once a family member is connected:
  * the service URL (same Cloud Run service name + region across deploys),
  * the `/t/<token>` portal path and her minted token (never expire, never
    casually revoked),
  * the `/mcp` mount path,
  * the no-auth-header posture (`MCP_SECRET` stays unset on any service her
    app points at).
Safe to change freely: portal UI, tool descriptions/additions, docs, schema
additions. Renaming/removing tools is safe for connectivity (clients list
tools dynamically) but wait for a natural moment.

## 6. API contract (portal)

JSON POST endpoints, token in body; single Cloud Run service also serving the
portal page and the MCP mount.

| endpoint | body (besides `token`) | effect |
|---|---|---|
| `/api/list` | `status?: all\|paid\|unpaid\|overdue, since?, until?` | matching expenses + **totals for exactly those rows** (`Store.summarize`) + `today` in `APP_TZ`, so the page never has to ask the device, **and `midnight_in`** (seconds left of that day) so a page left open rolls the date over AT midnight rather than 24h after the response |
| `/api/submit` | `date, amount, currency?, category?, description?, submitted_by?` | insert + history(`create`) |
| `/api/update` | `id, fields{date?,amount?,currency?,category?,description?,submitted_by?}, changed_by?` | update + history(`update`) |
| `/api/mark-paid` | `id, paid, paid_date?, changed_by?` | set paid state + history(`mark_paid`/`unmark_paid`) |
| `/api/delete` | `id, changed_by?` | delete + history(`delete`, pre-change snapshot) |
| `/api/history` | `id` | audit trail for one expense — since v0.13.0 also its refunds and the course it funds |
| `/api/refund` | `id, amount, date?, reason?, resize_package_to?` | record money that came back + history(`refund`); `resize_package_to` edits the funded course's `class_count` in the same transaction (+ history(`package_update`)) |
| `/api/refund-delete` | `refund_id` | remove one refund + history(`refund_delete`, pre-change refund in the snapshot) |
| `/api/classes-list` | `include_archived?` | packages with derived totals (ALL of them) + the untracked payments in `store.CLASS_CATEGORIES` only + `today`/`midnight_in`. The portal asks for archived rows and partitions them into 已结课 |
| `/api/classes-add` | `expense_id, name, kind, class_count, period_label?` | start tracking a course + history(`package_create`) |
| `/api/classes-log` | `package_id, kind, date? \| dates?, note?` | record one class (attended / missed_school / missed_us) — or a list of dates, all or nothing — + history(`class_log`) |
| `/api/classes-unlog` | `event_id` | take back one logged class + history(`class_unlog`); returns the package |
| `/api/classes-update` | `id, fields{name?,kind?,class_count?,period_label?,archived?}` | edit a package — never its money — + history(`package_update`); shrinking below the logged classes is refused |
| `/api/classes-delete` | `id` | remove a package and its class log + history(`package_delete`, every event in the snapshot) |

Every `/api/list` row also carries `package` (the course it funds: id, name,
kind, class_count, archived, attended, missed — or null) and `refunds` (each
with its id), which is what the refund box, its live preview and the row's
原价/退款 line render from.

**Validation (server-authoritative):** `amount > 0`; `date`/`paid_date` are
`YYYY-MM-DD`; `paid=true ⇒ paid_date`; unknown update fields rejected.
**Atomicity (resolves prior finding M3):** each mutation writes its history row
in the **same DB transaction** as the primary write — one connection,
commit-on-success / rollback-on-error.

## 7. MCP surface (operator)

Tools (18) on the Cloud Run streamable-HTTP MCP: `expenses_help`,
`expenses_list`, `expenses_add`, `expenses_update`, `expenses_mark_paid`,
`expenses_delete`, `expenses_refund`, `expenses_refund_delete`,
`expenses_history`, `expenses_mint_link`, `expenses_revoke_link`,
`expenses_list_links`, `classes_list`, `classes_add`, `classes_update`,
`classes_delete`, `classes_log`, `classes_log_delete` — inventory rationale in
`docs/MCP_DESIGN.md` (`expenses_summary` folded into `list`). Plus three persona prompts
(记账/对账/修复). Same store as the portal, so history/atomicity rules apply
identically. `expenses_mint_link` returns the full portal URL from
`PORTAL_BASE_URL`; `expenses_list_links` and `expenses_help` name the host and
never a token.

**Natural-speech design (primary requirement):** mutating tools accept a
fuzzy `query` instead of an id (one match acts; several return candidates;
mark-paid prefers the unpaid match); dates default to today in `APP_TZ`
(default Asia/Shanghai); amounts tolerate ¥/块/元/comma forms; server
instructions coach LLM clients with bilingual example utterances. Guidance
lives only in channels agents reliably read — tool descriptions, results,
error strings, annotations (`docs/MCP_DESIGN.md`); error strings coach the
correcting call; write results carry the running unpaid total.

## 8. UI

One mobile-first page, bilingual **中文 (default) / English**, four tabs:

The nav order is Due · Classes · History · Stats.

- **Due** — summary cards (due now · paid this month · upcoming within 30 days ·
  owed back to her), collapsible add form, then the unpaid rows. **Since
  v0.12.0 those are a partition of one list, not a filter:** `待付 · 未来30天`
  and `待付 · 30天以后`, both rendered expanded, so no unpaid row is invisible
  here. Owed-back-to-her keeps its own section; `本月已付` follows, and
  **excludes borrow** so it means the same thing as the card above it (they
  disagreed by ¥31,100 until v0.12.0); a repayment received this month closes
  the tab in `本月已还我`. The 30-day horizon on the summary *cards* is
  unchanged and deliberate (`Store.summarize`). Adding a row confirms with the
  description,
  due date and amount **the server stored**, read from the response.
  `DueTabVisibilityTests` sweeps today−60 … today+400 and fails if any unpaid
  row reaches no section. **Since v0.13.0 a paid row offers 退款:** an inline
  box for amount, date and reason — and, when the payment funds a per-class
  course, 课时改为 so the pack resizes in the same call. The row then shows
  the effective amount with 原价 and 退款 beneath it, lists each refund with
  an × that asks before undoing, and the edit box prefills the **original**
  amount under a label that says so (`RefundRowRenderingTests`,
  `RefundHandlerTests`). She is the one the coach refunds, and the alternative
  she would reach for is writing the amount down.
- **History** — a statement: one row per month (txns · paid · outstanding), most
  recent first, tap to expand into that month's items. Scheduled future months
  sit in their own group below, and since v0.12.0 carry the same `aria-expanded`
  and open state as a past month — the two branches had drifted. **Both money
  columns exclude borrow** (P4), so they agree with the Stats KPI, which
  buckets the same way. They deliberately do **not** agree with the Due tab's
  本月已付: History and Stats bucket by **due** month, the Due tab by
  **paid_date** month, so a bill due in July and paid in August is July's 已付
  here and August's 本月已付 there. Both are right for the question they
  answer; neither is a restatement of the other; the borrow rows are still listed inside the expanded month — an
  outstanding one marked 我垫付, a repaid one 已还我 (since v0.12.0; it read
  已付 before, the same word as a bill it is not counted with). `txns` counts every row, borrow included — it is a count, not a total.
- **Classes** — prepaid courses. A per-class pack shows classes and money
  remaining; a monthly/semester fee shows what is owed back, split into
  reclaimable and forfeited. Tap a course for its class log. Its payload is
  fetched when the tab is opened rather than on page load, and each fetch
  carries a generation number so a slow earlier response cannot repaint stale
  totals over fresh ones. Since v0.11.0: the payment dropdown offers only
  `store.CLASS_CATEGORIES` (`aden-edu`, `aden-sports`) — a view filter, not a
  money rule, since the store and MCP still link a package to any expense; a
  per-class pack is not asked for a period label; logging a class uses a date
  picker starting at the household's today, not a typed prompt; and removing a
  class record asks first, naming the record. Since v0.13.0 each open course
  has a second button row — 编辑 (name, class count, period label; never the
  money), 结课 (asks first) and 删除 — and finished courses move to a collapsed
  已结课 group at the foot of the tab rather than disappearing: the tab now
  fetches archived rows and partitions **one** list, and
  `ClassArchivePartitionTests` asserts every course reaches the markup exactly
  once, in the section it belongs to.
- **Stats** — figures and hand-rolled inline SVG charts (no chart library: no
  build step and no CDN is what makes this load behind the GFW).

The portal speaks in **her** voice — the owner works through the MCP — and spend
figures name Matt rather than claiming to be household totals. Visual design by
Claude Design (Fable), v0.7.0: a typographic ledger on warm paper.

## 9. Acceptance criteria

- **A1** Spouse: open link → add → see listed → edit → mark paid with date, on
  a phone.
- **A2** Every mutation writes exactly one history row, atomically; history is
  never mutated.
- **A3** Operator can list/summarize/mark-paid/mint from an MCP client.
- **A4** `paid=true` without `paid_date` rejected at DB CHECK and API layer.
- **A5** Revoked/unknown tokens rejected on every request; expiry enforced
  only for tokens minted with one.
- **A6** Test suite runs green in CI **without** a live Postgres (sqlite), and
  the schema applies cleanly to Postgres.
- **A7** Single casual utterances (中文 or EN) — add with spoken amount and no
  date, mark-paid by description — succeed via MCP without ids; ambiguous
  phrases return candidates rather than acting on a guess.
- **A8** No release may require re-authentication, reconnection, or
  reconfiguration by a link/connector holder (§5.1). Zero-credential
  operation is permanent unless the owner explicitly trades it away. A warm
  service must recover its stale pooled Postgres connection on the first
  request after a long idle period without user action; Cloud Run must refuse
  to start rather than silently fall back to ephemeral SQLite.

## 10. Versioning & docs

Semantic versioning in `docs/CHANGELOG.md`. `README.md` (user-facing),
`docs/RUNBOOK.md` (deploy, mint links, rotate/revoke) and this contract stay in
sync with behavior changes. `docs/IMPLEMENTATION_PLAN.md` is a **historical
record** frozen at v0.2.0, not a living document — it claimed otherwise for six
versions while saying "five tools".
