# Repository toolbar

The dashboard's compact repository strip is inside the task composer, immediately
below **Describe the task**. It provides GitHub repository search/selection, branch
information, a local diff chip when available, **Create PR**, and **⋯** actions.
Controls wrap on mobile. Every repository, branch, count and PR is real; no demo
repositories, local-project fallback, or invented remote checks are shown.

## Connected GitHub repositories

The selector lists **all repositories accessible to the GitHub CLI account on the
Nexus server**, including public/private owned, collaborator and organization
repositories allowed by that account's credentials/organization policies. It
uses paginated `gh api user/repos`, not filesystem projects or sibling checkouts.
The search field filters by owner/repository. Searching does not silently change
the current selection: explicitly choose a result to change the target.

Sign in with `gh auth login` on the computer running Nexus if needed. Dashboard
requests use that server's configured GitHub CLI authentication; an Arena browser
connection is not automatically forwarded to a separately installed local Nexus.
No dashboard token/password field is provided. Missing authentication is reported
as an error, never replaced by a list of local projects.

Loading the dashboard requests GitHub inventory and selected-repository metadata.
Inventory is cached for 60 seconds, tied to the authenticated account; **Refresh
GitHub repositories** forces a reload. PR lists and branches follow all pages.
Select a repository and submit a prompt to run a **direct GitHub task**.
The server uses GitHub CLI authentication (`gh auth login`, or a server-side
`GH_TOKEN`/`GITHUB_TOKEN` with appropriate repository permissions). Never send a
token in a task prompt or chat; the dashboard does not store GitHub tokens.

## Repo → task branch → approved commits → PR → merge

1. Choose an accessible repo, enter the task and click **Run**.
2. Approve creation of `nexus/task-<task-id>` from its default branch. The backend
   binds the task to that repository/account/base/branch; no manual branch naming.
3. The model sees remote list/read/write/edit/delete tools, not local shell/Git or
   unrelated local files. Approve each exact file diff, or explicitly allow that
   tool for this session. Each successful mutation creates a commit directly on
   the task branch using Git Data APIs. No local commit or push step is needed.
4. Click **Create PR**. Nexus derives the head/base/title/body from the persisted
   task, previews the GitHub diff, and asks only **Approve Create PR**. No typed
   confirmation, branch entry or manual PR number is needed in task mode.
5. Click **Merge PR**, inspect the task's actual PR/checks/diff, then **Approve
   Merge**. Existing head matching, GitHub checks/protection and one-use review
   safeguards still apply; no admin bypass or auto-merge is enabled.

A remote task cannot execute local builds/tests. The PR body and dashboard say
that local tests were not run; review GitHub CI before merging. GitHub workflows
may run on task branch pushes, subject to the repository's own workflow settings.
The task tools do not change GitHub rules, permissions or workflow settings to
bypass checks. Read-only tasks do not create branches or expose write tools.

Account changes, permission revocation, repository renames, unsafe/private paths,
symlink/submodule/binary edits, oversized text/tree/diffs and concurrent branch
advances fail closed. Remote edits are reviewed against an exact observed head;
ref updates never force-push. Failed/cancelled requests can leave an approved
branch, already-completed commits, or unreferenced Git objects; review the remote
state before retrying. There is no automatic rollback or blind replay.

Task metadata is persisted in the server database and the current browser session
remembers task IDs for PR actions after a refresh. API actions revalidate all
metadata; browser state is never an authority for repository/branch/PR identity.
The existing manual PR form remains available when no matching remote task is
selected. Selecting the **Local workspace** option keeps the original local task
behavior. Selecting a GitHub repo never switches the server's local checkout.

## Local commit and push

**Commit**, **Push**, and local `+ / −` counts are available **only when the selected
GitHub repository matches the server workspace's validated GitHub `origin`**.
Otherwise the chip reads **Local diff unavailable** and local actions are disabled;
PR actions remain available subject to GitHub permissions. No unrelated local
checkout is used. Protected main/master, detached HEAD, or a workspace branch
changed since the server first observed it cannot be mutated by the toolbar.

To commit, select exact files, enter a message, preview paths/diffs, then type the
exact confirmation. Unrelated staged changes remain staged. Deletions/new files
are supported; symlinks/private/runtime paths and oversized reviews are refused.
Counts include readable untracked files and label binary changes separately.
Ahead/behind uses existing local upstream refs; unavailable is not zero.

Commits may invoke repository hooks, signing and configured tools: trust the
workspace and configure Git author identity beforehand. Failure after staging can
leave selected paths staged; inspect status rather than assuming rollback. No
reset or automatic retry is performed.

Push previews the exact committed HEAD, origin and observed remote branch HEAD.
After confirmation it runs ordinary `git push origin <branch>`: no force-push,
deletion, implicit commit, or branch switch. Local HEAD/index/content and origin
are revalidated before execution.

## Create and list PRs

**Create PR** targets the selected GitHub repository, with no local checkout
required. Choose existing **remote base and head branches**, supply a reviewed
title/body, and preview the actual GitHub comparison of their observed commit
SHAs. The base defaults to the selected repository's default branch. Head/base
must differ and contain different commits; push your desired branch separately
first. Confirmation is mandatory. The server rechecks account, write permission,
repository identity and both branch SHAs before `gh pr create --repo ... --head
... --base ...`. It never silently pushes or clones.

**View pull requests** lists all open PRs in that repository, not only PRs for the
server workspace's current branch. It offers GitHub links and merge-review actions.

## Merge

Supply an explicit PR number and choose merge/squash/rebase. Preview requires an
open, non-draft PR, known mergeability, CLEAN merge state, and no pending/failed
reported checks. Review includes the remote PR metadata/checks and actual diff;
no configured checks is **not** evidence of passing tests. The PR need not match
the local workspace branch or commit.

Execution revalidates the account, repository permissions, PR head/base and checks,
then runs `gh pr merge <explicit-number> --repo ... --merge|--squash|--rebase
--match-head-commit <reviewed-sha>`. No `--admin`, `--auto`, branch deletion, local
checkout, or follow-up pull/reset is used. Remote branches can still race between
API calls and a command; GitHub protections are authoritative and merge uses an
exact-head precondition. PR creation is not an atomic branch-SHA transaction.

## Safety and validation

Previews are one-use, expire after five minutes, and are bounded to 2 MB. Remote
previews recheck remote state; matching-workspace previews additionally check
local HEAD/index/content. Concurrent reuse is rejected. Switching repository or
editing forms discards old/late browser previews. Active/queued agents, workflows
and managed dev apps block mutations. Enforced Studio isolation blocks GitHub
inventory/reads and host Git/GitHub mutations; there is no network bypass.

The API uses existing authentication/origin protection under
`/api/workbench/repository/`. Set `NEXUS_SERVE_TOKEN` before exposing a server on an
untrusted network. Commands run as the server OS account; repository selection is
not an OS sandbox or multi-user RBAC boundary. Hooks and external edits cannot be
made a global filesystem transaction.

Tests use disposable local repositories and scripted GitHub transports; they do
not mutate real GitHub repositories to verify toolbar behavior.
