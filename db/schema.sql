-- Family Expenses — schema (v0.13.0)
--
-- PORTABLE DDL: runs unmodified on Postgres (Neon, production) and sqlite
-- (tests). Rules that keep it portable:
--   * application-managed ISO-8601 UTC timestamps (TEXT) — no triggers,
--     no now()/to_char defaults;
--   * JSON audit snapshots stored as TEXT;
--   * no arrays, no PG-only expressions or casts;
--   * idempotent (CREATE TABLE IF NOT EXISTS) — applied at startup; dated
--     migration files begin only when a breaking change first appears.

CREATE TABLE IF NOT EXISTS expenses (
  id            TEXT PRIMARY KEY,
  date          TEXT NOT NULL,
  amount        REAL NOT NULL CHECK (amount > 0),
  currency      TEXT NOT NULL DEFAULT 'CNY',
  category      TEXT,
  description   TEXT,
  paid          BOOLEAN NOT NULL DEFAULT FALSE,
  paid_date     TEXT,
  submitted_by  TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  CONSTRAINT expenses_paid_date_check CHECK (
    paid = FALSE OR (paid_date IS NOT NULL AND paid_date <> '')
  )
);
CREATE INDEX IF NOT EXISTS idx_expenses_paid ON expenses(paid);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(date);

-- Append-only audit: one row per mutation; never updated or deleted.
--
-- Since v0.13.0 the trail also carries refunds and the class tracker's own
-- mutations, keyed by the payment that funds the course — a package is 1:1
-- with its expense and can never be re-pointed, so the expense id is a stable
-- home for "what happened to this course". The action list is mirrored in
-- app/models.py HISTORY_ACTIONS; a parity test fails if the two drift, and
-- Database._migrate_history_actions widens the CHECK on a database that
-- predates an entry (this is the one constraint IF NOT EXISTS cannot change).
CREATE TABLE IF NOT EXISTS expense_history (
  id          TEXT PRIMARY KEY,
  expense_id  TEXT NOT NULL,
  -- monotonic per-expense sequence (0-based); second-resolution timestamps
  -- alone cannot order same-second mutations deterministically
  seq         INTEGER NOT NULL,
  action      TEXT NOT NULL CHECK (
                action IN ('create', 'update', 'mark_paid', 'unmark_paid', 'delete',
                           'refund', 'refund_delete',
                           'package_create', 'package_update', 'package_delete',
                           'class_log', 'class_unlog')
              ),
  changed_by  TEXT,
  changed_at  TEXT NOT NULL,
  snapshot    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expense_history_expense
  ON expense_history(expense_id, seq);

-- ── refunds (v0.13.0) ────────────────────────────────────────────────────
-- Money that came BACK on a payment. The payment row keeps its original
-- amount — a refund is a second fact with its own date, not a correction of
-- the first — and every read derives the effective figure as
-- amount − SUM(refunds). Writing the payment down instead is what this table
-- replaces: it claimed he paid ¥1,800 on a day he paid ¥3,600.
--
-- ON DELETE CASCADE: a refund has no life apart from its payment, and the
-- application's delete snapshots the refunds into history before the row
-- goes. (Contrast class_packages, whose FK is there to REFUSE the delete.)
CREATE TABLE IF NOT EXISTS expense_refunds (
  id          TEXT PRIMARY KEY,
  expense_id  TEXT NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
  amount      REAL NOT NULL CHECK (amount > 0),
  date        TEXT NOT NULL,
  reason      TEXT,
  changed_by  TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expense_refunds_expense ON expense_refunds(expense_id);

-- Bookmarkable household links (pattern adapted from work-dashboards
-- portal_tokens, minus tenancy/scoping). expires_at NULL = never expires —
-- the household default; the holder never renews anything. Revocation is the
-- kill switch.
CREATE TABLE IF NOT EXISTS access_tokens (
  id            TEXT PRIMARY KEY,
  token         TEXT NOT NULL UNIQUE,
  label         TEXT,
  expires_at    TEXT,
  revoked       BOOLEAN NOT NULL DEFAULT FALSE,
  created_at    TEXT NOT NULL,
  last_used_at  TEXT,
  use_count     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_access_tokens_token ON access_tokens(token);

-- ── class tracker (v0.10.0) ──────────────────────────────────────────────
-- A package is a course paid for by exactly ONE expense row, and it stores no
-- money of its own. The per-class rate is derived at read time from
-- expenses.amount / class_count, so correcting the payment corrects the
-- tracker and the two can never disagree — the same reason Store.summarize is
-- the only totals implementation for expenses.
--
-- expense_id is UNIQUE for that reason: two packages on one payment would each
-- claim the whole amount and silently double-count the money.
CREATE TABLE IF NOT EXISTS class_packages (
  id            TEXT PRIMARY KEY,
  -- FK, not just a convention: the application refuses to delete a funding
  -- payment, but that check and the delete are not one atomic step under
  -- Postgres. Without this the loser of that race commits an orphan, and an
  -- orphan vanishes from every read (the package SELECT inner-joins expenses).
  expense_id    TEXT NOT NULL UNIQUE REFERENCES expenses(id),
  name          TEXT NOT NULL,
  -- 'per_class' — a pack of N classes; each attendance consumes one.
  -- 'period'    — a flat month/semester fee; each missed class is owed back.
  kind          TEXT NOT NULL CHECK (kind IN ('per_class', 'period')),
  class_count   INTEGER NOT NULL CHECK (class_count > 0),
  period_label  TEXT,
  archived      BOOLEAN NOT NULL DEFAULT FALSE,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_class_packages_expense ON class_packages(expense_id);

-- One row per class that happened or didn't. 'missed_school' and 'missed_us'
-- are both owed back on a period package, but reported apart: what the school
-- cancelled is reclaimable, what we skipped is what we forfeited.
CREATE TABLE IF NOT EXISTS class_events (
  id          TEXT PRIMARY KEY,
  package_id  TEXT NOT NULL REFERENCES class_packages(id) ON DELETE CASCADE,
  date        TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (
                kind IN ('attended', 'missed_school', 'missed_us')
              ),
  note        TEXT,
  logged_by   TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_class_events_package ON class_events(package_id, date);
