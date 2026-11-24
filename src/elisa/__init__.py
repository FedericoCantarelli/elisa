"""Entry point for the `el` command.

    el                      start the interactive interpreter
    el -c "<i1>; <i2>;"     run semicolon-separated instructions
    el -f <file>            run a newline-delimited file of instructions
    el version              print the version from pyproject.toml
    el rebuild              rebuild the database from the ledger log

The SQLite database defaults to ~/.elisa/data.db (change it with --db) and the
ledger log to ~/.elisa/ledger.log (change it with --log).

Everything el writes, including errors and warnings on stderr, is an ASCII
table made by `elisa.table.format_table`. With --json it is JSON instead: one
line per table, an array of row objects (see `elisa.table.format_json`), and
errors and warnings become {"error": ...} and {"warning": ...} objects.
"""

import argparse
import contextlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from elisa.interpreter import Interpreter
from elisa.interpreter import split_instructions
from elisa.ledger import ElError
from elisa.storage import Journal
from elisa.table import Formatter
from elisa.table import format_json
from elisa.table import format_table
from elisa.version import get_version


DEFAULT_LOG = Path.home() / ".elisa" / "ledger.log"
DEFAULT_DB = Path.home() / ".elisa" / "data.db"


def log_path(cli_value: str | None) -> Path:
    """Return the --log path if given, else the default ledger log."""
    if cli_value:
        return Path(cli_value).expanduser()
    return DEFAULT_LOG


def db_path(cli_value: str | None) -> Path:
    """Return the --db path if given, else the default database."""
    if cli_value:
        return Path(cli_value).expanduser()
    return DEFAULT_DB


def _write_table(
    fmt: Formatter,
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    stream: TextIO | None = None,
) -> None:
    """Write a table, formatted with `fmt`, to `stream` (stdout by default)."""
    (stream or sys.stdout).write(fmt(header, rows) + "\n")


def _notice(fmt: Formatter, kind: str, *lines: str) -> None:
    """Write an error or warning to stderr.

    As a table it has one column named after `kind` and a row per line of the
    message; as JSON it is one object, {kind: message}.
    """
    lines = tuple(line for text in lines for line in text.split("\n"))
    if fmt is format_json:
        sys.stderr.write(json.dumps({kind: "\n".join(lines)}, ensure_ascii=False))
        sys.stderr.write("\n")
    else:
        _write_table(fmt, [kind.upper()], [[line] for line in lines], sys.stderr)


def _err(fmt: Formatter, *lines: str) -> None:
    """Write an error to stderr, see `_notice`."""
    _notice(fmt, "error", *lines)


def run_batch(interp: Interpreter, instructions: list[tuple[str, str]]) -> int:
    """Run instructions as one transaction.

    If any instruction fails, nothing from the batch is saved.

    Args:
        interp: The interpreter to run them with.
        instructions: Pairs of (location for error messages, instruction).

    Returns:
        The process exit code: 0 on success, 1 on error.
    """
    try:
        with interp.journal.transaction():
            for where, line in instructions:
                try:
                    if not interp.execute(line):
                        break
                except ElError as exc:
                    raise ElError(f"{where}: {exc}") from None
    except ElError as exc:
        _err(interp.fmt, str(exc), "nothing was saved")
        return 1
    return 0


def repl(interp: Interpreter, args: argparse.Namespace) -> int:
    """Run the interactive interpreter until EXIT or end of input."""
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401  (line editing and history)
    _write_table(
        interp.fmt,
        ["SETTING", "VALUE"],
        [
            ["Ledger log", str(log_path(args.log))],
            ["Database", str(db_path(args.db))],
        ],
    )
    while True:
        try:
            line = input("el> ")
        except EOFError:
            sys.stdout.write("\n")
            return 0
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            continue
        try:
            if not interp.execute(line):
                return 0
        except ElError as exc:
            _err(interp.fmt, str(exc))


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run el."""
    parser = argparse.ArgumentParser(
        prog="el", description="Track accounts, budgets and expenses."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-c",
        metavar="INSTRUCTIONS",
        help='instructions separated by semicolons, e.g. "LIST ACCOUNT; LIST BUDGET;"',
    )
    mode.add_argument("-f", metavar="FILE", help="file with one instruction per line")
    parser.add_argument(
        "--log",
        metavar="PATH",
        help=f"ledger log of every action (default: {DEFAULT_LOG})",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"SQLite database with all transactions (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print output as JSON, one line per result, instead of tables",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["version", "rebuild"],
        help="version: print the el version; "
        "rebuild: rebuild the database from the ledger log",
    )
    args = parser.parse_args(argv)
    fmt = format_json if args.json else format_table

    if args.command and (args.c is not None or args.f is not None):
        parser.error(f"{args.command} cannot be combined with -c or -f")
    if args.command == "version":
        _write_table(fmt, ["PROGRAM", "VERSION"], [["el", get_version()]])
        return 0

    try:
        journal = Journal(log_path(args.log), db_path(args.db))
        if args.command == "rebuild":
            journal.rebuild()
            _write_table(
                fmt, ["REBUILT", "FROM"], [[str(journal.db.path), str(journal.path)]]
            )
            return 0
        if journal.log_changed:
            _notice(
                fmt,
                "warning",
                f"{journal.path} changed since el last wrote it",
                "run `el rebuild` to update the database from it",
            )
        interp = Interpreter(journal, fmt=fmt)
        if args.c is not None:
            parts = split_instructions(args.c)
            numbered = [(f"instruction {i}", p) for i, p in enumerate(parts, 1)]
            return run_batch(interp, numbered)
        if args.f is not None:
            path = Path(args.f)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ElError(f"cannot read {path}: {exc.strerror}") from None
            lines = [(f"{path}:{n}", ln) for n, ln in enumerate(text.splitlines(), 1)]
            return run_batch(interp, lines)
        return repl(interp, args)
    except ElError as exc:
        _err(fmt, str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
