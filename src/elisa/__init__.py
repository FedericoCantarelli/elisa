"""elisa: track bank accounts, budgets and expenses.

The package is both a library and the `el` command line tool (see
`elisa.cli`). Importing `elisa` does not start or configure the CLI; it gives
access to the domain model and storage:

    from pathlib import Path

    from elisa import Interpreter, Journal, format_json

    journal = Journal(Path("ledger.log"), Path("data.db"))
    results = []
    el = Interpreter(journal, out=results.append, fmt=format_json)
    el.execute("CREATE ACCOUNT checking 100 EUR")
    el.execute("LIST ACCOUNT")  # results[-1] is a JSON array of accounts

`Journal` keeps the SQLite database and the ledger log in sync, `Ledger` is
the in-memory state, and the operations (`CreateAccount`, `CreateBudget`,
`DeleteAccount`, `DeleteBudget`, `Add`) are what `Journal.commit` writes.
Every user-facing failure raises `ElError`.
"""

from elisa.cli import main
from elisa.interpreter import Interpreter
from elisa.interpreter import split_instructions
from elisa.ledger import CURRENCIES
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
from elisa.ledger import parse_amount
from elisa.ledger import parse_log_line
from elisa.storage import Database
from elisa.storage import Journal
from elisa.table import Formatter
from elisa.table import format_json
from elisa.table import format_table
from elisa.version import get_version


__version__ = get_version()

__all__ = [
    "CURRENCIES",
    "Account",
    "Add",
    "Budget",
    "CreateAccount",
    "CreateBudget",
    "Database",
    "DeleteAccount",
    "DeleteBudget",
    "ElError",
    "Formatter",
    "Interpreter",
    "Journal",
    "Ledger",
    "Operation",
    "__version__",
    "format_json",
    "format_table",
    "get_version",
    "main",
    "parse_amount",
    "parse_log_line",
    "split_instructions",
]
