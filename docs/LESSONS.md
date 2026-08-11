# Lessons — failures this codebase has actually had, and the rules they produced

Every rule in `CLAUDE.md` exists because something went wrong here. This file is
the evidence: what broke, how it was found, and what to do differently. A rule
you understand the origin of is one you can apply to a case it does not
literally name.

Two things to know before reading:

- **This is a money app.** Its defects do not throw. They produce a wrong date,
  a wrong course, a duplicate row — and a green test suite. "Nothing errored" is
  not evidence of anything.
- **The entries are ordered by how much they cost**, not by date.

---

## 1. A fix is not a verified state

**What happened.** v0.11.0 went through six review rounds. **Five of six fix
waves introduced a new defect of their own.** The fix for the stale-date bug was
ambiguous by construction; the fix for *that* froze the date permanently; the
fix for the deadlock was a blind retry that could double-log money; the fix for
the in-flight race read a value the reset did not clear. Each one passed its own
tests and read correctly to the person who wrote it.

**Rule.** Review the fix as adversarially as the original — it has had less
thought, not more, and it was written by someone who has just convinced
themselves they understand the problem. Never let "fixed" end a review round.
The gate is *zero must-fix and zero should-fix remaining*, re-checked after the
last change, not "every finding has a commit."

---

## 2. Same-model review inherits your framing; a different model does not

**What happened.** Three adversarial rounds by the same model family all
verified that `clsDates` was consistently written, read and cleared. The story
held. The bug was outside the story: **`<input type="date">` fires no `change`
event when you re-pick the value it already shows**, so a deliberate choice was
never recorded. Every same-model reviewer had reasoned about the data structure
because that was the vocabulary the design handed them. A cross-model pass
(`/codex-verify`) asked what the *browser* does instead, and also found the only
defect in the release that could move money — after three rounds had read the
same lines.

**Rule.** On money paths, a cross-model pass is a distinct gate, not a fourth
round of the same thing. The owner runs it in both directions (Claude reviews
Codex's work too). When reviewing, name your framing and then attack the frame,
not only what sits inside it.

---

## 3. Test stubs more permissive than reality make tests that cannot fail

**What happened.** Repeatedly, and it is the most common failure in this repo:

- a `<select>` stub accepted values that were not among its options, and the
  fixture had one candidate — so "keep her selection" and "take the first
  option" were the same string;
- a date-picker stub let a test set a DOM state the application could not
  produce, so the test pinned a behaviour that never happens;
- a tap driver hard-coded the button kind, so a "sibling button" test tapped the
  same button twice;
- an "answered flag" test supplied the flag under test, because the harness
  stubbed out the function that sets it.

**Rule.** For every stub, ask: *is this more permissive than the real object?*
Model the real thing's refusals, not just its successes. Then mutate the source
and watch the test die — see §4. Where a test asserts a value, check the value
was not supplied by the test itself through an untransformed path.

---

## 4. Prove the guard fails before trusting that it passes

**What happened.** This is `CLAUDE.md` P5, and it keeps earning its place.
Scripted string replacements have twice reported success and changed nothing,
with the suite still green because the affected path was short-circuited.

Two refinements the v0.11.0 review added:

- **A mutation runner must assert its target test exists and passes first.**
  `unittest` exits non-zero for a test name that does not resolve, which reads
  as "the mutation was caught". A renamed target hid a mutation that genuinely
  survived. Three targets across eight passes were stale.
- **A surviving mutation is ambiguous evidence.** It means the test is weak, the
  mutation is wrong, or both. In one pass, eight survived: three were real test
  gaps and five were mutations aimed at the wrong test or malformed. Look; do
  not assume either way.

**Rule.** Apply the mutation, watch the specific test fail, restore. Assert your
anchors matched exactly once. Never report a mutation count you have not
verified this way.

---

## 5. A value shown and a value written must be one thing

**What happened.** The class-date picker took four attempts. Every failure was
the same shape: the date on screen and the date that would be posted were two
separate computations, and they drifted apart silently.

1. `prompt()` — she typed the date by hand, under a label that said 到期日.
2. A stored map read at tap time — but the box was rendered from a different
   evaluation, so across midnight they disagreed.
3. A `data-seeded` attribute to infer "did she choose this?" — provably
   ambiguous: *untouched* and *deliberately picked what is displayed* produce
   identical DOM.
4. Pinning the date into the map at render — made them agree by freezing both,
   so a row opened at 23:50 logged yesterday for the life of the page.

What finally held: **the box is the source of truth.** The tap posts exactly the
date she can see. Nothing is inferred from events, which is what makes a
same-value re-pick work at all.

**Rule.** When a displayed value and a stored value can disagree, do not add a
mechanism to keep them in sync — remove one of them. Prefer reading what the
user can see, because that is the only version they can check.

---

## 6. A retry around a non-idempotent write is worse than the bug it fixes

**What happened.** A per-course in-flight lock was added to stop double-logging
a class. It could deadlock if a request never settled, so a 30-second release
was added. That release is a **blind retry**: `class_events` has no uniqueness
constraint, so a request that committed and lost its response would be retried
and write a second row — and `summarize_package` counts both. A cosmetic fix
introduced a money-moving defect. The same hole then reappeared through the
`.catch` path, which released the lock on any network failure.

**Rule.** Before permitting any retry, ask whether the write is idempotent. If
it is not, fail *closed*: hold the lock, and let the user reload. A stuck
control is visible and recoverable; a duplicate row is neither. See
`BACKLOG.md` §6 for the standing instruction not to reintroduce this.

---

## 7. Read the clock once per request

**What happened.** Three separate instances. `/api/list` computed `today` for
the summary and again for the response, so a request straddling midnight
bucketed rows against one day and labelled them with the next. Then `today` and
`midnight_in` came from two readings, so the page was told "it is yesterday, and
you have a full day left" — holding yesterday for another day. Then the overdue
filter inside `Store.list` read it *again*, dropping a newly-overdue row from
both the rows and their totals. Each fix left another instance behind.

**Rule.** Read the wall clock once at the top of a request and thread the value
down through every consumer — rows, totals, and the response label. When you fix
one, grep for the others in the same breath; there is always another.

---

## 8. Grep-shaped assertions pass on dead code

**What happened.** v0.10.0 shipped a Classes tab that **did not work at all** —
a document-level handler closed the row the class handler had just opened, and
an author `display:flex` out-ranked the UA `[hidden]` rule so every row showed
its Delete button. The suite was green: every portal guarantee was a string
match against `portal.html`.

**Rule.** Test `portal.html` by executing its real functions and handlers under
node against stubs (see `ClassRowRenderingTests`, `ClassTabInteractionTests`,
`ClassAddFormTests`). Use a source-text assertion only where a function nothing
calls would otherwise be invisible — a wiring check — and say so in the
docstring. Anchor such a regex on something that cannot drift: one written as
`<div class="row">.*?<div id="clsPeriodWrap">` matched no matter where the box
moved to.

---

## 9. Confident comments have been wrong

**What happened.** A comment asserted a rounding boundary was unreachable; it
was reached. A comment claimed two candidate fields "cannot" both match; they
can, because `created_at` is second-granular. A tool description told an agent a
course was invisible in the portal and to `expenses_update` the category to fix
it — both halves false, and acting on it would have moved a real ledger row
between analytics buckets.

**Rule.** A comment is a claim, not evidence. When reviewing, check the claim
against the code. When writing one, state what you verified rather than what you
believe. Tool descriptions and error strings are instructions something will
follow (P3) — a wrong one causes actions, not just confusion.

---

## 10. Money defects survive tidy fixtures

**What happened.** Every class-money defect in v0.10.0 passed a suite full of
¥2,200/10 and ¥2,000/8 — amounts that divide evenly. ¥1,000 over 3 classes is
what exposed them. A later round found the reverse: parts summed independently
could exceed the payment by ¥0.01 in ~0.8% of splits.

**Rule.** Money tests use amounts that do not divide: third-decimals, primes,
odd counts. Sweep properties over ranges rather than asserting hand-picked
examples. **A part is derived from the total, never the total from its parts** —
that is what makes the cap true by construction instead of by assertion.

---

## 11. Ask the reviewer whether your own machinery is worth keeping

**What happened.** By round six, the defects were clustered entirely in date and
concurrency code added *in response to earlier reviews*; the original feature had
been clean for three rounds. The reviewer was asked directly whether that
machinery was now more risk than the bugs it removed. It said to revert the
midnight repaint timer — and it was right: the timer re-armed against its own
expired deadline and became a one-second render loop.

**Rule.** When successive fixes keep failing in the same area, the mechanism is
the problem, not the details. Ask an outside reviewer whether to remove it. You
built it, so you are the worst-placed person to judge it. Accepting a smaller,
visible defect is often correct — record it in `BACKLOG.md` with the shape a
real fix would take.

---

## 12. A filter with no other half hides rows, and only the user finds out

**What happened.** The Due tab rendered `daysBetween(today, e.date) <= 30` and
nothing else. For four releases that was invisible, because everything in the
ledger was near-term. Then she entered five months of course fees in four
minutes, and the first row due in 50 days appeared in **no list and no card** —
History kept it inside a collapsed month. The success toast was a 1.7s flash of
a constant over a list that did not move, so from her side the app had done
nothing. She added the same ¥1,980 course again. Both rows are in production.

No test had ever executed `renderNow`. The suite was 341 tests green.

**Rule.** When a view filters a set, ask where the complement is rendered. "Not
shown here" must be "shown there", never "shown nowhere" — so partition ONE
list rather than filtering with a predicate and dropping the remainder; the
complement then cannot be forgotten.

**Write the second half as the NEGATION, not as a second predicate.** The first
fix used `<= 30` and `> 30`, which only *look* complementary: the comparison is
`NaN` for a date `Date.parse` cannot read, and `NaN` satisfies neither — so the
row was invisible again, by the same mechanism, in the code written to fix it.
`!inWindow(e)` has no such hole whatever the comparison does. A cross-model
review found this; my own passes did not. The store now rejects those dates
too, but the renderer must not *depend* on that: rows written before a check
existed are still in the database.

**Then the explanation of that fix was itself wrong**, in five places, and the
next review round caught it: not every impossible date gives `NaN`.
`2026-13-01` does; `2026-02-30` silently normalises to early March and renders,
sorts and totals as a row due on a day nobody entered. The fix was right and
the reason written beside it was not — §9 again, in a comment written *while
fixing a bug about unverified claims*. Run the probe before you describe the
behaviour, even when the code already works.

**And test membership, not presence.** "Every row appears somewhere" cannot
tell a partition from an overlap — a `>=` slip puts the boundary row in both
sections and the assertion still passes. Assert each row appears exactly once,
in the section it belongs to. And confirm a write by echoing what the
**server stored**, not what the form held: a constant toast is
indistinguishable from nothing happening, and echoing the form confirms a write
that may not have happened that way. The guard for this is a sweep across the
boundary asserting every row reaches the markup — an "is the section there?"
assertion passes on a section nothing puts rows into.

**Corollary that cost the most here:** the report was "it's absent from the
app", which sounds like data loss. It was not — the row was in Postgres and
correct the whole time. Query production **before** touching code; the fix for
a missing write and the fix for a hidden read have nothing in common.

---

## 13. Pin every number a living doc asserts, not just the one that bit you

**What happened.** Twice. In an earlier release the contract claimed 9 tools,
the runbook advertised 99 tests and the README disagreed with both;
`DocumentedCountsTests` was written to pin the **test count** and stop it
recurring. It recurred anyway, in the next column over: the README advertised
**10 MCP tools while 13 shipped**, and the guard did not look at tool counts.
Found by a doc-freshness sweep, not by the suite.

**Rule.** When a doc asserts a number about the code, add it to the parity test
in the same commit — test count, tool count, table count, whatever it is. A
guard scoped to the single instance that caused pain leaves every sibling
unguarded, and doc drift is invisible precisely because nothing fails.

Historical counts are different: `CHANGELOG.md` and `FIRST_DEPLOY_PLAN.md`
quote what was true at the time, on purpose. Parity tests must exclude them by
name, which is also why a freshness sweep needs a human read of any hit in
those files rather than a blind fix.

---

## 14. Verify anything that arrives from outside

**What happened.** A design handoff carried an in-memory demo backend reachable
at `if (!TOKEN) return demoApi(...)`. It never fired in production, but for a
ledger, silently accepting writes into a fake is the worst failure available.

**Rule.** `CLAUDE.md` P6. Check external files for: external requests (there
must be zero — the GFW is why), the CJK font stack, `esc()` on every
interpolation, category keys matching `store.CATEGORIES`, and any fallback that
could accept writes without persisting them. `CategoryParityTests` pins the
demo-backend check so it cannot come back on the next handoff.

---

## Adding to this file

When a review round finds something real, add the pair: **what actually
happened** (concretely enough that someone could reproduce it) and **the rule it
produces**. If it produced no rule, it belongs in `CHANGELOG.md` instead. If it
was found but deliberately not fixed, it belongs in `BACKLOG.md`.
