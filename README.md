# elisa

`el` is a small command-line tool for tracking bank accounts, budgets and expenses.

## Install

```sh
uv sync            # or: uv tool install -e .
uv run el          # or: uv run python -m elisa
```

## Usage

```sh
el                                   # interactive interpreter
el -c "LIST ACCOUNT; LIST BUDGET;"   # semicolon-separated instructions
el -f script.el                      # one instruction per line
el --log path/to/ledger.log ...      # use another ledger log
el --db path/to/data.db ...          # use another database
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
| `ADD <account> <amount> <description>` | Add money to an account |
| `SUB <account> <amount> <description>` | Take money from an account |
| `MV <source> <dest account> <amount> [description]` | Transfer between accounts |
| `MV <source> <budget> <amount> [description]` | Pay an expense from an account into a budget |
| `HELP`, `EXIT` | Interpreter only |

See the [language reference](docs/reference.md) for every command's syntax,
examples and output.

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
If the log was changed outside of `el`, for example edited by hand, `el` keeps
using the database and prints a warning until you run `el rebuild`.

Inside the program every movement is an ADD with a signed amount; a negative
ADD is written as SUB. On a budget, ADD increases the amount spent. Account
balances may go negative. Budget spending may go over the maximum but never
below zero.

Each change is written to both files together: the database transaction is
committed only after the log line is written, and if either write fails
neither file changes. With `-c` and `-f` the instructions run as one
transaction: if any of them fails, nothing from that run is saved.

## Checks

```sh
uv run ruff check .            # lint, using ruff.toml
uv run ruff format --check .   # formatting
uv run python -m unittest discover -s tests
```

`pre-commit install` also runs ruff on every commit.
