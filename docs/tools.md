# Tools, approval gate and safety

Nexus ships 15 tools. The model sees a JSON schema for each; the *human* sees a one-line preview
and a `y/N` prompt before anything touches disk, network or git history.

## Inventory

| Tool | Risk | What it does |
| --- | --- | --- |
| `list_files` | read | recursive listing with sensible excludes (`node_modules`, `.venv`, `.git`, `dist`, …) |
| `read_file` | read | line-numbered content, optional `start_line`/`end_line`, refuses >5 MB |
| `grep` | read | regex over file contents, `glob` filter, `max_results`, returns `file:line: text` |
| `write_file` | write | create / overwrite / `append`; returns a unified diff of what changed |
| `edit_file` | write | replace the first exact occurrence of `old_text`; whitespace-tolerant fallback; refuses ambiguous matches unless `replace_all` |
| `delete_file` | **danger** | delete a file or an *empty* directory (never recursive) |
| `run_command` | write | shell command in the workspace; returns stdout, stderr and `[exit N]`; timeout |
| `git` | write* | any git call; `status/diff/log/show/branch/remote/tag/rev-parse/ls-files/describe/shortlog/config/stash` are read-only and never prompt |
| `git_prepare` | write | branch + stage + conventional commit (+ optional push) in one call |
| `gh_create_pr` | write | open a PR via `gh` (or the REST API), auto-filling the template |
| `gh` | write | arbitrary `gh` CLI command |
| `gh_merge_pr` | **danger** | merge / squash / rebase / close a PR |
| `remember` | write | append a dated note to `.nexus/memory/<role>.md` |
| `memory` | write | long-term `MEMORY.md` / `USER.md` entries (add / remove / list) |
| `skill_view` | read | load a `SKILL.md` in full (no name = list the library) |
| `skill_manage` | write | create / patch / delete a skill (agentskills.io format) |
| `recall` | read | full-text search over your own past sessions |
| `ask_agent` | read | consult another role (one question, one answer, no tools, no recursion) |
| `finish` | read | end the run with a summary + follow-ups |

\* per-subcommand: read-only git never prompts, mutating git always does.

A role can be limited to a subset with `tools: [...]` in `roles.yaml`. In read-only/plan mode the
mutating tools are **removed from the registry**, not just hidden from the schema — a model that
hallucinates `write_file` gets `Unknown tool` instead of a modified file.

## The three safety layers

### 1. Path guard

Every file path is resolved against the workspace and must stay inside it:

```
✗ write_file ../../evil.txt
  → /tmp/evil.txt is outside the workspace (/repo). Pass --allow-outside if you really mean it.
```

Absolute paths are treated the same way. `--allow-outside` (or `PathGuard(allow_outside=True)`)
disables the check for the whole session — useful when you genuinely want the agent to edit
`~/.config`, dangerous otherwise.

### 2. Command deny list

`nexus/tools.py: DEFAULT_BLOCKED_PATTERNS` — matched case-insensitively against `command`,
`args`, `message`, `body`, `url` and against a reconstructed `git …` string:

| Pattern | Blocks |
| --- | --- |
| `rm -rf /…`, `rm -rf ~`, `rm -rf $HOME` | recursive delete of an absolute path |
| `sudo rm` | privileged delete |
| `mkfs`, `dd if=… of=/dev/…`, `> /dev/sdX` | disk destruction |
| `:(){ :\|:& };:` | fork bomb |
| `shutdown`, `reboot`, `halt` | host control |
| `git push --force` / `-f` (but not `--force-with-lease`) | history rewrite on a remote |
| `git reset --hard origin/…`, `git clean -f… /` | unrecoverable local loss |
| `chmod -R 777 /` | permission spray |
| `curl … \| sh`, `wget … \| sh` | pipe-to-shell installs |
| `npm publish` | irreversible release |
| `gh repo delete`, `git push --delete` | destructive remote operations |
| `drop database` | data loss |

By default a match is **refused** (the model gets `DENIED: blocked command pattern: …`). With
`--allow-dangerous` (or `settings.allow_dangerous: true`) it becomes a loud prompt instead — the
CLI prints the matched pattern in red before asking. Add your own with
`settings.blocked_command_patterns: ["\\bkubectl\\s+delete\\b"]`.

Known-good commands are deliberately *not* matched: `rm -rf ./build`, `git push origin feature/x`,
`git push --force-with-lease`, `npm run build`.

### 3. Approval modes

| Mode | Flag | Behaviour |
| --- | --- | --- |
| `ask` | default | prompt for every non-read tool |
| `auto` | `--yes` / `-y` / `NEXUS_YES=true` | auto-approve writes; **danger still blocked** unless `--allow-dangerous` |
| `plan` | `--plan` / `nexus ask` / `/ask` | read-only; every mutation is refused with an explanation the model can act on |

Prompt answers: `y` (once) · `n` (deny) · `a` (approve this tool for the rest of the session) ·
`s` (deny this tool for the rest of the session). `settings.always_allow_tools` and
`never_allow_tools` are the persistent versions of `a` and `s`.

In the VS Code sidebar the same decision is a modal with **Approve / Approve & remember / Deny**,
answered over `POST /api/approve/<task_id>`.

## Error handling philosophy

A tool failure is **data, not an exception**: it comes back to the model as
`is_error: true` content, so the agent can re-read the file, fix the regex or explain the
blocker. Three consecutive errors inject a system nudge ("stop retrying the same call"); six in a
row ends the run. Unknown tool names return the list of tools that *do* exist.

Shell commands return their exit code in the output (`[exit 3]`) rather than failing — a failing
test suite is information the agent needs, not a crash.

## Output hygiene

* every tool result is clipped to `settings.max_output_chars` (head + tail, with the number of
  dropped characters shown);
* `read_file` clips to `settings.max_read_chars`;
* when the conversation passes `settings.context_char_budget`, old tool results are compacted to
  their first 200 chars (the last few messages are never touched);
* API keys are never printed by a tool (the stores are outside the workspace), and the deny list
  blocks `cat .env`-style exfiltration only if you add the pattern — treat `--yes` on an untrusted
  repo accordingly.

## GitHub specifics

`gh_create_pr` prefers the `gh` CLI (it inherits your `gh auth login` session). Without `gh` it
falls back to `https://api.github.com/repos/<owner>/<repo>/pulls` with `GITHUB_TOKEN`, then adds
labels. The PR body is built from `.github/PULL_REQUEST_TEMPLATE.md`:

* `<!-- summary -->` → your `body` argument, or the title
* `<!-- motivation -->` → "Requested via Nexus Agent, auto-routed to **<role>**"
* appended footer → role → agent/model, branch, commit list, `git diff --stat` against the base
  branch, and the working-tree state at commit time

`gh_merge_pr` is `danger`: it always asks, even under `--yes`, and it never uses `--auto`.
