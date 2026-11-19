# Explainability and audit export (Task 21)

Opt-in. Nexus records **why** each approval decision was made in the
tamper-evident audit chain and can answer "why did you do that?" from those
records only.

| | |
| --- | --- |
| Policy file | `explain.yaml` (operator-owned, protected, default `enabled: false`) |
| Code | `nexus/explain/` |
| State | none of its own - rows live in the existing hash-chained audit trail |
| CLI | `nexus audit explain [last\|SEQ\|run:ID]`, `nexus audit export`, `nexus explain …` |
| REPL / voice | `/why [SEQ]`, `/why run [ID]`, "why did you do that?" (spoken commands: [voice.md](voice.md#spoken-commands-of-the-opt-in-features)) |
| Tool | `explain_action` (read-only) |

## Enable

```yaml
# explain.yaml
schema: 1
enabled: true
record: non_safe     # all | non_safe | denied | off
```

Takes effect for sessions started afterwards (the recorder is attached when a
tool registry is built). Turning it off stops recording immediately.

## What is recorded

One `explain.decision` row per gate decision selected by `record`:

```json
{"what": "explain.decision", "who": "gate", "target": "write_file", "result": "approved",
 "args_sha256": "…",
 "detail": {"risk_class": "MEDIUM", "scope": "once", "mode": "ask", "tainted": false,
            "preview": "notes.txt (5 chars, 1 lines)",
            "factors": ["The tool 'write_file' is declared 'write' and was classified MEDIUM for these arguments.",
                        "It was approved through the approval prompt (scope: once).",
                        "Policy asked for a notification after the action."]}}
```

The recorder is chained onto `ApprovalGate.decision_observer`: an observer that
was already there (run traces, replay) still runs first. It cannot change a
decision, and a failing audit write is swallowed rather than raised inside the
gate. Arguments are never stored - only their SHA-256 and a redacted one-line
preview.

`record: all` roughly doubles audit volume. The audit trail has a size cap
(`nexus audit verify` shows it); when it is full Nexus refuses tool execution
until you archive it, so prefer `non_safe` unless you need every read.

## Asking why

```bash
nexus audit explain            # the most recent recorded decision
nexus audit explain 41         # audit event #41
nexus audit explain run:last   # approvals of the latest recorded run (nexus trace)
```

```
2026-11-10T09:14:03+00:00: 'delete_file' was denied (DANGEROUS) - notes.txt.
 - The tool 'delete_file' is declared 'danger' and was classified DANGEROUS for these arguments.
 - It was refused: fresh human approval required; --yes/saved approvals cannot authorize this action.
 - permissions.yaml mode for 'delete_file' was ASK_EVERY_TIME.
Evidence: #40 permission.decision, #41 explain.decision
Audit chain verified (42 events).
```

Explanations are **template-built from audit rows; no model is involved**.
Every line restates a field of a cited row, so it can be checked against
`nexus audit tail`. If the chain does not verify, or a cited row fails its own
hash, the answer starts its integrity line with `WARNING` and the CLI exits 3.

Exit codes: `0` explained and verified, `1` nothing recorded, `2` off/usage
error, `3` explained but the trail failed verification.

## Export

```bash
nexus audit export --format jsonl --output audit-2026-11.jsonl
nexus audit export --format csv --since 2026-11-01 --action permission. > decisions.csv
```

* Rows are exported exactly as stored (already redacted at write time).
* `--output FILE` writes `FILE` and `FILE.manifest.json` (mode 0600, never
  overwrites): event count, first/last sequence, chain head, whether the chain
  verified, and the SHA-256 of the exported event lines.
* A JSONL export of the whole trail is itself a verifiable chain.
* CSV cells that start with `=`, `+`, `-`, `@` are prefixed with `'` so a
  spreadsheet does not run them as formulas.
* Each export is itself audited (`explain.export`).

## Known limits

* Only decisions made while `explain.yaml` was ON carry recorded factors.
  Older events are explained from whatever the existing audit row says
  (`permission.decision` mode and reason).
* It explains the *gate's* decision. It does not explain why the model chose to
  attempt an action; the model's reasoning is not recorded as fact.
* `run:` explanations come from the run trace store, which is not hash-chained;
  the output says so.
* A hash chain detects edits and deletions inside the file; it cannot detect
  someone replacing the whole file with a freshly built chain. Export the
  manifest regularly and keep it elsewhere if you need that guarantee.

## Tests

`pytest tests/test_explain.py`
