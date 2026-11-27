# Elisa

A small CLI for tracking bank accounts, budgets and expenses. The main goal of Elisa is to give the user a tool that can be used both as CLI and as Python package, in order to let them decide how to use it.

## Install
To install as a standalone CLI use the following commands (this also add `el` and `elisa` to your `PATH`)
```sh
uv tool install git+https://github.com/FedericoCantarelli/elisa   # or: pipx install git+...
```
To use it as a library instead, add it to a project's dependencies:
```sh
uv add git+https://github.com/FedericoCantarelli/elisa   # or: pip install git+...
```
### Development
To run it as an editable tool you can follow these commands:

```sh
git clone https://github.com/FedericoCantarelli/elisa
cd elisa
uv tool install -e .
```

## Library

`import elisa` exposes the same engine the CLI uses, without starting the CLI:

```python
import json
from pathlib import Path

from elisa import ElError, Interpreter, Journal, format_json

journal = Journal(Path("ledger.log"), Path("data.db"))
results: list[str] = []
el = Interpreter(journal, out=results.append, fmt=format_json)

el.execute("CREATE ACCOUNT checking 1500 EUR")
el.execute("LIST ACCOUNT")
accounts = json.loads(results[-1])  # [{"account": "checking", ...}]

try:
    el.execute("SUB nowhere 10 lunch")
except ElError as exc:
    error = str(exc)  # "no account named 'nowhere'"

with journal.transaction():  # all-or-nothing, like `el -c`
    el.execute("CREATE ACCOUNT savings 0 EUR")
    el.execute("MV checking savings 100")
```

Each `execute` call passes one formatted table to `out` (`format_table` for
ASCII, `format_json` for JSON). 

For direct access, `journal.ledger` holds the
current `Account` and `Budget` objects, and `journal.commit([...])` writes
operations such as `Add`.

## Usage

```sh
el                                   # interactive interpreter
el -c "LIST ACCOUNT; LIST BUDGET;"   # semicolon-separated instructions
el -f script.el                      # one instruction per line
el --log path/to/ledger.log ...      # use another ledger log
el --db path/to/data.db ...          # use another database
el --json -c "LIST ACCOUNT;"         # JSON output instead of tables
el version                           # version from pyproject.toml
el rebuild                           # rebuild the database from the ledger log
```

The database is `~/.elisa/data.db` and the ledger log is `~/.elisa/ledger.log`,
unless you pass `--db` or `--log`.

## Commands

Keywords are case-insensitive. Quote names or descriptions that contain spaces
(`'savings account'`). An unquoted description takes the rest of the line.

| Command | Effect |
|---|---|
| `CREATE ACCOUNT <name> <starting balance> <currency>` | New account |
| `CREATE BUDGET <name> <max budget> <currency> [description]` | New budget, with an optional description |
| `DELETE ACCOUNT <name>` / `DELETE BUDGET <name>` | Soft delete |
| `LIST ACCOUNT [ALL]` / `LIST BUDGET [ALL]` | List, `ALL` includes deleted ones |
| `LIST TRANSACTION <account or budget>` | Every transaction on that account or budget, deleted ones included |
| `ADD <account> <amount> <description>` | Add money to an account |
| `SUB <account> <amount> <description>` | Take money from an account |
| `MV <source> <dest account> <amount> [description]` | Transfer between accounts |
| `MV <source> <budget> <amount> [description]` | Pay an expense from an account into a budget |
| `HELP`, `EXIT` | Interpreter only |

See the [language reference](docs/reference.md) for every command's syntax,
examples and output.

All output is printed as ASCII tables, errors and warnings included:

```
el> LIST ACCOUNT
+----------+---------+----------+
| ACCOUNT  | BALANCE | CURRENCY |
+----------+---------+----------+
| checking | 1600.00 | EUR      |
+----------+---------+----------+
```

With `--json` each result is one line of JSON instead, an array with an object
per row; errors and warnings are `{"error": ...}` / `{"warning": ...}` on stderr:

```sh
$ el --json -c "LIST ACCOUNT"
[{"account": "checking", "balance": "1600.00", "currency": "EUR"}]
```

The supported currencies are `EUR` and `KWD`. An account only works with
accounts and budgets in its own currency, so `MV` from an EUR account into a
KWD budget (or the other way round) is refused. Amounts take at most two
decimals.

## Storage

`el` keeps its data in a SQLite database, which you can also query with any
SQLite tool. It holds every transaction that ever happened and the current
state of accounts and budgets:

| Table | Columns |
|---|---|
| `transactions` | `id`, `timestamp`, `operation`, `target`, `amount`, `currency`, `description` |
| `accounts` | `position`, `name`, `currency`, `balance`, `deleted` |
| `budgets` | `position`, `name`, `currency`, `max_amount`, `spent`, `deleted`, `description` |
| `meta` | `key`, `value` (size and SHA-256 of the ledger log after the last write) |

`operation` is `CREATE ACCOUNT`, `CREATE BUDGET`, `DELETE ACCOUNT`,
`DELETE BUDGET`, `ADD` or `SUB`, and `amount` is signed (a `SUB` is negative).
Amounts are stored as text (`'12.50'`) so they stay exact, and `position` keeps
the creation order.

Every transaction is also appended to the ledger log, a newline-delimited file
of atomic actions. Each line starts with the UTC time the command was run
(`YYYY-MM-DD HH:MM`), followed by a `CREATE`, `DELETE`, `ADD` or `SUB`
operation. `MV` is written as a `SUB` on the source and an `ADD` on the
destination:

```
2026-11-04 06:31 CREATE ACCOUNT checking 1500.00 EUR
2026-11-04 06:31 CREATE BUDGET groceries 300.00 EUR 'food and markets'
2026-11-04 06:32 SUB checking 54.20 'weekly shopping'
2026-11-04 06:32 ADD groceries 54.20 'weekly shopping'
```

The log holds everything needed to rebuild the database from scratch. `el`
does that when the database is missing, and `el rebuild` does it on request.
If the log was changed outside of `el`, for example edited by hand, `el` prints
a warning and refuses to make changes until you run `el rebuild`; reading
(`LIST`, `HELP`) still works. A log whose last line has no newline at the end
is never replayed, because that line may have been cut short.

Each change is written to both files together: the database transaction is
committed only after the log lines are written and synced to disk, and if
either write fails neither file changes (lines already appended are cut off
again). 

With `-c` and `-f` the instructions run as one transaction: if any of
them fails, nothing from that run is saved.

Several `el` processes can use the same files at once, for example two
terminals. Each write holds the database's write lock, reloads the current
state, applies the change to it and saves, so one process never overwrites
what another saved; each instruction also starts from the current state.

If `el` is killed while it writes the log (a crash or power loss), the log can
end with a line that was cut short. The next `el` moves that incomplete write
to `ledger.log.interrupted`, next to the log, and prints a warning; the
database never saw it, so nothing else needs fixing.

## Checks

```sh
uv run ruff check .            # lint, using ruff.toml
uv run ruff format --check .   # formatting
uv run python -m unittest discover -s tests
```

`pre-commit install` also runs ruff on every commit.
