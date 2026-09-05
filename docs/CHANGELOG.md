# Changelog — Family Expenses

Semantic versioning. Unreleased work accumulates under [Unreleased] and is cut
to a release entry when a chunk set ships.

## [Unreleased]

Nothing pending.

## [0.13.1] — 2026-09-05

Found by the owner's live smoke session the same afternoon, through the
assistant: `expenses_refund_delete` undid the money and not the resize.
¥1,000 for ten, refunded ¥500 and resized to five (still ¥100 a class), then
undone: gross ¥1,000, refund gone — and the pack still five classes, now
**¥200 a class**, a figure stored nowhere and chosen by nobody. The docstring
had called that deliberate. It was a bug in the model: `resize_package_to`
exists so the refund and the resize are one decision, and an undo that
reverses half of one decision is not an undo.

### Fixed
- **Deleting a refund now reverses the whole decision.** A refund that
  resized the funded course records the course and the class count before
  and after (`package_id`, `class_count_before`, `class_count_after` on
  `expense_refunds` — on the refund event itself, so it shows in
  `expenses_history`, not in a side table). Deleting the refund puts the
  count back in the same transaction through the ordinary package update,
  which writes its own `package_update` row with the author. Guarded: if
  anything changed the count since (a `classes_update`, a later refund) it
  is left alone; reverting an *upward* resize is a shrink and runs under the
  shrink rule, and if logged classes block it the count stays. Every outcome
  is said in the tool's note, her undo toast (课时恢复为 / 课时保持), and the
  `refund_delete` snapshot. Two regression tests mirror the reproduction:
  refund with resize, delete, assert ten classes at ¥100 again; and the
  same with a `classes_update` in between, asserting the guard leaves it.
- **The second in-place migration.** `expense_refunds` shipped this morning
  without those columns and `CREATE TABLE IF NOT EXISTS` cannot add them, so
  `Database._migrate_refund_columns()` adds each missing one at startup —
  inspection-driven, idempotent, best-effort with a loud warning, same
  posture as the action-constraint migration — and `smoke_live.py` gates on
  it. Production holds one refund row today, unresized, which reads as
  before.
- **`smoke_live.py` cleans up even when it fails.** The first post-deploy
  run of this release failed at the MCP step and then failed again inside
  its own cleanup, before the revoke — leaving a never-expiring `smoke-test`
  link live and an unpaid `[smoke-mcp] 足球课` row in her 待付 tab (both
  found and removed by hand the same hour; the second run then failed only
  because that row made "exactly the rows I created" untrue). Each cleanup
  step is now guarded on its own and the revoke sits in a `finally` of its
  own; the MCP failure prints its cause rather than only its type; and the
  paid-by-query step targets the smoke's own row, since a bare 足球课 also
  matches the household's real football rows.

**Deployed 2026-09-05** as `family-expenses-00016-h86`. Both migrations
report nothing missing on Neon; the smoke passes; and the prompt's exact
reproduction — ¥1,000 for ten, two attended, ¥500 refunded and resized to
five, undone — read ten classes at ¥100 again through the deployed tools, on
a throwaway row removed afterwards.

## [0.13.0] — 2026-09-05

A day of live use found the class tracker could not express a refund. A
prepaid 1:1 badminton pack — ¥3,600, ten classes, five attended — was half
refunded, and the ¥1,800 bought a group pack instead. The group half was two
tool calls. The 1:1 half had no path at all: no way to change a course's class
count, no way to remove a course from the MCP (the refusal pointed at "the
Classes tab in the portal", which the agent cannot reach), no way to name the
portal, no refund. The course and the payment were deleted by hand and rebuilt:
a new id, a new `created_at`, five attendance events created a month after
their dates, and a ledger claiming he paid ¥1,800 on a day he paid ¥3,600.
`docs/LESSONS.md` §15 records the shape of it.

Cut as a **minor**: five new tools, a new table, the first in-place
constraint change, and a portal she will see change. Nothing in §5.1 moves —
same service, same `/t/<token>`, same open `/mcp`.

**Deployed 2026-09-05** as `family-expenses-00015-njh` (carrying v0.12.0,
which had never left this machine). The startup migration applied on Neon with
nothing rejected; `smoke_live.py` passed end to end, refund and undo included.
**Production repair, the same day, through the deployed tools:** the rebuilt
row `70b8cd36dba8` was raised back to its gross ¥3,600 and refund
`3e46a5a36c6a` of ¥1,800 recorded against it dated 2026-09-05, so the ledger
now says what happened — ¥3,600 paid on 2026-08-15, ¥1,800 back on
2026-09-05 — with the effective amount unchanged at ¥1,800 and the pack five
classes at ¥360. The original row's event ids, deleted by hand that morning,
are gone for good; its history survives under the old id.

### Added
- **Refunds, as their own fact.** `expense_refunds` holds amount, date,
  reason and author against a paid row; the row keeps its amount and dates.
  Every read now exposes the **effective** figure under the existing name
  `amount`, with `gross_amount` and `refunded` beside it — deliberately under
  the existing key, because nine portal sites and three store sites already
  sum `amount`, and a net figure under a new name would have been a place for
  each of them to forget (which is how borrow took three releases). The two
  writes that round-trip an amount — `expenses_update(amount=)` and the
  portal's edit box — use the gross figure and refuse to go below what came
  back. A refund cannot exceed what is left, cannot land on an unpaid row
  (that is a price change), and reduces its row in the row's own month: the
  refund date is recorded and shown, but there is no September credit,
  because every tab buckets by row and a dated credit would be a negative
  row. Net is computed in Python in cents, never in float4 SQL.
- **`expenses_refund(expense_id|query, amount, date?, reason?, changed_by?,
  resize_package_to?)`** — prefers the paid match; `resize_package_to`
  changes the funded course's class count **in the same transaction**, since
  a refund on a course nearly always means fewer classes and the two halves
  applied separately reprice a ¥360 class at ¥180. **`expenses_refund_delete
  (refund_id)`** is the undo; without one a wrong refund would be fixed by
  deleting the payment.
- **`classes_update(package_id|query, class_count?, name?, kind?,
  period_label?, archived?)`** — the core miss. Shrinking below the classes
  already logged (attended on a pack, missed on a period fee) is refused and
  the error names the conflicting classes with their event ids; exactly the
  logged count passes, which is the refund case. `archived` was threaded
  through everything since v0.10.0 and settable by nothing; it is now.
- **`classes_delete(package_id|query)`** and **`classes_log_delete(event_id)`**,
  both destructive-flagged with "confirm first" in the description. The
  delete-refusal on a funding payment now names
  `classes_delete(package_id=…)` instead of the portal. No cascade on
  `expenses_delete`: a cascade on a fuzzy `query` target is the largest blast
  radius available here, and the two-step costs one round trip.
- **`classes_log(dates=[…])`** — restoring a term was five round trips. All
  or nothing; a comma-separated string is tolerated.
- **The class tracker writes history.** `package_create`, `package_update`,
  `package_delete`, `class_log` and `class_unlog` rows land in
  `expense_history` under the payment that funds the course (a package is 1:1
  with its expense and never re-pointed), with the author. A
  `package_delete` snapshot carries every logged class, so removing a course
  destroys nothing. Closes `docs/BACKLOG.md` §3.
- **The portal host reaches the agent.** `expenses_mint_link` returns `.url`
  — the full `https://<host>/t/<token>` from `PORTAL_BASE_URL`, the same value
  the Auth0 redirect already used (now read in one place, `app/config.py`);
  `expenses_list_links` and `expenses_help` name the host and never a token.
  When the variable is unset the tools say so rather than printing a
  placeholder that looks like a URL.
- **`classes_list(query?, verbose=False)`** — the default drops each
  package's event array (it was the heaviest call on the server) for a count
  and the last class; `verbose=true` returns every event with its id.
  **`expenses_list(query=)`** also matches the name and period label of the
  course a payment funds, so "羽毛球" finds a payment described "Badminton".
  The write-side resolver is unchanged (`docs/BACKLOG.md` §5 explains why).
- **Her side of all of it.** She is the one the coach hands a refund to, and
  without a portal counterpart the thing she would do is edit the amount
  down. A paid row now offers **退款**: amount, date, reason and — when the
  payment funds a per-class course — 课时改为, prefilled with the current
  count and sent only when she changes it. The row shows the effective amount
  with 原价 and 退款 beneath it, lists each refund with an × that asks before
  undoing, and the edit box prefills the **original** amount under a label
  that says so. Each course gains 编辑 (name, count, period label — never the
  money), 结课 (asks first) and 恢复; finished courses move to a collapsed
  已结课 group at the foot of the tab rather than vanishing, because the tab
  had hidden archived rows since v0.10.0 and nothing had ever been able to set
  the flag. The history box speaks each action's language: a refund is its
  amount and date, a course its name and count, a class its date.
  `/api/refund` and `/api/refund-delete` are new; `/api/list` rows carry
  `package` and `refunds`.

### Changed
- **`expense_history.action` is wider**, and that is the first change this
  project has made to a live table that `CREATE TABLE IF NOT EXISTS` cannot
  express. `Database._migrate_history_actions()` runs at startup, driven by
  inspecting the live constraint rather than a version number, so it is
  idempotent and needs no migrations table: Postgres drops and re-adds the
  check in one transaction; sqlite rebuilds the table in place inside one
  BEGIN/COMMIT. Best-effort like the hardening file — a live portal must
  boot — but not silent: if it did not apply, the store refuses **every
  write that needs a new action — refunds, course edits, and her class
  logging, which wrote no history before this release and worked** — with a
  message that opens in her language ("记录暂时保存不了…请告诉 Matt") and
  carries the operator detail after it, and writes nothing. `smoke_live.py`
  now gates on `history_actions_missing()` so a failed migration fails the
  post-deploy smoke by name. Rehearsed against an old-shaped sqlite database
  in the suite and against a local Postgres by hand: the auto-named
  constraint, the drop-and-add, idempotency, every new SQL path, and the
  seq-collision refusal with the connection recovering afterwards.
- **Refund amounts are rounded to cents at the write.** `_validate_amount`
  never rounds (a deliberate compatibility choice for `expenses.amount`),
  and every read rounds a refund for display while the effective amount sums
  the stored values — so two ¥100.005 refunds listed as ¥100.00 each under a
  ¥200.01 refunded figure. `expense_refunds` has no legacy rows, so rounding
  here changes nothing that exists. Found by the structural review.
- **A refunded row cannot go back to unpaid.** `mark_paid(paid=False)` had no
  refund guard, so 取消已付 on a ¥3,600 row refunded ¥1,800 put **¥1,800 on
  the 待付 card** for a bill nothing had moved on. The store now refuses it
  (the mirror of refund()'s own rule: money cannot have come back on an
  unpaid bill) and names `expenses_refund_delete`; the portal omits the
  button on such rows, the listed refunds' × being the way back. Found by
  the semantic review.
- **Her refund box can no longer reprice a pack silently.** Its zero-effort
  path — amount, save — left the count alone, which is a real choice but was
  a silent one: ¥1,800 over ten classes reads ¥180 a class, the exact figure
  this release exists to prevent, and nothing on her surface said so. The
  box now shows, live as she types, what the course will say after the
  refund (每节 / 剩), and the confirmation quotes the **server's** rate and
  classes left whether or not she resized. `/api/refund` and
  `Store.refund()` return the funded course either way, with `resized`.
- **A refund on a term fee is explained, not just recorded.** On a `period`
  package a refund usually settles classes the school owed back, and no
  value of `resize_package_to` can express that: `owed_amount` is derived
  from the missed events, so the natural resize left it unchanged and a
  smaller one *raised* it. The `expenses_refund` note and description, and
  a hint in her refund box, now say to remove the settled missed classes
  (`classes_log_delete`, named with their ids) and resize the term to what
  remains; a store test pins the consistent end state (¥2,000 for 8,
  3 cancelled, ¥750 back → 5 classes at ¥250, owed ¥0). Found by the
  semantic review.
- **A borrow row cannot be refunded.** The store accepted one while the
  portal hid the button, and a comment claimed a server refusal that did not
  exist (LESSONS §9). Now refused with coaching; the real gap — a partial
  repayment of money she fronted — is filed as `docs/BACKLOG.md` §11.
- **`classes_log(dates=[])` no longer logs today**, and a batch that repeats
  a day is refused: an empty list is a caller that named no class, and a
  money-moving write from it is nobody's intent. The comma-separated string
  form now actually reaches the store — the tool parameter was typed as a
  list alone, so pydantic refused the string before the coaching could.
- **Concurrent writes that read `SUM(refunds)` are serialised on Postgres**
  with a row lock on the payment, taken before the read in `refund()` (the
  "more than is left" check), in `update(amount=)` (the floor at what came
  back) and in `mark_paid(paid=False)` (the refunded-row refusal); nothing in
  the schema bounds the sum of refunds by the amount, and two in the same
  instant could have driven the effective figure negative or left an unpaid
  row with a refund on it. sqlite is serialised already, so the suite cannot
  see this; it was probed by hand on a local Postgres with two threads.
- **The tool-count parity guard was blind to three of the five docs it
  named.** Its regex matched "18 tools" and not "Tools (18)" or "inventory
  (18)", so the runbook, contract and MCP design passed vacuously — through
  the release that changed all three (the third review mutated them to 7 and
  the test stayed green; LESSONS §13). It now matches every form and refuses
  a doc that yields no claim. `StringTableParityTests` likewise evaluates the
  whole portal string table under node — the top-level guard stopped at
  `cat:{`, below which the seven new history-action labels sat unguarded.
- **Two notes printed the net figure under the word "paid"** — `classes_add`
  ("¥1800.00 paid" on a ¥3,600 payment) and `classes_delete`. Both quote the
  gross figure now, with the refund beside it.
- **`smoke_live.py` left a ¥1,000 unpaid row and a live course in her tabs on
  every run** since v0.11.0: the course payment's id was never added to the
  cleanup list, and the delete would have been refused anyway. It now removes
  the course first with `classes_delete`, and exercises a refund and its undo
  against production.
- **Found by the cross-model review** (P8: a distinct gate, after three
  same-model rounds had read the same handler): **her refund save had no
  in-flight lock.** The store accepts a second refund equal to what is left,
  so two fast taps on 记录退款 wrote two refund rows — ¥100 meant, ¥200 back.
  It now has the class log's lock: one request per row, released on the
  answer, never by a timer; a lost response keeps the row locked until
  reload (LESSONS §6) — and it releases only after the re-render, since
  until `/api/list` answers the old box is still on screen with a cleared
  amount field (pinned on real promises by `RefundReleaseOrderTests`).
  **`/api/classes-log` dropped a `dates` list** and logged one class for
  today; it forwards it now, all or nothing, and a batch is capped at the
  same 1,000 as a class count (50,000 dates wrote 50,000 rows in under a
  second). **The raw API took `archived: "false"` as true** (`bool("false")`);
  only a boolean or its words are accepted.
- A history-seq collision (two writes to one payment in the same instant,
  now reachable from her phone through the class tracker) reads as a 400
  "reload and try again" rather than a driver traceback, and rolls the whole
  write back.
- `Store.delete_class_event` returns the course's payload rather than a bool;
  `/api/classes-unlog` returns the package. `create_package`,
  `update_package`, `delete_package` and `delete_class_event` take
  `changed_by`; the API stamps the link's label as before.
- `find()`'s SELECT qualifies every column: with the optional package join,
  an unqualified `id` is an ambiguity error on both drivers.

### Fixed
- `test_overdue_status_filter` read the real clock; its 2026-08-20 fixture
  rows went overdue on their own and the suite had been red since 2026-08-21.
  It now passes a fixed today.
- **Found in the browser, not by the suite:** every tap on the Classes tab
  rebuilds it, and a rebuilt `<details>` is closed — so opening a finished
  course collapsed the 已结课 group it sits in, under her finger. The open
  state is now read from the element on screen before the re-render
  (LESSONS §5: the thing she can see is the truth), pinned by
  `test_the_finished_group_stays_open_across_the_re_render_a_tap_causes`.
  The node harness could not see this because a rendering test paints once;
  the real page paints on every tap.

### Tests
- 377 → 491. `tests/test_refunds.py` (store: the day replayed, the
  effective amount reaching every total and the course rate, a sweep over
  non-dividing amounts, refund+resize atomicity — a refused resize takes the
  refund down with it — the audit rows, the shrink rule naming the events,
  batch logging all-or-nothing, the migration on an old-shaped database, the
  unmigrated fail-closed path, the seq collision); `RefundAndCourseToolTests`
  (the acceptance test verbatim through the tools, the delete-refusal naming
  the tool and the id, the shrink rule at the tool boundary, descriptions,
  annotations, help, personas, the host in every channel and its absence);
  `RefundApiTests`; and under node, executing the real functions:
  `RefundRowRenderingTests` (net/原价/退款, the button only where money
  could come back, the edit prefill from gross, the refund box's resize only
  for a per-class course, `refundBody` sending the count only when changed,
  history lines per action, escaping of every new interpolation),
  `RefundHandlerTests`, `ClassArchivePartitionTests` (membership, not
  presence) and `ClassEditHandlerTests` (including the guard that an unknown
  button never falls through into a class log).

### Docs
- `docs/LESSONS.md` §15; `docs/BACKLOG.md` §3 closed, §2 and §5 annotated;
  contract §4/§6/§7/§8; `docs/MCP_DESIGN.md` inventory (18);
  `docs/RUNBOOK.md` §3, §4 and §6 (the migration and how to check it).

## [0.12.0] — 2026-08-11

A production report: she added a ¥1,980 football course for 10月 and it was
"absent from the app". **Nothing was lost** — the row was in Postgres, correct,
from the moment she saved it. It was invisible on the tab she adds from.

Cut as a **minor** for the same reason 0.11.0 was: it changes what she sees.

### Fixed
- **The Due tab hid every unpaid row due more than 30 days out.** `renderNow`
  has filtered `daysBetween(today, e.date) <= 30` since the v0.6.0 redesign and
  no test has ever executed it. It went unnoticed while rows were near-term. On
  2026-08-11 she entered five months of course fees in four minutes; the first
  one due in 50 days landed in no list and no card, and the History tab kept it
  inside a collapsed `已排期` month. Running the shipped `renderNow` against the
  live rows returns `没有待付的` — an empty Due tab, which is what she saw.

  The tab now partitions **one** unpaid list into `待付 · 未来30天` and
  `待付 · 30天以后`, rather than filtering with one predicate and dropping the
  remainder. Both sections render expanded. The 30-day window on the summary
  **cards** is unchanged — that horizon is deliberate (`Store.summarize`), and
  the bug was never the horizon, it was a list with no other half.

  Cost: she added the same course twice. `3dc9ed78d440` (`football （10月）`,
  the later of the two) was deleted by the owner on 2026-08-11 once both became
  visible; `dfa796a090b0` (`10月足球课`, ¥1,980, due 2026-09-30) is the one that
  stands. Unpaid went ¥261,860 → ¥259,880.
- **The add confirmation now names what the server stored** — description, due
  date and amount, read from the response rather than from the form. It was a
  1.7s flash of the constant "已添加" over a list that did not move, which is
  indistinguishable from nothing happening. Echoing the *form* would have
  confirmed a write that may not have happened that way (a cleared date becomes
  the household's today), so the row comes from the response and the toast is
  given 3.2s and a width bound to stay readable on a phone.
- **"本月已付" was two different numbers on the same tab.** The summary card
  read **¥24,399** and the section header directly below it read **¥55,499** —
  the same two words over a ¥31,100 gap. The card excluded borrow (P4: money
  she fronted is not household spending); `section()` totals whatever rows it
  is handed, and the paid list had no borrow filter. Found by rendering the
  shipping `renderCards`/`renderNow` under node against the live ledger, not by
  the suite.

  The paid list now excludes borrow, so the two agree at ¥24,399 — **and the
  repayment goes somewhere**, into a new `本月已还我` section, rather than
  disappearing. Dropping it without that would have recreated the bug above:
  `待还我` carries only what is still *owed*, so a repaid row would have left
  the tab entirely. `Store.summarize` is untouched; both figures were
  portal-side sums.
- **A date no calendar has is now refused.** `_validate_date` checked only the
  *shape* (`^\d{4}-\d{2}-\d{2}$`), so `2026-13-01` and `2026-02-30` were
  accepted and stored through the MCP. The two then fail **differently**, which
  is why both are refused at the write rather than handled downstream:
  `2026-13-01` makes `Date.parse` return `NaN`, and `NaN` is neither `<= 30`
  nor `> 30`, so the Due tab rendered it in **no section at all** — this
  release's own defect; `2026-02-30` normalises silently to early March and
  renders, sorts and totals as a row due on a day she never entered, which no
  renderer can detect. Leap years still work (2028-02-29 in, 2027-02-29 out),
  and the same validator covers `paid_date`, `since`/`until` and class events.
- **The Due partition is a complement, not a second predicate.** `later` was
  `> 30`, which only *looks* like the negation of `<= 30`; the two are not
  complementary whenever the comparison is not a number. It is now
  `!inWindow(e)`, which cannot have that hole whatever `daysBetween` returns —
  so the renderer no longer depends on the store's validation being perfect,
  and a bad row already in the database still appears.
- **`Store.summarize()["total"]` counted borrow** — the only one of these that
  is not a portal figure, and the one an agent reads. P4 says borrow is
  excluded from *every* household total; `total` appended the amount before the
  borrow guard, so live `.ledger_total.total` read **¥347,559** where household
  spending was **¥316,459**. `expenses_list`'s description points "这个月花了多少"
  at `.summary`, so the wrong number was one question away. `total` now excludes
  borrow and `total == paid + unpaid` holds by construction; `count` still
  counts every row, because a count is not a total. The test named
  *keeps borrow out of every expense figure* asserted five figures and not that
  one — it does now, plus a sweep of the invariant over non-dividing amounts.
- **A repaid loan was labelled 已付** — the same word as an ordinary settled
  bill, on a row excluded from every 已付 total on the page. `stateOf` checked
  `paid` before `borrow`, so the fix above made it worse: the row said 已付
  while the column it sat under no longer contained it. It now reads
  **已还我 / repaid to me** and keeps the lend styling. This also makes the
  contract's claim about how those rows are marked true — it was written from
  the unpaid case and was false for the live repaid one.
- **The summary card and the section header could differ by a yuan** on the
  same rows. Both reduced with plain `+` in different orders — the card in API
  order, the section sorted by `paid_date` — and binary floating point is
  order-dependent. One `sumAmounts()` now sums smallest-first for both, so the
  result depends on the set alone. Same reason `Store.summarize` uses `fsum`.
- **History counted a repayment as household spending** — the third tab this
  had to be said on, and the one this release had not audited. Its 已付/未付
  columns summed every row with no borrow guard, so **2026-07 read ¥61,300 已付
  where the card, the Due section and the Stats KPI all said ¥30,200 for the
  same rows**, and 未付 carried money owed *to* her under a header meaning money
  she owes. Both columns now exclude borrow. The rows are still listed inside
  the expanded month, marked 待还我 — excluded from the total, not from the
  statement, which is the distinction §9 of the backlog was closed on.

  The comment above `renderStats` had asserted for four releases that the two
  tabs agree ("bucketed by DUE month so this agrees with the statement tab").
  It was false by the whole repayment. `test_history_and_stats_agree_month_by_month`
  now holds them together instead of the sentence claiming it (LESSONS §9).
- **A scheduled month in History announced itself wrongly.** The future branch
  of `renderHistory` was written separately from the past branch and drifted: no
  `aria-expanded`, no `open` class, so a screen reader heard a plain row and the
  caret never rotated. Its item sort was also `a.date > b.date ? -1 : 1`, which
  claims `a > b` **and** `b > a` for two rows on the same day — an inconsistent
  comparator, and her two 2026-09-30 rows are exactly that case. Both branches
  now share `byDateAsc`/`byDateDesc`, which return 0 for equal dates.

### Tests
- `DueTabVisibilityTests` — the guard that was missing. Sweeps one unpaid row
  per day across today−60 … today+400 and asserts **every** one reaches the
  markup. On the old code 369 of 460 rendered nowhere. A test that merely
  asserted "the Later section exists" would pass on a section with the wrong
  predicate or one nothing puts rows into (LESSONS §8), which is why it sweeps.
  It runs the shipping `esc`/`money`/`daysBetween`/`isBorrow`/`stateOf`, not
  stubs of them.
- `ExpenseAddFormTests` gains three: the confirmation names the stored row; it
  does **not** echo the form (typed date and amount differ from the response —
  the case that discriminates); and a response carrying no row degrades to the
  old one-word toast instead of printing "undefined".
- Three more pin the paid figures: the card and the section agree; they agree
  on the figure that **excludes** what she fronted (both reading ¥55,499 would
  satisfy equality alone and still be wrong); and a repayment is still rendered
  somewhere.
- **The cross-model round rewrote the first draft of these tests.** `/codex-verify`
  returned DO NOT SHIP on three counts, two of which the suite could not see:

  - the sweep asserted each row appeared **at least once**, so an overlap was
    invisible — a `>= 30` boundary slip puts day 30 in both sections and every
    assertion still passed. It now splits the render into sections and asserts
    each row appears **exactly once, in the expected one**;
  - the borrow test asserted the row appeared with `st lend` somewhere. Removing
    `!isBorrow` from the unpaid list renders it in the due list *and* 待还我, and
    `stateOf` gives every copy `st lend` — so it passed. Membership is now the
    assertion;
  - all three confirmation tests used one fixture, which an `addedMsg` that
    hard-codes that exact string would satisfy (LESSONS §3). Two distinct server
    responses now go through the same form input.

  It also found the `NaN` partition hole and the impossible-date write above, and
  independently reported the 本月已付 disagreement that had been fixed an hour
  earlier — arriving at the same defect from the other direction.
- Ten mutation-checked in total, each target verified green and resolvable first
  (P5). Four of them are the exact mutations Codex named as surviving the first
  draft; two are "wrong fixes" that satisfy a weaker assertion — making the card
  count borrow so both figures agree at ¥55,499, and hard-coding the
  confirmation string.
- `scripts/smoke_live.py` pins the 30-day boundary against real Postgres: a row
  200 days out is unpaid but not upcoming. Recorded there — and here — that this
  assertion would **not** have caught this bug: `/api/list` was correct
  throughout and the loss was in rendering.

## [0.11.0] — 2026-08-11

Cut as a **minor** version, not the patch it was drafted as: it changes
behaviour she can see (the payment filter, the hidden period field, the date
picker), adds `midnight_in` to two API responses, and changes when
`classes_log` writes versus asks. P7 calls that a minor bump.

Six things the owner hit within a day of the class tracker going live, plus
what two review rounds found in the fixes for them. All of it is the Classes
tab; none of it moves a total or changes the MCP tool set, and §5.1 is not
engaged.

### Changed
- **The payment dropdown offers course payments only** — `aden-edu` and
  `aden-sports` (`store.CLASS_CATEGORIES`). It listed every expense, newest due
  date first, so twelve monthly living-expense rows dated out to 2027-07-31 sat
  on top of the four payments that were actually courses. **No date bound was
  added on top of that**: a course payment falls due in the future all the time,
  and that is the one most likely to be tracked next. Matching is case- and
  space-insensitive because `category` is free text and the MCP drifts from the
  canonical keys — the live ledger holds rows written as `living expenses`. This
  is a view concern, not a money rule: the store links a package to any expense
  and MCP `classes_add` still can, which is the escape hatch for a course paid
  under another key.
- **The empty dropdown says what it is filtering.** "先在「待付」里记下这笔付款"
  was going to become a lie the moment every course payment was tracked — the
  ledger would be full of payments she had already recorded. It now reads
  "没有可选的付款（只显示 Aden 教育 / Aden 运动）", which is true whether the
  ledger is empty, whether nothing is in the two categories, or whether every
  course is already linked, and it names the filter so the dead end explains
  itself.
- **A per-class pack is no longer asked for a 周期.** A pack of N classes is
  counted in classes, not in months. The box is hidden **and cleared** for that
  kind: a hidden field that still submits what she typed is the exact shape of
  bug this file keeps finding. It costs the row its only disambiguator, which
  is why the title now falls back to the funding payment's description — see
  below.
- **Logging a class uses a date picker.** It opened `prompt()` and asked a phone
  user to type `2026-08-05` by hand — under a label that said 到期日, the
  *expense* due date, which is not what a class date is. Now an
  `<input type="date">` in the row, labelled 上课日期 / Class date, starting at
  the household's today, for all three buttons (上了 · 停课 · 没去). A date she
  picks survives a re-render (tapping another row causes one) but not the log it
  was for — see the review fixes below for why that distinction is the whole
  safety property.

### Fixed after review
Two independent reviewers went over the first cut of this release. They found
the same defect first, and both reproduced it by executing the portal's own
JavaScript rather than reading it.

- **The remembered date never expired.** `clsDates` was written on every pick
  and cleared by nothing — so one backfill dated every later class in that
  session. Log a missed class on July 2, tap ✓上了 that evening for a class that
  happened today, and it filed under July 2 as well. Silent: the toast named no
  date, logging has no confirm step, `class_events` has no unique index on
  `(package_id, date, kind)`, and the log sorts date-DESC so the wrong row sinks
  *below* what she just looked at. **No total moved** —`summarize_package`
  tallies by `kind` and never reads a date — but the class log is the thing the
  owner argues with the school about. The memory now dies with the log it was
  for, and the success toast names the date.
- **The picker's value was decided at render time, not at tap time.** A row
  stays open across re-renders, so one left open across midnight posted
  *yesterday* — the same stale-date bug v0.9.0 had to fix once already, moved
  from the add form into the class log.

  The first fix for this inferred "she picked a date" by comparing the input to
  a `data-seeded` attribute, and a second review round showed that comparison is
  ambiguous by construction: *untouched* and *deliberately picked the date on
  screen* produce identical DOM, so after midnight her explicit choice of
  yesterday was silently discarded — and re-picking could not help, because
  nothing re-renders on a pick. It also read a value that `delete clsDates[id]`
  does not clear, so a second tap before the refresh landed reused it. **`clsDates`
  is now the single source of truth**: the change listener is the only writer,
  the log handler is the only reader, and unset means recompute today. The
  `data-seeded` attribute is gone.
- **Two per-class packs rendered byte for byte identical.** `period_label` was
  the row title's only disambiguator, and this release guarantees it is NULL for
  everything the portal creates. A class logged against the wrong 足球课 moves
  both rows' figures while both still look right. The title now falls back to
  the funding payment's **description** — not its date, which two terms bought
  in one sitting share — and the changelog's "put the month in the name" advice
  is no longer load-bearing. The same fallback fixes `classes_list`'s note.
- **`classes_log(query=…)` could log against the wrong course, silently.**
  Matching the funding payment's description — added earlier in this release so
  `'8月'` would resolve at all — is weak evidence: the payment for 游泳课 may
  well read "足球课 8月 (转游泳)". A reviewer demonstrated `query='足球课'`
  drawing a class off swimming, with no question asked and no `matched` key.
  Course names and period labels are now the **primary** match; the payment
  description is consulted only when those miss, and a payment-only match never
  resolves on its own — it comes back as a question whose hint says the match is
  the weaker kind.
- The MCP disambiguation `payment` string collapsed to identical text when two
  terms were bought in one sitting (same date, description and amount — the
  normal MCP entry path, since `expenses_add` defaults the date to today).
  Candidates carry `classes_logged` and `started`; `created_at` is only
  second-granular, so those can match too, and the hint now says plainly that
  such rows are separable only by `package_id` rather than telling the agent to
  ask the user to choose between two identical lines.
- The category filter only explained itself when it hid *everything*. The form
  label names it always (「哪一笔付款（只显示 Aden 教育 / Aden 运动）」), and
  `classes_add`'s tool description says the portal can only *start* a course
  from those two categories. A first draft of that description said such a
  course was "invisible" in her tab and told the agent to `expenses_update` the
  category — both false and actively harmful: only `candidates` is filtered
  (`app/api.py`), `packages` is not, so the course renders and logs normally,
  and moving a real row between categories would shift it between the Stats
  KPIs for no reason.
- Caught while fixing the above, not by a reviewer: wrapping the date input in
  its `<label>` would have rendered it at `.75rem` bold and letter-spaced, since
  `input { font:inherit }`. Paired by `for`/`id` instead.
- **A consequence of the period-label change, on the MCP side: two same-named
  packs became unresolvable.** `classes_log(query=…)` disambiguates on `name` +
  `period_label`, and a per-class pack created from the portal now has no period
  label — so two terms of 足球课 returned two candidates that were identical in
  every field shown, and the disambiguation question the agent is instructed to
  ask had no answer. Candidates now carry the **payment that funds each one**
  (date · description · amount), and `query` matches the payment description as
  well, so `query='8月'` reaches the right course again — `8月` lives in
  "Football (8月, 10课)" now that it has nowhere else to live. It comes back as
  a **question**, never a write: see the wrong-course fix above. The owner logs
  classes through MCP; this would have been his path, not hers.
- **Deleting an attendance record asked nothing.** A 12px `×` beside the class
  log, one mis-tap from erasing attendance — and on a period package, from
  handing the school back a class it owed. It now confirms, and the question
  names the record it would remove (`2026-08-05 · 上了`) rather than asking
  about nothing in particular.
- The row's tap handler toggles the row shut, and the new date picker lives
  inside the row: without a guard, tapping the date box collapsed the controls
  under her thumb before the picker could open.

- **A retired course could take a live one's class.** `classes_log(query=…)`
  searches archived packages too, so 足球课 from last term — archived — was a
  name match like any other and won outright over the running one. The write
  was then unverifiable: `classes_list` hides archived by default, so the note
  the agent reads back did not contain the class it had just logged. Live
  courses are preferred now, a retired one is still reachable when nothing live
  matches, and the candidates carry `archived` so the agent can say which is
  which. **Pre-existing** — v0.10.0 had it too, and the broken intermediate
  state of this release masked it by accident.
- **`classes_add`'s description told an agent to edit a real money row.** It
  claimed a course funded by another category was "invisible" in her tab and to
  `expenses_update` the category. Only `candidates` is filtered — `packages` is
  not — so such a course renders and logs normally, and moving the row would
  have shifted it between the Stats KPIs for nothing. A P3 channel is an
  instruction an agent will follow; this one was false in both halves.
- The post-deploy smoke gate exercised nothing this release changed: the class
  tools appeared in it only as names in the inventory assertion, so a deploy
  whose Classes tab was entirely broken still printed PASS. It now runs a full
  round trip — ¥1,000 over 3 classes, the split that does not divide evenly —
  and asserts `used + remaining == paid` against the live service.
- Three doc claims that had gone stale within this same entry are corrected
  (P7), and `docs/FEATURE_CONTRACT.md`'s API table now says `classes-list`
  filters only the candidate payments, not the packages.

### Fixed after cross-model review (`/codex-verify`, GPT-5.4)
Three same-model rounds had already run. A different model family found four
more, three of which the earlier rounds had looked straight at.

- **The date on screen and the date posted were two separate evaluations of
  `clsDates[id] || todayStr()`, taken moments apart** — so they could disagree,
  and did in three ways. The sharpest: re-picking the date the box already
  shows fires **no `change` event at all**, so her deliberate choice was never
  recorded and the tap posted today. That is round 2's defect returning through
  a different mechanism. An open row's date is now *pinned* into `clsDates` by
  the renderer, making the box and the tap one variable rather than two.
- **`todayStr()` rolled the date over 24h after the response, not at midnight.**
  A page opened at 23:50 kept offering yesterday until 23:50 the *next* day —
  the root cause under every midnight bug this release and v0.9.0 chased. The
  page cannot work it out from a date, so `/api/list` and `/api/classes-list`
  now send **`midnight_in`**, the seconds left of the household's day.
- **In-flight races could duplicate or misdate a class.** The double-tap guard
  was `b.disabled`, a property of one button node: any re-render replaced it
  with an enabled one, and it never covered the sibling buttons at all, so 停课
  during an in-flight ✓上了 wrote two events — and on a period package the
  second claims another class back from the school. **This is the one finding
  in the whole release that can move a money figure**, via extra `class_events`
  reaching `summarize_package`. The lock is now per course and lives outside
  the DOM. The post-log reset also no longer discards a date she picked while
  the request was still in flight, and finds the live input by id rather than
  through a row a re-render has since detached.
- **Two courses (or two dropdown options) could still read identically** when
  the payments behind them matched as well — same date, description and amount,
  which is exactly what two terms bought in one sitting look like. The package
  id is appended, but only where the visible text actually collides.

### Fixed after the cross-model round was itself reviewed
The fix wave above was sent back to Codex rather than trusted — this release's
first three fix waves were each defective, so "fixed" is not a verified state.
It found three more blockers, two of them created by those fixes.

- **The pin froze what it was meant to synchronise.** Storing today into
  `clsDates` when an open row rendered did make the box and the tap agree — by
  making both permanently wrong: a row opened at 23:50 showed and logged
  yesterday for the rest of the page's life, immune even to a refresh. And the
  regression test written for it constructed a state the renderer could no
  longer produce, so it could not have caught this. **The box itself is now the
  source of truth** — the tap posts exactly the date she can see, `clsDates`
  only carries a pick across a re-render, and `renderClasses()` is a pure
  repaint again. This also removes the original hole for free: no `change`
  event is needed to notice a date, because nothing is inferred from events.
- **`today` and `midnight_in` came from two separate clock readings**, so a
  request straddling midnight returned yesterday's date beside a nearly full
  day remaining — telling the page to hold yesterday for another day, which is
  the exact defect the countdown was added to fix. One reading now
  (`store.today_and_midnight`). The "clock read exactly once" test had been
  patching `today_str` rather than the clock, so it reported one read while the
  wall clock was read twice.
- **The uniqueness handle was not unique**: four characters of a twelve-character
  id, so two ids sharing a prefix collided again. Full id now.
- `clsBusy` could hold a course for the life of the page if a request never
  settled (neither `.then` nor `.catch` runs) — released after 30s as a
  backstop. The post-log reset was also value-guarded, so changing away and back
  to the same date during an in-flight log erased that choice; it is guarded on
  a pick counter now.
- `seconds_until_midnight()` did wall-clock arithmetic, an hour out either side
  of a DST change for any `APP_TZ` other than Shanghai. Both sides are converted
  to UTC first.

### Fixed after the third cross-model round
Two of these were created by the previous fix wave. The pattern held all the way
down: five of six fix waves in this release introduced a defect of their own.

- **The 30-second lock release was a worse bug than the deadlock it fixed.** It
  is a blind retry: the request it gives up on may already have committed, and
  `class_events` has no uniqueness constraint, so the retry writes a second row
  and `summarize_package` counts both. **Removed** — a course whose request
  never settles now stays locked until reload, which is visible and
  recoverable. Filed as `BACKLOG.md` §6, with a standing instruction not to add
  a timed unlock or automatic retry until the write is idempotent.
- **Nothing repainted at midnight.** Removing the pin meant a re-render
  corrects an untouched row — but a tab left visible across midnight never
  re-renders. She taps ✓上了 at 00:05 on a box still reading yesterday, and the
  box being the source of truth means yesterday is exactly what gets written.
  A repaint is now scheduled for the household's midnight, which also moves the
  Due tab's date default if she has not touched it.
- **`/api/list` still read the clock twice for `status="overdue"`**: the
  overdue filter inside `Store.list` called `today_str()` on its own, so a
  request straddling midnight could drop a newly-overdue row from the rows AND
  their totals while labelling the response the other day. The one reading is
  threaded down now. The existing "read exactly once" test used `status="all"`,
  which never enters that branch, so it could not prove its own claim.
- The in-flight guard was a counter of `change` events, which cannot see a
  re-pick of the value already displayed; it asks the box now — the same
  question the tap asks.
- The DST tests were taken at noon, *after* both transitions, so they passed
  with the wall-clock arithmetic too. The pre-transition windows are the ones
  that expose it, and both directions are covered now.

### Fixed after the fourth cross-model round — and one thing reverted
The reviewer was also asked, plainly, whether the machinery added in response to
earlier rounds was now more risk than the bugs it removed. Its answer was taken
as given rather than argued with.

- **The lock still released on a network failure**, which is the same unsafe
  retry as the timer: an unanswered request may have committed and lost its
  reply. `api()` now marks an error the server *answered*, and the class log
  releases only for those — a refusal (400) frees the course, an ambiguous
  failure holds it. Held is visible and costs a reload; a duplicate class event
  is neither visible nor recoverable.
- **The midnight repaint timer is REVERTED.** It re-armed against its own
  expired deadline and became a one-second render loop, refused to arm when a
  DST fall-back pushed the countdown past 24h, and misbehaved on a backward
  clock change. Its target bug is narrow and visible, `visibilitychange` covers
  a phone, and the timer was more dangerous than the behaviour it corrected.
  Accepted and filed under `BACKLOG.md` §6 with the shape a real fix would take.
- **`expenses_list(status='overdue')` still read the clock twice** — only
  `/api/list` had been fixed, and the MCP path is the owner's. `Store.find`
  takes the shared date now, and the tool reads it before selecting rows.
- Instead of trying to detect a date change made *during* a write — which a
  same-value re-pick makes undetectable, by event or by value — **the picker is
  disabled while its write is in flight**, and a repaint keeps it disabled
  rather than drawing a live-looking box over a locked course.

### Fixed after the fifth cross-model round
The first round with **no must-fix**. Three should-fixes, one of them a real
lifecycle defect the round before had introduced.

- **The date picker was disabled when a write began and never re-enabled.** On
  success a repaint usually replaced it, but `refreshClasses()` repaints only if
  its own request lands; after an answered refusal no repaint is requested at
  all. So mistyping a date left her with a dead box until she forced a repaint
  or reloaded. Release now re-finds the picker and enables it for a success or
  an answered refusal, and keeps it disabled for an ambiguous failure — where a
  live-looking box would invite the retry that can double-log.
- No renderer test proved `clsBusy` actually emits `disabled`, and none proved
  the picker came back; the fix above was green without a single test noticing.
- `ApiErrorShapeTests` set both `r.ok=false` and `j.ok=false`, so it could not
  tell the two halves of that check apart — dropping `|| !j.ok` survived it. The
  API answers **200 with `{ok: false}`** for a validation refusal, which is now
  its own case.

### Tests
250 → 339, and two review rounds closed three long-standing coverage gaps:
**BACKLOG §4 is closed** — both form-submit handlers now run under node, the
class one (`addClsForm`) and the expense one (`addForm`, live since v0.1 and the
one that writes money directly). All four mutations §4 named as surviving are
caught, as are four on the expense side, including `parseFloat`→`parseInt` on
the amount and a cleared due date sent blank. The two STR language tables
have a top-level key-parity guard (deleting an English key left the whole suite
green, and the dialog then reads the literal key), and the whole-course delete
confirm is driven by a test rather than only its endpoint.

A hundred and four mutations were applied to the new code — the filter deleted,
the category match made exact, the period box pinned open and pinned shut, the
cleared field left populated, the picker ignored, a blank date posted as-is, the
picker tap left toggling, the confirm removed, Cancel ignored, the remembered
date forgotten, the confirm's subject dropped, a typo in `CLASS_CATEGORIES`, the
payment stripped from the MCP candidates and the payment description dropped
from the matcher, plus the eleven covering the review fixes above — and every
one is caught — including five re-run after an audit found a mutation whose
target test had been renamed: `unittest` exits non-zero for a name that does
not resolve, so those had scored as "caught" without running anything. One of
them genuinely survived. The `change` listener that records a picked date had no executing
test at all in the first cut; a key/value swap in it kept every token the wiring
guard greps for while making the memory silently never work. `ClassPeriodFieldTests` and the extended
`ClassTabInteractionTests` execute the portal's own JS under node rather than
grepping its source; the two grep-shaped guards that remain are there because a
function nothing calls is a no-op the node harness cannot see.

## [0.10.0] — 2026-08-11

### Added
- **Class tracker — a fourth portal tab, three MCP tools, two tables.** Prepaid
  courses in the two shapes this household actually buys:
  - **per_class** — a pack of N classes (足球课 · 8月 · 10课). Attending draws one
    down; the tab answers "how many classes and how much money are LEFT".
  - **period** — a flat month/semester fee. Nothing is drawn down; the classes
    that did NOT happen are owed back. Reported as one owed figure **split into
    reclaimable** (`missed_school` — they cancelled) and **forfeited**
    (`missed_us` — we skipped), because only the first is worth arguing about.
    Whether that becomes a rollover or a refund is a conversation, not a field.
- `classes_list`, `classes_add`, `classes_log` on the MCP (13 tools). `classes_add`
  targets the payment by `query` like every other tool, and refuses to invent
  one: the expense must be in the ledger first.
- `db/class_packages` + `db/class_events` in `db/schema.sql` — additive, so no
  dated migration files are started.

### Money semantics
- **A package holds no money of its own.** The per-class rate is derived at read
  time from `expenses.amount / class_count`. Correct the payment and the tracker
  corrects itself; there is no second place for the price to be wrong, and
  `update_package` refuses an `amount` field and says where it lives.
- **`expense_id` is UNIQUE.** Two packages funded by one payment would each
  claim the whole amount and silently double-count it.
- **Amounts are exact ratios, never a rounded rate × n.** ¥1,000 over 3 classes
  reports a ¥333.33 rate for display, but used + remaining still equals ¥1,000.
- **Consumption is not spending**: `Store.summarize` knows nothing about
  classes, so no expense total moves when a class is logged. Attending a class
  you already paid for is not a new expense.
- **A payment that funds a package cannot be deleted** until the package is.
  Cascading would silently destroy an attendance log that took a term to build;
  the error says what to do instead.

### Fixed after review
Four independent passes (two adversarial agents, a cross-model Codex review,
and mutation testing) went over the first draft. All three reviewers
independently found the same two defects, and the mutation run showed **ten
ways to corrupt this feature's money that the suite let through**. Both are
fixed, and every one of those mutations is now caught.

- **The tab did not work.** Class rows reuse the expense list's `.ex-hd`
  markup, and a document-level handler bound to it called `toggleItem(null)` →
  re-render, closing the row the class handler had just opened. Tapping a
  course never revealed its class log. Separately, `.btns { display:flex }`
  out-ranked the UA `[hidden]` rule, so every row showed its action buttons —
  including Delete — permanently. Neither was visible from the API tests,
  which is all the first draft had.
- **A period package could report owing back more than was ever paid.** A
  wrong `class_count` or a bad month made `owed_amount` unbounded: ¥2,000 paid,
  ¥2,750 "owed". Money is now capped at the payment and the excess surfaces as
  `overrun`, matching what the per-class branch already did.
- **The reclaimable/forfeited split did not reconcile with the total.** Three
  independent `round()` calls meant ¥1,000 over 3 classes reported owed ¥666.67
  against parts of ¥333.33 + ¥333.33. (This round's fix — deriving the total
  from its parts — turned out to breach the cap in the other direction; see the
  second review round below for the rule that actually holds.)
- **The class figures were rounded to whole yuan in the portal** while the
  server and MCP reported cents — ¥667 on the tab whose job is telling a school
  what it owes.
- **`scripts/smoke_live.py` asserted an exact 10-tool set** and would have
  failed the next deploy. This is the second release running in which the
  post-deploy gate was broken by a change that never touched it.
- **A missing package reported "no expense with id …"** and pointed at
  `expenses_list`, which cannot produce a package id (P3). `PackageNotFoundError`
  names `classes_list`.
- **A lost UNIQUE race surfaced as a 500.** The duplicate pre-check is not
  atomic with the insert and the handlers run in a threadpool, so the constraint
  can be what fires; it is now translated to the same coaching the pre-check gives.
- **Changing a package's `kind` silently reinterpreted its whole log** —
  attendances stopped drawing down, misses became money owed. Refused once
  anything is logged.
- **Foreign keys** on `class_packages.expense_id` and `class_events.package_id`.
  The application refuses to delete a funding payment, but that check and the
  delete are not one atomic step; without the constraint the loser of that race
  commits an orphan, and an orphan vanishes from every read.
- A fractional `class_count` (JSON `1.9`) silently truncated to `1`, and the
  count divides the money. Refused.
- The Classes tab now tags each fetch with a generation number (a slow earlier
  response could repaint stale totals) and disables a log button in flight (a
  double tap logged the class twice — on a period package, claiming another
  class back from the school).
- `/api/classes-list` read the whole package list twice to collect one column.

### Fixed after the second review round
The fix wave above was itself reviewed — and had introduced a defect of its own,
the third time in this release that a fix wave did. A parallel mutation run
applied 80 mutations and found **41 that the 210-test suite let through**, each
with a probe proving it produced a genuinely wrong number.

- **Deriving a total from two rounded parts let a period package report owing
  back ¥0.01 MORE than was paid**, in ~0.8% of ordinary splits — the exact cap
  the previous round's own contract text promised. The rule is now one-way and
  stated as such: **a part is derived from the total, never the total from its
  parts.** `owed_amount = value(school + ours)`, `forfeited_amount` is the
  remainder, `remaining_amount = total - used_amount`. This also restores the
  exact ratio (¥1,000 over 3 is ¥666.67 again, not ¥666.66).
- **`ClassMoneyInvariantTests`** replaces hand-picked examples with a sweep over
  18 hostile amounts × 9 counts × every event mix — ~3,000 combinations
  asserting that parts sum to their total, nothing exceeds the payment, and
  nothing is negative. Every money defect this feature has shipped survived a
  suite of tidy examples (¥2,200/10 and ¥2,000/8 both divide evenly, so rounding
  could never bite). This is the guard that actually holds.
- **The school's share is now asserted directionally.** Flipping the allocation
  to us-first keeps every total correct and reconciling while handing the school
  back money she was going to claim — no invariant can see that.
- **`class_count = NaN` returned a 500** rather than a 400: `int(nan)` raises a
  bare `ValueError` outside the validator's own try.
- **A foreign-key violation was reported as "already tracked"**, sending someone
  to look for a package that does not exist. The diagnosis now runs after the
  failed transaction has rolled back — a query inside an aborted Postgres
  transaction is itself an error — and names the constraint that actually fired.
- **A non-integrity failure was laundered into a reason.** A dropped connection
  came back as "that payment is already tracked", so the agent stated it
  confidently and the write was lost.
- **Every course row collapsed after each log or unlog** — there was no
  open-state, so the re-render closed the row the tap had just opened.
- **A capped period row now says it is capped.** The counts report what happened
  and the money stops at the payment, so "(5)" beside ¥0.00 read as a bug.
- **`ClassTabInteractionTests`** runs the Classes tab's real click handler under
  node. Inverting one boolean in it made the whole tab inert while 210 tests
  stayed green, because every guarantee there was a string match.
- The HTTP layer was trusted to pass its arguments through: `date`,
  `period_label`, `note` and `include_archived` were all pinned at the store and
  unpinned at the boundary, so dropping any of them changed nothing visible to
  the suite.
- Raw-SQL tests for the constraints the app makes unreachable — the `expenses`
  foreign key, `class_count > 0`, and both kind allow-lists.
- `PackageNotFoundError` landed in three call sites and only one was pinned.

### Fixed after the third review round
The rule from round two — derive a part from the total, never a total from its
parts — was written into a comment but not actually implemented at the boundary.
`value(count)` is `round(amount * count / count, 2)`, which is **not** reliably
`round(amount, 2)`: an amount sitting on a half-cent crosses the tie differently
on the multiply/divide round trip. So a ¥100.035 payment over 6 classes still
reported **¥100.04 owed against ¥100.03 paid**, and the comment asserted that
was impossible.

- `value(n)` now returns the total at `n >= count` — the cap holds by
  construction rather than by assertion. `max(0.0, …)` on `remaining_amount`
  turned out to be masking the same defect rather than guarding anything.
- **The sweep's amounts were all exact 2-decimal values** while its own comment
  claimed "half-cent boundaries". Six third-decimal amounts added; they fail
  immediately against the old arithmetic.
- **An unbounded `class_count` reached the driver as an `OverflowError`** and
  became a 500, the same shape as the NaN case. Capped at 1000.
- **Tapping a course row silently reset the add form's payment picker**, so the
  next course could be linked to whichever payment happened to be listed first.
- `ClassRowRenderingTests` runs the real `renderClasses` and inspects its HTML:
  forcing every row closed — no buttons, no class log, permanently — passed the
  entire suite, because only the click half was executed.
- Two guards were correct but unproven (P5): the `isfinite` check, and the
  `if not _is_integrity_error(exc): raise` that keeps a dropped connection from
  being reported as "already tracked". Both now fail when removed.

### Fixed after the fourth review round
The first round to find **no MUST FIX**: the arithmetic was brute-forced over
6.5 million combinations — every amount shape, counts 1–30, every event mix
including large overruns — with zero invariant violations. What it did find was
seven guards that were correct but unproven, one of them protecting a real
defect:

- **`list_packages` hands each course its own classes**, and nothing checked it.
  It loads every event in one query and groups them in Python; giving the whole
  set to each package makes a course she has never attended report classes
  consumed and money spent. Every prior test had one package or no events, so
  the grouping was invisible. The code was right; the guard was missing.
- The add form's **rate hint** had no test at all — mutating its division to a
  multiplication showed "¥22,000.00 (¥2200 ÷ 10)" on screen at the moment she
  decides whether a course is priced right.
- The **stale-response generation counter**, the **payment-picker preservation**
  and the **empty class-log placeholder** were all unreferenced by the suite.
  (The picker guard written here was itself inert — its fixture held one
  candidate, so "keep her pick" and "take the first one" were the same string,
  and the `<select>` stub allowed values that were not options. Round eight
  caught it; the stub now models a real select and the fixture has two.)
- `test_a_course_with_no_classes_logged_still_renders` was a grep for a literal
  that also appears in the History tab, so deleting the branch it named left it
  passing. It now renders.

### Fixed after the fifth review round
The eighth round found no wrong behavior in the class tracker. It found one
inert guard and, outside the feature, one way to lock her out of the portal.

- **An infinite expense amount bricked the portal.** `inf > 0` is true, so it
  passed validation and committed; `JSONResponse` serialises with
  `allow_nan=False`, so **every subsequent `/api/list` returned 500** and the
  row could only be removed with database access. Reachable from the MCP and
  from any raw JSON body (the portal's own form cannot produce it). Not a class
  tracker defect — `_validate_amount` has been this way since v0.4.0 — but
  v0.10.0 hardened `class_count` against exactly nan/inf/overflow while
  `amount`, the more damaging field, kept none of it.
- The payment-picker guard added in round four **could not fail**: its fixture
  held one candidate, so "keep her pick" and "take the first one" were the same
  string, and its `<select>` stub accepted values that were not options. The
  stub now models a real select — replacing `innerHTML` resets the selection,
  and an absent value is refused — and the fixture has two candidates.
- `docs/FEATURE_CONTRACT.md` stated the period derivation backwards
  (`owed = reclaimable + forfeited`). The identity holds, but it is a
  consequence, not the rule — and computing the total from two rounded parts is
  precisely how it came to exceed the payment. A future implementer reading the
  contract as spec would have reintroduced it.

### Closed out
The ninth round returned **clean — no must-fix, no should-fix** — after an
independent 6.26-million-case sweep of the money arithmetic found zero invariant
violations. Two things closed alongside it:

- **An absurd expense amount could still overflow the totals.** Two rows near
  the float ceiling make `fsum` return `inf`, and every total then serialises to
  a 500 — the same unrecoverable lockout as the infinite amount, one layer up.
  Capped at ¥1e12; no household expense is a trillion yuan.
- `docs/BACKLOG.md` §4 records the one structural gap that survived all nine
  rounds: **both** of the portal's form-submit handlers — the expense one live
  since v0.1, and the new class one — are executed by no test.

### Changed
- `classes_log` validates the event kind **before** resolving which course was
  meant — a bad kind is wrong whichever course it is, and reporting "no such
  course" first cost a round trip to find the real mistake.
- The portal's candidate `<option>` escapes per value rather than once around
  the concatenation, so the invariant `ClassKindParityTests` checks is literally
  what the code does.

## [0.9.1] — 2026-08-11

### Fixed
- **`scripts/smoke_live.py` could not verify a deployment any more.** Its portal
  check followed the 302 to `/login` into Auth0, whose login page answers 400 to
  a scripted request — so post-deploy verification aborted with a traceback and
  read as a dead service. It now checks the redirect without following it (302 →
  `/login` is the *success* signal when the portal is behind Auth0), asserts
  `/api/*` refuses an unauthenticated caller, and leaves the write path to the
  MCP flow it already had. Broken since v0.5.0 added the portal login; found by
  running it, which is the only way this was ever going to surface.
- **The smoke output printed a live portal token** in the redirect Location.
  Tokens are bearer credentials and this output gets pasted into transcripts —
  P9. All hex runs of 32+ chars are now redacted at the print boundary.
- The closing line told the operator to mint the household link next. That was
  true on deploy day and misleading ever since.

## [0.9.0] — 2026-08-11

Clears `docs/BACKLOG.md`: every deferred code defect is fixed, the two
operational unknowns are resolved, and the doc drift is gone.

Each **code defect** has a regression test that was observed failing against the
unfixed code first. Two items are not code defects and their tests do not fail
against the old code, by design: the `APP_TZ` item was filed as a missing test,
and the constraint-hardening item is new behavior. Said plainly because the first
draft of this entry claimed all eight were failing-first, which was not true.

Reviewed before release by four independent passes. Three (two adversarial agents
and a cross-model Codex review) found the same regression in the first draft — a
portal that cached the server's date forever. A fourth then reviewed the fix for
that and found it guarded a backward clock jump but not a forward one. Both are
fixed below; the honest reading is that the review caught what the tests did not,
twice, and both times on the same code path — the date that gets written into the
ledger. Two things still lack a behavioral test and are named where they occur:
the portal's history rendering, and the `init()` warning path.

### Fixed
- **A search for a literal `%` returned the entire ledger.** `store.find` built
  its LIKE pattern without escaping `%`/`_`/`\`, so those characters in a
  natural-language query acted as wildcards — an agent told to find one expense
  could be handed all of them and act on the wrong row.
- **`expenses_add(paid=true)` spanned two transactions** — create, then
  mark_paid. A failure in between left the expense saved-but-unpaid while the
  tool reported an error, breaking the same-transaction history guarantee at the
  tool boundary. `Store.create` now takes `paid`/`paid_date` and writes one row
  with one `create` history entry. The default payment date (today, in `APP_TZ`)
  is decided in `Store.create` alone — `expenses_add` used to layer a second
  default on top of it, so the two could disagree and the store's rule was
  unreachable. The portal's history view now shows the payment date such a row
  carries, which the collapsed single entry would otherwise have hidden.
- **A missing expense raised a bare `KeyError`**, which the HTTP layer turned
  into a clean 404 but the MCP surfaced to the agent as just the id — no hint
  about how to retry. `NotFoundError` subclasses `KeyError` (so the 404 mapping
  is untouched) and carries coaching text naming `query=` as the alternative.
- **The portal decided "today" from the phone's clock** while the server used
  `APP_TZ`, so a travelling or mis-set device could move a row between Due and
  Upcoming, or a month between History and Scheduled. `/api/list` now returns
  the household's `today` and the page uses it; the device is only the
  first-paint fallback. The page advances that date by however long it has been
  open, using **elapsed** time rather than the device's absolute clock — the
  first draft pinned it forever, so a tab left open overnight would have gone on
  offering yesterday as the payment date, and an accepted default writes a wrong
  date into the ledger. The estimate is **capped at one day**, after which the
  page refetches instead: elapsed time is only sound while the device clock runs
  at the right *rate*, and an NTP correction mid-session reads as time passing —
  unbounded, that writes a payment date into a month that has not happened yet.
  Bounded to ±1 day either way; returning to a backgrounded tab re-syncs.
  `PortalDateArithmeticTests` executes the page's own date functions under node
  instead of grepping for them, which is the only reason either direction of
  this was caught.
- **`/api/list` read the clock twice** — once for the summary, once for the
  `today` it returns — so a request straddling midnight could bucket rows
  against one day and label them with the next. `expenses_list` had the same
  split between `summary` and `ledger_total`. Both now read it once.
- **Every API handler blocked the event loop.** The handlers are synchronous and
  all of them hit the database, so one slow round trip to Neon stalled every
  other request in the process, `/health` included. They now run in a
  threadpool — free, because production is on Neon's pooled endpoint and
  Postgres connections here are already thread-local.
- **`APP_TZ` was pinned by no test.** Every date assertion was a shape-only
  regex and `today_str()` fell back to UTC through a bare `except`, so an image
  without `tzdata` would silently put a China household a day behind with the
  suite green. Two zones 25 hours apart now pin the behavior, a test asserts the
  zone data is present, and the fallback logs a warning instead of hiding.

### Added
- **`db/hardening.sql` + `Database._apply_hardening`** — a `UNIQUE(expense_id,
  seq)` index on `expense_history`. `seq` is computed inside the write
  transaction, so two concurrent mutations of the same expense could both claim
  it and silently corrupt the audit order. Applied **best-effort**, each
  statement in its own transaction with a logged warning on failure: this is a
  live portal, and a constraint that cannot apply to existing data must not be
  able to stop the service from starting; `Database.init` logs a warning naming
  how many constraints are not in force. An operator check on 2026-08-11 found
  production free of duplicates — a point-in-time observation, not something
  this repo can verify.

### Changed
- **The ledger is CNY-only, and now says so.** `currency` was stored but never
  consulted — `summarize()`, the portal cards and the charts all add `amount`
  regardless — so a single foreign row would have made every monetary figure in
  the app silently wrong. Non-CNY is refused at the store with an error that
  explains why. The portal UI has no currency field and the MCP exposes no
  parameter, but `/api/submit` and `/api/update` read `currency` from the
  request body, so any link holder could reach it — the guard is load-bearing,
  not decorative. Every production row was CNY when this shipped (operator
  check, 2026-08-11).
- `docs/IMPLEMENTATION_PLAN.md` is labelled a **historical record** frozen at
  v0.2.0, and contract §10 no longer claims it stays in sync. It said "five
  tools" (ten) and named `/healthz` as the health endpoint; both are annotated
  rather than rewritten, since the point of the file is what was planned.

### Operations (investigated, no change needed)
- **The `maxScale` disagreement was not one.** `autoscaling.knative.dev/maxScale=3`
  on the revision template is the per-revision cap and governs the running
  revision (set by `deploy.sh --max-instances=3`);
  `run.googleapis.com/maxScale=20` on the service is Cloud Run's separate
  service-level ceiling across revisions. Effective limit: 3 instances.
- **The GCP budget alert already exists.** The item assumed a dedicated project;
  the service runs in `work-dashboards` (693424932326), covered by a $15/month
  project-scoped budget alerting at 50/90/100% and by a billing-account-wide $5
  budget. Nothing to create.

## [0.8.2] — 2026-08-11

Documentation pass. No behavior change except the new guard.

### Added
- `docs/BACKLOG.md` §2 — the portal decides "today" from the device clock, not
  `APP_TZ`, so travel or a wrong phone clock can shift a row between Due and
  Upcoming around midnight. §3 — currencies are summed without conversion; one
  non-CNY row makes every total meaningless. Both surfaced by cross-model review.
- `DocumentedCountsTests` — the living docs' test and tool counts are now
  asserted against reality. The count drifted four times in one session; it
  caught a stale prose mention within a minute of being written.

### Fixed
- **`unittest.main()` sat mid-file in three test modules.** Classes appended
  after it were invisible to a direct `python3 tests/test_x.py` run — they only
  executed under `discover`. Found while trying to prove the new guard works,
  which is the only reason it was noticed.
- Docs reconciled against the code: contract status was pinned at v0.4.5 and
  claimed 9 tools; the runbook advertised 99 tests; the README claimed
  onboarding was outstanding; `FIRST_DEPLOY_PLAN` still said "onboarding
  pending" and its re-runnable acceptance gate expected 9 tools.

### Changed
- **`FIRST_DEPLOY_PLAN.md` Wave 6 recorded COMPLETE (2026-08-11).** She is
  onboarded and using the portal daily; the owner's connector is live. This was
  the milestone the whole plan existed to reach.
- `CLAUDE.md` "Current state" rewritten from an accreting reverse-chronological
  changelog into orientation: what is live, that two real people depend on it,
  that the portal has a login while `/mcp` deliberately does not, and the two
  failure modes this project has actually hit (silent no-op edits; handoffs
  smuggling in a demo backend).
- Contract §4/§6/§8 describe the app as it is: `date` is the due date, `borrow`
  is arithmetic-bearing, portal writes are attributed server-side, `/api/list`
  takes `overdue` and summarises the rows it returns, and the UI is three tabs.
- `BACKLOG.md` §1's "do it before onboarding" advice is obsolete — that window
  closed today; the two items fixed in 0.8.0/0.8.1 were struck from §4.

## [0.8.1] — 2026-08-11

Six defects found by cross-model review (Codex/GPT-5.4) of 0.7.0–0.8.0. All six
passed the 117-test suite; each now has a regression test.

### Fixed
- **The category guidance was factually backwards.** `_HELP` and the result note
  told the agent an off-list category "will not appear in the totals or charts".
  It does: `summarize()` counts every category except exact `borrow` as ordinary
  household spending. The advice invited the very mis-bucketing it claimed to
  prevent — a borrow-synonym inflates household paid/unpaid instead of the
  money-owed-back figure. Both now say what actually happens.
- **`find(status="overdue")` ignored the filter entirely.** `list()` and `find()`
  each hand-rolled the status clause and drifted: `find()` handled only
  paid/unpaid, so `expenses_list(query=…, status="overdue")` returned paid and
  future rows, and a typo'd status silently meant "all". Extracted to a shared
  `_status_clause()` so they cannot diverge again; unknown statuses now raise.
- **Portal attribution was still client-spoofable.** `_author()` gave the
  client's `submitted_by`/`changed_by` precedence over the link label, so a
  request bearing the "wife" link could write any name into the audit trail —
  contradicting 0.8.0's own claim. The validated link's label is now the only
  author on this path.
- **History could hide scheduled money.** A ledger holding only future rows
  rendered "Nothing yet" and dropped every scheduled month; and `anyOut` was
  computed from past/current rows only, so a settled history hid the Outstanding
  column for scheduled rows that had money in it.
- **`since`/`until` skipped validation when `query` was set** — the MCP
  post-filter compared raw strings, so `since="not-a-date"` produced an
  arbitrary slice instead of a coached error.
- **Totals are now order-independent.** Bucket sums use `math.fsum` rather than
  `+=`; binary floating-point addition is order-dependent, so the Python
  aggregate is now identical to the SQL one it replaced for every input, not
  just household-sized ones.

### Tests
- Suite 117 → **124**, including the +30/+31 upcoming boundary the first pass
  never tested, `find(overdue)`, authoritative attribution, and order-independent
  summation.

## [0.8.0] — 2026-08-11

### Fixed
- **The summary described the whole ledger while the list was filtered.** Ask
  "what's owed this month" and two rows worth ¥5,780 rendered beneath a
  ¥247,780 headline. `Store.summarize(rows)` now derives totals from exactly the
  rows returned, and `summary()` is a thin wrapper over it — one code path, so
  the disagreement is no longer representable. Urgent because she is reading the
  portal now: the headline is the first thing on the page.
- **`expenses_list` silently dropped `since`/`until` whenever `query` was set**
  (`store.find` has no date support), so a date-bounded search quietly returned
  all time. The range is now applied to the matches.
- **Portal writes carried no author.** The form deliberately does not ask — one
  person types into it — but with two people writing to one ledger, "an expense
  exists" and "who logged it" are different facts. Writes are now stamped with
  the link's label, taken from the validated token row and never from the
  client, so it cannot be spoofed. Applies to edits and deletes too.
- **Removed the demo backend that arrived with the design handoff.**
  `if (!TOKEN) return demoApi(...)` never fired in production — the portal is
  only served at `/t/<token>` — but a ledger that silently accepts writes into
  an in-memory fake is the worst failure mode available here.

### Changed
- **`upcoming` is a 30-day window, not everything future.** Twelve months of
  living payments loaded in advance made the card answer a question nobody
  asked. Items beyond the window still count in `unpaid`.
- **History leads with the most recent month.** Scheduled future months were
  sorting above it — the top of the statement was 2027-07. Past and current
  months now come first (newest first), with future months in their own
  "Scheduled" group below. Items within a month are newest-first too.
- `status="overdue"` (unpaid and past its due date) on both `list` and `find`;
  the invalid-status error names it.

### Tests
- Suite 111 → **117**: summary-matches-rows, the upcoming window, the overdue
  filter, and two guards that the demo backend cannot return on a future design
  pass.

## [0.7.1] — 2026-08-11

### Fixed
- **The MCP never knew the category keys existed.** Nothing in `_HELP` or any
  tool description mentioned them, so asked to record borrowed money the agent
  invented `"loan repayment"` — which saved fine, matched nothing, and silently
  counted ¥31,100 as a paid household expense instead of a borrow. The playbook
  now lists every key and calls out `borrow` as the one with arithmetic behind
  it, explicitly warning against synonyms; `expenses_add` and `expenses_update`
  point at it too. This is the failure `docs/MCP_DESIGN.md` exists to prevent:
  guidance that lives nowhere the agent reads is not guidance.
- Write results now carry a coaching note when a category will not group, so a
  wrong guess self-corrects on the next call rather than mis-bucketing forever.
  Values are still stored verbatim — the MCP can write anything.
- `expenses_update` now returns a `note` at all. It was the only write tool
  without one, contrary to the module's own stated contract.

### Tests
- Suite 105 → **110**. The category-note helper short-circuits on an empty
  category, so a `NameError` in it stayed invisible to every existing test —
  each of which added expenses without one. The new tests pass a real category,
  which is what surfaced it.

## [0.7.0] — 2026-08-11

### Changed
- **Portal visual design, by Claude Design (Fable).** "The page IS the paper" —
  a typographic ledger rather than a stack of cards: warm paper ground, cinnabar
  seal-red accent, green stamp for paid, blue ink for money owed back, hairline
  rules instead of boxes. Dark mode is a designed night-ink palette, not an
  inverted light one. Chinese-first metrics (medium weights, 1.6 line-height,
  tabular numerals) — the gap the previous pass left, where Latin-shaped weights
  were inherited by CJK. Also adopts `font:-apple-system-body`, so the portal
  now tracks iOS Dynamic Type.
- Verified against the hard constraints before installing, not assumed: zero
  external requests (no CDN font, script, or image — the property that makes
  this load behind the GFW), CJK font stack intact, API wiring and
  token-from-path preserved, all 17 category keys still matching
  `store.CATEGORIES`, both language label sets complete, and no unescaped
  `catLabel`. The escaping and category-parity tests pass unchanged.
- **"What I paid for" → "Money I lent"** (English only). Her lending is not
  everything she pays for, so the old label over-claimed. 垫付 already means
  precisely "advanced on someone's behalf", so the Chinese was never the loose
  one. "I fronted" / "I paid" → "I lent" across the state chip, tile, chart
  title and category label.
- **Monthly bar labels round to thousands** — ¥22k, ¥9.8k, exact below ¥1,000.
  Twelve full figures across a ~320px viewBox bled into each other. Chart-only;
  the History tab still carries exact amounts, so nothing is lost.

### Removed
- The artifact wrapper the design arrived in. `.DS_Store` is now gitignored.

## [0.6.0] — 2026-08-11

### Added
- **Three-tab portal.** Due · History · Stats, tabs at the top with the summary
  cards inside the Due pane so they only appear where they apply.
- **Stats tab**, hand-rolled inline SVG — no chart library, because the
  no-build-step / no-CDN property is what makes the portal load on a phone in
  mainland China. Matt's payments by month, spend by category, and a borrow
  block: outstanding, fronted, repaid, and **average days to repay** (derived
  from fronted-date → repaid-date, which the ledger already stored).
- **Expandable months** in History; open months are component state so marking
  something paid from inside one does not snap it shut.
- `PORTAL_DEV_RELOAD=1` re-reads portal.html per request. Off by default, so
  production still serves from memory.

### Changed
- **Cards → compact list.** Two lines per item, hairline separators, actions
  revealed on tap. This screen is read far more than it is touched; every row of
  chrome was costing a row of ledger.
- **Mobile overlap fixed.** The old `.meta` flex row let the due date and
  category collide under ~400px. The sub-line is one wrapping text run now, and
  descriptions carry `min-width:0` so they cannot push the amount off-screen.
- **"Date" is "Due date" throughout** — the ledger answers *when must this be
  paid*, not *when was it entered*.
- **The portal speaks in her voice.** It is her surface; the owner works through
  the MCP. "Owed to her" → 待还我 / "Owed to me"; the borrow category is 我垫付 /
  "I paid (owed back)".
- **Spend figures name Matt** rather than claiming to be household totals — she
  spends too, and the ledger only sees what passes through it. Labelling them
  "Monthly spend" quietly erased her side.
- Removed the submitted_by field from the add form: only one person enters via
  the portal, so it asked a question with one answer. The column stays; history
  rows reference it.
- Category taxonomy replaced with the household's real one — living, the four
  Aden buckets, utilities/internet/mobile, and `borrow`.

### Fixed
- Chart marks are one hue in both modes. The theme's amber and violet fail the
  dark-surface lightness band as marks (they are tuned as text) — caught by
  running the palette validator rather than eyeballing it. Both charts compare
  magnitude, which wants sequential anyway.

### Tests
- Suite 102 → **105**: category parity between portal.html and store.CATEGORIES
  in both languages, and that the portal special-cases exactly the key the store
  does. Two hand-maintained lists that silently disagree would route spending
  into a bucket nobody looks at.

## [0.5.0] — 2026-08-11

### Added
- **`expenses_list_links` (10th tool).** Found by live testing: revocation was
  unreachable in practice. The agent had no way to discover *what* to revoke,
  and `expenses_revoke_link`'s description pointed it at the operator CLI — a
  channel an agent cannot use. A cross-reference is only guidance if it names a
  tool the agent can actually call. Returns id, label, status
  (active/expired/revoked), and usage; hides revoked links unless
  `include_revoked=true`.
- **Portal OAuth via Auth0 Universal Login (`app/auth.py`), off by default.**
  Same tenant as work-dashboards (JP region). Deliberate divergence from that
  app's `@auth0/auth0-spa-js` SPA pattern: this portal has no build step and a
  CDN script tag is an unreliable dependency from mainland China, so the code
  exchange is server-side and the session is a signed httpOnly cookie.
  - Layered *on top of* `/t/<token>`, not replacing it: the token still says
    which ledger, Auth0 says who you are. No tool or table was retired.
  - `PORTAL_ALLOWED_EMAILS` allowlist. Auth0 authenticates anyone who can sign
    up, so an empty list denies everyone — a half-finished config fails closed.
  - New routes `/login`, `/callback`, `/logout` exist **only** when configured.
  - `/mcp` untouched; `MCP_SECRET` still unset. Connected MCP clients see no
    login and need no reconfiguration.

### Compatibility
- With `AUTH0_*` / `SESSION_SECRET` unset, behavior is identical to 0.4.5 —
  pinned by `test_everything_still_open_when_oauth_is_off` and by the route-shape
  test asserting `/t/{token}` and `/api/*` do not move when OAuth is on.

### Tests
- Suite 77 → **98**: link-listing behavior and its ergonomics pins, plus
  `tests/test_auth.py` covering the flag (partial config fails closed), the
  allowlist (case-insensitive, empty = deny), the guards (portal 302, API 401,
  bad token still 404s without a login detour), and open-redirect protection on
  `?next=`.

## [0.4.5] — 2026-08-10

### Operations
- Deployed to the permanent service as revision `family-expenses-00005-5tz`
  (commit `0ce5f68`), serving 100% of traffic at the unchanged public URL.
  `scripts/smoke_live.py` PASS; A8 re-verified across `00004-pvt` → `00005-5tz`
  (same URL, `/mcp` still header-free, `MCP_SECRET` still unset). The fix was
  confirmed in the served HTML using a temporary token that was then revoked —
  no real token was minted or recorded. Wave 6 human onboarding is unblocked.

### Fixed
- **Stored XSS in the portal (security).** `render()` interpolated the category
  label into `innerHTML` unescaped whenever the category was not one of the six
  known keys (`app/portal.html`, the `esc(e.description) || catLabel` fallback);
  the same value was already escaped two lines later, which is what made it a
  slip rather than a decision. `category` is unvalidated end to end — the API
  takes `body.get("category")` raw and the column is bare `TEXT` — so any writer
  could plant markup, and with `/mcp` unauthenticated that means any caller who
  knows the URL. A rendered payload can read `location.pathname` and exfiltrate
  the never-expiring portal token. Now escaped at the render sink; stored values
  are deliberately left verbatim so the ledger and MCP round-trips stay honest.
- An expense with neither description nor category rendered as `[object Object]`
  — the fallback called `t("cat")`, which returns the category *map*, not a
  string. Now renders `–`.
- `data-id` is escaped defensively; ids are server-generated hex, so this is
  depth, not a live hole.

### Changed
- Added `.gitignore` and untracked the 12 committed `__pycache__/*.pyc` files.
  Their churn made `git status --porcelain` permanently non-empty, which tripped
  the clean-tree guard in `scripts/deploy.sh` and blocked every deploy after a
  test run. Also ignores `*.db` and `.env`.

### Tests
- `PortalEscapingTests` pins the fix by accounting for every `catLabel` occurrence
  in `render()` — each must be `var catLabel`, `esc(catLabel)`, or a bare
  truthiness guard; anything left over fails with the offending line number. A
  `+ catLabel` adjacency check would NOT have caught the original bug (the raw use
  sat between `||` operators), and asserting `esc(catLabel)` is present would not
  either (it already appeared elsewhere in `render()` while the hole was open).
  Verified in both directions against the vulnerable line before committing.
- A second test pins that writes are *not* sanitized: escaping belongs at the
  render sink, and mangling stored categories would corrupt MCP round-trips.
- Suite 75 → **77**.

## [0.4.4] — 2026-07-15

### Operations and verification
- First production deployment accepted on the permanent Cloud Run service in
  `asia-southeast1`, backed by an isolated Neon project. The same portal link
  and public no-header MCP URL survived a revision change; mobile acceptance,
  runtime-contract inspection, cleanup, and final public smoke all passed.
- The live smoke now covers portal update, mark-paid, unmark-paid, exact audit
  history, and a real MCP `ClientSession` handshake with the exact nine-tool
  and three-prompt inventories plus bilingual natural-input, ambiguity,
  correction, fuzzy-payment, history, and cleanup flows.
- `/favicon.ico` returns 204 so mobile/browser acceptance runs have a clean
  console instead of a cosmetic missing-favicon error.
- Suite 74 → **75**.

## [0.4.3] — 2026-07-15

### Fixed
- Added `/health` as the Cloud Run-safe health endpoint and moved live smoke
  checks to it. `/healthz` remains available locally and for compatibility,
  but Google's front end reserves some paths ending in `z` and intercepts
  this one with a 404 before requests reach the container.

## [0.4.2] — 2026-07-15
First-deployment hardening: the public MCP, pooled Postgres lifecycle, and
Cloud Run storage posture now fail safely under the production conditions the
local SQLite suite cannot reproduce.

### Fixed
- FastMCP binds its host policy to runtime `HOST` (default `0.0.0.0`), so a
  real Cloud Run Host header completes initialize and `tools/list` instead of
  returning 421. Stateless HTTP returns JSON responses, avoiding the SDK's
  unclosed SSE receive-stream warning while remaining protocol-compliant.
- Postgres thread-local connections evict closed handles and retry a failed
  first transaction statement once on a fresh connection. Mid-transaction
  failures are never replayed; the poisoned connection is evicted and rollback
  failure cannot mask the original error.
- Cloud Run (`K_SERVICE`) refuses to boot without a Postgres `DATABASE_URL`,
  preventing a missing secret binding from silently writing to ephemeral
  SQLite.
- Async MCP tests now close their event loops and complete the initialized
  notification handshake, removing lifecycle warnings from the release gate.

### Operations
- Added `scripts/deploy.sh`: clean-tree guard, explicit SHA build/deploy,
  Secret Manager binding, no `MCP_SECRET`, finite scale, and permanent
  Singapore region/service constants.
- Cloud Build no longer publishes or deploys a mutable `latest` image.
- Live smoke requires the Neon pooled URI and repeats token validation at
  least six times to cross psycopg's default prepared-statement threshold.
- Added regression/static gates for reconnect, production fail-closed,
  external-host MCP, pooled validation, and deployment contract. Suite 63 →
  **74**.

## [0.4.1] — 2026-07-14
Owner's final risk ranking encoded: the dominant risk is a family member
being forced to reconnect (→ disuse), not unauthorized edits.

### Added
- **Compatibility contract** (FEATURE_CONTRACT §5.1, acceptance A8): frozen
  surface = service URL, `/t/<token>` path + her token, `/mcp` mount,
  no-auth-header posture. Runbook gains "Don't break her setup" rules.
- `CompatibilityContractTests` pin the frozen surface in CI (mount path,
  route shapes, credential-free defaults, heavy use never invalidating a
  link). Suite 59 → **63**.

## [0.4.0] — 2026-07-14
Agent-ergonomics rework, motivated by the owner's experience of MCP guidance
"the agent never sees": all behavior now lives in channels agents reliably
read (tool descriptions, results, error strings, annotations) — codified in
**docs/MCP_DESIGN.md**.

### Added
- `expenses_help` tool — playbook-as-a-tool ("START HERE when unsure");
  works on clients that never surface server `instructions`.
- **Three personas as MCP prompts**: 记账 `jizhang` (quick add), 对账
  `duizhang` (settle up), 修复 `xiufu` (fix a mistake).
- Bilingual trigger phrases and cross-references ("to X use tool Y") inside
  every tool description — the channel that drives tool selection.
- **Coached error strings**: wrong amount says what parses ('¥300', '300块');
  bad date says relative words must be converted or omitted; touching paid
  via update redirects to expenses_mark_paid. One-round-trip self-correction.
- Write results carry a `note` with the running unpaid total.
- `expenses_add` records already-paid expenses in one call (`paid=true`,
  audit trail keeps create + mark_paid).
- Tool annotations: reads flagged read-only (fewer client permission
  prompts), delete/revoke flagged destructive.

### Changed
- `expenses_summary` **removed** (9 tools total): redundant with the summary
  already returned by `expenses_list`; redundant read tools split selection
  probability (see MCP_DESIGN.md).
- `amount` params accept numbers **or** strings — pydantic v2 does not coerce
  int→str, so the old `str` type silently rejected numeric arguments from
  agents (exactly the "worked for the dev, failed for the agent" class).
- `expenses_history` returns `{"history": [...]}` (object, not bare array).

### Tests
- Suite 51 → **59**; new `AgentErgonomicsTests` pin triggers, cross-refs,
  annotations, personas, numeric amounts, one-call paid add, result notes,
  and coaching text in errors — regressions in agent-visible channels fail CI.

## [0.3.0] — 2026-07-14
Auth scaled back to the owner's explicit threat model (low-stakes household
ledger, zero-tech user, unguessable URLs); MCP reworked for natural speech.

### Changed
- **Portal links never expire by default** (`expires_at` nullable; NULL =
  never). The holder never renews anything; revocation stays the kill switch.
  Bounded expiry still available via `expires_days`.
- **MCP bearer is now optional**: `MCP_SECRET` set → enforced (401 on
  mismatch); unset → `/mcp` is open (was fail-closed 503).
- `expenses_add`/`mark_paid` dates optional — default **today in `APP_TZ`**
  (default `Asia/Shanghai`), not UTC.
- Amounts tolerate spoken/pasted forms: `¥300`, `300块`, `1,200元`, `300 rmb`.

### Added
- **Natural-language targeting**: `expenses_mark_paid` / `expenses_update` /
  `expenses_delete` accept a fuzzy `query` ("足球课") instead of an id —
  one match acts (mark-paid prefers the unpaid match), several matches return
  candidates for the assistant to disambiguate; zero matches return a hint.
- New MCP tools `expenses_update` and `expenses_delete`; `expenses_list`
  gains a `query` text filter. Tool count now 9.
- Server instructions coach LLM clients: bilingual example utterances,
  defaults, confirm-before-delete, reply in the user's language.
- `store.find()`, `today_str()`; runbook §4 "what you (or she) can say".

### Tests
- Suite 37 → **51** (natural-speech flows, ambiguity, never-expire tokens,
  open/gated middleware) — all green; live MCP smoke re-run in open mode
  covering the full conversational flow end-to-end.

## [0.2.0] — 2026-07-14
First complete implementation (chunks 1–5), ready for first deploy.

### Added
- **Store** (`app/store.py`, `app/db.py`): portable Postgres/sqlite layer;
  create/update/mark-paid/delete/list/summary/history; every mutation writes
  one append-only `expense_history` row in the same transaction (M3); token
  mint/validate/revoke, fail-closed with usage tracking (M2);
  `expense_history.seq` for deterministic ordering.
- **Portal** (`app/web.py`, `app/api.py`, `app/portal.html`): `/t/<token>`
  mobile-first bilingual (中文/EN) page — add, inline edit, mark paid with
  date, filters, totals, per-item history; JSON API revalidates the token on
  every request (401/400/404 mapping).
- **MCP** (`app/mcp_server.py`, `app/main.py`): FastMCP streamable-HTTP with 7
  operator tools incl. `expenses_mint_link`/`expenses_revoke_link`; `/mcp`
  gated by `Authorization: Bearer $MCP_SECRET`, fail-closed when unset; one
  Cloud Run service serves portal + API + MCP.
- **Ops**: `Dockerfile`, `cloudbuild.yaml`, `scripts/mint_link.py`,
  `docs/RUNBOOK.md`.
- **Tests**: 37 (store, HTTP tier, MCP tools, bearer middleware, combined
  app) — run on sqlite with no DB server; plus live smokes: real uvicorn
  portal flow and a real MCP client handshake with bearer auth.

### Changed (architecture pivot, owner direction)
- The feature moved from a `work-dashboards` in-repo portal to this
  **standalone repo**. `work-dashboards` is reference-only (patterns:
  portal-token links, Neon, Cloud Run streamable-HTTP MCP) and receives no
  commits or pushes. Isolation from the business system is structural
  (separate repo / DB / services). MCP hosting: Cloud Run (owner: "already
  works; no need to introduce new tech").
### Removed
- Superseded localStorage prototype (`index.html`) — replaced by the
  server-backed portal (history preserved in git).

## Planning history (v0.1.x, in work-dashboards — superseded)
- `0.1.1` — contract + plan revised per independent adversarial verification
  (3 must-fix / 6 should-fix / 3 nits). Carried forward: M2 (first-class token
  minting), M3 (same-transaction audit writes). Moot after pivot: M1/S2/S3
  (work-dashboards SPA), S5 (tenancy), S6 (money-as-cents audit).
- `0.1.0` — initial contract + plan.
