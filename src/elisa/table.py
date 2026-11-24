"""Format output as ASCII tables.

Every line `el` writes, to stdout or stderr, is a table made by `format_table`:

    +----------+---------+----------+
    | ACCOUNT  | BALANCE | CURRENCY |
    +----------+---------+----------+
    | checking | 1600.00 | EUR      |
    +----------+---------+----------+

A table with no rows still shows its header. A cell that contains newlines
spans several lines of its row.
"""

from collections.abc import Collection
from collections.abc import Sequence


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
    for row in rows:
        if len(row) != len(header):
            raise ValueError(
                f"row has {len(row)} cells but the header has {len(header)}: {row!r}"
            )
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
