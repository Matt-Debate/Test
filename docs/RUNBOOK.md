# Runbook — Family Expenses

Operator guide: deploy, mint the household link, connect MCP clients, rotate.

Production service: `family-expenses` in `asia-southeast1` at
`https://family-expenses-bejtu5m47a-as.a.run.app`. Keep this service, region,
and URL stable after onboarding.

## 1. One-time setup

### Database (Neon)
Create a **separate** Neon project/database for family data (keeps it fully
apart from any business database). Copy the connection string
(`postgres://…`). The schema applies itself idempotently at service startup —
no manual migration step for v1.

### Deploy to Cloud Run
```bash
# after creating the dedicated service account and Secret Manager binding
scripts/deploy.sh --dry-run
scripts/deploy.sh
```
The script permanently pins service `family-expenses` in
`asia-southeast1` (Singapore, colocated with Neon), builds and deploys the
current clean Git SHA, and binds `DATABASE_URL` from
`family-expenses-database-url`. Production refuses to boot on SQLite.
Auth posture: links never expire and `/mcp` is open and header-free — that
part is frozen. The PORTAL may additionally sit behind Auth0 login (§8);
that is opt-in and does not touch `/mcp`. `APP_TZ` (default `Asia/Shanghai`) controls what "today"
means for default dates.

### Don't break her setup (the one rule that matters)
Once a family member's phone has the portal bookmark and/or an MCP connector,
these must stay stable across every future deploy:
- **Same Cloud Run service name + region** — that keeps the URL identical.
  Redeploying new code to the same service is always safe.
- **Never set `MCP_SECRET`** on a service her app points at — her connector
  has no header and would silently start failing. (The gate exists in code
  for a hypothetical abuse-response someday; enabling it means redoing her
  connector by hand and accepting that cost.)
- **Don't revoke her token** unless you mean to cut her off; mint+swap first.
- Freely changeable anytime: portal UI, tool descriptions, new tools, docs,
  additive schema changes. Test with your own connector before caring.

## 2. Mint the household link

Via MCP (from Claude, after §3): *"make a link for my wife"* →
`expenses_mint_link(label="wife")` — never expires.

Or via CLI against the same database:
```bash
DATABASE_URL='postgres://…' python3 scripts/mint_link.py \
  --label wife --base-url https://<service-url>
```
Send the printed `https://<service-url>/t/<token>` link over WeChat; she
bookmarks it. That's her entire onboarding — no account, no password, nothing
to renew, ever.

## 3. Connect MCP clients

- **Claude (claude.ai / Claude Code):** add a custom connector / MCP server
  with URL `https://<service-url>/mcp`. Do not add an authorization header.
- **ChatGPT (developer mode):** same URL, no authorization header.

Tools: `expenses_help`, `expenses_list`, `expenses_add`, `expenses_update`,
`expenses_mark_paid`, `expenses_delete`, `expenses_history`,
`expenses_mint_link`, `expenses_revoke_link`, `expenses_list_links`
(design: `docs/MCP_DESIGN.md`).

**Personas** (appear as prompt templates in Claude apps; optional):
记账 `jizhang` = dictate expenses; 对账 `duizhang` = walk the unpaid list and
check off; 修复 `xiufu` = find and fix a wrong entry. The tools alone handle
cold requests — personas just set the tone and workflow.

## 4. What you (or she) can say to it

The tools are built for casual speech — fuzzy matching by description, dates
defaulting to today (China time), amounts tolerating ¥/块/元/commas. All of
these work as single utterances, Chinese or English:

| say | happens |
|---|---|
| 我还要付什么？/ what do I owe? | lists unpaid + total |
| 足球课300块 / football class 300 | adds it, dated today |
| 足球课付了 / paid the football class | marks it paid today (prefers the unpaid match) |
| 钢琴课改成350 / change piano to 350 | edits the amount |
| 删掉游泳课 / delete swim class | deletes (client confirms first; audit row kept) |
| 这个月花了多少？/ totals? | summary |
| 给我老婆做个链接 / make a link for my wife | mints a never-expiring portal link |

If a phrase matches several expenses, the tool returns the candidates and the
assistant asks which one — nothing is guessed silently.

## 5. Rotate / revoke

- Lost or leaked link: `expenses_revoke_link` (or
  `scripts/mint_link.py --revoke <token-or-id>`), then mint a new one.
- Do not set or rotate an MCP secret; the no-header connector posture is a
  permanent compatibility constraint. If abuse ever requires changing it,
  treat that as an explicit owner-approved breaking operational change.
- Inspect links: `scripts/mint_link.py --list` (shows label, expiry, usage).

## 6. Operations notes

- **Backups:** rely on Neon's point-in-time restore; the `expense_history`
  table is additionally an application-level audit of every change.
- **Logs:** Cloud Run request logs; the app logs via uvicorn.
- **Health:** use `GET /health` in Cloud Run checks. `/healthz` is retained
  locally, but Google's front end reserves some paths ending in `z`.
- **Scale:** min-instances=0 is fine (stateless HTTP MCP; Neon serverless).
  Cold starts of a couple seconds are acceptable for this use.
- **Schema changes:** v1 applies `db/schema.sql` idempotently at startup.
  The first breaking change must introduce a dated migration file — see
  `docs/IMPLEMENTATION_PLAN.md`.

## 7. Local development

```bash
pip install -r requirements.txt
python3 -m unittest discover -s tests        # 373 tests, sqlite, no server
python3 scripts/mint_link.py --label dev     # local sqlite file
python3 -m app.main                          # http://localhost:8080
```

## 8. Portal OAuth (optional, off by default)

The portal can sit behind Auth0 Universal Login, layered on top of `/t/<token>`
— the token still selects the ledger, Auth0 establishes who is asking. Pattern
copied from work-dashboards (same JP tenant), but server-side session rather
than the SPA library: no build step here, and a CDN script tag is unreliable
from mainland China.

**It is inert until all four vars are set.** Deploy freely with them unset.

1. In Auth0 (`work-os.jp.auth0.com`), create a **Regular Web Application**
   (not SPA — this is a confidential client with a secret).
2. Set **Allowed Callback URLs** to `https://<service-url>/callback` and
   **Allowed Logout URLs** to `https://<service-url>`. Auth0 matches these
   exactly; a trailing-slash mismatch is the usual first failure.
3. Put the client secret in Secret Manager; never a literal env var.
4. Deploy with:
   ```
   AUTH0_DOMAIN=work-os.jp.auth0.com
   AUTH0_CLIENT_ID=<client id>
   AUTH0_CLIENT_SECRET=<from Secret Manager>
   SESSION_SECRET=<random 32+ bytes>
   PORTAL_ALLOWED_EMAILS=her@example.com,you@example.com
   PORTAL_BASE_URL=https://<service-url>
   ```

Notes that bite:
- `PORTAL_ALLOWED_EMAILS` empty = **nobody** gets in. Auth0 authenticates
  anyone who can create an account, so the allowlist is the real gate.
- `PORTAL_BASE_URL` avoids a redirect_uri built as `http://` behind Cloud Run's
  TLS termination, which Auth0 rejects.
- Rotating `SESSION_SECRET` signs everyone out.
- **Do not** use a Google social connection if she is in mainland China — it is
  blocked. Use an Auth0 database connection or email OTP.
- `/mcp` is unaffected: no login, `MCP_SECRET` still unset, connectors keep
  working.
- The login page carries WorkOS branding because branding is tenant-level and
  this tenant is shared with the admin app. Filed as `docs/BACKLOG.md` §1 —
  the fix is a separate Auth0 tenant, cheapest to do before onboarding.
