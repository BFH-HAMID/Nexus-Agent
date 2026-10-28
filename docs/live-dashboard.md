# Dashboard hardware and live execution

## Hardware panel

The former **GitHub account** connection instructions and **VS Code sidebar**
installation instructions are removed from the dashboard setup card, including
their buttons and automatic GitHub account probe. This does not remove Git/PR
features or extension support. The local-model picker remains available.

The replacement **SERVER HARDWARE** panel reads the existing authenticated
`GET /api/workbench/metrics` service:

- CPU utilization and logical cores, RAM usage/total and utilization meter.
- Workspace-filesystem disk usage and Nexus process resident memory.
- NVIDIA GPU name/utilization and VRAM usage/total per readable GPU, when
  `nvidia-smi` succeeds. Unsupported GPU fields remain unavailable, not zero.
- Source and measurement timestamp. Data refreshes every five seconds while
  the tab is visible, or on **Refresh**. Requests do not overlap. Failed requests
  clear old readings instead of displaying them as current measurements.

The panel uses theme-matched resource cards, a measured CPU ring, RAM/disk usage
bars, a separate process-memory tile, GPU availability badge, and collapsible
measurement details. Bars/rings transition only to sampled values. Refresh rotation
and the status pulse run only during an actual request; sampled cards briefly
highlight on completion. Reduced-motion preferences disable these animations.
Unavailable samples clear the visual gauges rather than leaving stale usage bars.

These are **server environment** readings, not the browser's hardware. Virtual
machines and containers can report host totals; the panel does not claim these
are container allocation limits. Non-NVIDIA GPU telemetry is not implemented.
No model request or hardware-installation command runs to gather telemetry.

## Live process feed

Every approved tool starts with its real tool name and expandable arguments.
The feed includes model request status, streamed assistant text where supplied,
commands, stdout/stderr, process termination, tool results and applied file edits.
**Thinking** means a model call is in flight; it is not a fabricated thought
transcript or disclosure of hidden model reasoning. Providers without prose
streaming still publish their response when it arrives.

Approved `run_command`, direct `git` and GitHub CLI tools use the same bounded
streaming subprocess runner. A command card shows the actual command, cwd and PID,
separate stdout/stderr as the program writes to its pipes, and the observed exit
code or cancellation/timeout status. Program-level buffering can delay output;
this is not a PTY or an interactive terminal. Other tools publish start/result
activity; blocking HTTP requests and internal compound-helper commands do not
promise byte-by-byte progress.

**Stop command & task** uses the existing cancellation endpoint for that current
task. It does not accept arbitrary PIDs or stop another task. A request is shown
as requested until the actual termination event arrives. The shell process group
is terminated and reaped on POSIX, including descendants holding pipes open;
Windows uses best-effort process-tree cleanup. Model/network requests may still
wait for their configured timeout. This is not an unmanaged background-server
launcher; long-running dev apps retain their existing Studio Runtime controls.

`write_file` (including new files and append), `edit_file` and `delete_file`
publish an **Applied diff** after the operation succeeds. Binary or large deleted
files show metadata rather than loading arbitrary content for a text diff.
Command-produced edits are visible through command results and the existing
changed-files explorer; they are not represented as invented per-file edit events.

## Limits and safety

- All original approval, read-only, path, isolation and role-policy checks remain.
  There is no new shell API or approval bypass.
- Live command output is capped at the tool output limit (at most 64,000
  characters); excess output is drained with an explicit truncation notice.
  UI tool output is bounded, and applied diffs are bounded to 400 lines/24,000
  characters. Retained output can be expanded instead of only the old 1,200-character
  preview. These are bounded views, not a claim of unlimited terminal history.
- Events use real task SSE streams and existing event-ID replay/deduplication.
  Completion and termination events survive task cancellation. The existing
  bounded server history and 400-step browser history still apply.
- Output, command arguments and diffs are rendered as text/escaped markup.
  Program output is not trusted HTML. Protect dashboard access and logs; file
  contents and command output can contain sensitive information.

## Verification

Real disposable subprocess tests verify output **before completion**, separated
stderr, bounded high-volume output, timeouts, cancellation, descendant cleanup,
and cleanup when event delivery fails. An HTTP/SSE test exercises a real command
and cancellation through the actual server endpoints, with only the model
scripted. Browser tests use explicit test-only API/events to check display,
stop failures/retry, HTML escaping, GPU-unavailable/failure states and stale data
clearing. No production demo data or simulated hardware is installed.

## Live run data versus configuration

The configured-agent count is configuration inventory, not a count of online
services. Several agents can share an endpoint. The **LIVE RUN DATA** card only
tracks the selected Nexus task's real events, not activity in an external Arena
chat. Without a selected run it explains how to start/open one. Model turns count
observed model-request events; tool calls count actual tool-call events. Average
tool duration uses the latest three completed-tool duration measurements, and
shows **No results yet** until a timed result arrives. Values update immediately
on events as well as on the normal display timer. Geolocation is not collected;
the old permanently unavailable geolocation placeholder is replaced by tool calls.

## Coding execution versus a text answer

For recognized imperative implementation/edit/command tasks, a prose-only reply
or `finish` alone is not execution evidence. Nexus asks for real workspace tools
at most twice (also bounded by the turn budget), then marks the run **blocked** if
none succeeded. Failed or denied tools do not count. Informational questions and
read-only/plan runs can still finish with text. Intent recognition is a conservative
heuristic, not a semantic guarantee for every language or prompt.

Successful workspace tool activity is displayed as **acceptance unverified**, not
proof that every requirement, test, commit or push succeeded. Inspect the changed
files and actual command/test output. Nonzero command/Git/PR outcomes are tool
errors. The Git preparation helper stops at failed checkout/staging/commit/push;
it never pushes after a failed commit. Partial branch/index changes are not rolled
back automatically. An approval denial stops the run without retrying that denial.
Turn exhaustion and repeated tool errors do not report success.

The composer identifies the actual target: the local server workspace when no
GitHub repository is selected, or the selected GitHub repository in direct API
mode. Direct GitHub tasks create an approved isolated remote branch and expose
only remote file tools; no clone or local checkout/shell is used. Local builds
and tests are unavailable in that mode. Run Nexus with
`serve --workspace <existing-project-directory>` and choose the local-workspace
option if you need local execution instead. The model is instructed to inspect
the target files/manifests before editing, not invent their contents.
These guards improve execution honesty; they cannot make an incapable model
implement a feature or grant credentials/permissions.

Context and completion evidence now use separate record identifiers, preventing
a completed run's evidence from colliding with its stored outbound context.
Evidence storage errors remain visible and do not fabricate success or evidence.
Streaming assistant text is finalized in its existing log entry, not duplicated.
