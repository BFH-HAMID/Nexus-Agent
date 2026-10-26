# Repository toolbar

The dashboard has an Arena-style compact repository strip immediately below the
**Describe the task** input (inside its composer, not below the page header):

**repository selector · current branch · +additions / −deletions · Create PR · ⋯**

Select the diff-count chip to review changed files and optionally commit them.
The **⋯ Repository actions** popover contains **Refresh status**, **Commit selected
files**, **Push branch**, **View pull requests**, and **Merge PR**. It supports
keyboard focus, Escape and outside-click dismissal, and opens above the composer
without being clipped by the task card. On small screens the controls wrap rather
than overflowing. Status refreshes after a run finishes, without remote fetching.

This matches the supplied visual placement and compact interaction pattern, not
Arena's private backend or session/workspace management. Branch selection remains
read-only; Git-only checkout selection never silently changes the task workspace.

Every count/branch/file/PR comes from real Git or an explicitly requested GitHub CLI
query. There are no demo repositories or invented remote checks.

## Repository selection

The selector lists the current server workspace and existing non-hidden local Git
checkouts directly beside it (bounded to 100 sibling directories). It does not
browse arbitrary server paths or clone remote repositories. Clone/open additional
trusted checkouts locally beside the workspace if you want them listed.

Selection changes **Git controls only**. It does not change the agent workspace,
start a task, create/check out a branch, or fetch remote data. The current branch
is displayed read-only. A detached HEAD, main/master, or a branch changed since
this server first observed the checkout cannot be mutated through this toolbar.
The Arena session branch is never renamed or switched by these controls.

Refresh reads local refs/status/diffs. Ahead/behind values use local upstream refs;
no upstream is shown as unavailable, not zero. Line counts include readable
untracked files; binary changes are labelled separately. Private/runtime paths
are not selectable for commit.

## Commit

1. Select **Commit**, choose exact changed files, and enter the message.
2. Select **Preview action**, inspect paths/diffs/content or binary size/hash, and
   the exact repository, branch and HEAD.
3. Type the exact displayed confirmation and select **Execute reviewed action**.

Only selected working-tree files are committed. Unrelated staged changes remain
staged. File deletions and new files are supported. Symlinks/private paths and
oversized reviews are refused. Preview is bounded to 2 MB; large changes belong
in a terminal review.

A commit can invoke repository hooks, Git signing, and locally configured tools:
use only trusted checkouts. Git author identity must already be configured.
If commit fails after staging selected files, the error explicitly warns that the
index may have changed; inspect status rather than assuming rollback. No reset,
automatic retry, or secret identity/configuration changes are performed.

## Push and PR

Push previews the exact committed HEAD, origin repository and observed remote
branch HEAD. Typed confirmation is mandatory. It runs ordinary `git push origin
<branch>`, never force-push, deletion, or an implicit commit.

**Create PR** requires that exact HEAD already be pushed. Supply a reviewed title,
body and base, preview, and confirm. The diff uses the local origin/base ref and
is labelled as such. This action uses `gh pr create --repo ... --head ...`; it
never silently pushes. **PRs** explicitly reads actual open PRs for that selected
repository/branch and offers links and merge-review actions.

Remote actions require a credential-free GitHub `origin` URL and configured Git/
`gh` authentication. No token/password field is provided. No GitHub request runs
merely because the page loads or a repository is selected.

## Merge

Supply an explicit PR number and choose merge/squash/rebase. Preview requires:

- Open, non-draft PR matching the exact local branch/commit.
- Known mergeability, CLEAN merge state, no pending/failed reported checks.
- Review of actual PR metadata/checks; no configured checks is **not** test proof.

After typed confirmation, the server rechecks repo/HEAD/index, origin/remote HEAD,
PR head, base and checks. It runs `gh pr merge <explicit-number> --repo ...
--merge|--squash|--rebase --match-head-commit <reviewed-sha>`. It never uses
`--admin`, `--auto`, branch deletion, local checkout or a follow-up pull/reset.
GitHub branch protection and server rules still apply. Remote changes can race a
review; exact-head matching and GitHub's own protection remain authoritative.

## Safety and validation

Previews are one-use, expire after five minutes, and are invalidated by local
HEAD/index/working-tree changes. Concurrent reuse is rejected. Switching the
selector/forms discards old or late-arriving browser previews. Active/queued
agents, workflows and managed dev apps block mutations. Enforced Studio isolation
also blocks host Git/GitHub mutations; there is no host-network bypass.

The API uses the existing server authentication and origin protections under
`/api/workbench/repository/`. Set `NEXUS_SERVE_TOKEN` before exposing a server on
an untrusted network. Commands run under the server OS account; repository
selection is not an OS sandbox or multi-user RBAC boundary. External edits and
hook side effects cannot be made a global filesystem transaction by this toolbar.

Tests make real commits only in disposable fixture repositories. GitHub push/PR/
merge transports are scripted in tests; no real commit/push/PR/merge is performed
on the user's checkout while implementing or smoke-testing the toolbar.
