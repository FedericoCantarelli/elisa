"""Parse el instructions and turn them into log operations.

ADD, SUB and MV all become `Add` operations with a signed amount:

    ADD acc 10 desc      ->  Add(acc, +10, desc)
    SUB acc 10 desc      ->  Add(acc, -10, desc)
    MV  a b 10 [desc]    ->  Add(a, -10, desc), Add(b, +10, desc)

A negative `Add` is written to the log as SUB, see `elisa.ledger`.
"""

import shlex
import sys
from collections.abc import Callable
from decimal import Decimal

from elisa.ledger import Add
from elisa.ledger import CreateAccount
from elisa.ledger import CreateBudget
from elisa.ledger import DeleteAccount
from elisa.ledger import DeleteBudget
from elisa.ledger import ElError
from elisa.ledger import Ledger
from elisa.ledger import Operation
from elisa.ledger import parse_amount
from elisa.ledger import parse_currency
from elisa.storage import Journal
from elisa.table import format_table


HELP_HEADER = ["COMMAND", "DESCRIPTION"]
HELP = [
    ["CREATE ACCOUNT <name> <starting balance> <currency>", "New account"],
    [
        "CREATE BUDGET <name> <max budget> <currency> [description]",
        "New budget, with an optional description",
    ],
    ["DELETE ACCOUNT <name>", "Soft delete an account"],
    ["DELETE BUDGET <name>", "Soft delete a budget"],
    ["LIST ACCOUNT [ALL]", "List accounts, ALL includes deleted ones"],
    ["LIST BUDGET [ALL]", "List budgets, ALL includes deleted ones"],
    ["ADD <account> <amount> <description>", "Add money to an account"],
    ["SUB <account> <amount> <description>", "Take money from an account"],
    [
        "MV <source account> <dest account or budget> <amount> [description]",
        "Transfer to an account or pay into a budget of the same currency",
    ],
    ["HELP", "Show this table"],
    ["EXIT", "Leave the interpreter"],
    ["", "Keywords are case-insensitive, quote names with spaces"],
    ["", "Currencies: EUR, KWD"],
]

MV_USAGE = "MV <source account> <dest account or budget> <amount> [description]"
LIST_USAGE = "LIST ACCOUNT|BUDGET [ALL]"


def split_instructions(text: str) -> list[str]:
    """Split on semicolons that are not inside quotes (used by `el -c`)."""
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for ch in text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch == ";":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if quote:
        raise ElError("unterminated quote")
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def write_stdout(text: str) -> None:
    """Write one table of output to stdout."""
    sys.stdout.write(text + "\n")


def _positive(text: str) -> Decimal:
    amount = parse_amount(text)
    if amount <= 0:
        raise ElError(f"amount must be greater than zero: {text!r}")
    return amount


def _kind(word: str) -> str:
    kind = word.upper()
    if kind in ("ACCOUNT", "ACCOUNTS"):
        return "ACCOUNT"
    if kind in ("BUDGET", "BUDGETS"):
        return "BUDGET"
    raise ElError(f"expected ACCOUNT or BUDGET, got {word!r}")


def _usage(text: str) -> ElError:
    return ElError(f"usage: {text}")


class Interpreter:
    """Runs el instructions against a journal."""

    def __init__(
        self, journal: Journal, out: Callable[[str], None] = write_stdout
    ) -> None:
        """Create an interpreter that writes its output through `out`."""
        self.journal = journal
        self.out = out

    @property
    def ledger(self) -> Ledger:
        """The current in-memory state."""
        return self.journal.ledger

    def execute(self, line: str) -> bool:
        """Run one instruction.

        Returns:
            False if the instruction was EXIT, True otherwise.

        Raises:
            ElError: If the instruction is invalid.
        """
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError as exc:
            raise ElError(str(exc)) from None
        if not tokens:
            return True
        verb, args = tokens[0].upper(), tokens[1:]
        if verb in ("EXIT", "QUIT"):
            return False
        handler = getattr(self, f"_cmd_{verb.lower()}", None)
        if handler is None:
            raise ElError(f"unknown command {tokens[0]!r} (try HELP)")
        handler(args)
        return True

    # ---- translation into log operations -------------------------------

    def translate(self, verb: str, args: list[str]) -> list[Operation]:
        """Turn a CREATE, DELETE, ADD, SUB or MV instruction into operations."""
        verb = verb.upper()
        if verb == "CREATE":
            return self._translate_create(args)
        if verb == "DELETE":
            if len(args) != 2:
                raise _usage("DELETE ACCOUNT|BUDGET <name>")
            if _kind(args[0]) == "ACCOUNT":
                return [DeleteAccount(args[1])]
            return [DeleteBudget(args[1])]
        if verb in ("ADD", "SUB"):
            if len(args) < 2:
                raise _usage(f"{verb} <account> <amount> <description>")
            name, amount, *desc = args
            self.ledger.account(name)  # ADD and SUB only target accounts
            value = _positive(amount)
            signed = value if verb == "ADD" else -value
            return [Add(name, signed, " ".join(desc))]
        if verb == "MV":
            return self._translate_mv(args)
        raise ElError(f"{verb} does not produce log operations")

    def _translate_create(self, args: list[str]) -> list[Operation]:
        if len(args) >= 4 and _kind(args[0]) == "BUDGET":
            _, name, amount, currency, *desc = args
            max_amount = parse_amount(amount, "max budget")
            currency = parse_currency(currency)
            return [CreateBudget(name, max_amount, currency, " ".join(desc))]
        if len(args) != 4:
            raise _usage(
                "CREATE ACCOUNT <name> <starting balance> <currency>"
                " | CREATE BUDGET <name> <max budget> <currency> [description]"
            )
        _, name, amount, currency = args
        _kind(args[0])
        balance = parse_amount(amount, "balance")
        return [CreateAccount(name, balance, parse_currency(currency))]

    def _translate_mv(self, args: list[str]) -> list[Operation]:
        if len(args) < 3:
            raise _usage(MV_USAGE)
        src, dst, amount, *desc = args
        source = self.ledger.account(src)
        if self.ledger.kind_of(dst) == "budget":
            dest = self.ledger.budget(dst)
        else:
            dest = self.ledger.account(dst)
        if src == dst:
            raise ElError("source and destination are the same")
        if source.currency != dest.currency:
            # An account only moves money to accounts and budgets in its own
            # currency: EUR with EUR, KWD with KWD.
            raise ElError(
                f"currency mismatch: {src!r} is {source.currency} but {dst!r}"
                f" is {dest.currency}"
            )
        value = _positive(amount)
        description = " ".join(desc) or f"MV {src} -> {dst}"
        return [Add(src, -value, description), Add(dst, value, description)]

    def _commit(self, verb: str, args: list[str]) -> list[Operation]:
        ops = self.translate(verb, args)
        self.journal.commit(ops)
        return ops

    # ---- commands -------------------------------------------------------

    def _cmd_create(self, args: list[str]) -> None:
        (op,) = self._commit("CREATE", args)
        self._show(op.name, description=True)

    def _cmd_delete(self, args: list[str]) -> None:
        (op,) = self._commit("DELETE", args)
        kind = "account" if isinstance(op, DeleteAccount) else "budget"
        self._table(["ACTION", "KIND", "NAME"], [["deleted", kind, op.name]])

    def _cmd_add(self, args: list[str]) -> None:
        self._commit("ADD", args)
        self._show(args[0])

    def _cmd_sub(self, args: list[str]) -> None:
        self._commit("SUB", args)
        self._show(args[0])

    def _cmd_mv(self, args: list[str]) -> None:
        self._commit("MV", args)
        self._show(args[1])

    def _cmd_list(self, args: list[str]) -> None:
        if not args or len(args) > 2:
            raise _usage(LIST_USAGE)
        include_deleted = False
        if len(args) == 2:
            if args[1].upper() != "ALL":
                raise _usage(LIST_USAGE)
            include_deleted = True
        if _kind(args[0]) == "ACCOUNT":
            self._list_accounts(include_deleted)
        else:
            self._list_budgets(include_deleted)

    def _cmd_help(self, args: list[str]) -> None:
        self._table(HELP_HEADER, HELP)

    # ---- output ---------------------------------------------------------

    def _show(self, name: str, description: bool = False) -> None:
        """Print the current state of one account or budget as a table.

        With `description`, a budget also shows its DESCRIPTION column.
        """
        if self.ledger.kind_of(name) == "budget":
            b = self.ledger.budgets[name]
            header = ["BUDGET", "MAX", "SPENT", "REMAINING", "CURRENCY"]
            row = [
                b.name,
                str(b.max_amount),
                str(b.spent),
                str(b.remaining),
                b.currency,
            ]
            if description:
                header.append("DESCRIPTION")
                row.append(b.description)
            self._table(header, [row], right={1, 2, 3})
        else:
            a = self.ledger.accounts[name]
            self._table(
                ["ACCOUNT", "BALANCE", "CURRENCY"],
                [[a.name, str(a.balance), a.currency]],
                right={1},
            )

    def _list_accounts(self, include_deleted: bool) -> None:
        header = ["ACCOUNT", "BALANCE", "CURRENCY"]
        rows = []
        for a in self.ledger.accounts.values():
            if a.deleted and not include_deleted:
                continue
            row = [a.name, str(a.balance), a.currency]
            if include_deleted:
                row.append("deleted" if a.deleted else "")
            rows.append(row)
        if include_deleted:
            header.append("STATUS")
        self._table(header, rows, right={1})

    def _list_budgets(self, include_deleted: bool) -> None:
        header = ["BUDGET", "MAX", "SPENT", "REMAINING", "CURRENCY", "DESCRIPTION"]
        rows = []
        for b in self.ledger.budgets.values():
            if b.deleted and not include_deleted:
                continue
            row = [
                b.name,
                str(b.max_amount),
                str(b.spent),
                str(b.remaining),
                b.currency,
                b.description,
            ]
            if include_deleted:
                row.insert(5, "deleted" if b.deleted else "")
            rows.append(row)
        if include_deleted:
            header.insert(5, "STATUS")
        self._table(header, rows, right={1, 2, 3})

    def _table(
        self, header: list[str], rows: list[list[str]], right: set[int] = frozenset()
    ) -> None:
        """Write one table through `out`; every interpreter output goes here."""
        self.out(format_table(header, rows, align_right=right))
