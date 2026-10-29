# Module 2 — GitHub automation: implementation and acceptance

Module 2's supported review-first workflow is implemented and covered by offline
integration acceptance. It extends `repository.py`, `github_repositories.py` and
`github_tasks.py`, with runnable project scaffolds and exact-head CI gates. It is
not a general computer permission system or a claim of live GitHub/Windows certification. Selected
GitHub tasks still use GitHub APIs directly: no clone, local path selection,
workspace switch, or manual branch/PR-number fields were added.

## Credential boundary

The dashboard repository transport, typed legacy GitHub tools and new provisioning CLI now require
`GITHUB_TOKEN` from the server environment or workspace `.env`. Existing GitHub CLI
login and `GH_TOKEN` alone are **not** accepted by those paths. A configured token
is provided to `gh` in the child environment, not its command line or persistent
records. `GH_HOST` is pinned to github.com, debug logging and unrelated enterprise
credentials are removed from that child environment. Raw provider failure output
is withheld; known secret formats and the exact active token are masked in success
output. Typed `gh` launches use argv (not a shell), bound github.com context, and the
existing cancellation/process runner. Their stdout/stderr are withheld from live
stream events because secrets can cross chunk boundaries; only lifecycle events
stream live. Complete bounded success output is masked afterward. Truncated output
is refused rather than returning a partial token. The parent environment is not changed. No token input field was added.

Copy `.env.example` to `.env` locally and fill it privately. Do not paste credentials
into chat, tasks, issue bodies, CLI flags or the example file. `.env` stays ignored;
`.env.example` is deliberately tracked with **empty** credential values. GitHub API
permissions remain authoritative; Nexus never grants scopes or bypasses org rules.

Prefer fine-grained tokens scoped to necessary repositories with Metadata read,
Contents read/write, Pull requests read/write and Issues read/write. Grant Workflows
write only for separately approved workflow modifications. Repository creation and
organization creation may require additional account/org authorization and a token
whose access includes the newly created repository. Missing scope fails closed;
Nexus does not silently fall back to another identity or stored `gh` login.

## Repository and issue creation (preview by default)

PowerShell:

```powershell
nexus github -C "C:\Users\HAMID\Nexus-Agent" create my-project --private --description "Project description"
nexus github -C "C:\Users\HAMID\Nexus-Agent" create my-project --private --execute
nexus github -C "C:\Users\HAMID\Nexus-Agent" issue OWNER/REPOSITORY --title "Track follow-up" --body "Acceptance criteria and evidence" --execute
```

Visibility is required: choose `--private` or `--public`. Optionally specify
`--organization ORG`. Preview shows exact owner/name/visibility and request body;
without `--execute` there is **no external mutation**. `--execute` then requires an
interactive exact confirmation, e.g. `CREATE PRIVATE REPOSITORY OWNER/my-project`.
There is no `--yes` shortcut. Account identity and existing isolation/busy guards
are rechecked. A five-minute preview is consumed before the POST; an uncertain
outcome is not automatically retried. Inspect GitHub before repeating a creation.
Repository creation asks GitHub to initialize a README; it does not clone, create a
local project folder, deploy, or merge into main. Issue creation requires an
accessible selected repository and fresh write authorization.

The existing authenticated Workbench routes also consume this feature:
`repository/resource-preview` and `repository/resource-execute`. Completed resource
records are stored through the existing Studio audit/state store. No second server
or database was introduced. No new provisioning dashboard buttons are claimed.

## Mandatory pre-commit checks in the existing review-first flows

- Dashboard local commits require a conventional subject, such as
  `feat(api): add login`. Full selected working files and the message are scanned
  before preview and again immediately before staging/commit. Unrelated staged
  files remain excluded by the existing `git commit --only` behavior.
- Direct GitHub task changes scan the complete resulting file before preview and
  again **before uploading any blob or creating a commit**. Generated messages use
  `chore(nexus): ...`. Non-force refs, branch-head pinning, fresh identity checks,
  role permissions and existing per-mutation approvals remain in place.
- Legacy `git_prepare` now scans **all actual staged blobs**, including previously
  staged paths, before committing. A clean working file cannot hide a staged secret.
  The typed `git commit` path requires exactly `-m MESSAGE`/`--message MESSAGE`, a
  conventional subject and a staged-blob scan; implicit `-a`, amend and file-sourced
  messages are refused. Stage explicitly first. `git_prepare` retains its existing
  whole-index commit behavior, so inspect unrelated staged files before using it.
- Git/GitHub typed argument checks run before approval and tool-argument events,
  rejecting recognized secrets and the exact active token without echoing them.
- These pre-commit scans cannot be disabled by the Studio `secret_scan` setting.
  They reject known token/key/password assignments, incomplete private-key markers,
  and the exact active GitHub token even if its format is unknown. Errors do not
  echo the finding. Binary/oversized/unscannable local files fail closed.
- Secret-free `.env.example` files are now selectable; real `.env` files, runtime
  state, private-key files, symlinks and unsafe paths remain restricted.

Secret scanning is heuristic, not proof that content contains no credentials.
These checks concern new selected content, not all historical commits. They do
not make repository hooks trusted or sandbox code execution.

## Fresh approval and typed GitHub restrictions

Registered dangerous tools (including delete and PR merge/close), known typed Git
network mutations, `git_prepare(push=true)`, and typed GitHub mutations now require
fresh human approval even with `--yes`, `always_allow`, session approval or
`allow_dangerous`. With no approval callback they fail closed. Recognized read-only
`gh pr list/view/diff/checks` and similar queries retain existing behavior. This is
not yet a general shell command policy/parser.

Generic typed `gh` refuses auth/alias/plugin commands, custom hosts/headers,
enterprise targets, stdin/file expansion, browser launching and `--auto`/`--admin`.
Use the reviewed provisioning CLI for repo creation; generic repo mutations and
release asset uploads are deliberately unavailable through this narrower path.
Body links remain data, not transport targets. The legacy PR helper now routes
through the same tested preview/execute service, with a second exact-diff human
approval. Its old unverified REST fallback is removed rather than bypassing CI.
Generic `gh api` is GET-only (GraphQL/fields/writes refused); raw PR mutations must
use reviewed helpers. Alternate typed Git commit producers (merge/rebase/am/
cherry-pick/revert/commit-tree/update-ref) are refused, and controlled local commits
run with hooks disabled. Arbitrary shell containment belongs to Module 3.

## Constrained token-only HTTPS Git transport

The existing local toolbar and typed `git push`/`git_prepare(push=true)` now share
`github_git.py`. Supported forms are `push origin BRANCH` and
`ls-remote --heads origin refs/heads/BRANCH`. `git_prepare`'s existing `-u` form is
accepted, but this transport deliberately does **not** change local upstream config.
It does not change branches, origins, credentials or Git configuration on disk.

The transport requires an exact workspace repository root and credential-free
`https://github.com/OWNER/REPOSITORY[.git]` origin. SSH origins fail closed; any
intentional origin conversion remains a separate human Git operation. It reads a
validated canonical URL rather than trusting an alternate `pushurl`. Credentials
come from the same environment/.env GITHUB_TOKEN policy, delivered through a child
`gh auth git-credential` helper. GitHub CLI must be installed for this transport.
No token appears in argv or temporary files.

System/global Git credentials/config, inherited Git configuration injection, SSH
agent settings and proxy environment are excluded. Local/worktree transport URL
rewrites, includes, credentials, HTTP/proxy/pushurl overrides are refused without
echoing their values. Empty temporary hooks directories disable hook execution;
only HTTPS protocol is allowed, TLS verification remains on and redirects are off.
The helper and runtime executables/PATH are trusted; this is not OS isolation.

Push is limited to the **current local branch**, excluding main/master and the
freshly observed remote default branch. It reads remote state, scans outgoing
commit messages and snapshots (including older secret versions later deleted),
checks that local HEAD did not advance and pushes the immutable scanned SHA to
that branch without force. A missing local object for the remote head fails rather
than fetching. GitHub's non-force/ref protections remain authoritative; there is
no claim of a cross-process atomic Git snapshot or immunity to concurrent human
configuration changes. Do not concurrently mutate the checkout during review.

**Current bounded-text limits:** at most 200 outgoing commits, 1,000 unique blobs,
2 MB per blob and 20 MB total; a 60-second scan budget is checked between local
Git calls (an individual call can take up to its 30-second timeout). Network calls
are individually timeout-bounded. Every outgoing snapshot is scanned; an unchanged
inherited binary/private path can still cause rejection. A new remote branch
conservatively scans all reachable local history, not merely the latest diff.
Binary files, symlinks, submodules and private paths are refused instead of being
silently skipped. Broader binary-project and reviewed-history support remain
future work. No live Windows helper/GitHub push acceptance has been performed.

Typed transport retains cancellation/process lifecycle events but withholds live
network stdout/stderr. The toolbar is synchronous with bounded network timeouts.
Uncertain network errors do not trigger automatic retries: inspect GitHub first.

## Runnable project workflow (no manual branch or PR-number entry)

```powershell
# First create an initialized remote, with explicit visibility and approval.
nexus github create my-shop --private --execute
# Review all generated files and MIT license, then approve a separate new folder/repo.
nexus github scaffold shop --repo OWNER/my-shop --stack python --execute
# Node's dependency-free HTTP service/test scaffold is also supported: --stack node
nexus github projects
nexus github project shop push --execute
# Wait for real GitHub push CI to finish; creating a PR before tests fails closed.
nexus github project shop pr --execute
nexus github project shop merge --execute
# For subsequent edited files, review/commit first, then repeat push -> CI -> PR.
nexus github project shop commit --message "feat: add endpoint" --execute
```

The scaffold creates `projects/shop/` as its **own** Git repository, never resetting,
cleaning, cloning into or switching the parent workspace. Its generated branch is
`nexus/project-shop`. README, MIT license, gitignore, empty env example, functional
Python/Node `/health` service, real tests and GitHub push/PR test workflow are all
reviewable. Python uses stdlib unittest; Node uses node:test; no dependency install
or fabricated test results are necessary. Initial local author identity is explicitly
`Nexus Agent`, configured only in that new project. Operator changes remain possible.

Folder/repository/base/branch/published heads/PR are persisted in the **existing
Studio store**, not another learning database. `project ... commit` reuses the existing
exact-file preview/commit and secret scanner. Project publish scans immutable
committed text and uses GitHub's Git Data API to build on the remote base tree,
create/update only the project task ref, preserve remote README ancestry and delete
only paths previously owned by that project. It never force-pushes unrelated local
initial history into main. Local and remote commit SHAs differ deliberately; CI gates
use the actual **remote** SHA. No clone/fetch is needed or hidden. Base-only files can
remain on the remote: the local scaffold is not represented as a complete remote clone.
Publishing workflow files can consume Actions minutes; approval explicitly warns of
CI budget/scopes. GitHub may require Workflows write permission and organization policy
approval. Uncertain mutations consume approvals and require manual inspection, not
blind retries. Text publish is bounded to 200 files, 500 KB each, a 2 MB request tree
and a scan deadline checked between local commands.

The same mapped operations have real authenticated Workbench consumers:
`repository/projects`, `repository/project-preview`, `repository/project-execute`.
No new project-toolbar buttons or fake UI data are claimed.

## Tests-before-PR and approval-before-main

Every supported PR create and merge path (toolbar, direct task, mapped project and
legacy reviewed helpers) checks GitHub's latest check runs and combined statuses
for the **exact immutable reviewed head** during both preview and execution.
At least one `test`/`tests` job must have completed successfully within 24 hours;
pending, failed, skipped, missing, truncated, wrong-head or stale evidence blocks.
Lint alone, zero configured checks, prose and local-only test claims do not qualify.
All returned checks/statuses must succeed. This deliberately stricter bounded
policy supports up to 100 checks/statuses; larger sets fail closed. Check-run names
and outcomes are GitHub-reported evidence, not proof of test quality or immunity to
repository-owner CI tampering. Review the workflow and diff.

Generated workflows run on **push** as well as PR so tests can exist before PR
creation. Merge additionally rechecks open/non-draft state, clean mergeability,
base/head identity and uses an explicit PR number and `--match-head-commit` after
fresh approval. There is no auto/admin merge or automatic branch deletion. External
actors can race GitHub refs; GitHub protections remain authoritative and no
cross-service atomicity is claimed. Protected-branch/ruleset configuration is an
operator responsibility.

## Module 2 acceptance and operational limits

| Requirement | Consumer / acceptance |
|---|---|
| Env/.env GITHUB_TOKEN only, no key output | shared credential/transport tests; no GH_TOKEN/login fallback |
| Public/private/org repository + initialization | reviewed resource creation; response visibility/default branch validated |
| Per-project folder/repo, branch, conventional commit | mapped scaffold lifecycle, real disposable Git tests |
| Push, PR, merge and issues | existing/API project flows, fresh approval and immutable-head checks |
| Scan before commit/publish | full selected files, staged blobs, committed outbound versions; negative secret tests |
| README/gitignore/license/CI scaffold | runnable Python/Node tests and YAML validation |
| Tests -> PR -> approval -> main | scripted full project integration, stale/revoked evidence failures |
| Existing structures preserved | parent Git/file invariants and dashboard regressions |

**Code/consumer and offline acceptance gates are implemented.** Outstanding operational
verification is live, explicitly authorized GitHub creation/push/PR and native Windows
Git/gh. No such live operation was performed here. The bounded local HTTPS text-only
transport limits above are intentional fail-closed constraints; mapped text projects
use the Git Data API. Unrestricted binary/large-history transport and arbitrary shell
or external application containment are not claimed. General computer permissions,
snapshots/undo, Office/browser/VS Code actions and OS kill controls belong to Module 3.
Do not enable unattended autonomy before those boundaries and later budgets are verified.

## Verification

All GitHub APIs in the new tests are **scripted**. They exercise token precedence,
no GH_TOKEN fallback, child-environment handling, secret masking, no secret values
in errors/argv, mandatory rescans, conventional commits, one-use approval,
identity changes, public/private/org provisioning, issues, busy guards and uncertain
outcomes. Additional legacy tests cover actual index-vs-worktree content,
preexisting staged secrets, binary/private/symlink rejection, safe deletion of a
legacy secret, real typed commits, credential-bearing arguments before logging,
argv shell-metacharacter safety, chunked/truncated output, host context, exit
status, pre-cancelled launches and repeated fresh approvals. Disposable local Git
tests exercise real commits. Forty-three additional HTTPS tests cover canonical
URLs, immutable SHAs, current/default/protected branches, historical secrets,
local/worktree/env injection, hooks/credentials, scan limits, head drift, cancellation,
shared entry points and default subprocess behavior. No live repository,
issue, branch, PR or merge was created during this implementation.

Module 2 closure passed **848 Python tests and 29 browser tests**. The subsequent
Module 3 foundation, including 25 dedicated policy tests, passes **873 Python tests
and 29 browser tests** with a restarted current-code server. Twenty dedicated
project-completion tests include executable Python/Node scaffolds, real local Git,
scripted remote lifecycle/CI and stale/revoked-approval failures. Ruff, `git diff
--check` and wheel build also pass. Live GitHub mutations/native Windows remain
unperformed; scripted GitHub outcomes are not presented as live integration proof.
