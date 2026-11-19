# DevPilot (Task 25)

Opt-in. You describe one small change. A model writes a patch, DevPilot puts
the patch into a throw-away checkout of your last commit, runs your test
command there, and lets the model try again a few times. Your working tree is
changed only by a separate `apply` that you approve after reading the diff.
DevPilot never commits, pushes or opens a pull request.

> **The checkout is not a sandbox.** It keeps half-finished edits away from
> your files. It does not contain the code. Your test command runs on this
> machine with your rights, and it runs code that a model wrote and that nobody
> has read yet. That code can read any file you can read, use the network and
> write anywhere you can write. Turn DevPilot on only for projects where you
> accept that, and prefer a local model.

| | |
| --- | --- |
| Policy file | `devpilot.yaml` (operator-owned, protected, default `enabled: false`) |
| Code | `nexus/devpilot/` |
| Needs | `git`; nothing else is installed |
| State | `<NEXUS_HOME>/devpilot/<workspace-hash>/runs/<run id>/` |
| CLI | `nexus devpilot status\|detect\|run\|list\|show\|diff\|apply\|discard` |
| Tools | `devpilot_run` (DANGEROUS), `devpilot_show` (READ), `devpilot_apply` (DANGEROUS) |
| Slash | `/devpilot`, `/devpilot show RUN`, `/devpilot diff RUN` (read-only) |

## Enable

```yaml
# devpilot.yaml
schema: 1
enabled: true
agent: "ollama"        # an agents.yaml name; "" = your default agent
allow_cloud: false     # true = your source files may go to a hosted model
```

```bash
nexus devpilot detect                        # the commands a run would start
nexus devpilot run "Make parse_date() in app/dates.py accept ISO week dates" -f app/dates.py
nexus devpilot diff dp-20261110-101500-1a2b3c4d
nexus devpilot apply dp-20261110-101500-1a2b3c4d
```

Name the files in the task or pass them with `-f`. The model starts with no
file content at all (see [What the model sees](#what-the-model-sees)).

## What one run does

1. Checks the preconditions (below). Nothing has started yet.
2. Clones your last commit into `<run folder>/tree`. Uncommitted work is not
   part of it; the run tells you how many tracked files had uncommitted changes.
3. Runs lint and test once on the unchanged checkout ("checks before the
   patch"). Skip this with `checks.baseline: false`.
4. Asks the model for a patch, checks it against the [patch rules](#patch-rules),
   writes it into the checkout and runs lint, then test.
5. Repeats step 4 with the check output until the checks pass or a
   [limit](#limits) is reached.
6. Stores the patch and the record, deletes the checkout.

One run at a time per workspace. A run is refused when:

- the workspace is not the top folder of a git repository, or has no commit;
- a file given with `-f` has uncommitted changes or is not in the last commit;
- the agent is a cloud agent and `allow_cloud` is false;
- `permissions.yaml` sends shell commands to the Docker backend (DevPilot runs
  its checks on the host and does not pretend otherwise);
- the emergency stop is latched.

### Run status

| Status | Meaning |
| --- | --- |
| `passed` | The test command exited 0 with the patch in place (and the lint command, if there is one). |
| `failed` | There is a patch, and the last check did not exit 0. |
| `unverified` | There is a patch, but no test command ran. |
| `no_patch` | The model produced no valid change. |
| `error` | The run could not complete: the model did not answer, git failed, or a check command could not run on the unchanged checkout. The record has no patch. |
| `applied` | The patch was written to the working tree. |

**`passed` is a statement about an exit code.** It does not say that the task
is done, that the tests test the change, or that the patch is safe. Three
things help you judge it:

- The record shows the checks before the patch. "failed, then passed" means
  the patch changed the outcome. "passed, then passed" only means nothing that
  is tested broke.
- The record says when the patch touches test files. Read those hunks first.
- The model's one-line summary is printed as "the model's own summary
  (unverified)". It is not given to the calling agent.

By the time one attempt passes, the code of the earlier attempts has already
run on your machine. Between checks DevPilot puts every tracked and patched
file back to "last commit plus patch" and deletes Python bytecode caches in
the checkout. It cannot undo other effects: untracked files, caches of other
tools, anything outside the checkout. **Run your tests yourself after
`apply`.**

## Check commands

DevPilot runs at most two commands: lint, then test. They come from
`devpilot.yaml` or from the fixed table below, never from a model.

| Project marker (first match wins) | Test | Lint |
| --- | --- | --- |
| `pyproject.toml`, `setup.py`, `setup.cfg`, `pytest.ini`, `tox.ini`, or `test_*.py` in `tests/` or `test/` | `{python} -m pytest -q` | `<python> -m ruff check .` when `ruff.toml`, `.ruff.toml` or `[tool.ruff]` exists |
| `package.json` | `npm test` (`yarn`/`pnpm` by lock file) when `scripts.test` exists and is not the npm placeholder | `npm run lint` when `scripts.lint` exists |
| `Cargo.toml` | `cargo test` | none |
| `go.mod` | `go test ./...` | `go vet ./...` |
| `Makefile` with a `test:` target | `make test` | none |

To choose them yourself:

```yaml
checks:
  test: "{python} -m pytest -q -x tests/unit"   # "" = detect, "none" = no tests
  lint: "none"
  timeout_seconds: 600                           # per command
```

- Each is **one plain command**, started without a shell: no pipes, `;`, `&&`,
  redirects or `$VAR`. Put anything more in a script and name the script.
- **Use commands that do not rewrite files**: `ruff check .`, not
  `ruff check --fix .`; `prettier --check`, not `--write`. DevPilot undoes what
  the checks change in tracked and patched files, so a fixing command would
  pass here and fail for you. The record carries a note when that happened.
- The commands must be the same in the working tree and in the last commit. If
  an uncommitted `package.json` or `pyproject.toml` changes what would be
  detected, the run stops before any model call. Commit it, or set the commands
  in `devpilot.yaml`.
- For a large test-suite, name a focused command. Every attempt runs it again.

### How the commands run

- Working folder: the checkout.
- Environment: yours, without variables whose names end in `TOKEN`, `API_KEY`,
  `PRIVATE_KEY`, `SECRET`, `PASSWORD`, `PASSWD`, `AUTHORIZATION`, `CREDENTIAL`,
  `_PIN` or `_KEY` (or their plurals), without `SSH_AUTH_SOCK`, `SSH_ASKPASS`,
  `GPG_AGENT_INFO`, `NETRC` and without any `GIT_*` variable. This removes
  credentials from the environment only. Credential **files** in your home
  folder stay readable.
- No input; the command and its child processes are stopped at the time limit,
  on `nexus stop` and on cancel. A process that detaches itself from its
  process group is not stopped.
- Output: the first 2,000 and the last 10,000 bytes are kept. Terminal control
  sequences are removed and known credentials are masked before the text is
  stored or sent to the model.

### Python: `{python}` and the per-run environment

`{python}` as a word of a check command means "the project's interpreter":
`.venv` or `venv` in the workspace, otherwise the interpreter that runs Nexus.
DevPilot does not run it directly. It builds a small virtualenv for the run
(`<run folder>/env`, no packages of its own, a fraction of a second) whose
start-up file

- adds the site-packages of the project's interpreter, so your installed
  dependencies are found, and
- puts the checkout in front of them.

Why: a project that is installed in editable mode (`pip install -e .`) is
imported from the working tree, not from the checkout. `PYTHONPATH` (which
DevPilot also sets, checkout first) fixes that only for processes that honour
it; tests that start `python -I` or `python -E` would silently test your
working tree instead of the patch. With `{python}` both cases import the
checkout. Your own virtualenv is not modified.

What it covers: packages at the repository root or under `src/`, and folders
that an editable install adds to the import path. What it does not cover:
editable installs that map packages to other folders through an import hook,
and console scripts on your `PATH`. If your tests need more than that, give
`checks.test` a command that builds its own environment (`tox`, `nox`,
`uv run`).

If the environment cannot be built, the run goes on with the project's
interpreter and records a note.

A pytest exit code of 3 or 4 on the unchanged checkout (internal error, usage
error) ends the run before any model call: the runner could not start, which
no patch will fix.

### Files the checks need that git does not track

The checkout holds tracked files only. For untracked folders the checks need
(typically `node_modules`):

```yaml
checks:
  link: [node_modules]
```

Each entry becomes a symbolic link from the checkout to the same path in your
workspace. The folder is **shared, not copied**: code under test can change
it. An entry is skipped with a note when it does not exist or when the
repository tracks that path. A patch can never touch a path that goes through
a link. Do not link files that hold credentials.

Submodules are not checked out.

## Patch rules

The model answers with one JSON object: a list of files, each with `edit`
(exact find/replace, the text must occur once), `write` (the whole file) or
`delete`, and optionally `need_files`. A reply that breaks a rule changes
nothing; the model is told why and the reply still counts as a model call.

A patch is refused when it

- names a path that is absolute, contains `..`, a backslash or non-printable
  characters, or goes through a symbolic link;
- touches a private or runtime path (`.git`, `.env`, keys), an operator-owned
  policy file (`permissions.yaml`, `devpilot.yaml`, ...), or a path in
  `patch.protect` (by default CI definitions, `.gitmodules`, `.gitattributes`,
  `.envrc`, `.pre-commit-config.yaml`, `.husky`, `.vscode`, `.devcontainer`);
- is outside `patch.allow`, when you set it;
- changes an existing file whose content was not in the model's current
  message. A new file needs no look-up;
- changes a file that is not part of the last commit but exists in the
  checkout (the checks created it), a file that a git filter handles (Git
  LFS), or creates a file that `.gitignore` ignores;
- changes more than `patch.max_files` files in the run, or makes a file larger
  than `patch.max_file_bytes`;
- creates files while `patch.allow_new_files` is false, or deletes files while
  `patch.allow_delete` is false (the default);
- contains binary data, credential-like text, a `[REDACTED...]` placeholder, or
  adds control characters, a bare carriage return or text-direction characters
  (the "Trojan Source" set U+202A-202E, U+2066-2069);
- edits a file that itself holds credential-like text.

Line endings of an existing file are kept. A missing final newline is added.

Not checked: zero-width characters and look-alike letters. Bengali, Persian
and other scripts use zero-width joiners legitimately, so DevPilot does not
refuse them.

## What the model sees

Each call is one system message and one JSON document. The model has no tools
and no memory of earlier calls; the document carries everything.

- The task. It is the only instruction; credentials in it are masked.
- The check commands and the patch limits.
- Up to `context.listing` tracked file names.
- The content of up to `context.max_files` files within `context.max_bytes`:
  files the patch already changed, files given with `-f`, tracked files named
  in the task or in failing check output, and files the model asked for.
- The patch so far, as a diff.
- What the last failing check printed (up to 9,000 characters per command),
  marked as untrusted data, and what was wrong with the last reply.

Never shown, and therefore never editable:

- files with credential-like text. This cannot be overridden;
- files with instruction-like text (possible prompt injection). The operator
  can show them with `nexus devpilot run --include-flagged`; the run is then
  marked and an agent that reads it is marked tainted. The agent tool has no
  such option;
- binary files, files that are not UTF-8, files over `patch.max_file_bytes`,
  private paths and paths through symbolic links.

The reason is listed for the model under `files_not_shown`. Test fixtures
often contain fake tokens or prompt-injection samples, so expect some test
files to be out of reach. In the Nexus repository itself that is about one
text file in six by default, and about one in fifteen with `--include-flagged`.

## Limits

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_iterations` | 4 (1-10) | Model calls per run. **Every call counts**: a patch, a request to read files, a reply that breaks a rule, a reply that is not JSON. `--max-iterations` can lower it, not raise it. |
| `max_tokens_per_call` | 4000 | Output limit of one call. |
| `max_cost_usd` | 0.50 | Estimated spend. Checked before each call, so the last call can go past it. 0 = no limit. |
| `timeout_seconds` | 1800 | Whole run. Checked before each model call, and no check gets more time than is left. A model call that is under way is not interrupted. |
| `checks.timeout_seconds` | 600 | One check command. |
| `keep_runs` | 10 | Older run records are deleted when a new run starts. |

`nexus stop` (emergency stop) ends a running check at once and the run at its
next step.

## Cloud models

A cloud agent is any agent whose endpoint is not on this machine or a private
network address. The provider name does not matter: a hosted
OpenAI-compatible service is a cloud agent.

With `allow_cloud: false` (the default) a cloud agent is refused, and a local
agent's `fallback` is **not** used when it is a cloud agent. If the local
model is down the run ends with status `error`; your source files are not sent
elsewhere. With `allow_cloud: true` the normal fallback chain applies.

## Review and apply

```bash
nexus devpilot show RUN      # status, commands, checks before and after, files, notes
nexus devpilot diff RUN      # unified diff; control and text-direction characters are spelled out as <U+XXXX>
nexus devpilot apply RUN     # prints the summary, asks y/N; --yes skips the question
```

`apply` writes the stored patch into the working tree, all files or none. It
is refused when

- the run is not `passed` (set `apply.require_passing_checks: false` to allow
  `failed` and `unverified` runs);
- the repository is no longer at the commit the run started from;
- a file to change is not byte-for-byte the committed version (a difference in
  CRLF/LF line endings alone is accepted and kept), or a file to add already
  exists;
- a path is now protected by `devpilot.yaml` or `permissions.yaml`;
- the stored patch does not match its digest;
- the emergency stop is latched.

Nothing is committed or pushed. To undo: `git restore <files>` brings back the
committed versions, and you delete the added files. Each write is also in
`nexus agent snapshots`. To commit or open a pull request use git, or the
existing reviewed tools `git_prepare` and `gh_create_pr`.

## Agent tools

Registered only while DevPilot is on.

| Tool | Risk | Approval prompt shows |
| --- | --- | --- |
| `devpilot_run(task, files?)` | DANGEROUS | the exact commands that will run, "not sandboxed", the commit, the agent, whether source goes to a cloud model, the number of model calls |
| `devpilot_show(run_id)` | READ | - |
| `devpilot_apply(run_id, patch_sha256)` | DANGEROUS | the files and line counts, check result, whether tests are touched |

DANGEROUS tools need a fresh human approval for every call; `--yes` and
"always allow" do not apply. A run is refused when the commands differ from
the ones in the approved prompt. `devpilot_apply` needs the `patch_sha256` of
the patch that was shown.

## Records and audit

A run folder holds `run.json` (task, commands, check results and output for
every attempt, notes, token use) and `blobs/` (the old and new content of each
changed file). The checkout and the per-run environment are deleted when the
run ends, and at the start of the next run if a crash left them behind.
`nexus devpilot discard RUN` deletes a record.

Audit actions: `devpilot.run` (result = status), `devpilot.apply`
(`applied`, `refused`, `failed`), `devpilot.discard`.

## Differences from the task description

- **Clone, not worktree.** The checkout is `git clone --shared` with the
  remote removed, not `git worktree`. A worktree writes into your repository's
  `.git`, shares its refs and runs its hooks. The clone reads your objects and
  writes nothing back.
- **No push, no pull request.** The description had these "off by default and
  DANGEROUS". DevPilot does not have them at all and hands over to
  `git_prepare` / `gh_create_pr`, which already have their own review.
- **Running is DANGEROUS, not only applying.** A run executes unread
  model-written code on the host.
- **Apply needs the exact commit and byte-identical files.** No merging.

## Known limits

- Not a sandbox (see the top of this page).
- Tested on Linux only. The Windows paths (`Scripts\python.exe`, symbolic
  links for `checks.link`) are written but have not been run.
- Tested with scripted model replies. No real model was available where this
  was built, so prompt quality with a particular model is unmeasured.
- One task per run, text files only, at most `patch.max_files` files.
- Files with credential-like or instruction-like text are out of reach (see
  above).
- No submodules, no Git LFS content.
- `{python}` covers the layouts listed above, not every editable install.
- A check that needs services, credentials or untracked configuration will
  fail in the checkout unless its command provides them.
