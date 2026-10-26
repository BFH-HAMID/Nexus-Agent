# Dashboard Workbench

Open `nexus gui`, then **Workbench** (top bar), or **Ctrl+K / Cmd+K** for the
searchable command palette. Features use real observations and explicit user
operations. No provider probes, model inference, GitHub requests, tests, model
pull/delete or pushes happen just because the page was opened. Initial loading
reads local preferences only; opening Workbench also reads the installed Ollama
model list and configured agents/roles.

## The 20 requested updates

| # | Feature | Where / behavior |
|---|---|---|
| 1 | Provider health | Health & metrics → select a configured provider and check it. Includes source, check time and measured probe latency. A model-list endpoint is **not** an inference/quota guarantee. |
| 2 | Per-role model choice | Model & safety policy → pick role and configured agent or installed Ollama model; save mapping. Main composer's explicit local-model selection overrides role mappings for that run. |
| 3 | Local-only | Saved policy enforces private/loopback Ollama inference, router, role workers, learning and no hosted fallback. No selected local model means refusal, not a cloud request. Hosted health probes are blocked too. This is **model routing policy**, not an OS firewall/network sandbox. |
| 4 | Pre-approval diff | File write/edit requests carry unified diffs and proposed before/after data. Append and fuzzy edit semantics are respected. Changed source files invalidate approval. Shell/opaque tools cannot promise a file diff and explicitly say so. Auto-approve intentionally skips manual review. |
| 5 | Durable history | History & plans → search latest 300 runs and inspect actual logs/results. Stored in `$NEXUS_HOME/dashboard.db`, outside the repository; scoped to workspace. Interrupted runs are labelled, not silently resumed. |
| 6 | Plan → review → execute | Submit a read-only planning task, load the completed run, review/edit its plan, type the execution confirmation. Workspace fingerprint must still match. |
| 7 | Changed files | Files & diffs lists workspace Git changes plus untracked files, with content and diff vs HEAD. Run details include that run's recorded changed files. Private/runtime paths are excluded. |
| 8 | Task queue | All server tasks are serialized. Queue tasks while a run is active; inspect, pause/resume, move waiting items up/down, cancel before execution. Pause does not stop an active run. Restarted queues remain in history, not automatically re-executed. |
| 9 | Failed-step retry | Select an error run; confirm side-effect review. Requires a verifiable unchanged Git fingerprint. A pipeline resumes from its recorded failed step with earlier successful summaries as context. No rollback, no guarantee of idempotence. |
| 10 | Folder boundaries | Save allowed relative write folders. Out-of-scope file edits are refused even with auto-approve; shell/git/other writes are refused when they cannot be confined. Normal workspace path guards and dangerous-command restrictions remain. Read access and internal learning memory are not an OS sandbox. |
| 11 | Ollama manager | Installed sizes and loaded models from tags/ps. Explicit pull jobs show Ollama's real stream status/completed/total bytes; typed confirmation for pull/delete. Jobs refresh every 3 seconds while the models pane is open. |
| 12 | Capabilities | Read Ollama capability metadata. Missing metadata is unknown, not invented support. An optional explicitly confirmed small inference test checks JSON formatting and reports its response/pass/fail. One successful formatting test is not proof of general tool reliability. |
| 13 | Machine metrics | Explicit CPU/RAM/process-memory sample via psutil; optional NVIDIA GPU/VRAM via nvidia-smi. Other/unavailable GPU data is null/unavailable, not synthesized. Measures the **server**, not browser PC. |
| 14 | Speed/usage/cost | Recorded streaming first-token timing, actual reported token usage and run duration. Ollama's explicit test uses eval_count/eval_duration for generation tokens/sec when available. Exact-model pricing can be user-sourced in settings; estimates are labelled and missing rates/usage mean unavailable. Not a billing integration. |
| 15 | Error center | Saved provider/tool errors with quota (429), balance (402), missing model/route (404) and reachability hints. Not a threat scanner. |
| 16 | Git status | Local branch/HEAD/dirty files/ahead-behind. Uses local refs, **no hidden fetch**. GitHub account connection remains in the existing connection card. |
| 17 | PR wizard | Explicit test job, clean committed feature branch, base/title/body/diff/remote/test-evidence preview, then typed push/create confirmation. Five-minute one-use preview, stale-worktree/branch/remote guards, no force push/auto merge/auto commit. Commit and branch creation stay in the user's terminal. |
| 18 | Issues/checks/logs | List issues/PRs, read PR checks and failed workflow logs by run number via the existing gh account. Load issue data into the task editor as **untrusted repository content**, never instructions to obey automatically. |
| 19 | Layout/focus | Show/hide, vertically resize, move cards within their own columns and persist layout. Approval/live process cannot be hidden. Focus mode keeps live process/approval. |
| 20 | Language/palette | English/Bengali navigation, headings and action labels; main run/stop/approval labels follow the choice. Technical JSON, provider errors and longer help explanations retain their original English text. Searchable Ctrl+K palette supports both languages. |

## Safety and operational limits

- All workbench APIs use the same server-token authorization as task/approval APIs.
  Use `NEXUS_SERVE_TOKEN` before exposing Nexus beyond your trusted local machine.
- Settings apply to new runs. Turning local-only on is also checked when a queued
  run starts, so an earlier queued cloud run cannot silently ignore that switch.
- No credentials are accepted or stored by Workbench. GitHub uses `gh auth login`.
- Tests execute repository code; the tests button therefore requires confirmation.
  Test evidence includes status/output and workspace fingerprint; stale or failed
  evidence must not be presented as a passing current build. View completed jobs
  under Ollama models → background jobs, then preview the PR again for new evidence.
- GitHub actions require the account's repository permissions and installed `gh`.
  If push succeeds but PR creation fails, the UI reports the failure; review GitHub
  before retrying (it does not claim a rollback).
- There is no automatic durable execution resume. The SQLite history does not
  restore an in-flight provider request, shell process, background download or
  approval after server restart. Review side effects before starting a new run.
- Settings/history are local plaintext application state. Logs can contain file
  contents. Protect `$NEXUS_HOME` and do not expose this server to untrusted users.

## Testing

`python -m pytest -q` covers preference isolation, policy enforcement, diffs,
plan/retry/fingerprint validation, history, queue, HTTP authorization and existing
engine behavior. `tests/browser` runs real headless Chromium with **test-only API
fixtures** for controls, layout, modal keyboard flow and mobile behavior. See its
README for system/browser prerequisites. Live inference, actual model downloads,
GPU hardware and remote GitHub mutations require the user's configured machine;
unit/browser fixtures do not prove those external services work.

## Additional Studio features

The dashboard now also has a **Studio** button for persistent sessions, review
inboxes, canonical outgoing context, checkpoints, verification evidence and data
exports. See [Studio implementation status](studio.md) for the exact implemented,
runtime requirements and security boundaries. The selected additions are mapped individually there, including required Docker/OS-keystore prerequisites and verification limits.
