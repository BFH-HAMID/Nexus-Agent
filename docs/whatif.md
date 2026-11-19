# What-if simulator (Task 20)

Opt-in. Predicts what a tool call, a shell command or a Chief plan **would** do
- files touched, a diff preview, reversibility, external effects, approvals
needed - without running anything.

| | |
| --- | --- |
| Policy file | `whatif.yaml` (operator-owned, protected, default `enabled: false`) |
| Code | `nexus/whatif/`, Chief hook in `nexus/chief_reviews.py` |
| State | `<NEXUS_HOME>/whatif/<workspace-hash>/scenarios/` |
| CLI | `nexus whatif status\|action\|command\|plan\|ask\|list\|show` |
| REPL / voice | `/whatif …`, "what if I run …", "what happens if I delete …" (spoken commands: [voice.md](voice.md#spoken-commands-of-the-opt-in-features)) |
| Tool | `whatif_simulate` (read-only) |

## Enable and use

```yaml
# whatif.yaml
schema: 1
enabled: true
```

```bash
nexus whatif command "git push --force origin main"
nexus whatif action edit_file --args '{"path": "app.py", "old_text": "a", "new_text": "b"}'
nexus whatif action delete_file --args '{"path": "data.csv"}'
nexus whatif plan chief-1a2b3c4d5e6f          # a planned Chief mission
nexus whatif plan steps.json                  # [{"id": "...", "title": "...", "risk_level": "..."}]
```

## What is simulated

| Input | Output |
| --- | --- |
| `write_file` / `edit_file` / `delete_file` | resolved path, create/overwrite/delete, unified diff preview, whether the real gate would refuse it (outside workspace, protected policy file, text not found) |
| `run_command`, `git` | pattern classification: read-only, destructive, external, network, runs project code, chained |
| any other tool | declared risk class, external/destructive by name |
| plan steps | per-step effects, dependency waves, counts of irreversible / external / high-risk steps, a 0-100 risk score, worst case, rollback notes |

The simulator module imports no tool handler and no process API; file access is
read-only, confined to the workspace and size-bounded. Every record says
`"executed": false`.

Open questions ("what if we upgrade to Python 3.13?") need `mode: model`. The
answer is one tool-free, strict-JSON call, shown as a **model estimate**; a
cloud agent is refused unless `allow_cloud: true`. "Cloud" is every agent whose
endpoint is not on this machine or a private network address, and while
`allow_cloud` is false the call is never handed to a cloud `fallback` agent
either: it fails instead.

## Chief-of-Staff plans

```yaml
chief:
  simulate_before_approval: true
  hold_irreversible: true
```

Adds one sentence to every approval card ("What-if: 1 irreversible, 1 external
of 4 steps. Worst case: …"). With `hold_irreversible: true`, a plan containing
an irreversible or external step needs an explicit GO: it does not auto-start
and an up-front `--confirm GO` does not release it. The simulator never
approves or blocks an individual tool call - the normal approval gate still
decides those.

## Known limits

* Static analysis is pattern-based. `python script.py` or `make` is reported as
  "executes project code; effects depend on that code" - the simulator does not
  read the script. Treat "unknown" as unknown, not as safe.
* A forced push is recognised by `--force`, `--force-with-lease`, a short flag
  containing `f` (`-f`, `-uf`) or a `+refspec`. Aliases and configuration that
  force a push are not seen.
* Plan-step effects are inferred from the step title and instruction text.
* A diff preview reflects the file at simulation time; it may differ by the
  time the real action runs.

## Tests

`pytest tests/test_whatif.py` (offline; process spawning is patched to fail).
