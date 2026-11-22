# el language reference

Every command `el` understands, as implemented in `src/elisa/interpreter.py`.
For how to start `el` and where it stores data, see the [README](../README.md).

## Syntax

- One instruction per line in the interpreter and in `-f` files. With `-c`,
  separate instructions with `;` (a `;` inside quotes doesn't count).
- Keywords (`CREATE`, `ACCOUNT`, `ALL`, …) are case-insensitive. Names are
  case-sensitive: `checking` and `Checking` are different accounts.
- Words are split like a shell does. Quote a name or description that contains
  spaces with `'…'` or `"…"`.
- An unquoted description takes the rest of the line, so `ADD checking 10 tip
  from Anna` has the description `tip from Anna` (repeated spaces collapse to one).
- `#` starts a comment that runs to the end of the line.
- Amounts are decimal numbers with at most two decimals (`12`, `12.5`, `12.50`).
  Amounts for ADD, SUB and MV must be greater than zero.
- Currencies are written as ISO codes, in any case (`EUR`, `eur`). The supported
  ones are `EUR` and `KWD`. An account only works with accounts and budgets in
  its own currency: EUR with EUR, KWD with KWD.
- Accounts and budgets share one namespace: a name can't be both. Deleted names
  stay reserved.

## Accounts and budgets

### CREATE ACCOUNT

```
CREATE ACCOUNT <name> <starting balance> <currency>
```

The starting balance may be zero or negative.

```
el> CREATE ACCOUNT checking 100 EUR
created account:checking
el> CREATE ACCOUNT 'savings account' 500.00 eur
created account:savings account
```

### CREATE BUDGET

```
CREATE BUDGET <name> <max budget> <currency> [description]
```

The maximum must be greater than zero. Budgets have no time period.

```
el> CREATE BUDGET groceries 300 EUR 'food and markets'
created budget:groceries
el> CREATE BUDGET fun 50 EUR
created budget:fun
```

### DELETE ACCOUNT, DELETE BUDGET

```
DELETE ACCOUNT <name>
DELETE BUDGET <name>
```

Deletes are soft: the item disappears from `LIST` and can't be used anymore,
but its history stays and `LIST … ALL` still shows it.

```
el> DELETE ACCOUNT 'savings account'
deleted account:savings account
```

### LIST ACCOUNT, LIST BUDGET

```
LIST ACCOUNT [ALL]
LIST BUDGET [ALL]
```

`ACCOUNTS` and `BUDGETS` also work. `ALL` includes deleted items and adds a
`STATUS` column. Items appear in creation order.

```
el> LIST ACCOUNT ALL
ACCOUNT          BALANCE  CURRENCY  STATUS
checking         1273.30  EUR
savings account   700.00  EUR       deleted
el> LIST BUDGET
BUDGET        MAX  SPENT  REMAINING  CURRENCY  DESCRIPTION
groceries  300.00  54.20     245.80  EUR       food and markets
fun         50.00  60.00     -10.00  EUR
```

With nothing to show, it prints `no accounts` or `no budgets`.

## Moving money

Each of these prints the new balance of every account or budget it touched.
An account line is `account:<name> <balance> <currency>`; a budget line is
`budget:<name> <spent> of <max> <currency>, <remaining>`.

### ADD

```
ADD <account> <amount> [description]
```

Adds money to an account (income, refunds). Works on accounts only.

```
el> ADD checking 1500 salary november
account:checking 1600.00 EUR
```

### SUB

```
SUB <account> <amount> [description]
```

Takes money from an account. The balance may go negative. Works on accounts only.

```
el> SUB checking 12.50 'coffee beans'
account:checking 1587.50 EUR
```

### MV

```
MV <source account> <destination account or budget> <amount> [description]
```

Moves money out of an account. If the destination is an account, it's a
transfer. If it's a budget, it's an expense paid from the account and charged
to that budget. Spending can go over a budget's maximum; `REMAINING` then turns
negative. Both sides must use the same currency, so an EUR account can't pay
into a KWD budget or transfer to a KWD account. Without a description, the log
records `MV <source> -> <destination>`.

```
el> MV checking 'savings account' 200
account:checking 1387.50 EUR
account:savings account 700.00 EUR
el> MV checking groceries 54.20 weekly shopping
account:checking 1333.30 EUR
budget:groceries 54.20 of 300.00 EUR, 245.80
el> MV checking fun 60 concert
account:checking 1273.30 EUR
budget:fun 60.00 of 50.00 EUR, -10.00
el> CREATE BUDGET rent 400 KWD
created budget:rent
el> MV checking rent 100
error: currency mismatch: 'checking' is EUR but 'rent' is KWD
```

## Interpreter commands

| Command | Effect |
|---|---|
| `HELP` | Print the list of commands |
| `EXIT` or `QUIT` | Leave the interpreter (Ctrl-D works too). In `-c`/`-f`, stops the batch and keeps what ran before it |

## Errors

Errors go to stderr as `error: <message>`. In the interpreter, the failed
instruction changes nothing and you can carry on. With `-c` or `-f` the whole
batch is one transaction: one failure means nothing from that run is saved, and
the message says which instruction or `file:line` failed.

```
$ el -c "SUB checking 1.234 x"
error: instruction 1: invalid amount: '1.234' has more than two decimals
nothing was saved
```
