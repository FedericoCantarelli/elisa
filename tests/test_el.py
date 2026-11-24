"""Tests for the el interpreter, ledger and command line."""

import contextlib
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
import unittest.mock
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from elisa import db_path
from elisa import log_path
from elisa import main
from elisa.interpreter import Interpreter
from elisa.interpreter import split_instructions
from elisa.ledger import ElError
from elisa.storage import Journal
from elisa.table import format_json
from elisa.table import format_table
from elisa.version import PYPROJECT
from elisa.version import get_version


STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} ")


def cells(table: str) -> list[list[str]]:
    """Return the header and rows of an ASCII table as stripped cells."""
    return [
        [cell.strip() for cell in line[1:-1].split("|")]
        for line in table.splitlines()
        if line.startswith("|")
    ]


class ElTestCase(unittest.TestCase):
    """Base case with a fresh log and database in a temporary folder."""

    def setUp(self) -> None:
        """Create the temporary log and database and an interpreter on them."""
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "ledger.log"
        self.db = Path(self.tmp.name) / "data.db"
        self.output: list[str] = []
        self.interp = self.fresh()

    def tearDown(self) -> None:
        """Remove the temporary folder."""
        self.tmp.cleanup()

    def fresh(self) -> Interpreter:
        """Return an interpreter that loads the state from disk."""
        return Interpreter(Journal(self.log, self.db), out=self.output.append)

    def run_all(self, *lines: str) -> None:
        """Execute each instruction in order."""
        for line in lines:
            self.interp.execute(line)

    def log_ops(self) -> list[str]:
        """Return the log lines without their timestamps."""
        return [STAMP.sub("", line) for line in self.log.read_text().splitlines()]

    def run_cli(self, *args: str) -> int:
        """Run the el command line against the temporary log and database."""
        return main(["--log", str(self.log), "--db", str(self.db), *args])

    def query(self, sql: str) -> list[tuple]:
        """Return the rows of a query on the temporary database."""
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


class TestLogFormat(ElTestCase):
    """What ends up in the log file."""

    def test_every_line_starts_with_utc_minute_timestamp(self) -> None:
        """Lines start with YYYY-MM-DD HH:MM."""
        self.run_all("CREATE ACCOUNT a 1 EUR", "ADD a 1 x")
        for line in self.log.read_text().splitlines():
            self.assertRegex(line, STAMP)

    def test_sub_is_logged_as_sub(self) -> None:
        """SUB keeps a positive amount in the log."""
        self.run_all("CREATE ACCOUNT checking 100 EUR", "SUB checking 12.5 coffee")
        self.assertEqual(self.log_ops()[-1], "SUB checking 12.50 coffee")
        balance = self.interp.ledger.accounts["checking"].balance
        self.assertEqual(balance, Decimal("87.50"))

    def test_mv_is_logged_as_sub_and_add(self) -> None:
        """MV writes a SUB on the source and an ADD on the destination."""
        self.run_all("CREATE ACCOUNT a 100 EUR", "CREATE ACCOUNT b 0 EUR", "MV a b 40")
        self.assertEqual(
            self.log_ops()[-2:],
            ["SUB a 40.00 'MV a -> b'", "ADD b 40.00 'MV a -> b'"],
        )

    def test_only_create_delete_add_sub_in_log(self) -> None:
        """No other verbs reach the log."""
        self.run_all(
            "CREATE ACCOUNT a 100 EUR",
            "CREATE BUDGET food 300 EUR",
            "ADD a 5 x",
            "SUB a 5 y",
            "MV a food 1",
            "DELETE BUDGET food",
            "DELETE ACCOUNT a",
            "LIST ACCOUNT ALL",
        )
        verbs = {op.split()[0] for op in self.log_ops()}
        self.assertEqual(verbs, {"CREATE", "ADD", "SUB", "DELETE"})

    def test_state_rebuilt_from_log(self) -> None:
        """Replaying the log gives the same state."""
        self.run_all(
            "CREATE ACCOUNT a 100 EUR",
            "CREATE BUDGET food 300 EUR",
            "MV a food 30",
            "SUB a 10 rent",
            "DELETE ACCOUNT a",
        )
        self.db.unlink()
        ledger = self.fresh().ledger
        self.assertEqual(ledger.accounts["a"].balance, Decimal("60.00"))
        self.assertTrue(ledger.accounts["a"].deleted)
        self.assertEqual(ledger.budgets["food"].spent, Decimal("30.00"))


class TestCommands(ElTestCase):
    """Behaviour of each command."""

    def test_create_and_list_account(self) -> None:
        """A new account shows in LIST ACCOUNT."""
        self.run_all("CREATE ACCOUNT checking 100 EUR", "LIST ACCOUNT")
        self.assertEqual(
            cells(self.output[-1]),
            [["ACCOUNT", "BALANCE", "CURRENCY"], ["checking", "100.00", "EUR"]],
        )

    def test_mv_to_budget_records_expense(self) -> None:
        """MV into a budget raises its spending."""
        self.run_all(
            "CREATE ACCOUNT a 100 EUR",
            "CREATE BUDGET food 300 EUR",
            "MV a food 25 'weekly shopping'",
        )
        budget = self.interp.ledger.budgets["food"]
        self.assertEqual(budget.spent, Decimal("25.00"))
        self.assertEqual(budget.remaining, Decimal("275.00"))
        self.assertEqual(self.interp.ledger.accounts["a"].balance, Decimal("75.00"))

    def test_budget_can_be_crossed(self) -> None:
        """Spending may go over the maximum."""
        self.run_all(
            "CREATE ACCOUNT a 100 EUR", "CREATE BUDGET food 10 EUR", "MV a food 15"
        )
        self.assertEqual(self.interp.ledger.budgets["food"].remaining, Decimal("-5.00"))

    def test_balance_can_go_negative(self) -> None:
        """An account may be overdrawn."""
        self.run_all("CREATE ACCOUNT a 10 EUR", "SUB a 25 rent")
        self.assertEqual(self.interp.ledger.accounts["a"].balance, Decimal("-15.00"))

    def test_soft_delete_hidden_unless_all(self) -> None:
        """Deleted accounts show only with LIST ACCOUNT ALL."""
        self.run_all(
            "CREATE ACCOUNT a 1 EUR", "CREATE ACCOUNT b 2 EUR", "DELETE ACCOUNT a"
        )
        self.output.clear()
        self.interp.execute("LIST ACCOUNT")
        self.assertEqual([row[0] for row in cells(self.output[-1])], ["ACCOUNT", "b"])
        self.output.clear()
        self.interp.execute("LIST ACCOUNT ALL")
        self.assertIn(["a", "1.00", "EUR", "deleted"], cells(self.output[-1]))

    def test_budget_soft_delete(self) -> None:
        """Deleted budgets show only with LIST BUDGET ALL."""
        self.run_all("CREATE BUDGET food 300 EUR", "DELETE BUDGET food")
        self.output.clear()
        self.interp.execute("LIST BUDGET")
        self.assertEqual(len(self.output), 1)
        self.assertEqual(len(cells(self.output[0])), 1)  # header only
        self.output.clear()
        self.interp.execute("LIST BUDGET ALL")
        self.assertEqual(cells(self.output[-1])[-1][5], "deleted")

    def test_every_reply_is_a_table(self) -> None:
        """Each command replies with one or more tables."""
        self.run_all(
            "CREATE ACCOUNT a 100 EUR",
            "CREATE BUDGET food 300 EUR",
            "SUB a 10 x",
            "MV a food 25",
            "DELETE BUDGET food",
        )
        account = ["ACCOUNT", "BALANCE", "CURRENCY"]
        budget = ["BUDGET", "MAX", "SPENT", "REMAINING", "CURRENCY"]
        self.assertEqual(
            [cells(table) for table in self.output],
            [
                [account, ["a", "100.00", "EUR"]],
                [
                    [*budget, "DESCRIPTION"],
                    ["food", "300.00", "0.00", "300.00", "EUR", ""],
                ],
                [account, ["a", "90.00", "EUR"]],
                [budget, ["food", "300.00", "25.00", "275.00", "EUR"]],
                [["ACTION", "KIND", "NAME"], ["deleted", "budget", "food"]],
            ],
        )
        self.assertEqual(self.log_ops()[-2], "ADD food 25.00 'MV a -> food'")

    def test_mv_shows_only_destination(self) -> None:
        """MV prints one table with the destination's new state."""
        self.run_all("CREATE ACCOUNT a 100 EUR", "CREATE ACCOUNT b 0 EUR")
        self.output.clear()
        self.interp.execute("MV a b 40")
        self.assertEqual(
            self.output,
            [
                format_table(
                    ["ACCOUNT", "BALANCE", "CURRENCY"],
                    [["b", "40.00", "EUR"]],
                    align_right={1},
                )
            ],
        )

    def test_create_budget_shows_budget(self) -> None:
        """CREATE BUDGET replies with the new budget and its description."""
        self.interp.execute("CREATE BUDGET food 300 EUR 'weekly groceries'")
        self.assertEqual(
            cells(self.output[-1]),
            [
                ["BUDGET", "MAX", "SPENT", "REMAINING", "CURRENCY", "DESCRIPTION"],
                ["food", "300.00", "0.00", "300.00", "EUR", "weekly groceries"],
            ],
        )

    def test_help_is_a_table(self) -> None:
        """HELP lists the commands in a COMMAND, DESCRIPTION table."""
        self.interp.execute("HELP")
        rows = cells(self.output[-1])
        self.assertEqual(rows[0], ["COMMAND", "DESCRIPTION"])
        self.assertIn("LIST BUDGET [ALL]", [row[0] for row in rows])

    def test_keywords_case_insensitive(self) -> None:
        """Keywords and currency codes may be lower case."""
        self.run_all("create account a 1 eur", "list account all")
        self.assertIn("a", self.output[-1])

    def test_exit_stops(self) -> None:
        """EXIT returns False so the caller stops."""
        self.assertFalse(self.interp.execute("EXIT"))


class TestListTransactions(ElTestCase):
    """LIST TRANSACTION <name> shows the history of one account or budget."""

    HEADER: ClassVar = ["TIMESTAMP", "OPERATION", "AMOUNT", "CURRENCY", "DESCRIPTION"]

    def history(self, name: str) -> list[list[str]]:
        """Run LIST TRANSACTION and return its rows without timestamps."""
        self.output.clear()
        self.interp.execute(f"LIST TRANSACTION '{name}'")
        header, *rows = cells(self.output[-1])
        self.assertEqual(header, self.HEADER)
        for row in rows:
            self.assertRegex(row[0] + " ", STAMP)
        return [row[1:] for row in rows]

    def setUp(self) -> None:
        """Create two accounts and a budget and move money between them."""
        super().setUp()
        self.run_all(
            "CREATE ACCOUNT a 100 EUR",
            "CREATE ACCOUNT b 0 EUR",
            "CREATE BUDGET food 300 EUR groceries",
            "ADD a 50 salary",
            "MV a food 20 market",
            "MV a b 10",
        )

    def test_account_history(self) -> None:
        """An account shows its creation and every movement, oldest first."""
        self.assertEqual(
            self.history("a"),
            [
                ["CREATE ACCOUNT", "100.00", "EUR", ""],
                ["ADD", "50.00", "EUR", "salary"],
                ["SUB", "-20.00", "EUR", "market"],
                ["SUB", "-10.00", "EUR", "MV a -> b"],
            ],
        )

    def test_only_that_target(self) -> None:
        """Transactions on other accounts and budgets are left out."""
        self.assertEqual(
            self.history("b"),
            [
                ["CREATE ACCOUNT", "0.00", "EUR", ""],
                ["ADD", "10.00", "EUR", "MV a -> b"],
            ],
        )

    def test_budget_history_includes_delete(self) -> None:
        """A deleted budget still has its history, delete included."""
        self.interp.execute("DELETE BUDGET food")
        self.assertEqual(
            self.history("food"),
            [
                ["CREATE BUDGET", "300.00", "EUR", "groceries"],
                ["ADD", "20.00", "EUR", "market"],
                ["DELETE BUDGET", "", "", ""],
            ],
        )

    def test_plural_and_lower_case(self) -> None:
        """TRANSACTIONS and lower-case keywords also work."""
        self.output.clear()
        self.interp.execute("list transactions b")
        self.assertEqual(len(cells(self.output[-1])), 3)

    def test_unknown_name(self) -> None:
        """An unused name is an error."""
        with self.assertRaises(ElError) as ctx:
            self.interp.execute("LIST TRANSACTION nope")
        self.assertIn("no account or budget named 'nope'", str(ctx.exception))

    def test_name_is_required(self) -> None:
        """LIST TRANSACTION needs exactly one name."""
        for line in ("LIST TRANSACTION", "LIST TRANSACTION a b"):
            with self.assertRaises(ElError):
                self.interp.execute(line)

    def test_sees_unsaved_batch_commits(self) -> None:
        """Inside a -c batch it includes the commits not saved yet."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.run_cli("-c", "SUB b 4 snack; LIST TRANSACTION b")
        self.assertEqual(code, 0)
        self.assertEqual(
            cells(out.getvalue())[-1][1:], ["SUB", "-4.00", "EUR", "snack"]
        )


class TestCurrencies(ElTestCase):
    """EUR and KWD are both supported, each paired with its own kind."""

    def test_kwd_account_pays_kwd_budget(self) -> None:
        """A KWD account can pay into a KWD budget."""
        self.run_all(
            "CREATE ACCOUNT dinar 100 KWD",
            "CREATE BUDGET rent 80 kwd",
            "MV dinar rent 30.50 june",
        )
        self.assertEqual(
            cells(self.output[-1])[1], ["rent", "80.00", "30.50", "49.50", "KWD"]
        )
        self.assertEqual(
            self.log_ops()[-2:],
            ["SUB dinar 30.50 june", "ADD rent 30.50 june"],
        )


class TestBudgetDescription(ElTestCase):
    """The optional description of CREATE BUDGET."""

    def test_description_is_optional(self) -> None:
        """A budget without a description has an empty one."""
        self.interp.execute("CREATE BUDGET food 300 EUR")
        self.assertEqual(self.interp.ledger.budgets["food"].description, "")
        self.assertEqual(self.log_ops(), ["CREATE BUDGET food 300.00 EUR"])

    def test_quoted_description(self) -> None:
        """A quoted description is stored and logged as one value."""
        self.interp.execute("CREATE BUDGET food 300 EUR 'groceries and markets'")
        self.assertEqual(
            self.interp.ledger.budgets["food"].description, "groceries and markets"
        )
        self.assertEqual(
            self.log_ops(), ["CREATE BUDGET food 300.00 EUR 'groceries and markets'"]
        )

    def test_unquoted_description_takes_rest_of_line(self) -> None:
        """Like ADD, an unquoted description takes the rest of the line."""
        self.interp.execute("CREATE BUDGET food 300 EUR weekly groceries")
        self.assertEqual(
            self.interp.ledger.budgets["food"].description, "weekly groceries"
        )

    def test_description_in_database_and_rebuild(self) -> None:
        """The description is saved to the database and survives a rebuild."""
        self.interp.execute("CREATE BUDGET food 300 EUR 'weekly groceries'")
        self.assertEqual(
            self.query("SELECT description FROM budgets"), [("weekly groceries",)]
        )
        self.assertEqual(
            self.query("SELECT description FROM transactions"),
            [("weekly groceries",)],
        )
        self.db.unlink()
        budget = self.fresh().ledger.budgets["food"]
        self.assertEqual(budget.description, "weekly groceries")

    def test_list_budget_shows_description(self) -> None:
        """LIST BUDGET has a DESCRIPTION column, after STATUS with ALL."""
        self.run_all(
            "CREATE BUDGET food 300 EUR 'weekly groceries'",
            "CREATE BUDGET fun 50 EUR",
        )
        self.output.clear()
        self.interp.execute("LIST BUDGET")
        header, food, fun = cells(self.output[0])
        self.assertEqual(header[-1], "DESCRIPTION")
        self.assertEqual(food[-2:], ["EUR", "weekly groceries"])
        self.assertEqual(fun[-2:], ["EUR", ""])
        self.interp.execute("DELETE BUDGET food")
        self.output.clear()
        self.interp.execute("LIST BUDGET ALL")
        header, food, _ = cells(self.output[0])
        self.assertEqual(header[-2:], ["STATUS", "DESCRIPTION"])
        self.assertEqual(food[-2:], ["deleted", "weekly groceries"])

    def test_account_takes_no_description(self) -> None:
        """CREATE ACCOUNT still takes exactly four arguments."""
        with self.assertRaises(ElError):
            self.interp.execute("CREATE ACCOUNT a 1 EUR savings")

    def test_old_database_gets_description_column(self) -> None:
        """A database made before descriptions existed is upgraded."""
        self.db.unlink()
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute(
                "CREATE TABLE budgets (position INTEGER NOT NULL, name TEXT"
                " PRIMARY KEY, currency TEXT NOT NULL, max_amount TEXT NOT NULL,"
                " spent TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)"
            )
        conn.close()
        self.fresh().execute("CREATE BUDGET food 300 EUR groceries")
        self.assertEqual(
            self.query("SELECT name, description FROM budgets"),
            [("food", "groceries")],
        )


class TestErrors(ElTestCase):
    """Invalid instructions are rejected with an ElError."""

    def assert_error(self, line: str, fragment: str) -> None:
        """Assert that the instruction fails with a message containing fragment."""
        with self.assertRaises(ElError) as ctx:
            self.interp.execute(line)
        self.assertIn(fragment, str(ctx.exception))

    def test_unsupported_currency(self) -> None:
        """Currencies other than EUR and KWD are refused."""
        self.assert_error("CREATE ACCOUNT a 1 USD", "unsupported currency")

    def test_mv_eur_account_to_kwd_budget(self) -> None:
        """An EUR account cannot pay into a KWD budget."""
        self.run_all("CREATE ACCOUNT a 100 EUR", "CREATE BUDGET b 50 KWD")
        before = self.log.read_text()
        self.assert_error("MV a b 10", "currency mismatch")
        self.assertEqual(self.log.read_text(), before)

    def test_mv_kwd_account_to_eur_budget(self) -> None:
        """A KWD account cannot pay into an EUR budget."""
        self.run_all("CREATE ACCOUNT a 100 KWD", "CREATE BUDGET b 50 EUR")
        self.assert_error("MV a b 10", "'a' is KWD but 'b' is EUR")

    def test_mv_between_accounts_of_different_currencies(self) -> None:
        """Transfers between an EUR and a KWD account are refused."""
        self.run_all("CREATE ACCOUNT a 100 EUR", "CREATE ACCOUNT k 100 KWD")
        self.assert_error("MV k a 10", "currency mismatch")

    def test_duplicate_name_across_accounts_and_budgets(self) -> None:
        """Accounts and budgets cannot share a name."""
        self.interp.execute("CREATE ACCOUNT food 1 EUR")
        self.assert_error("CREATE BUDGET food 10 EUR", "already used")

    def test_operations_on_deleted_account(self) -> None:
        """A deleted account cannot be used."""
        self.run_all("CREATE ACCOUNT a 1 EUR", "DELETE ACCOUNT a")
        self.assert_error("ADD a 1 x", "deleted")

    def test_add_to_budget_directly_is_rejected(self) -> None:
        """ADD and SUB only work on accounts."""
        self.interp.execute("CREATE BUDGET food 10 EUR")
        self.assert_error("ADD food 1 x", "no account")

    def test_amount_must_be_positive(self) -> None:
        """Amounts in ADD, SUB and MV must be above zero."""
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        self.assert_error("SUB a -5 x", "greater than zero")

    def test_too_many_decimals(self) -> None:
        """Amounts take at most two decimals."""
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        self.assert_error("ADD a 1.234 x", "two decimals")

    def test_failed_mv_writes_nothing(self) -> None:
        """A failed MV leaves the log untouched."""
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        before = self.log.read_text()
        self.assert_error("MV a missing 1", "no account")
        self.assertEqual(self.log.read_text(), before)

    def test_unknown_command(self) -> None:
        """Unknown verbs are refused."""
        self.assert_error("FOO", "unknown command")

    def test_budget_cannot_go_below_zero_in_log(self) -> None:
        """A log that would make a budget's spending negative is refused."""
        self.log.write_text(
            "2026-11-04 10:00 CREATE BUDGET food 10.00 EUR\n"
            "2026-11-04 10:01 SUB food 5.00 refund\n"
        )
        self.db.unlink()
        with self.assertRaises(ElError) as ctx:
            self.fresh()
        self.assertIn("below zero", str(ctx.exception))


class TestCli(ElTestCase):
    """The -c and -f modes."""

    def test_split_instructions_respects_quotes(self) -> None:
        """Semicolons inside quotes do not split."""
        self.assertEqual(
            split_instructions("ADD a 1 'x; y'; LIST ACCOUNT;"),
            ["ADD a 1 'x; y'", "LIST ACCOUNT"],
        )

    def test_dash_c(self) -> None:
        """-c runs every instruction and saves them."""
        code = self.run_cli("-c", "CREATE ACCOUNT a 10 EUR; SUB a 3 snack;")
        self.assertEqual(code, 0)
        self.assertEqual(self.fresh().ledger.accounts["a"].balance, Decimal("7.00"))

    def test_dash_c_error_saves_nothing(self) -> None:
        """-c with an error leaves the log as it was."""
        self.interp.execute("CREATE ACCOUNT a 10 EUR")
        before = self.log.read_text()
        code = self.run_cli("-c", "SUB a 3 snack; SUB nope 1 x;")
        self.assertEqual(code, 1)
        self.assertEqual(self.log.read_text(), before)

    def test_dash_f_error_saves_nothing(self) -> None:
        """-f with an error leaves the log as it was."""
        script = Path(self.tmp.name) / "script.el"
        script.write_text(
            "CREATE ACCOUNT a 10 EUR\n\n# comment\nSUB a 1 x\nSUB nope 1 x\n"
        )
        code = self.run_cli("-f", str(script))
        self.assertEqual(code, 1)
        self.assertFalse(self.log.exists())

    def test_dash_f(self) -> None:
        """-f runs a newline-delimited file."""
        script = Path(self.tmp.name) / "script.el"
        script.write_text("CREATE ACCOUNT a 10 EUR\nSUB a 1 x\n")
        self.assertEqual(self.run_cli("-f", str(script)), 0)
        self.assertEqual(self.fresh().ledger.accounts["a"].balance, Decimal("9.00"))


class TestPaths(unittest.TestCase):
    """Where the ledger log and the database are taken from."""

    def test_log_flag_overrides_default(self) -> None:
        """--log is used when given."""
        self.assertEqual(log_path("/flag/j.log"), Path("/flag/j.log"))

    def test_log_default_is_home_elisa(self) -> None:
        """Without --log the log is ~/.elisa/ledger.log."""
        self.assertEqual(log_path(None), Path.home() / ".elisa" / "ledger.log")

    def test_env_is_ignored(self) -> None:
        """EL_LOG no longer changes the log file."""
        with unittest.mock.patch.dict(os.environ, {"EL_LOG": "/env/j.log"}):
            self.assertEqual(log_path(None), Path.home() / ".elisa" / "ledger.log")

    def test_db_flag_overrides_default(self) -> None:
        """--db is used when given."""
        self.assertEqual(db_path("/flag/x.db"), Path("/flag/x.db"))

    def test_db_default_is_home_elisa(self) -> None:
        """Without --db the database is ~/.elisa/data.db."""
        self.assertEqual(db_path(None), Path.home() / ".elisa" / "data.db")


class TestDatabase(ElTestCase):
    """The SQLite copy of the current state."""

    def test_state_is_stored(self) -> None:
        """Accounts and budgets are written to the database."""
        self.run_all(
            "CREATE ACCOUNT a 100 EUR",
            "CREATE BUDGET food 300 EUR",
            "MV a food 30.5",
            "DELETE ACCOUNT a",
        )
        self.assertEqual(
            self.query("SELECT name, currency, balance, deleted FROM accounts"),
            [("a", "EUR", "69.50", 1)],
        )
        self.assertEqual(
            self.query(
                "SELECT name, currency, max_amount, spent, deleted FROM budgets"
            ),
            [("food", "EUR", "300.00", "30.50", 0)],
        )

    def test_list_order_survives_reload(self) -> None:
        """Accounts come back in creation order."""
        self.run_all("CREATE ACCOUNT z 1 EUR", "CREATE ACCOUNT a 2 EUR")
        self.assertEqual(list(self.fresh().ledger.accounts), ["z", "a"])

    def test_every_transaction_is_stored(self) -> None:
        """Each operation becomes a row in transactions, with a signed amount."""
        self.run_all(
            "CREATE ACCOUNT a 100 EUR",
            "CREATE BUDGET food 300 EUR",
            "MV a food 12.5 'weekly shopping'",
            "ADD a 3 refund",
            "DELETE BUDGET food",
            "LIST ACCOUNT",
        )
        self.assertEqual(
            self.query(
                "SELECT operation, target, amount, currency, description"
                " FROM transactions ORDER BY id"
            ),
            [
                ("CREATE ACCOUNT", "a", "100.00", "EUR", None),
                ("CREATE BUDGET", "food", "300.00", "EUR", None),
                ("SUB", "a", "-12.50", "EUR", "weekly shopping"),
                ("ADD", "food", "12.50", "EUR", "weekly shopping"),
                ("ADD", "a", "3.00", "EUR", "refund"),
                ("DELETE BUDGET", "food", None, None, None),
            ],
        )
        stamps = [r[0] for r in self.query("SELECT timestamp FROM transactions")]
        for stamp in stamps:
            self.assertRegex(stamp + " ", STAMP)

    def test_one_log_line_per_transaction(self) -> None:
        """The log and the transactions table hold the same actions."""
        self.run_all("CREATE ACCOUNT a 1 EUR", "CREATE ACCOUNT b 0 EUR", "MV a b 1")
        (count,) = self.query("SELECT COUNT(*) FROM transactions")[0]
        self.assertEqual(count, len(self.log_ops()))

    def test_state_is_read_from_database(self) -> None:
        """The state is loaded from the database, not from the log."""
        self.run_all("CREATE ACCOUNT a 100 EUR")
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE accounts SET balance = '1.00'")
        conn.close()
        self.assertEqual(self.fresh().ledger.accounts["a"].balance, Decimal("1.00"))

    def test_edited_log_is_reported_not_applied(self) -> None:
        """A hand-edited log sets log_changed and leaves the database alone."""
        self.run_all("CREATE ACCOUNT a 100 EUR")
        with self.log.open("a") as fh:
            fh.write("2026-11-04 10:00 SUB a 40.00 cash\n")
        journal = Journal(self.log, self.db)
        self.assertTrue(journal.log_changed)
        self.assertEqual(journal.ledger.accounts["a"].balance, Decimal("100.00"))

    def test_rebuild_replays_the_log(self) -> None:
        """Journal.rebuild replaces the database with the log's contents."""
        self.run_all("CREATE ACCOUNT a 100 EUR")
        with self.log.open("a") as fh:
            fh.write("2026-11-04 10:00 SUB a 40.00 cash\n")
        journal = Journal(self.log, self.db)
        journal.rebuild()
        self.assertFalse(journal.log_changed)
        self.assertEqual(journal.ledger.accounts["a"].balance, Decimal("60.00"))
        self.assertEqual(self.query("SELECT balance FROM accounts"), [("60.00",)])
        self.assertEqual(
            self.query("SELECT timestamp, operation, amount FROM transactions")[-1],
            ("2026-11-04 10:00", "SUB", "-40.00"),
        )
        self.assertFalse(Journal(self.log, self.db).log_changed)

    def test_el_rebuild_command(self) -> None:
        """`el rebuild` rebuilds the database from the log."""
        self.run_all("CREATE ACCOUNT a 100 EUR", "SUB a 1 x")
        before = self.query("SELECT * FROM transactions")
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("DELETE FROM transactions")
        conn.close()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_cli("rebuild"), 0)
        self.assertEqual(self.query("SELECT * FROM transactions"), before)

    def test_warns_when_log_changed(self) -> None:
        """A warning is printed when the log was changed outside of el."""
        self.run_all("CREATE ACCOUNT a 100 EUR")
        with self.log.open("a") as fh:
            fh.write("# a comment\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_cli("-c", "LIST ACCOUNT"), 0)
        self.assertIn("el rebuild", err.getvalue())

    def test_built_from_log_when_missing(self) -> None:
        """A missing database is recreated from the log, transactions included."""
        self.run_all("CREATE ACCOUNT a 100 EUR", "SUB a 1 x")
        before = self.query("SELECT * FROM transactions")
        self.db.unlink()
        self.fresh()
        self.assertEqual(
            self.query("SELECT name, balance FROM accounts"), [("a", "99.00")]
        )
        self.assertEqual(self.query("SELECT * FROM transactions"), before)

    def test_failed_mv_leaves_database_untouched(self) -> None:
        """A failed MV changes neither file."""
        self.run_all("CREATE ACCOUNT a 1 EUR")
        before = self.query("SELECT * FROM accounts")
        with self.assertRaises(ElError):
            self.interp.execute("MV a missing 1")
        self.assertEqual(self.query("SELECT * FROM accounts"), before)

    def test_dash_c_error_leaves_database_untouched(self) -> None:
        """-c with an error leaves the database as it was."""
        self.interp.execute("CREATE ACCOUNT a 10 EUR")
        before = self.query("SELECT * FROM accounts")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_cli("-c", "SUB a 3 snack; SUB nope 1 x;"), 1)
        self.assertEqual(self.query("SELECT * FROM accounts"), before)
        self.assertEqual(
            cells(err.getvalue()),
            [
                ["ERROR"],
                ["instruction 2: no account named 'nope'"],
                ["nothing was saved"],
            ],
        )

    def test_log_write_failure_rolls_back_everything(self) -> None:
        """If the log cannot be written, the database and memory stay as before."""
        self.run_all("CREATE ACCOUNT a 10 EUR")
        before = self.query("SELECT * FROM accounts")
        with (
            unittest.mock.patch.object(Path, "open", side_effect=OSError(28, "full")),
            self.assertRaises(ElError),
        ):
            self.interp.execute("SUB a 3 snack")
        self.assertEqual(self.query("SELECT * FROM accounts"), before)
        self.assertEqual(self.interp.ledger.accounts["a"].balance, Decimal("10.00"))
        self.assertEqual(self.fresh().ledger.accounts["a"].balance, Decimal("10.00"))

    def test_dash_c_writes_database(self) -> None:
        """-c saves its changes to the --db file."""
        self.assertEqual(self.run_cli("-c", "CREATE ACCOUNT a 10 EUR; SUB a 3 x;"), 0)
        self.assertEqual(self.query("SELECT balance FROM accounts"), [("7.00",)])


class TestVersion(unittest.TestCase):
    """The el version command."""

    def test_version_matches_pyproject(self) -> None:
        """The version is the one declared in pyproject.toml."""
        text = PYPROJECT.read_text()
        self.assertIn(f'version = "{get_version()}"', text)

    def test_el_version_prints_it(self) -> None:
        """`el version` prints the version and exits 0."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["version"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.getvalue(),
            format_table(["PROGRAM", "VERSION"], [["el", get_version()]]) + "\n",
        )


class TestJson(ElTestCase):
    """--json prints every result as one line of JSON."""

    def run_json(self, *args: str) -> tuple[int, list, list]:
        """Run el --json; return the exit code and parsed stdout/stderr lines."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.run_cli("--json", *args)
        parse = [json.loads(line) for line in out.getvalue().splitlines()]
        errors = [json.loads(line) for line in err.getvalue().splitlines()]
        return code, parse, errors

    def test_one_line_per_result(self) -> None:
        """Each instruction's output is one JSON array of row objects."""
        code, out, err = self.run_json("-c", "LIST ACCOUNT; CREATE ACCOUNT a 10 EUR")
        self.assertEqual((code, err), (0, []))
        self.assertEqual(
            out, [[], [{"account": "a", "balance": "10.00", "currency": "EUR"}]]
        )

    def test_budget_and_transactions(self) -> None:
        """Keys are the lowercased column names and amounts stay strings."""
        _, out, _ = self.run_json(
            "-c",
            "CREATE ACCOUNT a 10 EUR; CREATE BUDGET food 5 EUR; MV a food 2;"
            " LIST TRANSACTION food",
        )
        self.assertEqual(
            out[2],
            [
                {
                    "budget": "food",
                    "max": "5.00",
                    "spent": "2.00",
                    "remaining": "3.00",
                    "currency": "EUR",
                }
            ],
        )
        self.assertEqual(
            [(t["operation"], t["amount"]) for t in out[3]],
            [("CREATE BUDGET", "5.00"), ("ADD", "2.00")],
        )

    def test_error_is_json_on_stderr(self) -> None:
        """An error is an {"error": ...} object on stderr and exits 1."""
        code, out, err = self.run_json("-c", "SUB nope 1 x")
        self.assertEqual((code, out), (1, []))
        self.assertEqual(
            err,
            [{"error": "instruction 1: no account named 'nope'\nnothing was saved"}],
        )

    def test_warning_is_json_on_stderr(self) -> None:
        """The log-changed warning is a {"warning": ...} object."""
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        with self.log.open("a") as fh:
            fh.write("# a comment\n")
        _, _, err = self.run_json("-c", "LIST ACCOUNT")
        self.assertIn("el rebuild", err[0]["warning"])

    def test_version(self) -> None:
        """`el --json version` prints the version as JSON."""
        _, out, _ = self.run_json("version")
        self.assertEqual(out, [[{"program": "el", "version": get_version()}]])

    def test_interpreter_formatter(self) -> None:
        """The interpreter uses the formatter it is given."""
        lines: list[str] = []
        interp = Interpreter(
            Journal(self.log, self.db), out=lines.append, fmt=format_json
        )
        interp.execute("CREATE ACCOUNT a 1 EUR")
        self.assertEqual(
            json.loads(lines[0]),
            [{"account": "a", "balance": "1.00", "currency": "EUR"}],
        )


class TestTable(unittest.TestCase):
    """The ASCII table formatter behind all output."""

    def test_layout(self) -> None:
        """Columns are padded to the widest cell and framed with rules."""
        self.assertEqual(
            format_table(["NAME", "N"], [["a", "1.00"], ["long name", "10.00"]]),
            "+-----------+-------+\n"
            "| NAME      | N     |\n"
            "+-----------+-------+\n"
            "| a         | 1.00  |\n"
            "| long name | 10.00 |\n"
            "+-----------+-------+",
        )

    def test_align_right(self) -> None:
        """Columns in align_right are right-aligned, but not their header."""
        table = format_table(["N"], [["1.00"], ["10.00"]], align_right={0})
        self.assertEqual(
            table.splitlines()[1:5],
            ["| N     |", "+-------+", "|  1.00 |", "| 10.00 |"],
        )

    def test_no_rows_shows_header(self) -> None:
        """An empty table still has its header."""
        self.assertEqual(format_table(["A"], []), "+---+\n| A |\n+---+")

    def test_multiline_cell(self) -> None:
        """A cell with newlines spans several lines of its row."""
        self.assertEqual(
            format_table(["A", "B"], [["x\ny", "z"]]).splitlines()[3:5],
            ["| x | z |", "| y |   |"],
        )

    def test_row_length_must_match_header(self) -> None:
        """A row with the wrong number of cells is a bug, not a user error."""
        with self.assertRaises(ValueError):
            format_table(["A", "B"], [["only one"]])

    def test_json(self) -> None:
        """format_json gives an array of objects keyed by lowercased columns."""
        self.assertEqual(
            format_json(["NAME", "N"], [["é", "1.00"]], align_right={1}),
            '[{"name": "é", "n": "1.00"}]',
        )
        self.assertEqual(format_json(["A"], []), "[]")
        with self.assertRaises(ValueError):
            format_json(["A", "B"], [["only one"]])


if __name__ == "__main__":
    unittest.main()
