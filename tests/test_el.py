"""Tests for the el interpreter, ledger and command line."""

import contextlib
import io
import json
import os
import re
import runpy
import sqlite3
import tempfile
import unittest
import unittest.mock
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from typing import Any
from typing import ClassVar

import elisa
import elisa.cli
import elisa.version
from elisa.cli import db_path
from elisa.cli import log_path
from elisa.cli import main
from elisa.interpreter import Interpreter
from elisa.interpreter import split_instructions
from elisa.ledger import Add
from elisa.ledger import CreateAccount
from elisa.ledger import ElError
from elisa.ledger import Ledger
from elisa.ledger import parse_amount
from elisa.ledger import parse_log_line
from elisa.storage import Database
from elisa.storage import Journal
from elisa.storage import _transaction_row
from elisa.storage import read_log_state
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

    def test_huge_exponent_is_an_error(self) -> None:
        """An amount too large for cents is an ElError, not a crash."""
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        self.assert_error("ADD a 1e40 x", "invalid amount")

    def test_line_break_in_description_is_rejected(self) -> None:
        """A line break would split the log line, so it is refused."""
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        self.assert_error('ADD a 1 "x\ny"', "line breaks")
        self.assertEqual(self.interp.ledger.accounts["a"].balance, Decimal("1.00"))

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
        log_before = self.log.read_text()
        full = OSError(28, "No space left on device")
        with (
            unittest.mock.patch("os.fsync", side_effect=full),
            self.assertRaises(ElError) as ctx,
        ):
            self.interp.execute("SUB a 3 snack")
        self.assertIn("cannot write", str(ctx.exception))
        self.assertIn("No space left on device", str(ctx.exception))
        self.assertEqual(self.query("SELECT * FROM accounts"), before)
        self.assertEqual(self.log.read_text(), log_before)  # partial write cut off
        self.assertFalse(self.fresh().journal.log_changed)
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


class TestInterpreterErrors(ElTestCase):
    """Usage errors and bad input that the interpreter rejects."""

    def assert_error(self, line: str, fragment: str) -> None:
        """Assert that the instruction fails with a message containing fragment."""
        with self.assertRaises(ElError) as ctx:
            self.interp.execute(line)
        self.assertIn(fragment, str(ctx.exception))

    def test_usage_errors(self) -> None:
        """Each command reports its usage when the arguments don't fit."""
        self.interp.execute("CREATE ACCOUNT a 10 EUR")
        cases = {
            "CREATE ACCOUNT a": "usage: CREATE ACCOUNT",
            "DELETE ACCOUNT": "usage: DELETE",
            "ADD a": "usage: ADD",
            "SUB": "usage: SUB",
            "MV a": "usage: MV",
            "LIST": "usage: LIST",
            "LIST ACCOUNT ALL extra": "usage: LIST",
            "LIST ACCOUNT EVERYTHING": "usage: LIST",
        }
        for line, fragment in cases.items():
            with self.subTest(line=line):
                self.assert_error(line, fragment)

    def test_unknown_kind(self) -> None:
        """Only ACCOUNT and BUDGET are kinds."""
        self.assert_error("DELETE THING x", "expected ACCOUNT or BUDGET, got 'THING'")
        self.assert_error("CREATE THING x 1 EUR", "expected ACCOUNT or BUDGET")

    def test_unterminated_quote(self) -> None:
        """A quote that is never closed is an error, not a crash."""
        self.assert_error("ADD a 1 'oops", "No closing quotation")
        with self.assertRaises(ElError):
            split_instructions("LIST 'x")

    def test_mv_to_itself(self) -> None:
        """MV needs two different items."""
        self.interp.execute("CREATE ACCOUNT a 10 EUR")
        self.assert_error("MV a a 1", "source and destination are the same")

    def test_mv_to_deleted_budget(self) -> None:
        """A deleted budget can't be paid into."""
        self.run_all(
            "CREATE ACCOUNT a 10 EUR", "CREATE BUDGET food 5 EUR", "DELETE BUDGET food"
        )
        self.assert_error("MV a food 1", "budget 'food' is deleted")

    def test_delete_missing_budget(self) -> None:
        """DELETE BUDGET needs an existing budget."""
        self.assert_error("DELETE BUDGET nope", "no budget named 'nope'")

    def test_create_budget_needs_positive_max(self) -> None:
        """A budget's maximum must be greater than zero."""
        self.assert_error("CREATE BUDGET food 0 EUR", "greater than zero")

    def test_list_and_help_produce_no_operations(self) -> None:
        """Only commands that change state translate into operations."""
        with self.assertRaises(ElError) as ctx:
            self.interp.translate("LIST", ["ACCOUNT"])
        self.assertIn("does not produce log operations", str(ctx.exception))

    def test_blank_and_comment_lines_do_nothing(self) -> None:
        """Empty input and comments are ignored."""
        self.assertTrue(self.interp.execute(""))
        self.assertTrue(self.interp.execute("   # just a comment"))
        self.assertEqual(self.output, [])

    def test_quit_is_exit(self) -> None:
        """QUIT works like EXIT."""
        self.assertFalse(self.interp.execute("quit"))


class TestLedger(unittest.TestCase):
    """Parsing and rules of the domain model."""

    def test_parse_amount_rejects_non_numbers(self) -> None:
        """Text, NaN and infinity aren't amounts."""
        for text in ("abc", "NaN", "Infinity", "-inf"):
            with self.subTest(text=text), self.assertRaises(ElError) as ctx:
                parse_amount(text)
            self.assertIn("invalid amount", str(ctx.exception))

    def test_add_without_description_round_trips(self) -> None:
        """An ADD with no description has no trailing field."""
        op = Add("a", Decimal("1.00"))
        self.assertEqual(op.to_line(), "ADD a 1.00")
        self.assertEqual(parse_log_line(f"2026-01-01 10:00 {op.to_line()}")[1], op)

    def test_delete_budget_round_trips(self) -> None:
        """DELETE BUDGET lines parse back."""
        stamp, op = parse_log_line("2026-01-01 10:00 DELETE BUDGET food")
        self.assertEqual(
            (stamp, op.to_line()), ("2026-01-01 10:00", "DELETE BUDGET food")
        )

    def test_malformed_log_lines(self) -> None:
        """Broken log lines are reported, not applied."""
        cases = {
            "2026-01-01 10:00 ADD a 1 'x": "malformed log line",
            "2026-01-01": "malformed log line",
            "2026-13-01 10:00 ADD a 1": "invalid timestamp",
            "2026-01-01 10:00 FOO a": "unknown log operation",
            "2026-01-01 10:00 CREATE ACCOUNT a 1": "unknown log operation",
        }
        for line, fragment in cases.items():
            with self.subTest(line=line), self.assertRaises(ElError) as ctx:
                parse_log_line(line)
            self.assertIn(fragment, str(ctx.exception))

    def test_add_to_unknown_target(self) -> None:
        """An Add on a name that doesn't exist fails."""
        with self.assertRaises(ElError) as ctx:
            Ledger().apply(Add("ghost", Decimal("1.00")))
        self.assertIn("no account or budget named 'ghost'", str(ctx.exception))

    def test_unknown_operation_is_a_bug(self) -> None:
        """Anything that isn't an operation raises TypeError."""
        with self.assertRaises(TypeError):
            Ledger().apply(object())  # ty: ignore[invalid-argument-type]


class FailingConnection:
    """A real connection whose commit, or one SQL statement, fails."""

    def __init__(self, conn: sqlite3.Connection, fail_on: str) -> None:
        """Wrap `conn`; fail_on is "commit" or the SQL text to fail."""
        self.conn = conn
        self.fail_on = fail_on

    def __getattr__(self, name: str) -> object:
        """Delegate everything else to the real connection."""
        return getattr(self.conn, name)

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        """Fail on the chosen statement."""
        if sql == self.fail_on:
            raise sqlite3.OperationalError("database is locked")
        return self.conn.execute(sql, *args)

    def commit(self) -> None:
        """Fail like a full disk would, if chosen."""
        if self.fail_on == "commit":
            raise sqlite3.OperationalError("disk I/O error")
        self.conn.commit()


class TestStorageFailures(ElTestCase):
    """The database and log report failures as ElError and stay consistent."""

    def test_database_cannot_be_created(self) -> None:
        """A database path inside a file can't be opened."""
        self.log.write_text("")
        with self.assertRaises(ElError) as ctx:
            Database(self.log / "data.db")
        self.assertIn("cannot open database", str(ctx.exception))

    def test_database_read_errors(self) -> None:
        """Every read reports a broken database as ElError."""
        db = self.interp.journal.db
        broken = sqlite3.OperationalError("database is locked")
        with unittest.mock.patch.object(Database, "_connect", side_effect=broken):
            for read in (db.log_state, db.load, lambda: db.transactions("a")):
                with self.subTest(read=read), self.assertRaises(ElError) as ctx:
                    read()
                self.assertIn("cannot read database", str(ctx.exception))
            with self.assertRaises(ElError) as ctx:
                self.interp.execute("CREATE ACCOUNT a 1 EUR")
            self.assertIn("cannot read database", str(ctx.exception))
            with self.assertRaises(ElError) as ctx:
                self.interp.journal.commit([CreateAccount("a", Decimal(1), "EUR")])
            self.assertIn("cannot open database", str(ctx.exception))
        self.assertFalse(self.log.exists())

    def test_lock_error(self) -> None:
        """If the write lock can't be taken, nothing is written."""
        real = Database._connect

        def failing(db: Database) -> FailingConnection:
            return FailingConnection(real(db), fail_on="BEGIN IMMEDIATE")

        with (
            unittest.mock.patch.object(Database, "_connect", failing),
            self.assertRaises(ElError) as ctx,
        ):
            self.interp.journal.commit([CreateAccount("a", Decimal(1), "EUR")])
        self.assertIn("cannot lock database", str(ctx.exception))
        self.assertFalse(self.log.exists())

    def test_write_error_changes_nothing(self) -> None:
        """If the rows can't be written, the log stays as it was."""
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        before = self.log.read_text()
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute(
                "CREATE TRIGGER read_only BEFORE INSERT ON transactions"
                " BEGIN SELECT RAISE(FAIL, 'read only'); END"
            )
        conn.close()
        with self.assertRaises(ElError) as ctx:
            self.interp.execute("ADD a 1 x")
        self.assertIn("cannot write database", str(ctx.exception))
        self.assertEqual(self.log.read_text(), before)
        self.assertEqual(self.interp.ledger.accounts["a"].balance, Decimal("1.00"))

    def commit_fails(self) -> contextlib.AbstractContextManager:
        """Make every database commit fail, like a full disk would."""
        real = Database._connect

        def failing(db: Database) -> FailingConnection:
            return FailingConnection(real(db), fail_on="commit")

        return unittest.mock.patch.object(Database, "_connect", failing)

    def test_commit_failure_takes_the_lines_back(self) -> None:
        """If the commit fails, the appended lines are cut from the log."""
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        before = self.log.read_text()
        with self.commit_fails(), self.assertRaises(ElError) as ctx:
            self.interp.execute("ADD a 1 x")
        self.assertIn("cannot write database", str(ctx.exception))
        self.assertEqual(self.log.read_text(), before)
        journal = Journal(self.log, self.db)
        self.assertFalse(journal.log_changed)
        self.assertEqual(journal.ledger.accounts["a"].balance, Decimal("1.00"))

    def test_commit_and_undo_failure_asks_for_rebuild(self) -> None:
        """If the lines can't be taken back either, el says to rebuild."""
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        with (
            self.commit_fails(),
            unittest.mock.patch.object(Journal, "_truncate", side_effect=OSError),
            self.assertRaises(ElError) as ctx,
        ):
            self.interp.execute("ADD a 1 x")
        self.assertIn("run `el rebuild`", str(ctx.exception))
        self.assertEqual(self.log_ops()[-1], "ADD a 1.00 x")
        journal = Journal(self.log, self.db)
        self.assertTrue(journal.log_changed)
        journal.rebuild()
        self.assertEqual(journal.ledger.accounts["a"].balance, Decimal("2.00"))

    def test_commit_failure_during_rebuild(self) -> None:
        """A failed commit while rebuilding leaves the log alone."""
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        before = self.log.read_text()
        with self.commit_fails(), self.assertRaises(ElError) as ctx:
            self.interp.journal.rebuild()
        self.assertIn("cannot write database", str(ctx.exception))
        self.assertEqual(self.log.read_text(), before)

    def test_appends_are_synced(self) -> None:
        """Each append is fsynced before the database commits."""
        with unittest.mock.patch("os.fsync") as fsync:
            self.interp.execute("CREATE ACCOUNT a 1 EUR")
        fsync.assert_called_once()

    def test_log_that_cannot_be_read(self) -> None:
        """A log path that is a directory is reported."""
        with self.assertRaises(ElError) as ctx:
            read_log_state(Path(self.tmp.name))
        self.assertIn("cannot read", str(ctx.exception))

    def test_replay_skips_blank_lines_and_comments(self) -> None:
        """Blank lines and # comments in the log are ignored on rebuild."""
        self.log.write_text(
            "# written by hand\n\n2026-01-01 10:00 CREATE ACCOUNT a 5 EUR\n"
        )
        self.db.unlink()
        self.assertEqual(self.fresh().ledger.accounts["a"].balance, Decimal("5.00"))

    def test_unknown_operation_row_is_a_bug(self) -> None:
        """Only the five operations can be stored."""
        with self.assertRaises(TypeError):
            _transaction_row(
                Ledger(),
                "2026-01-01 10:00",
                object(),  # ty: ignore[invalid-argument-type]
            )


class TestMain(ElTestCase):
    """Command-line paths not covered by the other tests."""

    def run_quiet(self, *args: str) -> tuple[int, str, str]:
        """Run el with stdout and stderr captured."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.run_cli(*args)
        return code, out.getvalue(), err.getvalue()

    def test_exit_in_batch_keeps_what_ran(self) -> None:
        """EXIT stops a -c batch and keeps the instructions before it."""
        code, _, _ = self.run_quiet(
            "-c", "CREATE ACCOUNT a 1 EUR; EXIT; CREATE ACCOUNT b 1 EUR"
        )
        self.assertEqual(code, 0)
        self.assertEqual(list(self.fresh().ledger.accounts), ["a"])

    def test_command_cannot_combine_with_c(self) -> None:
        """The version and rebuild commands can't be combined with -c or -f."""
        with self.assertRaises(SystemExit) as ctx:
            self.run_quiet("-c", "LIST ACCOUNT", "version")
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_dash_f_file(self) -> None:
        """An unreadable -f file is an error."""
        code, _, err = self.run_quiet("-f", str(Path(self.tmp.name) / "missing.el"))
        self.assertEqual(code, 1)
        self.assertIn("cannot read", cells(err)[1][0])

    def test_bad_dash_c_quoting(self) -> None:
        """An unterminated quote in -c is reported and exits 1."""
        code, out, err = self.run_quiet("-c", "LIST 'x")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(cells(err), [["ERROR"], ["unterminated quote"]])

    def test_rebuild_json(self) -> None:
        """`el --json rebuild` names both files."""
        code, out, _ = self.run_quiet("--json", "rebuild")
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out), [{"rebuilt": str(self.db), "from": str(self.log)}]
        )

    def test_module_entry_points(self) -> None:
        """`python -m elisa` and running elisa/cli.py both call main."""
        argv = ["el", "--log", str(self.log), "--db", str(self.db), "version"]
        for run in (
            lambda: runpy.run_module("elisa", run_name="__main__"),
            lambda: runpy.run_path(elisa.cli.__file__, run_name="__main__"),
        ):
            with (
                self.subTest(run=run),
                unittest.mock.patch("sys.argv", argv),
                contextlib.redirect_stdout(io.StringIO()) as out,
                self.assertRaises(SystemExit) as ctx,
            ):
                run()
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn(get_version(), out.getvalue())


class TestRepl(ElTestCase):
    """The interactive interpreter."""

    def run_repl(
        self, *inputs: object, json_output: bool = False
    ) -> tuple[int, str, str]:
        """Feed `inputs` to input() (exceptions are raised) and run el."""
        out, err = io.StringIO(), io.StringIO()
        flags = ["--json"] if json_output else []
        with (
            unittest.mock.patch("builtins.input", side_effect=list(inputs)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = self.run_cli(*flags)
        return code, out.getvalue(), err.getvalue()

    def test_session(self) -> None:
        """Commands run, errors are shown and the session goes on until EXIT."""
        code, out, err = self.run_repl(
            "CREATE ACCOUNT a 1 EUR", "FOO", KeyboardInterrupt(), "LIST ACCOUNT", "EXIT"
        )
        self.assertEqual(code, 0)
        rows = cells(out)
        self.assertIn(["Ledger log", str(self.log)], rows)
        self.assertIn(["Database", str(self.db)], rows)
        self.assertEqual(rows.count(["a", "1.00", "EUR"]), 2)
        self.assertEqual(cells(err), [["ERROR"], ["unknown command 'FOO' (try HELP)"]])
        self.assertEqual(self.fresh().ledger.accounts["a"].balance, Decimal("1.00"))

    def test_end_of_input_quits(self) -> None:
        """Ctrl-D ends the session cleanly."""
        code, out, _ = self.run_repl(EOFError())
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("\n"))

    def test_json_session(self) -> None:
        """With --json the banner, results and errors are JSON lines."""
        code, out, err = self.run_repl("LIST ACCOUNT", "FOO", "EXIT", json_output=True)
        self.assertEqual(code, 0)
        banner, listing = (json.loads(line) for line in out.splitlines())
        self.assertEqual(banner[0], {"setting": "Ledger log", "value": str(self.log)})
        self.assertEqual(listing, [])
        self.assertEqual(json.loads(err), {"error": "unknown command 'FOO' (try HELP)"})


class TestVersionFallback(unittest.TestCase):
    """Where the version comes from when pyproject.toml can't be used."""

    def version_with(self, text: str | None) -> str:
        """Return get_version() with pyproject.toml replaced by `text`."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pyproject.toml"
            if text is not None:
                path.write_text(text)
            with unittest.mock.patch.object(elisa.version, "PYPROJECT", path):
                return get_version()

    def test_falls_back_to_package_metadata(self) -> None:
        """Missing, invalid or foreign pyproject.toml files use the metadata."""
        installed = metadata.version("elisa")
        for text in (None, "not = [toml", '[project]\nname = "other"\nversion = "9"\n'):
            with self.subTest(text=text):
                self.assertEqual(self.version_with(text), installed)

    def test_unknown_without_metadata(self) -> None:
        """Without pyproject.toml or metadata the version is unknown."""
        with unittest.mock.patch.object(
            metadata, "version", side_effect=metadata.PackageNotFoundError
        ):
            self.assertEqual(self.version_with(None), "unknown")


class TestConcurrency(ElTestCase):
    """Two el processes on the same files don't overwrite each other."""

    def session(self) -> Interpreter:
        """Open another interpreter on the same files, like a second terminal."""
        return Interpreter(Journal(self.log, self.db), out=lambda _: None)

    def test_no_lost_updates(self) -> None:
        """Changes from two open sessions both reach the database."""
        self.interp.execute("CREATE ACCOUNT a 100 EUR")
        first, second = self.session(), self.session()
        first.execute("SUB a 30 rent")
        second.execute("SUB a 50 food")
        self.assertEqual(self.query("SELECT balance FROM accounts"), [("20.00",)])
        self.assertEqual(second.ledger.accounts["a"].balance, Decimal("20.00"))
        journal = Journal(self.log, self.db)
        self.assertFalse(journal.log_changed)
        journal.rebuild()
        self.assertEqual(journal.ledger.accounts["a"].balance, Decimal("20.00"))

    def test_sees_changes_from_other_sessions(self) -> None:
        """Each instruction starts from what the database holds now."""
        other = self.session()
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        other.execute("LIST ACCOUNT")
        self.assertIn("a", other.ledger.accounts)
        other.execute("DELETE ACCOUNT a")
        with self.assertRaises(ElError) as ctx:
            self.interp.execute("ADD a 1 x")
        self.assertIn("deleted", str(ctx.exception))

    def test_stale_journal_is_checked_against_database(self) -> None:
        """Operations are checked again against the state inside the lock."""
        self.interp.execute("CREATE ACCOUNT a 1 EUR")
        stale = Journal(self.log, self.db)
        self.interp.execute("DELETE ACCOUNT a")
        before = self.log.read_text()
        with self.assertRaises(ElError) as ctx:
            stale.commit([Add("a", Decimal("1.00"))])
        self.assertIn("deleted", str(ctx.exception))
        self.assertEqual(self.log.read_text(), before)

    def test_batch_is_applied_to_current_state(self) -> None:
        """A -c batch is re-applied on top of changes saved while it ran."""
        self.interp.execute("CREATE ACCOUNT a 100 EUR")
        batch = Journal(self.log, self.db)
        with batch.transaction():
            batch.commit([Add("a", Decimal("-10.00"))])
            self.interp.execute("SUB a 5 meanwhile")
        self.assertEqual(batch.ledger.accounts["a"].balance, Decimal("85.00"))
        self.assertEqual(self.query("SELECT balance FROM accounts"), [("85.00",)])


class TestLogRecovery(ElTestCase):
    """The log stays usable after a write was interrupted or the log edited."""

    TORN = "2026-11-24 07:00 SUB a 5"

    def setUp(self) -> None:
        """Start with one committed account."""
        super().setUp()
        self.interp.execute("CREATE ACCOUNT a 100 EUR")
        self.committed = self.log.read_text()
        self.aside = self.log.with_name("ledger.log.interrupted")

    def append(self, text: str) -> None:
        """Append raw text to the log, as a crashed el or an editor would."""
        with self.log.open("a") as fh:
            fh.write(text)

    def test_interrupted_write_is_moved_aside(self) -> None:
        """A cut-off line after the committed size is moved to .interrupted."""
        tail = f"2026-11-24 07:00 SUB a 50.00 x\n{self.TORN}"
        self.append(tail)
        journal = Journal(self.log, self.db)
        self.assertEqual(journal.recovered, self.aside)
        self.assertFalse(journal.log_changed)
        self.assertEqual(self.log.read_text(), self.committed)
        self.assertEqual(self.aside.read_text(), tail)
        self.fresh().execute("SUB a 1 x")
        self.assertEqual(self.log_ops()[-1], "SUB a 1.00 x")

    def test_cli_warns_about_recovery(self) -> None:
        """The CLI says where the interrupted write was saved."""
        self.append(self.TORN)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_cli("-c", "LIST ACCOUNT"), 0)
        self.assertIn(str(self.aside), err.getvalue())
        self.assertNotIn("el rebuild", err.getvalue())

    def test_rebuild_moves_interrupted_write_aside(self) -> None:
        """Rebuilding first moves the cut-off line away instead of failing."""
        self.append(self.TORN)
        self.interp.journal.rebuild()
        self.assertEqual(self.interp.journal.recovered, self.aside)
        self.assertEqual(self.interp.ledger.accounts["a"].balance, Decimal("100.00"))

    def test_complete_lines_are_left_for_rebuild(self) -> None:
        """Whole lines added by hand are kept and reported, not moved."""
        self.append("2026-11-24 07:00 SUB a 5.00 cash\n")
        journal = Journal(self.log, self.db)
        self.assertIsNone(journal.recovered)
        self.assertTrue(journal.log_changed)

    def test_hand_edit_before_committed_size_is_not_touched(self) -> None:
        """A log changed by hand earlier on is never cut."""
        self.log.write_text(self.committed.replace("100.00", "200.00") + self.TORN)
        journal = Journal(self.log, self.db)
        self.assertIsNone(journal.recovered)
        self.assertTrue(journal.log_changed)
        self.assertTrue(self.log.read_text().endswith(self.TORN))

    def test_changed_log_blocks_writes_until_rebuild(self) -> None:
        """While the log differs from the database, el refuses to write."""
        self.append("2026-11-24 07:00 SUB a 5.00 cash\n")
        interp = self.fresh()
        with self.assertRaises(ElError) as ctx:
            interp.execute("ADD a 1 x")
        self.assertIn("run `el rebuild` before making changes", str(ctx.exception))
        interp.execute("LIST ACCOUNT")  # reading still works
        interp.journal.rebuild()
        interp.execute("ADD a 1 x")
        self.assertEqual(interp.ledger.accounts["a"].balance, Decimal("96.00"))

    def test_replay_refuses_incomplete_last_line(self) -> None:
        """A last line without a newline is never applied silently."""
        self.append(self.TORN)
        self.db.unlink()
        with self.assertRaises(ElError) as ctx:
            self.fresh()
        self.assertIn("ledger.log:2: the last line is incomplete", str(ctx.exception))

    def test_repair_failure_is_reported(self) -> None:
        """If the log can't be cut, el says so instead of guessing."""
        self.append(self.TORN)
        with (
            unittest.mock.patch.object(
                Journal, "_truncate", side_effect=OSError(13, "denied")
            ),
            self.assertRaises(ElError) as ctx,
        ):
            Journal(self.log, self.db)
        self.assertIn("cannot repair", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
