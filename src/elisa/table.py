"""Format output as ASCII tables or JSON.

Every line `el` writes, to stdout or stderr, is a table made by `format_table`:

    +----------+---------+----------+
    | ACCOUNT  | BALANCE | CURRENCY |
    +----------+---------+----------+
    | checking | 1600.00 | EUR      |
    +----------+---------+----------+

A table with no rows still shows its header. A cell that contains newlines
spans several lines of its row.

With `--json`, `format_json` renders the same header and rows instead, as one
line holding a JSON array with an object per row, keyed by the lowercased
column names:

    [{"account": "checking", "balance": "1600.00", "currency": "EUR"}]

Both share the `Formatter` signature, so callers pick one and pass it along.
"""

import json
from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Sequence


Formatter = Callable[..., str]
"""`format_table` or `format_json`: (header, rows, *, align_right=()) -> str."""


def _check_rows(header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    for row in rows:
        if len(row) != len(header):
            raise ValueError(
                f"row has {len(row)} cells but the header has {len(header)}: {row!r}"
            )


def format_table(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    align_right: Collection[int] = (),
) -> str:
    """Format a header and rows as an ASCII table.

    Args:
        header: The column names.
        rows: The table rows, each with one cell per column.
        align_right: Indexes of the columns to right-align (numbers).

    Returns:
        The table as lines joined by newlines, without a trailing newline.

    Raises:
        ValueError: If a row doesn't have one cell per column.
    """
    _check_rows(header, rows)
    split = [[str(cell).split("\n") for cell in row] for row in [header, *rows]]
    widths = [
        max(len(line) for row in split for line in row[i]) for i in range(len(header))
    ]
    rule = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def fmt(row: list[list[str]], align: bool) -> list[str]:
        height = max((len(cell) for cell in row), default=1)
        lines = []
        for n in range(height):
            cells = []
            for i, (cell, w) in enumerate(zip(row, widths, strict=True)):
                text = cell[n] if n < len(cell) else ""
                cells.append(
                    text.rjust(w) if align and i in align_right else text.ljust(w)
                )
            lines.append("| " + " | ".join(cells) + " |")
        return lines

    head, *body = split
    out = [rule, *fmt(head, align=False), rule]
    for row in body:
        out.extend(fmt(row, align=True))
    if body:
        out.append(rule)
    return "\n".join(out)


def format_json(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    align_right: Collection[int] = (),
) -> str:
    """Format a header and rows as a one-line JSON array of objects.

    Each row becomes an object keyed by the lowercased column names. Values
    stay strings, so amounts keep their exact two decimals.

    Args:
        header: The column names.
        rows: The table rows, each with one cell per column.
        align_right: Ignored; accepted so it can stand in for `format_table`.

    Returns:
        The JSON text, without a trailing newline.

    Raises:
        ValueError: If a row doesn't have one cell per column.
    """
    _check_rows(header, rows)
    keys = [name.lower() for name in header]
    objects = [dict(zip(keys, map(str, row), strict=True)) for row in rows]
    return json.dumps(objects, ensure_ascii=False)
