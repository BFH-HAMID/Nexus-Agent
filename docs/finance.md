# Personal finance (Task 29)

Export a statement from your bank as CSV and import it. Nexus keeps the
transactions in a sealed ledger on this machine and tells you where the money
went: by category and month, against your budgets, which payments repeat, and
what stands out.

It reads. It never connects to a bank, never pays or transfers anything, and
never goes online by itself.

| | |
|---|---|
| Policy file | `finance.yaml` (operator-owned, protected, default `enabled: false`) |
| Command | `nexus finance status\|import\|summary\|months\|list\|recurring\|unusual\|accounts\|imports\|categories\|categorize\|suggest\|forget` |
| Slash command | `/finance [summary [YYYY-MM]] \| months [N] \| recurring \| unusual [YYYY-MM]` (read-only) |
| Voice | "how much did I spend last month", "am I over budget" (only with `voice: true`) |
| Agent tools | none by default; `finance_summary`, `finance_transactions`, `finance_insights` (all READ) with `agent_tools: true` |
| Extra | `pip install "nexus-agent[secure-state]"` for the encrypted ledger |
| Code | `nexus/finance/` |
| Tests | `tests/test_finance.py` |

## Enable

```yaml
# finance.yaml
schema: 1
enabled: true
currency: BDT
budgets:
  groceries: 15000
```

```bash
pip install "nexus-agent[secure-state]"   # for the encrypted ledger (or set encrypt_at_rest: false)
nexus finance status
```

While the policy is OFF nothing is registered, nothing is read and nothing is
written.

## A typical run

```text
$ nexus finance import city-oct.csv
city-oct.csv -> account main (BDT)
columns: date=Txn Date, debit=Withdrawal (Dr), credit=Deposit (Cr), balance=Balance, description=Narration
dates: 2026-10-01 to 2026-10-31, read as day/month
9 transactions: 85,000.00 in, 41,393.50 out
new: 9   already in the ledger: 0
the balance column agrees with the amounts
2 row(s) are not transactions and were left out:
  line 5 (no amount): 01/10/2026 Balance B/F 50,000.00
  line 15 (no date): Total 41,393.50 85,000.00
first rows:
  2026-10-01      -25,000.00  HOUSE RENT OCT  [rent]
  2026-10-03       -2,350.50  POS 482911 SHWAPNO GULSHAN  [groceries]
  2026-10-05       85,000.00  SALARY OCT 2026 ACME LTD  [salary]
  2026-10-05       -1,000.00  bKash transfer …5678  [uncategorized]
  2026-10-13       -1,820.00  POS 482977 SHWAPNO GULSHAN  [groceries]
Add 9 transaction(s) to the ledger? [y/N] y
added 9 transaction(s) as imp-33a23696 (take them out again: nexus finance forget imp-33a23696)

$ nexus finance summary
2026-10 (latest month with transactions) - all accounts
BDT  income 85,000.00   spending 41,393.50   net 43,606.50
  rent                   25,000.00   60.4%  budget 25,000.00, 0.00 left
  cash                   10,000.00   24.2%
  groceries               4,170.50   10.1%  budget 4,000.00, OVER by 170.50
  subscriptions           1,200.00    2.9%
  uncategorized           1,000.00    2.4%
  fees                       23.00    0.1%
  salary              +85,000.00 in
1 transaction(s) have no category: nexus finance categories
```

`import` shows what it read **before** it stores anything, and asks. `--yes`
skips the question. Without a terminal and without `--yes` it refuses. Check
three lines of the preview every time: the columns, the totals, and the
balance line.

Importing the same file again adds nothing. Importing a statement that
overlaps an earlier one adds only the rows that are new.

## What a statement must look like

A text file with the suffix `.csv`, `.tsv` or `.txt`, with one row of column
titles and one transaction per row.

| | |
|---|---|
| Encoding | UTF-8; otherwise Windows-1252, with a note in the preview |
| Separator | `,` `;` tab or `\|`, found from the file; a `sep=;` first line is honoured |
| Above and below the table | bank name, account holder, opening balance, totals: skipped and listed in the preview |
| Dates | `2026-10-05`, `20261005`, `05/10/2026`, `5.10.26`, `05-Oct-2026`, `October 5, 2026`; a time of day after the date is ignored |
| Amounts | `1,234.56`, `1,23,456.78`, `(1,234.56)`, `-1234.56`, `1234.56-`, `৳ 500`, `Tk. 500`, `500.00 DR`; with `decimal: ","` also `1.234,56` |
| Digits | Bengali and other decimal digits are read as 0-9 |
| Amount columns | one signed column, **or** a debit column and a credit column |

Column titles are recognised by their words, for example *Date*, *Txn Date*,
*Value Date*, *Narration*, *Particulars*, *Details*, *Withdrawal*, *Deposit*,
*Debit*, *Credit*, *Amount*, *Balance*. If a file has titles Nexus does not
know, or two that could both be the amount, the import is refused and the
message says which titles to name:

```yaml
accounts:
  card:
    columns:
      amount: "Billing Amount"     # the columns you do not name are still found by their titles
```

**Day or month first.** `03/04/2026` can be 3 April or 4 March. If any date
in the file has a day above 12, the file itself decides. If none has,
`statements.day_first` decides and the preview says that it was assumed. A
file that mixes both orders is refused.

**Money in and money out.** With a debit and a credit column, credit is money
in. With one column, a minus sign means money out. If your statement shows
purchases as positive numbers (many card statements do), set
`expenses: positive` for that account. A *type* column is used only when every
row in it says debit or credit.

### When an import is refused

Nexus refuses the whole file rather than store a number it is not sure about.
Nothing is imported from a refused file. The message names the lines.

| Message | Usual cause |
|---|---|
| `N values under M column titles ('50' has none)` | a separator inside a number or a description without quotation marks: `05/10/2026,SHOP,-12,50` |
| `a debit (1.00) and a credit (234.56) in one row` | a shifted row, or a statement that is not one transaction per row |
| `amount '…': looks like an amount with a decimal ','` | set `decimal: ","` for this account |
| `more than one column could be the amount ('A', 'B')` | name the right one under `accounts.<name>.columns` |
| `the dates are day/month in line 4 and month/day in line 9` | two exports pasted together |
| `a quoted value runs over N lines` | a quotation mark that is never closed |
| `date 2031-01-05 is outside 1990 .. one year from now` | a mistyped year, or the wrong day/month order |
| `was imported before (imp-…) and reads differently now` | you changed a setting of this account; forget the earlier import first |

## Accounts

Without an `accounts` section every import goes to the account `main`, or to
the name you give with `--account`. Add a section when your statements differ:

```yaml
accounts:
  main: {}
  card:
    currency: USD
    decimal: "."
    day_first: false
    expenses: positive
```

Once accounts are listed, an import must name one of them with `--account`.
Without the option it goes to `main`, if you listed that name. A file that is
already in the ledger under one account is refused under another.

`nexus finance accounts` shows each account with its date range and, when the
statement has a balance column, the closing balance of the newest statement.

Amounts in different currencies are **never added together** and never
converted. Each currency gets its own block in every report.

## Categories

A category is a name and a list of keywords. A keyword matches when its words
appear in the description as whole words, in that order: `shop` matches
`CORNER SHOP` and not `SHOPPING MALL`. The longest matching keyword wins; a
tie goes to the category listed first.

`nexus finance categories` shows the categories in use and the merchant texts
that have none. With no `categories` section the built-in list is used
(salary, rent, groceries, eating_out, transport, utilities, health, shopping,
subscriptions, cash, fees). Your own section replaces it entirely.

```yaml
categories:
  groceries: [shwapno, agora, "meena bazar"]
  rent: ["house rent"]
  transfer: ["own account", "credit card payment", "payment thank you"]
```

**`transfer` is special.** It is for money that moves between your own
accounts. Transfers are counted neither as income nor as spending and are
shown on a line of their own. Without it, paying your card bill counts as
spending on one account and as income on the other.

**Teaching by example.** Keywords do not have to cover everything:

```bash
nexus finance list --search "milk delivery"      # shows the ids
nexus finance categorize tx-2c343bf3988b5e4e groceries
# 'milk delivery' is now groceries: 5 transaction(s)
```

This stores the *merchant words* of that transaction (the description without
numbers and month names) with the category. Every payment with the same words
follows, now and after later imports. What you teach wins over the keywords.
`nexus finance categorize ID uncategorized` takes it back.

A category is never stored with a transaction. It is worked out each time
from your keywords and what you taught, so changing `finance.yaml` changes
every report at once.

### Optional: ask a model

```yaml
categorize:
  mode: model
  agent: local-llm        # a name from agents.yaml; empty = the workspace default
  allow_cloud: false
```

```text
$ nexus finance suggest
fitness club dhanmondi  ->  health
milk delivery  ->  groceries
Remember these 2 categories? [y/N]
```

- The model is called only by `nexus finance suggest`. No report calls it.
- It gets the merchant words that have no category yet, plus your category
  names with up to six keywords each. It gets **no** amounts, dates, account
  names, ids or numbers. Text that reads like an instruction is not sent.
- A cloud agent is refused unless `allow_cloud: true`. A local agent is never
  swapped for a cloud fallback.
- The answer is checked against your category names. Any other name is
  dropped. `transfer` is never accepted from a model, because it takes a
  payment out of the totals.
- Nothing is remembered until you say yes.

## Budgets

```yaml
budgets:
  groceries: 15000
  eating_out: 6000
```

One amount per category and month, in the policy `currency`. The summary
shows what is left or by how much you are over. A refund in a category gives
budget back. Budgets appear only in a month that has transactions in the
policy currency. There is no budget for `transfer`.

## Reports

| Command | What it shows |
|---|---|
| `nexus finance summary [YYYY-MM] [--account NAME]` | income, spending, net and every category for one month |
| `nexus finance months [N]` | income, spending and net for the latest N months (default 6) |
| `nexus finance list [--month] [--category] [--search WORDS] [--limit N]` | transactions, newest first |
| `nexus finance recurring` | payments that look regular |
| `nexus finance unusual [YYYY-MM]` | spending worth a second look |

Without a month, a report covers the latest month that has transactions, and
says so. Every report takes `--json`. Amounts are exact decimal numbers
throughout; no floating point touches money.

### Recurring payments

```text
$ nexus finance recurring
monthly             25,000.00 BDT  HOUSE RENT NOV          [rent]  300,000.00 a year, next about 2026-12-02
monthly              1,200.00 BDT  NETFLIX.COM …1111       [subscriptions]  14,400.00 a year, next about 2026-12-14  (last payment 1,500.00, before 1,200.00)
monthly              2,000.00 BDT  FITNESS CLUB DHANMONDI  [uncategorized]  24,000.00 a year, last on 2026-08-05: stopped?
```

Money going out is listed when all of this holds (transfers are left out):

- at least three payments on different days with the same merchant words, in
  the same currency;
- the usual gap between them is weekly (6-8 days), every two weeks (13-15),
  monthly (27-33), quarterly (85-97) or yearly (355-375), and at least three
  gaps in four fit;
- at least four payments in five are within 30% of the usual amount.

The line notes a last payment that differs by more than 5% from the usual
one, and a payment that has not come for one and a half times its usual gap.

### Unusual spending

```text
$ nexus finance unusual
2026-11-01      -25,000.00 BDT  HOUSE RENT NOV
    at or above alerts.large_amount (20,000.00)
2026-11-06       -9,000.00 BDT  POS 483200 SHWAPNO GULSHAN
    at least 3 times the usual 2,040.00 for groceries
2026-11-05         -640.00 BDT  PHARMACY LAZZ
    same amount and merchant as on 2026-11-04: charged twice?
```

Three checks, on money going out in one month:

- **Large.** At or above `alerts.large_amount` (policy currency only; `0`
  switches it off).
- **Far above usual.** At least `alerts.unusual_factor` times the median
  payment of its category over the six months before. It needs five earlier
  payments in that category and never applies to `uncategorized`.
- **Twice.** The same amount to the same merchant on the same account, on the
  same day or the next.

Both reports are rules of thumb. They point at lines to look at. They do not
say that anything is wrong, and they miss things.

## Voice

Only with `voice: true`. The answer is spoken; it is not sent to a model.

| You say | Answer |
|---|---|
| "How much did I spend last month?" | "In October 2026 you spent 41,150 BDT and received 85,000 BDT. Most went to rent with 25,000 BDT, then …" |
| "How much did I spend on groceries this month?" | "In November 2026 you spent 10,900 BDT on groceries." |
| "What did we spend in March on health?" | the same, for the latest March that has begun |
| "Am I over budget?" / "Budget status" | "In November 2026 you are over budget on groceries by 6,400 BDT." |

- Amounts are spoken in the policy currency, rounded to whole units.
- Without a period, the answer covers the latest month that has
  transactions, and names it.
- The figures are **not spoken** to a voice that does not match the enrolled
  one, after the assistant's last answer used untrusted content, or while
  voice privacy mode is on. Without a speaker check nobody can be told apart
  (see [voice.md](voice.md#spoken-commands-of-the-opt-in-features)).
- Other sentences are left to the assistant as before.
- Anyone in the room hears the answer.

## Slash command

`/finance` prints the summary of the latest month. `/finance summary 2026-10`,
`/finance months 3`, `/finance recurring` and `/finance unusual` print the
same text as the commands above. The output goes to your terminal and is not
added to the conversation. There is no slash command that imports, forgets or
changes anything.

## Agent tools

None are registered unless you set `agent_tools: true`.

| Tool | Risk | What it returns |
|---|---|---|
| `finance_summary` | READ | the summary of one month |
| `finance_transactions` | READ | up to 50 transactions per call, filtered by month, category and words |
| `finance_insights` | READ | `kind: recurring` or `kind: unusual` |

There is no tool that imports a statement, changes a category or a budget,
forgets anything or sends anything.

Read this before you switch it on:

- **What a tool returns goes to the model that runs the session.** If that is
  a cloud model, your figures leave this machine. Nexus cannot take them back.
- Descriptions are written by whoever sent or took the money. They are
  wrapped as untrusted data. A description that reads like an instruction is
  replaced by `[withheld: reads like an instruction]`; its amount and date
  stay.
- The tools refuse to run in a session that has already seen untrusted
  content from elsewhere (a web page, a document, a message).
- After a tool has run, the session counts as tainted: actions with an
  outside effect (shell commands, `git push`, `gh`, the browser, MCP tools
  that write) need a fresh approval from you and cannot be auto-approved.
- **What this does not stop:** a tool that only *reads* from outside and needs
  no approval, such as a read-only MCP tool. If the session later reads
  hostile content through such a tool, a model that is tricked could put
  figures into a second read. If you use such tools, ask about money in a
  session of its own.
- Every call is recorded in the audit log.

## The ledger

`<NEXUS_HOME>/finance/<workspace>/ledger.json.enc`

- One record per transaction: id, account, date, amount, currency,
  description, import id. In descriptions, any run of nine or more digits
  (account, card and phone numbers) keeps only its last four: `…5678`.
- One record per import: id, file name, SHA-256 of the file, account, time,
  date range, number of rows added, closing balance when the statement has
  one.
- The merchant words you taught, with their category (at most 5,000).
- Not stored: the statement file, the running balance of each row, the bank's
  reference column, anything above the column titles.
- AES-256-GCM with a key in the OS keyring while `encrypt_at_rest: true`.
  Without a usable keyring the import is refused before the preview is shown;
  there is no plaintext fallback. `nexus finance status` reports the problem.
- `limits.max_transactions` is a hard stop. A full ledger refuses the import
  and drops nothing.

Taking data out again:

```bash
nexus finance imports                      # the import ids
nexus finance forget imp-33a23696          # one import
nexus finance forget --before 2025-01-01   # everything older
nexus finance forget --all                 # the whole ledger, including what you taught
```

`forget` shows how many transactions it would drop and asks. It never touches
a statement file.

## Policy keys

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | master switch |
| `encrypt_at_rest` | `true` | seal the ledger with a key in the OS keyring |
| `currency` | `BDT` | three-letter code for your accounts and budgets |
| `statements.decimal` | `"."` | `"."` reads `1,234.56`; `","` reads `1.234,56` |
| `statements.day_first` | `true` | used only when the file does not tell |
| `statements.max_file_bytes` | `2000000` | 1,000 to 20,000,000 |
| `statements.max_rows` | `20000` | 10 to 100,000 rows per file |
| `accounts.<name>` | none | `currency`, `decimal`, `day_first`, `expenses` (`negative` or `positive`), `columns`; at most 12 accounts |
| `accounts.<name>.columns` | none | title for `date`, `description`, `amount`, `debit`, `credit`, `balance`, `currency`, `id`, `type`; either `amount` or `debit`/`credit` |
| `categories.<name>` | built-in list | 1 to 40 keywords; at most 30 categories |
| `budgets.<category>` | none | monthly amount in `currency`, above 0, at most two decimals |
| `limits.max_transactions` | `20000` | 100 to 100,000 |
| `alerts.large_amount` | `0` | `0` = off |
| `alerts.unusual_factor` | `3` | 2 to 100 |
| `categorize.mode` | `rules` | `rules` or `model` |
| `categorize.agent` | `""` | agent for `suggest`; empty = workspace default |
| `categorize.allow_cloud` | `false` | allow a cloud agent to see merchant words |
| `categorize.max_tokens_per_call` | `600` | 100 to 4,000 |
| `voice` | `false` | answer spoken money questions |
| `agent_tools` | `false` | register the three read-only tools |

An unknown key, a wrong type or a value out of range makes the policy
invalid, and an invalid policy counts as OFF.

## Audit

| Action | Recorded |
|---|---|
| `finance.import` | file name, import id, account, rows added, rows already known, date range |
| `finance.forget` | what was forgotten and how many transactions |
| `finance.learn` | how many descriptions, which category names, who |
| `finance.read` | the tool name, the month and the number of rows returned to the agent |

No row contains an amount or a description.

## Known limits

- **Tested with made-up statements only.** The test files follow common
  layouts (one amount column, debit and credit columns, text above and below
  the table, several separators and date styles). No export of a real bank
  has been tried. Read the preview of your first import line by line.
- **CSV only.** No PDF, XLSX, OFX, QIF or MT940.
- **One table per file.** A second table with other column titles is named in
  the preview and not read.
- **A shifted row that still fits the columns is not caught by the row
  checks.** If a statement has a balance column, the preview says when the
  balance does not follow the amounts. Without a balance column there is no
  such check.
- **Overlapping exports must describe a transaction the same way.** A row is
  recognised as already known by account, date, amount, currency and its
  description (or the bank's own id, used only when every row of the file has
  a different one). If your bank rewrites a description between two exports,
  for example from "pending" to the final text, that transaction is counted
  twice. The line `new: N   already in the ledger: M` shows it.
- **After changing an account's settings** (`decimal`, `expenses`, `columns`),
  forget the earlier imports of that account and import them again.
- **No manual entries and no splitting.** A cash purchase is not in the
  ledger, and one payment cannot be divided over two categories.
- **A category belongs to merchant words, not to one transaction.** Two
  payments with the same words always share a category. Month names are not
  part of the words, so "MAY CAFE" and "CAFE" count as one merchant.
- **Currencies are never converted or added together.** Budgets and the
  large-amount check apply to the policy currency only.
- **Recurring and unusual are rules of thumb**, as described above.
- **No reminders and no background job.** Nothing runs unless you ask.
- **`agent_tools: true` hands your figures to the session's model**, and a
  later read-only call to the outside is not blocked. See *Agent tools*.
- Tested on Linux, with scripted model replies only.

Speed, measured on the development machine: a statement of 100,000 rows is
read in about 2.4 s. Text files of 18-19 MB built to be slow to parse were
refused in under 3 s.

## Tests

```bash
pytest tests/test_finance.py -q
```

The tests run offline and need no keyring: the encrypted cases use an
in-memory one.
