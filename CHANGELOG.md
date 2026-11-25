## 0.3.1 (2026-11-25)

### Fix

- resolve ty type-check errors in interpreter and tests

### Refactor

- split the CLI into elisa.cli and expose a public library API

## 0.3.0 (2026-11-24)

### BREAKING CHANGE

- plain-text output such as "account:x 90.00 EUR" and
"error: ..." is replaced by tables.

### Feat

- add --json output option
- add LIST TRANSACTION command
- format all CLI output as ASCII tables

### Fix

- prevent lost updates and log corruption on concurrent or interrupted writes

## 0.2.0 (2026-11-22)

### Feat

- add SQLite storage, KWD currency and budget descriptions
