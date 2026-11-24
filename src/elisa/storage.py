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
tell when the log was changed outside of it; until it is rebuilt, `el` refuses
to write.

Writes happen under the SQLite write lock (`Database.write_lock`), which also
serialises appends to the log. Inside it `Journal` reloads the state from the
database, applies the operations, writes the rows, appends and fsyncs the log
lines, and then commits. A failed commit cuts the lines from the log again.
A write cut short by a crash leaves bytes after the committed log size that
don't end with a newline; they are moved to `<log>.interrupted`.

`Journal.transactions(name)` returns the history of one account or budget,
including commits of a running transaction that are not saved yet.
"""

import copy
import hashlib
import os
import sqlite3
from collections.abc import Generator
from contextlib import closing
from contextlib import contextmanager
from contextlib import suppress
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

TransactionRow = tuple[str, str, str | None, str | None, str | None]
"""A stored transaction: timestamp, operation, amount, currency, description."""


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

    @contextmanager
    def _reader(self, conn: sqlite3.Connection | None) -> Generator[sqlite3.Connection]:
        """Use `conn` if given, else a connection of our own."""
        if conn is not None:
            yield conn
            return
        with closing(self._connect()) as own:
            yield own

    def log_state(self, conn: sqlite3.Connection | None = None) -> LogState | None:
        """Return the log size and digest after the last write, if any."""
        try:
            with self._reader(conn) as c:
                rows = dict(c.execute("SELECT key, value FROM meta"))
        except sqlite3.Error as exc:
            raise self._error("cannot read", exc) from None
        if "log_size" not in rows or "log_sha256" not in rows:
            return None
        return int(rows["log_size"]), rows["log_sha256"]

    def load(self, conn: sqlite3.Connection | None = None) -> Ledger:
        """Return the current state stored in the database."""
        ledger = Ledger()
        try:
            with self._reader(conn) as c:
                for name, currency, balance, deleted in c.execute(
                    "SELECT name, currency, balance, deleted FROM accounts"
                    " ORDER BY position"
                ):
                    ledger.accounts[name] = Account(
                        name, currency, Decimal(balance), bool(deleted)
                    )
                for name, currency, max_amount, spent, deleted, desc in c.execute(
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

    def transactions(self, target: str) -> list[TransactionRow]:
        """Return every stored transaction on `target`, oldest first."""
        try:
            with closing(self._connect()) as conn:
                return list(
                    conn.execute(
                        "SELECT timestamp, operation, amount, currency, description"
                        " FROM transactions WHERE target = ? ORDER BY id",
                        (target,),
                    )
                )
        except sqlite3.Error as exc:
            raise self._error("cannot read", exc) from None

    @contextmanager
    def write_lock(self) -> Generator[sqlite3.Connection]:
        """Hold the database write lock for the duration of the block.

        Another el process that wants to write waits until the block ends.
        Unless the block committed, everything it wrote is rolled back.
        """
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise self._error("cannot open", exc) from None
        with closing(conn):
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise self._error("cannot lock", exc) from None
            try:
                yield conn
            finally:
                if conn.in_transaction:
                    conn.rollback()

    def store(
        self,
        conn: sqlite3.Connection,
        ledger: Ledger,
        records: list[Record],
        rebuild: bool = False,
    ) -> None:
        """Write new transactions and the new state, without committing.

        Args:
            conn: A connection holding the write lock.
            ledger: The state after the new transactions.
            records: The new transactions, in order.
            rebuild: Remove every row first, used when replaying the log.

        Raises:
            ElError: If the rows cannot be written.
        """
        try:
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
            raise self._error("cannot write", exc) from None

    @staticmethod
    def commit(conn: sqlite3.Connection, state: LogState) -> None:
        """Record the new log state and commit; sqlite3.Error is left to the caller."""
        conn.executemany(
            UPSERT_META, [("log_size", str(state[0])), ("log_sha256", state[1])]
        )
        conn.commit()


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


def _read_bytes(path: Path) -> bytes:
    """Return the contents of the log, b"" if it doesn't exist."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise ElError(f"cannot read {path}: {exc.strerror}") from None


def _state_of(data: bytes) -> LogState:
    return len(data), hashlib.sha256(data).hexdigest()


def read_log_state(path: Path) -> LogState:
    """Return the size and SHA-256 of the log, (0, digest of b"") if missing."""
    return _state_of(_read_bytes(path))


class Journal:
    """A ledger stored in the SQLite database and mirrored in the ledger log.

    Every write happens under the database write lock: the state is reloaded
    from the database, the operations are applied to it, the rows are
    written, the log lines are appended and synced, and only then the
    database is committed. So two el processes never work from stale state,
    and the log and the database change together.
    """

    def __init__(self, path: Path, db_path: Path) -> None:
        """Load the state from the database.

        A new or empty database is first built from the log, if there is one.
        An interrupted write at the end of the log is moved aside (see
        `recovered`). `log_changed` is set when the log no longer matches what
        the database last wrote, for example because it was edited by hand.
        """
        self.path = path
        self.db = Database(db_path)
        self._pending: list[Record] | None = None
        # where an interrupted write at the end of the log was moved to
        self.recovered: Path | None = None
        stored = self.db.log_state()
        if stored is None:
            self.rebuild()
        else:
            if stored != read_log_state(self.path):
                with self.db.write_lock() as conn:
                    self._recover(self.db.log_state(conn))
            self.ledger = self.db.load()
            self.log_changed = self.db.log_state() != read_log_state(self.path)

    def refresh(self) -> None:
        """Reload the state from the database, unless inside a transaction."""
        if self._pending is None:
            self.ledger = self.db.load()

    def rebuild(self) -> None:
        """Replace everything in the database with a replay of the log."""
        self._write([], rebuild=True)

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
                if not raw.endswith("\n"):
                    raise ElError(
                        f"{self.path}:{number}: the last line is incomplete (no"
                        " newline at the end); finish or remove it, then run"
                        " `el rebuild`"
                    )
                try:
                    stamp, op = parse_log_line(line)
                    ledger.apply(op)
                except ElError as exc:
                    raise ElError(f"{self.path}:{number}: {exc}") from None
                records.append((stamp, op))
        return ledger, records

    def _recover(self, stored: LogState | None) -> None:
        """Move an interrupted write at the end of the log aside.

        el writes whole lines and commits the database only after the log
        write finished, so bytes after the last committed size that don't end
        with a newline are a write that was cut short (a crash or power loss
        during the append). The database never saw them. They are appended
        to `<log>.interrupted` and cut from the log. Call with the write lock
        held, so that no other el is appending at the same time.
        """
        if stored is None:
            return
        size = stored[0]
        data = _read_bytes(self.path)
        if len(data) <= size or data.endswith(b"\n"):
            return
        if _state_of(data[:size]) != stored:
            return  # changed by hand before the committed size: not ours
        aside = self.path.with_name(self.path.name + ".interrupted")
        try:
            with aside.open("ab") as fh:
                fh.write(data[size:])
                fh.flush()
                os.fsync(fh.fileno())
            self._truncate(size)
        except OSError as exc:
            raise ElError(f"cannot repair {self.path}: {exc.strerror}") from None
        self.recovered = aside

    def commit(self, ops: list[Operation]) -> None:
        """Check all operations, then apply them and save them.

        Either every operation is applied or none is, so an MV never
        leaves half of itself in the database or the log. Inside a
        transaction nothing is saved until the transaction ends.
        """
        self.ledger.check(ops)
        records = [(now_utc(), op) for op in ops]
        if self._pending is not None:
            for op in ops:
                self.ledger.apply(op)
            self._pending.extend(records)
            return
        self._write(records)

    def transactions(self, target: str) -> list[TransactionRow]:
        """Return every transaction on an account or budget, oldest first.

        Inside a transaction this includes the commits not saved yet.
        """
        rows = self.db.transactions(target)
        for stamp, op in self._pending or []:
            _, verb, name, amount, currency, desc = _transaction_row(
                self.ledger, stamp, op
            )
            if name == target:
                rows.append((stamp, verb, amount, currency, desc))
        return rows

    @contextmanager
    def transaction(self) -> Generator[None]:
        """Group commits so that they are all saved, or none is.

        If the block raises, or saving fails, the in-memory state is
        restored and neither the database nor the log changes.
        """
        self.refresh()
        saved = copy.deepcopy(self.ledger)
        self._pending = []
        try:
            yield
            records, self._pending = self._pending, None
            self._write(records)
        except BaseException:
            self.ledger = saved
            raise
        finally:
            self._pending = None

    def _write(self, records: list[Record], rebuild: bool = False) -> None:
        """Save records (or replay the log) under the database write lock.

        The operations are re-applied to the state read from the database
        inside the lock, not to `self.ledger`, which may be stale if another
        el wrote in the meantime. If any step before the commit fails, the
        database is rolled back and the log is left as it was. If the commit
        itself fails, the appended lines are cut from the log again.

        Raises:
            ElError: If an operation no longer applies, the log changed since
                el last wrote it, or either file cannot be written.
        """
        if not records and not rebuild:
            return
        with self.db.write_lock() as conn:
            stored = self.db.log_state(conn)
            self._recover(stored)
            if rebuild:
                ledger, records = self._replay()
                self.db.store(conn, ledger, records, rebuild=True)
                state = read_log_state(self.path)
                size = None
            else:
                size, _ = state = read_log_state(self.path)
                if stored != state:
                    raise ElError(
                        f"{self.path} changed since el last wrote it; run"
                        " `el rebuild` before making changes"
                    )
                ledger = self.db.load(conn)
                for _, op in records:
                    ledger.apply(op)
                self.db.store(conn, ledger, records)
                lines = [f"{stamp} {op.to_line()}\n" for stamp, op in records]
                state = self._append(lines, size)
            try:
                self.db.commit(conn, state)
            except sqlite3.Error as exc:
                self._undo_append(size, exc)
        self.ledger = ledger
        self.log_changed = False

    def _undo_append(self, size: int | None, exc: sqlite3.Error) -> None:
        """After a failed commit, cut the log back to `size` and raise."""
        try:
            if size is not None:
                self._truncate(size)
        except OSError:
            raise ElError(
                f"saved to the log but not to database {self.db.path}: {exc}"
                " (run `el rebuild` to fix it)"
            ) from None
        raise self.db._error("cannot write", exc) from None

    def _append(self, lines: list[str], size: int) -> LogState:
        """Append lines to the log, which is `size` bytes long, and sync them.

        If writing or syncing fails, whatever part of the lines reached the
        file is cut off again, so the log stays as the database knows it.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write("".join(lines))
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            with suppress(OSError):
                self._truncate(size)
            raise ElError(f"cannot write {self.path}: {exc.strerror}") from None
        return read_log_state(self.path)

    def _truncate(self, size: int) -> None:
        """Cut the log back to `size` bytes and sync it."""
        with self.path.open("r+b") as fh:
            fh.truncate(size)
            fh.flush()
            os.fsync(fh.fileno())
