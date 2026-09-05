# MCP Design — what agents actually read

Motivation (owner, 2026-07-14): a previous MCP took "dozens of edits" because
guidance was written where the agent never looked, and tools weren't selected
when expected. This doc fixes the channel model in writing so every future
edit lands where it has effect.

## Channel reliability (what an LLM client shows its model)

| channel | reliability | what we put there |
|---|---|---|
| **Tool name + description + param schema** | ~always in context | the ONLY place guidance is guaranteed seen. Trigger phrases (中文+EN), defaults, cross-references ("to X use tool Y") |
| **Tool results** | always read after a call | ambiguity candidates + `hint`, running unpaid total (`note`) so the agent confirms naturally |
| **Error strings** | always read on failure | coaching: what was wrong AND what to call/pass instead — one-round-trip self-correction |
| **Tool annotations** (readOnly/destructive) | used by clients for permission UX | reads flagged read-only (fewer prompts); delete/revoke flagged destructive |
| **Server `instructions`** | inconsistent across clients — may never be shown | bonus copy of the playbook; never the only home of a rule |
| **MCP prompts** | user-invoked only, where the client exposes them | the three personas (记账 / 对账 / 修复) |
| **Resources** | rarely auto-read | not used |

**Rule: if a behavior matters, it must be encoded in the top four rows.**

## Tool inventory (18) and why

| tool | why it exists / selection cue |
|---|---|
| `expenses_help` | playbook-as-a-tool: works on clients that never surface `instructions`; description says "START HERE when unsure" — agents do call help tools when confused |
| `expenses_list` | the one read for items AND totals ("我还要付什么", "花了多少"). A separate `expenses_summary` was **removed**: redundant read-only tools split selection probability and its output was already inside `list` |
| `expenses_history` | disputes/troubleshooting ("谁改的") |
| `expenses_add` | "足球课300块"; accepts already-paid in one call (paid=true) so "昨天交了300" isn't a two-step |
| `expenses_mark_paid` | "付了/交了/paid" — the highest-frequency write, so it gets fuzzy `query` targeting with unpaid-preference |
| `expenses_update` | corrections ("改成350"); cannot touch paid — error redirects to mark_paid |
| `expenses_delete` | mistakes only; destructive-flagged; description says confirm first and redirects "it's paid" to mark_paid |
| `expenses_mint_link` / `expenses_revoke_link` | link lifecycle ("给我老婆做个链接" / kill switch) |
| `expenses_list_links` | added v0.5.0 after live testing: revoke was **unreachable in practice** — the agent had no way to discover what to revoke, and `expenses_revoke_link` pointed it at the operator CLI, a channel an agent cannot use. Returns ids + usage, never token values, so revocation works without permanent secrets entering the chat. A cross-reference is only guidance if it names a tool the agent can actually call |
| `classes_list` | added v0.10.0 with the class tracker: "还剩几节课" is a different question from "what do I owe", and answering it from `expenses_list` would mean the agent doing the arithmetic itself — which is where wrong money comes from |
| `classes_add` | starts tracking a course FROM a payment already in the ledger. Deliberately cannot invent the expense: the money belongs to the ledger, and a package that carried its own amount would be a second place for the price to be wrong |
| `classes_log` | "今天上了/取消了/没去" — the high-frequency class write. Takes fuzzy `query` on the course name like every other mutating tool, and validates the event kind BEFORE resolving the course so a bad kind does not cost a round trip. Since v0.13.0 also `dates=[…]`: restoring a term was five round trips, and a batch that half-applies is a state nobody can reason about, so it is one transaction |
| `expenses_refund` | added v0.13.0 after a day when a half-refunded ¥3,600 course could only be expressed by deleting the payment and the course and rebuilding both — a new id, a new `created_at`, five attendance events "created" a month after their dates, and a ledger claiming he paid ¥1,800 on a day he paid ¥3,600. Records the refund as its own dated row; every read derives the effective amount. `resize_package_to` changes the course's class count in the SAME transaction, because a refund on a course nearly always means fewer classes and the two halves applied separately reprice a ¥360 class at ¥180 |
| `expenses_refund_delete` | the undo. Without it a mistaken refund would be fixed by deleting the whole payment, which is the rebuild the refund tool exists to prevent. Since v0.13.1 it undoes the WHOLE decision: a course the refund resized goes back to its previous class count (the refund row remembers both counts), unless something changed it since — the owner's live smoke found the half-undo leaving a pack at ¥200 a class that nobody chose |
| `classes_update` | "课时改成5 / archive it / rename it". The core miss of that day: `class_count` divides the money and had no in-place path. Refuses to shrink below the classes already logged and NAMES them, rather than silently zeroing what remains |
| `classes_delete` | so a course can be removed without the portal — the old delete-refusal pointed the agent at "the Classes tab", a surface it cannot reach (the same failure as `expenses_revoke_link` pointing at the CLI, one release later). The class log survives in the payment's history, which is what makes this safe to offer |
| `classes_log_delete` | one mislogged class had no undo except deleting the course. Event ids come from `classes_log`'s result and `classes_list(verbose=true)` — the default list is light because the full event array made `classes_list` the heaviest call on the server |

Principles: no two tools answer the same user intent; every mutating tool
takes `query` (fuzzy, candidates-on-ambiguity, never guesses); every write
returns the figure that answers the question it was called about — the unpaid
total for expenses, classes-and-money-left for a course; params tolerate what speech produces (numbers
or strings for amounts, omitted dates).

## Personas (MCP prompts)

Three, matching the owner's three usage modes. They set role, workflow, and
tone (reply in the user's language, one-line confirmations, confirm deletes):

- **记账 `jizhang`** — quick add: dictated expenses, minimal questions.
- **对账 `duizhang`** — settle up: walk the unpaid list, check off payments.
- **修复 `xiufu`** — fix a mistake: locate → disambiguate → correct → explain
  via history; never delete without asking.

Prompts are user-invoked (a picker in Claude apps); they are an accelerator,
not a dependency — the tools alone carry every rule needed for cold requests.

## Priority order

Owner's ranking: **availability-for-the-family > everything else**. A change
that risks forcing a reconnect is worse than a change that risks a bad ledger
entry. See FEATURE_CONTRACT §5.1 (compatibility contract) — mount path, URL
stability, and the no-header posture are frozen.

## Regression guardrails

`tests/test_mcp.py::AgentErgonomicsTests` pins all of this: bilingual triggers
present in descriptions, cross-references intact, annotations correct,
personas registered, numeric amounts accepted, one-call already-paid add,
notes on write results, and coaching text inside error strings. If an edit
moves guidance out of an agent-visible channel, a test fails.
