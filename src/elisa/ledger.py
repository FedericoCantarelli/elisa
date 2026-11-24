"""State of accounts and budgets, and the format of the ledger log.

The ledger log is a newline-delimited text file. Each line starts with the UTC time
the command was run, followed by one operation in command syntax:

    2026-11-04 06:30 CREATE ACCOUNT checking 100.00 EUR
    2026-11-04 06:31 CREATE BUDGET groceries 300.00 EUR
    2026-11-04 06:32 SUB checking 12.50 'weekly shopping'
    2026-11-04 06:32 ADD groceries 12.50 'weekly shopping'
    2026-11-04 06:40 DELETE ACCOUNT checking

Only CREATE, DELETE, ADD and SUB appear in the log. Inside the program every
movement of money is an `Add` with a signed amount: a negative `Add` is
written to the log as SUB, and a SUB line is read back as a negative `Add`.
Replaying the whole file from the top rebuilds the current state. Reading
and writing the files is done by `elisa.storage`.
"""

import copy
import shlex
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from decimal import Decimal


CURRENCIES = frozenset({"EUR", "KWD"})
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M"


class ElError(Exception):
    """A user-facing error: bad syntax, unknown account, and so on."""


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------


@dataclass
class Account:
    """A bank account with a balance, which may go negative."""

    name: str
    currency: str
    balance: Decimal
    deleted: bool = False


@dataclass
class Budget:
    """A spending budget. Spending may exceed the maximum."""

    name: str
    currency: str
    max_amount: Decimal
    spent: Decimal = ZERO
    deleted: bool = False
    description: str = ""

    @property
    def remaining(self) -> Decimal:
        """Amount left before the maximum is reached, negative once crossed."""
        return self.max_amount - self.spent


# --------------------------------------------------------------------------
# Log operations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateAccount:
    """Create an account with a starting balance."""

    name: str
    balance: Decimal
    currency: str

    def to_line(self) -> str:
        """Render the operation as a log line, without the timestamp."""
        return f"CREATE ACCOUNT {quote(self.name)} {self.balance} {self.currency}"


@dataclass(frozen=True)
class CreateBudget:
    """Create a budget with a maximum amount and an optional description."""

    name: str
    max_amount: Decimal
    currency: str
    description: str = ""

    def to_line(self) -> str:
        """Render the operation as a log line, without the timestamp."""
        line = f"CREATE BUDGET {quote(self.name)} {self.max_amount} {self.currency}"
        if self.description:
            line += f" {quote(self.description)}"
        return line


@dataclass(frozen=True)
class DeleteAccount:
    """Soft-delete an account."""

    name: str

    def to_line(self) -> str:
        """Render the operation as a log line, without the timestamp."""
        return f"DELETE ACCOUNT {quote(self.name)}"


@dataclass(frozen=True)
class DeleteBudget:
    """Soft-delete a budget."""

    name: str

    def to_line(self) -> str:
        """Render the operation as a log line, without the timestamp."""
        return f"DELETE BUDGET {quote(self.name)}"


@dataclass(frozen=True)
class Add:
    """Add a signed amount to an account or a budget.

    On an account it changes the balance. On a budget it changes the amount
    spent, so a positive Add on a budget is an expense charged to it.
    """

    target: str
    amount: Decimal
    description: str = ""

    def to_line(self) -> str:
        """Render as ADD, or as SUB with a positive amount when negative."""
        verb = "SUB" if self.amount < 0 else "ADD"
        line = f"{verb} {quote(self.target)} {abs(self.amount)}"
        if self.description:
            line += f" {quote(self.description)}"
        return line


Operation = CreateAccount | CreateBudget | DeleteAccount | DeleteBudget | Add


def quote(text: str) -> str:
    """Quote a name or description so that shlex reads it back intact."""
    return shlex.quote(text)


def parse_amount(text: str, what: str = "amount") -> Decimal:
    """Parse a money amount with at most two decimals.

    Args:
        text: The amount as typed, such as "12.5".
        what: What the amount is, used in error messages.

    Returns:
        The amount rounded to cents.

    Raises:
        ElError: If the text is not a finite number with two decimals at most.
    """
    try:
        value = Decimal(text)
        if not value.is_finite():
            raise ElError(f"invalid {what}: {text!r}")
        if value != value.quantize(CENT):
            raise ElError(f"invalid {what}: {text!r} has more than two decimals")
        return value.quantize(CENT)
    except ArithmeticError:  # quantize overflows on huge exponents, e.g. 1e40
        raise ElError(f"invalid {what}: {text!r}") from None


def parse_currency(text: str) -> str:
    """Return the upper-case ISO code, or raise ElError if it is unsupported."""
    code = text.upper()
    if code not in CURRENCIES:
        allowed = ", ".join(sorted(CURRENCIES))
        raise ElError(f"unsupported currency {text!r} (allowed: {allowed})")
    return code


def parse_log_line(line: str) -> tuple[str, Operation]:
    """Parse one log line into its timestamp and operation."""
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        raise ElError(f"malformed log line: {exc}") from None
    if len(tokens) < 3:
        raise ElError("malformed log line")
    date, time, verb, *args = tokens
    timestamp = f"{date} {time}"
    try:
        datetime.strptime(timestamp, TIMESTAMP_FORMAT)
    except ValueError:
        raise ElError(f"invalid timestamp {timestamp!r}") from None
    verb = verb.upper()
    kind = args[0].upper() if args else ""
    if verb == "CREATE" and kind == "ACCOUNT" and len(args) == 4:
        op = CreateAccount(
            args[1], parse_amount(args[2], "balance"), parse_currency(args[3])
        )
    elif verb == "CREATE" and kind == "BUDGET" and len(args) in (4, 5):
        op = CreateBudget(
            args[1],
            parse_amount(args[2], "max budget"),
            parse_currency(args[3]),
            args[4] if len(args) == 5 else "",
        )
    elif verb == "DELETE" and kind == "ACCOUNT" and len(args) == 2:
        op = DeleteAccount(args[1])
    elif verb == "DELETE" and kind == "BUDGET" and len(args) == 2:
        op = DeleteBudget(args[1])
    elif verb in ("ADD", "SUB") and len(args) in (2, 3):
        amount = parse_amount(args[1])
        if verb == "SUB":
            amount = -amount
        op = Add(args[0], amount, args[2] if len(args) == 3 else "")
    else:
        raise ElError(f"unknown log operation: {line!r}")
    return timestamp, op


def now_utc() -> str:
    """Return the current UTC time in the log's timestamp format."""
    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------


@dataclass
class Ledger:
    """In-memory state rebuilt from the log.

    Account and budget names share one namespace, so that MV can tell
    whether its destination is an account or a budget. A soft-deleted name
    stays reserved.
    """

    accounts: dict[str, Account] = field(default_factory=dict)
    budgets: dict[str, Budget] = field(default_factory=dict)

    def apply(self, op: Operation) -> None:
        """Validate and apply one operation.

        Raises:
            ElError: If the operation is not valid in the current state.
        """
        match op:
            case CreateAccount(name, balance, currency):
                self._check_free(name)
                self.accounts[name] = Account(name, currency, balance)
            case CreateBudget(name, max_amount, currency, description):
                self._check_free(name)
                if max_amount <= 0:
                    raise ElError("max budget must be greater than zero")
                self.budgets[name] = Budget(
                    name, currency, max_amount, description=description
                )
            case DeleteAccount(name):
                self.account(name).deleted = True
            case DeleteBudget(name):
                self.budget(name).deleted = True
            case Add(target, amount, _):
                if target in self.accounts:
                    self.account(target).balance += amount
                elif target in self.budgets:
                    budget = self.budget(target)
                    if budget.spent + amount < 0:
                        raise ElError(f"budget {target!r} cannot go below zero")
                    budget.spent += amount
                else:
                    raise ElError(f"no account or budget named {target!r}")
            case _:
                raise TypeError(f"unknown operation {op!r}")

    def check(self, ops: list[Operation]) -> None:
        """Raise ElError if the operations would not all apply cleanly."""
        trial = copy.deepcopy(self)
        for op in ops:
            trial.apply(op)

    def account(self, name: str) -> Account:
        """Return a live account, or raise ElError if missing or deleted."""
        acc = self.accounts.get(name)
        if acc is None:
            raise ElError(f"no account named {name!r}")
        if acc.deleted:
            raise ElError(f"account {name!r} is deleted")
        return acc

    def budget(self, name: str) -> Budget:
        """Return a live budget, or raise ElError if missing or deleted."""
        bud = self.budgets.get(name)
        if bud is None:
            raise ElError(f"no budget named {name!r}")
        if bud.deleted:
            raise ElError(f"budget {name!r} is deleted")
        return bud

    def kind_of(self, name: str) -> str | None:
        """Return "account", "budget", or None if the name is unused."""
        if name in self.accounts:
            return "account"
        if name in self.budgets:
            return "budget"
        return None

    def _check_free(self, name: str) -> None:
        kind = self.kind_of(name)
        if kind is not None:
            raise ElError(f"name {name!r} is already used ({kind})")
