# Family Expenses

A tiny, private household expense portal. One bookmarkable link for a family
member to **submit and edit expenses that need paying** (with a "mark paid on
date" check-off and a full edit history), plus an **MCP server** so the owner
can query the ledger from Claude or ChatGPT.

Built to replace free-text WeChat messages — not a business system.

## How it fits together

- **Portal** — one mobile-first page (中文 default / English toggle) served at
  `/t/<token>`. Anyone with the link can add, edit, and mark expenses paid; no
  accounts. Every change is recorded in an append-only history.
- **Store** — Postgres (Neon) in production; the same portable SQL runs the
  test suite on sqlite with no database server.
- **MCP** — streamable-HTTP server (Python `mcp` SDK) on Cloud Run: 18 tools
  built for casual speech (fuzzy targeting, coached errors, bilingual
  triggers) plus 记账/对账/修复 persona prompts — design rationale in
  `docs/MCP_DESIGN.md`.
- **Access** — deliberately minimal, matching the owner's threat model: the
  portal link is a random 64-hex URL that **never expires** (the holder never
  renews anything; revocation is the kill switch), and the MCP endpoint is
  open unless `MCP_SECRET` is set. Nothing here is sensitive beyond a
  household ledger.

## Repository layout

| path | contents |
|---|---|
| `db/schema.sql` | portable DDL (Postgres + sqlite), applied idempotently at startup |
| `app/` | store, web portal, MCP server |
| `tests/` | suite runs on sqlite — no live DB needed |
| `scripts/` | operator tooling (mint links) |
| `docs/` | feature contract, implementation plan, changelog, runbook |

## Quick start

```bash
pip install -r requirements.txt
python3 -m unittest discover -s tests        # 498 tests, sqlite, no DB server
python3 -m app.main                          # http://localhost:8080
python3 scripts/mint_link.py --label wife --base-url http://localhost:8080
```

Deploying to Cloud Run + Neon, minting the real link, and connecting
Claude/ChatGPT to the MCP: see **`docs/RUNBOOK.md`**.

## Status

**v0.13.0** in the repo; **in daily household use since 2026-08-11** on Cloud
Run + Neon (the deployed revision is recorded in `docs/FEATURE_CONTRACT.md`).
Four-tab portal behind Auth0 login with refunds and course editing on her
side; an 18-tool MCP, open and header-free, on the owner's. Version history in
`docs/CHANGELOG.md`; known deferred issues in `docs/BACKLOG.md`.
Default branch: `main`.
