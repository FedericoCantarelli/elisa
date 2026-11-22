"""Persistence: the SQLite database and the ledger log.

The database is where `el` keeps its data. It holds every transaction that
ever happened, plus the current state of accounts and budgets:

    transactions(id, timestamp, operation, target, amount, currency, description)
    accounts(position, name, currency, balance, deleted)
    budgets(position, name, currency, max_amount, spent, deleted, description)
    meta(key, value)

`operation` is CREATE ACCOUNT, CREATE BUDGET, DELETE ACCOUNT, DELETE BUDGET,
ADD or SUB. `amount` is signed: an ADD is positive and a SUB negative. All
amounts are stored as text, such as '12.50', so they stay exact.

The ledger log (see `elisa.ledger`) is an append-only record of the same
atomic actions. Every transaction is written to both, in one unit, so the
whole database can be rebuilt from the log at any time: `el` does it when
the database is missing, and `el rebuild` does it on request. `meta` holds
the size and SHA-256 of the log as it was after the last write, so `el` can
tell when the log was changed outside of it.
"""

import copy
import hashlib
import sqlite3
from collections.abc import Callable
from collections.abc import Generator
from contextlib import closing
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from elisa.ledger import Account
from elisa.ledger import Add
from elisa.ledger import Budget
from elisa.ledger import CreateAccount
from elisa.ledger import CreateBudget
from elisa.ledger import DeleteAccount
from elisa.ledger import DeleteBudget
from elisa.ledger import ElError
from elisa.ledger import Ledger
from elisa.ledger import Operation
from elisa.ledger import now_utc
from elisa.ledger import parse_log_line


SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    operation   TEXT NOT NULL,
    target      TEXT NOT NULL,
    amount      TEXT,
    currency    TEXT,
    description TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    position INTEGER NOT NULL,
    name     TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    balance  TEXT NOT NULL,
    deleted  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS budgets (
    position   INTEGER NOT NULL,
    name       TEXT PRIMARY KEY,
    currency   TEXT NOT NULL,
    max_amount TEXT NOT NULL,
    spent       TEXT NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

INSERT_TRANSACTION = """
INSERT INTO transactions (timestamp, operation, target, amount, currency, description)
VALUES (?, ?, ?, ?, ?, ?)
"""

UPSERT_ACCOUNT = """
INSERT INTO accounts (position, name, currency, balance, deleted)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (name) DO UPDATE SET
    position = excluded.position,
    currency = excluded.currency,
    balance = excluded.balance,
    deleted = excluded.deleted
"""

UPSERT_BUDGET = """
INSERT INTO budgets (position, name, currency, max_amount, spent, deleted, description)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (name) DO UPDATE SET
    position = excluded.position,
    currency = excluded.currency,
    max_amount = excluded.max_amount,
    spent = excluded.spent,
    deleted = excluded.deleted,
    description = excluded.description
"""

UPSERT_META = "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)"

LogState = tuple[int, str]
"""Size in bytes and SHA-256 hex digest of the log file."""

Record = tuple[str, Operation]
"""A transaction: its UTC timestamp and the operation."""


class Database:
    """The SQLite file where el keeps its transactions and state."""

    def __init__(self, path: Path) -> None:
        """Open the database at `path`, creating it and its tables if needed."""
        self.path = path
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn:
                conn.executescript(SCHEMA)
                _add_missing_columns(conn)
        except (OSError, sqlite3.Error) as exc:
            raise self._error("cannot open", exc) from None

    def _connect(self) -> sqlite3.Connection:
        # no implicit transactions: save() opens its own with BEGIN IMMEDIATE
        return sqlite3.connect(self.path, isolation_level=None)

    def _error(self, action: str, exc: Exception) -> ElError:
        return ElError(f"{action} database {self.path}: {exc}")

    def log_state(self) -> LogState | None:
        """Return the log size and digest after the last write, if any."""
        try:
            with closing(self._connect()) as conn:
                rows = dict(conn.execute("SELECT key, value FROM meta"))
        except sqlite3.Error as exc:
            raise self._error("cannot read", exc) from None
        if "log_size" not in rows or "log_sha256" not in rows:
            return None
        return int(rows["log_size"]), rows["log_sha256"]

    def load(self) -> Ledger:
        """Return the current state stored in the database."""
        ledger = Ledger()
        try:
            with closing(self._connect()) as conn:
                for name, currency, balance, deleted in conn.execute(
                    "SELECT name, currency, balance, deleted FROM accounts"
                    " ORDER BY position"
                ):
                    ledger.accounts[name] = Account(
                        name, currency, Decimal(balance), bool(deleted)
                    )
                for name, currency, max_amount, spent, deleted, desc in conn.execute(
                    "SELECT name, currency, max_amount, spent, deleted, description"
                    " FROM budgets ORDER BY position"
                ):
                    ledger.budgets[name] = Budget(
                        name,
                        currency,
                        Decimal(max_amount),
                        Decimal(spent),
                        bool(deleted),
                        desc,
                    )
        except sqlite3.Error as exc:
            raise self._error("cannot read", exc) from None
        return ledger

    def save(
        self,
        ledger: Ledger,
        records: list[Record],
        write_log: Callable[[], LogState],
        rebuild: bool = False,
    ) -> None:
        """Store new transactions and the new state, and append to the log.

        The rows are written inside a database transaction, then
        `write_log` appends to the log, then the transaction is committed.
        If anything before the commit fails, the database is rolled back and
        the error is raised, so neither file changes. If only the commit
        fails, the log holds the change and the database does not; `el`
        notices the mismatch on the next start and `el rebuild` fixes it.

        Args:
            ledger: The state after the new transactions.
            records: The new transactions, in order.
            write_log: Appends the matching lines to the log and returns the
                new log state. When rebuilding, it only reads the state.
            rebuild: Remove every row first, used when replaying the log.

        Raises:
            ElError: If the database or the log cannot be written.
        """
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise self._error("cannot open", exc) from None
        with closing(conn):
            try:
                # take the write lock now, so that another el waits for us
                conn.execute("BEGIN IMMEDIATE")
                if rebuild:
                    for table in ("transactions", "accounts", "budgets", "meta"):
                        conn.execute(f"DELETE FROM {table}")
                    conn.execute("DELETE FROM sqlite_sequence")
                conn.executemany(
                    INSERT_TRANSACTION,
                    [_transaction_row(ledger, stamp, op) for stamp, op in records],
                )
                conn.executemany(UPSERT_ACCOUNT, _account_rows(ledger))
                conn.executemany(UPSERT_BUDGET, _budget_rows(ledger))
            except sqlite3.Error as exc:
                conn.rollback()
                raise self._error("cannot write", exc) from None
            try:
                size, digest = write_log()
            except BaseException:
                conn.rollback()
                raise
            try:
                conn.executemany(
                    UPSERT_META, [("log_size", str(size)), ("log_sha256", digest)]
                )
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise ElError(
                    f"saved to the log but not to database {self.path}: {exc}"
                    " (run `el rebuild` to fix it)"
                ) from None


def _transaction_row(
    ledger: Ledger, stamp: str, op: Operation
) -> tuple[str, str, str, str | None, str | None, str | None]:
    match op:
        case CreateAccount(name, balance, currency):
            return stamp, "CREATE ACCOUNT", name, str(balance), currency, None
        case CreateBudget(name, max_amount, currency, description):
            desc = description or None
            return stamp, "CREATE BUDGET", name, str(max_amount), currency, desc
        case DeleteAccount(name):
            return stamp, "DELETE ACCOUNT", name, None, None, None
        case DeleteBudget(name):
            return stamp, "DELETE BUDGET", name, None, None, None
        case Add(target, amount, description):
            entity = ledger.accounts.get(target) or ledger.budgets[target]
            verb = "SUB" if amount < 0 else "ADD"
            return stamp, verb, target, str(amount), entity.currency, description
    raise TypeError(f"unknown operation {op!r}")


def _account_rows(ledger: Ledger) -> list[tuple[int, str, str, str, int]]:
    return [
        (i, a.name, a.currency, str(a.balance), int(a.deleted))
        for i, a in enumerate(ledger.accounts.values())
    ]


def _budget_rows(ledger: Ledger) -> list[tuple[int, str, str, str, str, int, str]]:
    return [
        (
            i,
            b.name,
            b.currency,
            str(b.max_amount),
            str(b.spent),
            int(b.deleted),
            b.description,
        )
        for i, b in enumerate(ledger.budgets.values())
    ]


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Add columns that databases made by older versions of el lack."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(budgets)")}
    if "description" not in columns:
        conn.execute(
            "ALTER TABLE budgets ADD COLUMN description TEXT NOT NULL DEFAULT ''"
        )


def read_log_state(path: Path) -> LogState:
    """Return the size and SHA-256 of the log, (0, digest of b"") if missing."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    except OSError as exc:
        raise ElError(f"cannot read {path}: {exc.strerror}") from None
    return len(data), hashlib.sha256(data).hexdigest()


class Journal:
    """A ledger stored in the SQLite database and mirrored in the ledger log."""

    def __init__(self, path: Path, db_path: Path) -> None:
        """Load the state from the database.

        A new or empty database is first built from the log, if there is one.
        `log_changed` is set when the log no longer matches what the database
        last wrote, for example because it was edited by hand.
        """
        self.path = path
        self.db = Database(db_path)
        self._pending: list[Record] | None = None
        stored = self.db.log_state()
        if stored is None:
            self.rebuild()
            self.log_changed = False
        else:
            self.ledger = self.db.load()
            self.log_changed = stored != read_log_state(self.path)

    def rebuild(self) -> None:
        """Replace everything in the database with a replay of the log."""
        ledger, records = self._replay()
        state = read_log_state(self.path)
        self.db.save(ledger, records, lambda: state, rebuild=True)
        self.ledger = ledger
        self.log_changed = False

    def _replay(self) -> tuple[Ledger, list[Record]]:
        ledger = Ledger()
        records: list[Record] = []
        if not self.path.exists():
            return ledger, records
        with self.path.open(encoding="utf-8") as fh:
            for number, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    stamp, op = parse_log_line(line)
                    ledger.apply(op)
                except ElError as exc:
                    raise ElError(f"{self.path}:{number}: {exc}") from None
                records.append((stamp, op))
        return ledger, records

    def commit(self, ops: list[Operation]) -> None:
        """Check all operations, then apply them and save them.

        Either every operation is applied or none is, so an MV never
        leaves half of itself in the database or the log. Inside a
        transaction nothing is saved until the transaction ends.
        """
        self.ledger.check(ops)
        stamp = now_utc()
        records = [(stamp, op) for op in ops]
        saved = copy.deepcopy(self.ledger)
        for op in ops:
            self.ledger.apply(op)
        if self._pending is not None:
            self._pending.extend(records)
            return
        try:
            self._save(records)
        except BaseException:
            self.ledger = saved
            raise

    @contextmanager
    def transaction(self) -> Generator[None]:
        """Group commits so that they are all saved, or none is.

        If the block raises, or saving fails, the in-memory state is
        restored and neither the database nor the log changes.
        """
        saved = copy.deepcopy(self.ledger)
        self._pending = []
        try:
            yield
            self._save(self._pending)
        except BaseException:
            self.ledger = saved
            raise
        finally:
            self._pending = None

    def _save(self, records: list[Record]) -> None:
        if records:
            lines = [f"{stamp} {op.to_line()}\n" for stamp, op in records]
            self.db.save(self.ledger, records, lambda: self._append(lines))

    def _append(self, lines: list[str]) -> LogState:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write("".join(lines))
        except OSError as exc:
            raise ElError(f"cannot write {self.path}: {exc.strerror}") from None
        return read_log_state(self.path)
