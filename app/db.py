"""Database layer — one portable interface over Postgres (prod) and sqlite
(tests/dev).

Driver selection (in order):
  * explicit ``url`` argument;
  * ``DATABASE_URL`` env var — ``postgres://``/``postgresql://`` → psycopg,
    ``sqlite:///path`` → sqlite;
  * fallback: local sqlite file ``family_expenses.db``.

SQL in the store is written once with ``:name`` parameters (sqlite's native
style) and translated to psycopg's ``%(name)s`` on the fly. The schema
(db/schema.sql) is portable DDL applied idempotently by :meth:`Database.init`.

Transactions: :meth:`Database.tx` yields a connection whose writes commit on
clean exit and roll back on any exception — the mechanism behind the
"expense write + history row are atomic" contract guarantee (M3).
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
_HARDENING_PATH = _SCHEMA_PATH.parent / "hardening.sql"

# :name → %(name)s. Negative lookbehind guards ``::`` casts (none in our SQL,
# but cheap insurance).
_PG_PARAM_RE = re.compile(r"(?<!:):([a-zA-Z_]\w*)")


def _to_pg(sql: str) -> str:
    return _PG_PARAM_RE.sub(r"%(\1)s", sql)


class Database:
    """Thin driver-agnostic wrapper. One instance per process."""

    def __init__(self, url: str | None = None):
        configured_url = url or os.environ.get("DATABASE_URL")
        if os.environ.get("K_SERVICE"):
            if not configured_url:
                raise RuntimeError(
                    "DATABASE_URL is required on Cloud Run; refusing ephemeral SQLite fallback"
                )
            if not configured_url.startswith(("postgres://", "postgresql://")):
                raise RuntimeError(
                    "Cloud Run requires a Postgres DATABASE_URL; refusing SQLite storage"
                )
        self.url = configured_url or "sqlite:///family_expenses.db"
        self.is_pg = self.url.startswith(("postgres://", "postgresql://"))
        self._local = threading.local()  # Postgres: one connection per thread
        self._lock = threading.RLock()   # sqlite: one shared connection, serialized
        self._sqlite_conn: sqlite3.Connection | None = None

    # ── connections ───────────────────────────────────────────────────────
    def _connect(self):
        if self.is_pg:
            import psycopg
            from psycopg.rows import dict_row

            return psycopg.connect(self.url, row_factory=dict_row)
        path = self.url[len("sqlite:///"):] if self.url.startswith("sqlite:///") else self.url
        # Single shared connection: an in-memory sqlite DB is per-connection,
        # and web servers dispatch requests across threads — per-thread
        # connections would each see their own (empty) database. All sqlite
        # transactions are serialized by self._lock instead.
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _conn(self):
        if self.is_pg:
            conn = getattr(self._local, "conn", None)
            if conn is None or conn.closed:
                if conn is not None:
                    self._discard_pg_connection(conn)
                conn = self._connect()
                self._local.conn = conn
            return conn
        with self._lock:
            if self._sqlite_conn is None:
                self._sqlite_conn = self._connect()
            return self._sqlite_conn

    def _discard_pg_connection(self, conn) -> None:
        """Evict a failed thread-local connection without masking its error."""
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None
        try:
            conn.close()
        except Exception:
            pass

    def _replace_pg_connection(self, conn):
        self._discard_pg_connection(conn)
        fresh = self._connect()
        self._local.conn = fresh
        return fresh

    def close(self) -> None:
        if self.is_pg:
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None
            return
        with self._lock:
            if self._sqlite_conn is not None:
                self._sqlite_conn.close()
                self._sqlite_conn = None

    # ── transactions ──────────────────────────────────────────────────────
    @contextmanager
    def tx(self):
        """Yield a :class:`_Tx`; commit on success, roll back on exception.

        sqlite transactions hold the process-wide lock for their duration so
        concurrent request threads serialize instead of interleaving writes.
        """
        if self.is_pg:
            tx = _Tx(self._conn(), True, database=self)
            try:
                yield tx
                tx.connection.commit()
            except BaseException as exc:
                try:
                    tx.connection.rollback()
                except BaseException:
                    # A dead connection commonly raises again during rollback.
                    # Preserve the original error and ensure the next request
                    # cannot reuse the corpse.
                    self._discard_pg_connection(tx.connection)
                if _is_pg_connection_error(exc):
                    self._discard_pg_connection(tx.connection)
                raise
            return
        with self._lock:
            conn = self._conn()
            try:
                yield _Tx(conn, False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    # ── schema ────────────────────────────────────────────────────────────
    def init(self, schema_path: Path | str = _SCHEMA_PATH) -> None:
        """Apply the idempotent schema (CREATE TABLE IF NOT EXISTS ...)."""
        script = Path(schema_path).read_text(encoding="utf-8")
        if self.is_pg:
            with self.tx() as tx:
                tx.execute(script)  # psycopg: multi-statement OK without params
        else:
            conn = self._conn()
            try:
                conn.executescript(script)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        self._migrate_history_actions()
        failed = self._apply_hardening()
        if failed:
            # the constraint's whole value is turning silent corruption into a
            # loud error; if it could not be applied, that must not itself be
            # silent. One line per startup, greppable in the Cloud Run logs.
            import sys

            print(
                f"WARNING: {len(failed)} constraint(s) in db/hardening.sql are "
                "NOT in force — the audit-order guarantee is unprotected until "
                "the underlying data is repaired",
                file=sys.stderr,
            )

    # ── migrations ────────────────────────────────────────────────────────
    def history_actions_missing(self) -> list[str]:
        """The HISTORY_ACTIONS the live CHECK constraint does not yet allow.

        Empty means the audit table accepts every action the store can write.
        Read by the migration to decide whether to run, and by tests to prove
        it ran; both drivers are inspected through their own catalog.
        """
        from .models import HISTORY_ACTIONS

        if self.is_pg:
            with self.tx() as tx:
                rows = tx.query(
                    "SELECT pg_get_constraintdef(c.oid) AS def FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'expense_history' AND c.contype = 'c'"
                )
            defs = [r["def"] for r in rows if "action" in (r["def"] or "")]
        else:
            with self.tx() as tx:
                row = tx.query_one(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'expense_history'"
                )
            defs = [row["sql"]] if row and row["sql"] else []
        if not defs:
            return []  # no CHECK on action at all: nothing to widen
        text = " ".join(defs)
        return [a for a in HISTORY_ACTIONS if f"'{a}'" not in text]

    def _migrate_history_actions(self) -> None:
        """Widen expense_history's action CHECK to HISTORY_ACTIONS, once.

        The first change this project has made to a live table that
        ``CREATE TABLE IF NOT EXISTS`` cannot express. It is driven by
        inspection rather than by a version number, so it is idempotent and
        needs no migrations table: if every action is already allowed, nothing
        runs. Best-effort like the hardening file — a live portal must boot —
        but a failure here is not silent: the store refuses the first refund
        with a message naming this step, so nothing half-writes.

        Postgres: drop the inline CHECK (auto-named ``<table>_<column>_check``,
        but looked up rather than assumed) and add the wider one, in one
        transaction. sqlite cannot alter a constraint at all, so the table is
        rebuilt in place — the standard sqlite pattern — inside one explicit
        BEGIN/COMMIT so a failure leaves the old table untouched.
        """
        import sys

        from .models import HISTORY_ACTIONS

        try:
            missing = self.history_actions_missing()
            if not missing:
                return
            literals = ", ".join(f"'{a}'" for a in HISTORY_ACTIONS)
            if self.is_pg:
                with self.tx() as tx:
                    rows = tx.query(
                        "SELECT c.conname, pg_get_constraintdef(c.oid) AS def "
                        "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                        "WHERE t.relname = 'expense_history' AND c.contype = 'c'"
                    )
                    for r in rows:
                        if "action" in (r["def"] or ""):
                            name = str(r["conname"]).replace('"', '""')
                            tx.execute(
                                f'ALTER TABLE expense_history DROP CONSTRAINT "{name}"'
                            )
                    tx.execute(
                        "ALTER TABLE expense_history ADD CONSTRAINT "
                        "expense_history_action_check "
                        f"CHECK (action IN ({literals}))"
                    )
            else:
                conn = self._conn()
                with self._lock:
                    try:
                        conn.executescript(
                            "BEGIN;\n"
                            "DROP TABLE IF EXISTS expense_history__new;\n"
                            "CREATE TABLE expense_history__new (\n"
                            "  id TEXT PRIMARY KEY, expense_id TEXT NOT NULL,\n"
                            "  seq INTEGER NOT NULL,\n"
                            f"  action TEXT NOT NULL CHECK (action IN ({literals})),\n"
                            "  changed_by TEXT, changed_at TEXT NOT NULL,\n"
                            "  snapshot TEXT NOT NULL);\n"
                            "INSERT INTO expense_history__new "
                            "(id, expense_id, seq, action, changed_by, changed_at, snapshot) "
                            "SELECT id, expense_id, seq, action, changed_by, changed_at, snapshot "
                            "FROM expense_history;\n"
                            "DROP TABLE expense_history;\n"
                            "ALTER TABLE expense_history__new RENAME TO expense_history;\n"
                            "CREATE INDEX IF NOT EXISTS idx_expense_history_expense "
                            "ON expense_history(expense_id, seq);\n"
                            "COMMIT;"
                        )
                    except BaseException:
                        conn.rollback()
                        raise
            still = self.history_actions_missing()
            if still:
                raise RuntimeError(f"constraint still rejects {still}")
        except Exception as exc:
            print(
                f"WARNING: expense_history action migration did not apply ({exc}); "
                "refunds and class-tracker audit rows will be REFUSED until it does",
                file=sys.stderr,
            )

    def _apply_hardening(self, path: Path | str = _HARDENING_PATH) -> list[str]:
        """Apply db/hardening.sql best-effort; return the statements that failed.

        These constraints can legitimately fail against pre-existing data. This
        is a live portal one family member depends on, so a constraint that
        cannot be applied is a warning in the logs — never a service that
        refuses to start. Each statement gets its own transaction so one
        failure does not roll back the others.
        """
        import sys

        path = Path(path)
        try:
            statements = _sql_statements(path.read_text(encoding="utf-8"))
        except Exception as exc:
            # reading/parsing the file is part of "best effort" too — a missing
            # or unreadable hardening.sql must not be able to stop the service
            print(f"WARNING: could not read {path} ({exc})", file=sys.stderr)
            return []
        failed: list[str] = []
        for statement in statements:
            try:
                with self.tx() as tx:
                    tx.execute(statement)
            except Exception as exc:
                failed.append(statement)
                print(
                    f"WARNING: could not apply constraint ({exc}); "
                    f"statement: {' '.join(statement.split())[:120]}",
                    file=sys.stderr,
                )
        return failed


def _sql_statements(script: str) -> list[str]:
    """Split a comment-annotated DDL script into individual statements.

    Deliberately naive — it only has to handle db/hardening.sql, which is
    CREATE INDEX statements and `--` comments. No string literals, no
    semicolons inside identifiers.
    """
    stripped = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("--")
    )
    return [s.strip() for s in stripped.split(";") if s.strip()]


def _is_pg_connection_error(exc: BaseException) -> bool:
    import psycopg

    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


class _Tx:
    """Cursor facade bound to one in-flight transaction."""

    def __init__(self, conn, is_pg: bool, database: Database | None = None):
        self._conn = conn
        self._is_pg = is_pg
        self._database = database
        self._executed = False

    @property
    def connection(self):
        return self._conn

    def execute(self, sql: str, params: dict | None = None):
        if self._is_pg:
            sql = _to_pg(sql)
            try:
                result = self._conn.execute(sql, params or {})
            except BaseException as exc:
                if not _is_pg_connection_error(exc):
                    raise
                if self._executed or self._database is None:
                    if self._database is not None:
                        self._database._discard_pg_connection(self._conn)
                    raise
                # Long-idle pooler disconnects surface on the first statement.
                # No statement has succeeded, so replaying this one statement
                # once on a fresh connection is transaction-safe.
                self._conn = self._database._replace_pg_connection(self._conn)
                result = self._conn.execute(sql, params or {})
            self._executed = True
            return result
        return self._conn.execute(sql, params or {})

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        cur = self.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: dict | None = None) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None
