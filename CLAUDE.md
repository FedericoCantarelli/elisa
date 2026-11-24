# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`el` (package `elisa`) is a stdlib-only Python 3.13+ CLI for tracking bank accounts, budgets and expenses. The README is the user-facing spec for command syntax and log format, and `docs/reference.md` documents every command with real output examples. Keep them, the `HELP` rows in `interpreter.py`, and the module docstrings in sync when behavior changes; regenerate reference examples by running `el` against throwaway files rather than writing tables by hand.

## Commands

```sh
uv sync                                          # install (dev group includes ruff)
uv run el                                        # interactive interpreter
uv run el --log /tmp/l.log --db /tmp/d.db -c "LIST ACCOUNT;"   # throwaway files, never ~/.elisa/
uv run ruff check .                              # lint (ruff.toml)
uv run ruff format --check .                     # format check
uv run python -m unittest discover -s tests      # all tests
uv run python -m unittest tests.test_el.TestLogFormat.test_sub_is_logged_as_sub   # single test
```

`uv run` re-syncs and may rewrite `uv.lock`; use `uv run --frozen ...` so it stays untouched unless dependencies are meant to change.

`ElTestCase` gives each test its own `ledger.log` and `data.db` in a temp dir; `self.query(sql)` reads the test database and `self.output` collects the interpreter's output, one string per table. Use the module-level `cells(table)` helper to turn a table into header + rows of stripped cells instead of matching raw text; `TestTable` covers `format_table` itself. Tests that call `run_cli` without redirecting stdout print tables to the console, which is expected.

Tests use `unittest`, not pytest. Without network/`uv`, `PYTHONPATH=src python3 -m unittest discover -s tests` also works.

## Architecture

Five modules under `src/elisa/`:

- **`ledger.py`** — domain model and log format. `Ledger` holds in-memory `Account`/`Budget` state; operations and `parse_log_line` define the log lines.
- **`storage.py`** — persistence. `Journal` wraps a `Ledger` with a `Database` (SQLite, the primary store) and the append-only ledger log. The database has `transactions` (every operation, signed `amount`, timestamp), `accounts`, `budgets` (current state; budgets have a `description`, added to older databases by `_add_missing_columns`) and `meta` (log size and SHA-256 after the last write). On startup `Journal` loads state from the database; only a new/empty database (no `meta`) is built by replaying the log. If `meta` doesn't match the log on disk, `Journal.log_changed` is set and the CLI warns; `Journal.rebuild()` / `el rebuild` replaces the database with a replay of the log. `Journal.transactions(name)` returns one target's rows from `transactions` plus any unsaved `_pending` records, so `LIST TRANSACTION` sees earlier commits of the same `-c`/`-f` batch.
- **`interpreter.py`** — parses instruction text (via `shlex`) and translates it into log `Operation`s. Commands dispatch by name to `_cmd_<verb>` methods; output goes through an injectable `out` callable that receives one whole table per call (tests capture it). `HELP` is a list of `[command, description]` rows rendered with `HELP_HEADER`. `_show(name)` prints one account or budget as a table: `CREATE` shows the new item (budgets with `DESCRIPTION`), `ADD`/`SUB` the account, and `MV` only the destination. `DELETE` prints an `ACTION`, `KIND`, `NAME` table. `LIST TRANSACTION <name>` (handled in `_cmd_list` before the `ALL` check; not a `_kind`) works on deleted names too.
- **`table.py`** — `format_table(header, rows, *, align_right=())` renders an ASCII table (`+---+` rules, `| cell |` rows, header-only when there are no rows, multi-line cells allowed, `ValueError` if a row's length doesn't match the header). It is the only way `el` writes output: the interpreter's `_table` passes each table to `out`, and `__init__.py`'s `_write_table`/`_err` use it for the REPL banner, `version`, `rebuild`, warnings and errors.
- **`__init__.py`** — argparse entry point (`el = elisa:main`), path resolution (`--log`, else `~/.elisa/ledger.log`; `--db`, else `~/.elisa/data.db`; no env vars), the `version` and `rebuild` commands, REPL, and batch mode for `-c`/`-f`.

Key invariants that span modules:

- **Only five operation types exist**: `CreateAccount`, `CreateBudget`, `DeleteAccount`, `DeleteBudget`, `Add`. Every money movement is an `Add` with a signed `Decimal`; a negative `Add` serializes as `SUB`, and `SUB` lines parse back to negative `Add`. `MV` is not an operation — the interpreter expands it into two `Add`s. Each op has `to_line()`, and `parse_log_line()` must round-trip it.
- **Atomicity**: `Journal.commit(ops)` dry-runs all ops on a deep copy (`Ledger.check`) before writing anything, so a multi-op instruction (MV) is all-or-nothing. `Journal.transaction()` additionally buffers records and restores the ledger on error; `run_batch` wraps all of `-c`/`-f` in one transaction. Every save is `Database.save`: `BEGIN IMMEDIATE`, insert `transactions` rows, upsert state, append the log lines, write `meta`, commit. A failure before the commit rolls back the database and leaves the log unchanged; a failed commit after the log write raises an `ElError` telling the user to run `el rebuild`.
- **Shared namespace**: account and budget names share one namespace (MV uses it to decide whether the destination is a budget). Deletes are soft; deleted names stay reserved.
- **Money rules**: amounts are `Decimal` quantized to cents, max two decimals; `CURRENCIES` is `EUR` and `KWD`; MV requires source and destination to share a currency (EUR with EUR, KWD with KWD). Account balances may go negative; budget `spent` may exceed max but never go below zero. `ADD`/`SUB` target accounts only; budgets are charged via `MV`.
- **Output is always tabular**: every stdout/stderr write goes through `format_table`; never write plain lines or call `self.out` with a non-table string. New commands pick their own columns and right-align amount columns via `align_right`. Only argparse's own usage/errors remain plain text.
- **Errors**: user-facing failures raise `ElError`; the CLI catches it and writes an `ERROR` table to stderr (`_err`, one row per message line).

`version.py` reads the version from `pyproject.toml` in a source checkout, falling back to package metadata. `el`, `elisa` and `python -m elisa` (via `__main__.py`) all run `elisa/__init__.py:main`.

## Conventions

- Ruff enforces Google-style docstrings (`D`), no `print` (`T20` — write to `sys.stdout`/`sys.stderr`), no relative imports, and one import per line (`force-single-line`) with two blank lines after imports. Output goes through `format_table`, not direct `sys.stdout.write` of plain text.
- Do not commit.

## Context Rules
- Do not read or search files inside `dist/`, `node_modules/`, or `logs/`
- Do not modify `uv.lock` manually; it is managed by the `uv` tool.
