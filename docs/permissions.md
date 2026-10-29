# Module 3 foundation — permissions, not a sandbox

The permission foundation and supported computer adapters are implemented; platform acceptance gates remain open. `nexus/permissions.py` is consumed by the
existing NexusAgent/ApprovalGate; there is no second tool executor or learning DB.

## Policy

The workspace `permissions.yaml` is reloaded for each tool decision. Missing policy
also defaults to ASK_EVERY_TIME. Unknown keys, malformed YAML, symlinked/oversized
policy files or unknown modes fail closed. Operator-owned policy example:

```yaml
schema: 1
default: ASK_EVERY_TIME
tools:
  read_file: READ_ONLY
  write_file: ASK_EVERY_TIME
  run_command: ASK_EVERY_TIME
```

- **READ_ONLY:** true read calls only; refuses mutation/execution even under --yes.
  Git branch/tag/config labels alone do not make mutating arguments read-only.
- **ASK_EVERY_TIME:** every mutation requires a fresh callback decision; --yes,
  always-allow and cached session approval do not bypass it.
- **AUTO_ALLOW:** permits existing local-write automation only when the caller
  already requested auto mode. It cannot override plan mode, a caller's stricter
  ask/denial, never-allow, Studio isolation/role policy or available tool ceilings.
  Shell/Git execution, Office mutations/app launches and registered dangerous/known external mutations still
  require fresh human approval. This is not unrestricted unattended command mode.

Agent file tools cannot edit/delete the policy, including through a symlink or
hardlink alias. They cannot escape the workspace even if a legacy caller requested
allow_outside. Existing private/runtime path restrictions remain, now case-insensitive.
A policy change/revocation while an approval is waiting is rechecked before execution.
Task cancellation after approval refuses file execution as well as process launch.

Policy files must be operator-trusted: do not grant AUTO_ALLOW to policies from
unreviewed third-party repositories. The host user and approved programs can change
files; these checks are not an atomic filesystem/OS isolation boundary.

## Audit and compatibility

Decisions append bounded JSONL under the existing Nexus state home:
`<NEXUS_HOME>/permissions/<workspace-sha256>/audit.jsonl`. Default NEXUS_HOME is the
existing Nexus user configuration directory. Records contain tool/mode/outcome/
reason and a SHA256 argument digest, not argument bodies or file contents. Audit
symlinks/path escapes/oversize/failure block tool execution. Operators must archive
a full audit manually; the agent does not silently delete it. Read-only/plan calls
do not create files in the source project just to audit a decision.

Existing Studio restrictions run first and cannot be expanded by the new policy.
The test repository fixture explicitly grants AUTO_ALLOW for safe local writes;
this is **test-only** consent, not a production default. Tests that execute shell
commands now supply explicit human-approval callbacks. Scripted providers do not
bypass permissions in production code.

## Computer adapters and remaining acceptance

Opt-in Playwright, VS Code and DOCX/XLSX/PPTX adapters, emergency command/Windows
hotkey listener, CLI dry-run and optional scope-locked Docker execution are now
implemented. See [supported computer control and exact limits](computer-control.md).
Native Windows/desktop/Office interoperability and actual Docker acceptance remain
unverified. Host programs are not sandboxed or universally reversible; unsupported
computer actions fail closed or stay manual. These boundaries must not be presented
as unrestricted OS control or all-platform safety certification.

## Typed-file snapshots and `nexus agent undo`

The existing **local** `write_file`, `edit_file` and `delete_file` handlers now persist
an original-byte pre-image **before** mutation when using the real agent permission
gate. GitHub API file tools keep their existing remote commit/revision flow; they do
not create misleading local snapshots. Bare legacy ToolContexts without a permission
consumer retain their old behavior, not an implicit backup guarantee.

```powershell
nexus agent -C "C:\Users\HAMID\Nexus-Agent" snapshots
# Default is preview only; latest means the most recent still-applied action.
nexus agent -C "C:\Users\HAMID\Nexus-Agent" undo
nexus agent -C "C:\Users\HAMID\Nexus-Agent" undo SNAPSHOT_ID --execute
# Enter exactly: UNDO SNAPSHOT_ID relative/path.txt
```

- Every approved typed write/append/edit/delete has a record ID also attached to its
  real FILE_CHANGE event. Other binary, invalid UTF-8, multi-hardlinked, special-permission,
  directories and files over **500 KB** refuse mutation rather than promise a fake
  backup. Appends and CRLF pre-images are byte-preserving; undo restores bytes and
  ordinary POSIX mode bits. ACLs, original timestamps and directory-tree recovery
  are not claimed.
- Original text is scanned with the existing mandatory secret scanner before local
  backup, and planned text before mutation. Known secrets/private paths are refused;
  heuristic detection is not proof that arbitrary proprietary/sensitive text is safe.
  Snapshots are **local plaintext, not encrypted**, at
  `<NEXUS_HOME>/snapshots/<workspace-sha256>/`. POSIX files request 0600 and directory
  0700; host ACL/Windows privacy needs native acceptance. Treat the archive as private.
- The archive is bounded to **1,000 records / 20 MB**, with no silent rotation/deletion.
  Quota or persistence errors stop new mutations. Operators must archive records
  manually; preserving history can deliberately block new work. File locks coordinate
  Nexus processes; metadata uses bounded atomic replace and file fsync.
- Undo is CLI/operator-only, not an extra model-exposed tool that widens role access.
  Preview contains paths, hashes, modes, state and lineage, **not backed-up bodies**.
  `--execute` requires fresh exact interactive confirmation through the existing gate;
  no --yes bypass. READ_ONLY, plan mode and native policy still refuse restoration.
- Preview and execution validate after-image bytes, mode, file identity/modification
  time and parent identity. Later user edits, even same-content file replacement or
  a symlink retarget, block undo. A policy change or user edit during approval also
  blocks it. There is no `git reset`, `git clean`, force overwrite or recursive removal.
- Proven consecutive agent actions on the same file have explicit lineage, allowing
  repeated undo. Intervening user work breaks lineage: undoing a later agent action
  restores that user's work, but cannot then erase it by rebasing an older snapshot.
- Pending, uncertain, restoring, corrupt or already-undone records cannot be replayed.
  A crash/exception after mutation may leave an uncertain outcome and preserved
  pre-image for **manual inspection**, not an automatic claim of successful recovery.
  Approved shell programs are not tracked by these typed snapshots. This is not an
  atomic filesystem sandbox against concurrent hostile host processes.

Workspace file operations also exclude Nexus's trusted runtime home even when that
home is nested inside the chosen workspace. Recursive list/grep filter private and
out-of-workspace resolved children instead of exposing runtime archives through a
broad parent query. These checks do not replace the remaining command/sandbox work.

Acceptance: 33 dedicated offline recovery tests execute real file mutations and CLI
confirmation, byte/mode restoration, sequential lineage, later-edit/symlink/hardlink
refusals, parent removal, corrupt/secret/oversized pre-images, quota, interrupted
mutations, pre-log credential rejection and nested-state filtering. Native Windows/live application acceptance
remains unperformed.

Current recovery regression: **906 Python tests passed**, **29 browser tests passed**
against a fresh current-code server; Ruff, diff checks and wheel build passed. The
33 recovery tests and 25 permission tests are offline/local; GitHub outcomes elsewhere
remain scripted, not live mutation proof. No OS power-loss/host-race atomicity is claimed.

Accepted macro-free DOCX/XLSX/PPTX pre-images now use the same bounded snapshots/undo,
with Office archive/XML validation and literal credential scanning. Other binary
formats remain refused. Module 4 admission started separately without enabling any
unattended tools; see [Jarvis foundation](jarvis.md).
