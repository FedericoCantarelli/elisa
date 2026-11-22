"""Entry point for the `el` command.

    el                      start the interactive interpreter
    el -c "<i1>; <i2>;"     run semicolon-separated instructions
    el -f <file>            run a newline-delimited file of instructions
    el version              print the version from pyproject.toml
    el rebuild              rebuild the database from the ledger log

The SQLite database defaults to ~/.elisa/data.db (change it with --db) and the
ledger log to ~/.elisa/ledger.log (change it with --log).
"""

import argparse
import contextlib
import sys
from pathlib import Path

from elisa.interpreter import Interpreter
from elisa.interpreter import split_instructions
from elisa.ledger import ElError
from elisa.storage import Journal
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


def _err(text: str) -> None:
    sys.stderr.write(f"error: {text}\n")


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
        _err(f"{exc}\nnothing was saved")
        return 1
    return 0


def repl(interp: Interpreter, args: argparse.Namespace) -> int:
    """Run the interactive interpreter until EXIT or end of input."""
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401  (line editing and history)
    sys.stdout.write(f"Ledger log: {log_path(args.log)}\n")
    sys.stdout.write(f"Database:   {db_path(args.db)}\n")
    sys.stdout.write("Type HELP for commands, EXIT or Ctrl-D to quit.\n")
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
            _err(str(exc))


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
        "command",
        nargs="?",
        choices=["version", "rebuild"],
        help="version: print the el version; "
        "rebuild: rebuild the database from the ledger log",
    )
    args = parser.parse_args(argv)

    if args.command and (args.c is not None or args.f is not None):
        parser.error(f"{args.command} cannot be combined with -c or -f")
    if args.command == "version":
        sys.stdout.write(f"el {get_version()}\n")
        return 0

    try:
        journal = Journal(log_path(args.log), db_path(args.db))
        if args.command == "rebuild":
            journal.rebuild()
            sys.stdout.write(f"rebuilt {journal.db.path} from {journal.path}\n")
            return 0
        if journal.log_changed:
            sys.stderr.write(
                f"warning: {journal.path} changed since el last wrote it;"
                " run `el rebuild` to update the database from it\n"
            )
        interp = Interpreter(journal)
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
        _err(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
