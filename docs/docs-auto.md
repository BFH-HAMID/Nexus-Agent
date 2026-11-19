# Document automation (Task 28)

Drop documents into one inbox folder. Nexus works out what each one is, reads
the obvious fields (date, total, number), and **proposes** where to file it.
A file is moved only after you approve the move. Nexus can also fill your own
text templates.

| | |
|---|---|
| Policy file | `docs_auto.yaml` (operator-owned, protected, default `enabled: false`) |
| Command | `nexus docs-auto status\|scan\|list\|show\|find\|file\|undo\|forget\|templates\|generate` |
| Slash command | `/docs`, `/docs scan`, `/docs find WORDS` (read-only) |
| Agent tools | `docs_scan` READ, `docs_find` READ, `docs_file` WRITE, `docs_generate` WRITE |
| Extra | `pip install "nexus-agent[docs-auto]"` (pypdf, python-docx) for PDF and DOCX |
| Code | `nexus/docs_auto/` |
| Tests | `tests/test_docs_auto.py` |

## Enable

```yaml
# docs_auto.yaml
schema: 1
enabled: true
```

```bash
pip install "nexus-agent[docs-auto]"      # only for pdf and docx
pip install "nexus-agent[secure-state]"   # for the encrypted index (or set encrypt_at_rest: false)
mkdir inbox
nexus docs-auto status
```

While the policy is OFF nothing is registered, nothing is read and nothing is
written.

## A typical run

```text
$ nexus docs-auto scan
doc-1ed1879ab419  proposed     invoice    acme.txt  [2026-03-12, 288650.00 BDT, no. INV-2026-0042]
    -> documents/invoices/2026/2026-03-12_invoice_INV-2026-0042.txt
doc-bf9baedbc018  proposed     invoice    power bill.pdf  [2026-11-02, 3450.00 BDT, no. DP-55871]
    -> documents/invoices/2026/2026-11-02_invoice_DP-55871.pdf
doc-3de2b3788e32  needs_review unsorted   scan0001.pdf
    -> -  (the PDF has no text layer (a scan?); text recognition is not part of this feature)

$ nexus docs-auto file doc-1ed1879ab419
inbox/acme.txt  ->  documents/invoices/2026/2026-03-12_invoice_INV-2026-0042.txt
Move 1 file(s)? [y/N] y
filed acme.txt as documents/invoices/2026/2026-03-12_invoice_INV-2026-0042.txt

$ nexus docs-auto file doc-3de2b3788e32 --category receipt    # you decide what it is
$ nexus docs-auto file --all                                  # every document that has a proposal
$ nexus docs-auto undo doc-1ed1879ab419                       # move it back
```

`scan` reads files and writes the index. It never moves, renames or deletes a
file. `file` asks before it moves anything; `--yes` skips the question. Without
a terminal and without `--yes` it refuses.

## What a scan looks at

- Files directly in the inbox folder. Sub-folders and hidden files are ignored.
- In name order, at most `limits.max_files_per_scan` (50) per scan. The scan
  says how many files it did not look at.
- Types listed in `types`: `txt`, `md`, `pdf`, `docx`.

A file is skipped, with the reason shown, when it is a symbolic link, a folder,
not a regular file, empty, larger than `limits.max_file_bytes`, of another
type, or has the same content as another inbox file or as a document that is
already filed.

How the text is read:

| Type | Reader | Limits |
|---|---|---|
| `txt`, `md` | UTF-8 text | other encodings and binary files are not read |
| `pdf` | the text layer, through pypdf | first `limits.max_pdf_pages` pages; password-protected, damaged and text-less files are not read |
| `docx` | paragraph text, through the existing office reader | files up to 500 kB, first 32,000 characters; macros, embedded content and external links are refused |

A document whose text cannot be read is kept with status `needs_review` and
the reason. You can still file it by hand with `--category`.

A record whose file has left the inbox without being filed is dropped at the
next scan.

## Classification

Each category has keywords. A keyword is a whole word or phrase, matched
without regard to case.

- A keyword counts once per occurrence, up to three times, plus one if it
  occurs in the first 300 characters.
- The category with the highest score wins if the score reaches
  `classify.min_score` (2) and is higher than the runner-up.
- Otherwise the document is `unsorted` and waits for you (`needs_review`).

Built-in categories, used when `categories` is empty:

| Category | Folder below the archive |
|---|---|
| `invoice` | `invoices/{year}` |
| `receipt` | `receipts/{year}` |
| `contract` | `contracts` |
| `statement` | `statements/{year}` |
| `letter` | `letters/{year}` |
| `report` | `reports/{year}` |

The built-in keywords are English, with a few Bengali ones. Your own
categories replace the built-in ones:

```yaml
categories:
  tax:
    keywords: ["tax return", "NBR"]
    folder: "tax/{year}/{month}"     # variables: {year} {month} {category}
  school:
    keywords: [tuition, semester]    # folder defaults to the category name
```

### Optional: ask a model

```yaml
classify:
  mode: model
  agent: local-llm       # agents.yaml name; empty = workspace default
  allow_cloud: false
```

- A model is asked only when the keywords leave a document `unsorted`.
- It gets the first 6,000 characters, wrapped as untrusted data, and the
  category names with up to eight keywords each. It must answer with one of
  those names.
- Any other answer, or a failure, leaves the document `unsorted` with a note.
  The model never supplies a path or a file name.
- With `allow_cloud: false` a cloud agent is not called at all, and a local
  agent's cloud fallback is not used.
- One call per document. A later scan does not ask again. To ask again, run
  `nexus docs-auto forget ID` and scan.

## Extracted fields

Field extraction uses fixed rules. No model is involved.

| Field | Rule |
|---|---|
| `date` | the first date within 30 characters after "invoice date", "date of issue", "issue date", "dated", "date" or "তারিখ"; otherwise the first date that is not the due date |
| `due_date` | the first date within 30 characters after "due date", "payment due", "due by", "pay by", "due on" or "due:" |
| `total`, `currency` | the amount directly after "grand total", "total due", "amount due", "balance due", "net payable", "total amount", "amount paid", "total" or "মোট", on the same line. The most specific label wins; if it occurs more than once, the last one. |
| `total_guessed` | set when no label was found and the largest amount with a currency was taken instead |
| `number` | the value after invoice / receipt / order / reference / ref / contract / bill / document / চালান / রসিদ plus "no", "number", "num", "id", "নং", "#" or ":". It must contain a digit. |
| `emails` | the first five different e-mail addresses |
| `title` | the first line with at least three characters, cut at 80 |

Dates are read as `2026-03-12`, `12/03/2026`, `12.03.2026`, `12 March 2026` and
`March 12, 2026`, with English or Bengali month names and Bengali digits.
Impossible dates are dropped. `dates.day_first` decides `03/04/2026`.

Amounts need a currency sign or code (৳, Tk, BDT, taka, $, USD, €, EUR, £, GBP,
₹, Rs, INR), except directly after a "total" label. A total without a currency
takes the currency used most in the document.

## Where a document goes

```text
<archive>/<category folder>/<naming>.<extension>
```

- `{year}` and `{month}` in the folder and `{date}` in the name come from the
  document's date, or from the file's modification date when no date was found.
- `{ref}` is the document number, or the original file name when there is none.
  `{name}` is the original file name.
- Every part that comes from the document is reduced to letters, digits,
  `.`, `_` and `-`, and to 40 characters. It cannot contain a path separator.
- If the name is taken, `-2`, `-3` and so on are added.

Before a file moves, Nexus checks that:

1. the file still has the content that was scanned;
2. the source and the destination are inside the workspace, with no symbolic
   link anywhere in the path;
3. neither is a private or runtime path (`.git`, `.nexus`, `.env`, key files,
   `node_modules`) or an operator-owned file or folder;
4. the destination is the one that was shown for approval;
5. nothing exists at the destination. Nothing is ever overwritten.

If the index cannot be written after the move, the move is taken back.

`undo` moves a filed document back to its place in the inbox. It refuses when
the filed file was changed or that place is taken.

## Templates

A template is a `.md` or `.txt` file in `templates.folder` (`doc_templates`),
UTF-8, at most 200 kB, with `{{name}}` placeholders.

```text
# doc_templates/reminder.md
Dear {{name}},

Invoice {{number}} of {{total}} {{currency}} is due on {{due_date}}.

{{today}}
```

```bash
nexus docs-auto templates
nexus docs-auto generate reminder.md --from doc-1ed1879ab419 --set name="Rahim Traders"
```

- Values come from `--set NAME=VALUE`, from the fields of an indexed document
  (`--from ID`: `date`, `due_date`, `total`, `currency`, `number`, `emails`,
  `title`, `category`) and from `today`. `--set` wins.
- A placeholder without a value is a refusal. So is a value without a
  placeholder.
- A value is plain text of at most 2,000 characters. It is inserted once and
  never expanded again.
- Text that looks like a credential is refused.
- The result is shown and written only after you confirm (or `--yes`), as
  `<templates.output>/<today>_<template name>.<md|txt>`. An existing file is
  never overwritten; `-2`, `-3` and so on are added.

There are no loops or conditions, and the output is text only.

## Agent tools

Registered only while the policy is ON.

| Tool | Risk | What it does |
|---|---|---|
| `docs_scan` | READ | scan the inbox and return the proposals |
| `docs_find` | READ | search names, categories and fields in the index |
| `docs_file` | WRITE | move one scanned document; arguments `doc_id` and an optional known `category` |
| `docs_generate` | WRITE | fill a template; arguments `template`, `values`, `from_doc` |

- File names and fields were written by whoever made the document. The output
  of `docs_scan` and `docs_find` is wrapped as untrusted data and marks the
  session as tainted. Documents with instruction-like text are flagged.
- The model cannot choose a destination. Nexus computes it from your policy
  and replaces whatever the model sent before the approval prompt is shown.
- The approval prompt for `docs_file` shows the source and the destination.
  The prompt for `docs_generate` shows the destination and the first 1,500
  characters of the filled text.
- There is no tool for `undo` or `forget`.

## The index

`<NEXUS_HOME>/docs_auto/<workspace>/index.json.enc`

- One record per document: id, content hash, name, size, category, score,
  method (`rules`, `model`, `operator`, `agent`), fields, proposed and filed
  path, status (`proposed`, `needs_review`, `filed`), timestamps.
- Never the document text.
- AES-256-GCM with a key in the OS keyring while `encrypt_at_rest: true`.
  Without a usable keyring nothing is stored; there is no plaintext fallback.
- When the index reaches `limits.max_records`, the oldest filed records are
  forgotten. Their files stay where they are.
- `nexus docs-auto forget ID` drops one record. `forget --missing` drops every
  filed record whose file is no longer there. Neither touches a file.

## Policy keys

| Key | Default | Meaning |
|---|---|---|
| `encrypt_at_rest` | `true` | seal the index |
| `inbox` | `inbox` | folder that is scanned |
| `archive` | `documents` | filed documents go below it |
| `types` | `[txt, md, pdf, docx]` | file types that are read |
| `naming` | `{date}_{category}_{ref}` | file name; must contain `{ref}` or `{name}` |
| `dates.day_first` | `true` | `03/04/2026` is 3 April |
| `limits.max_files_per_scan` | 50 | 1–500 |
| `limits.max_file_bytes` | 5,000,000 | 1,000–50,000,000 |
| `limits.max_text_chars` | 200,000 | 1,000–2,000,000 |
| `limits.max_pdf_pages` | 50 | 1–500 |
| `limits.max_records` | 5,000 | 10–20,000 |
| `classify.mode` | `rules` | `rules` or `model` |
| `classify.agent` | `""` | agent for `mode: model` |
| `classify.allow_cloud` | `false` | let document text go to a cloud agent |
| `classify.max_tokens_per_call` | 300 | 100–2,000 |
| `classify.min_score` | 2 | 1–20 |
| `categories` | `{}` | your categories (at most 24); empty = built-in |
| `templates.folder` | `doc_templates` | where templates are read |
| `templates.output` | `documents/generated` | where filled templates are written |

Folders are relative to the workspace, at most five levels deep. The inbox and
the archive must be separate, and so must the inbox and the template folders.
Unknown keys are refused.

## Audit

| Action | Recorded |
|---|---|
| `docs_auto.scan` | number of documents and of skipped files |
| `docs_auto.file` | destination, id, category, who (`operator` or `agent`) |
| `docs_auto.undo` | the path the file was moved back from |
| `docs_auto.forget` | id, or the number of dropped records |
| `docs_auto.generate` | output path, template name, length |

No row contains document text or template values.

## Known limits

- **No text recognition.** A scanned PDF or an image has no text to read. It
  waits as `needs_review`.
- **Fields are a best guess.** The rules can pick the wrong date or total.
  Check them before you rely on them.
- **Dot-decimal amounts only.** `1.200,50 €` is not read at all rather than
  read wrongly.
- **Two-digit years are not read.** Ambiguous day/month dates follow
  `dates.day_first`.
- **Keywords can be fooled.** A letter that mentions "invoice" three times is
  proposed as an invoice. That is why filing needs your approval.
- **One inbox, on request.** There is no watcher and no background job.
- **PDF parsing runs in this process.** A hostile PDF is bounded only by the
  size and page limits.
- **Files moved by hand are not tracked.** The record stays `filed` until you
  run `forget --missing` or the index is full.
- **Not safe against another program changing the folders at the same
  moment.** Paths are checked just before the move, not during it.
- **A failed model call is not retried** until the record is forgotten.
- Tested on Linux, with scripted model replies only.

## Tests

```bash
pytest tests/test_docs_auto.py -q
```

The tests run offline. The PDF cases need `pypdf` and the DOCX case needs
`python-docx`; they are skipped when the package is missing.
