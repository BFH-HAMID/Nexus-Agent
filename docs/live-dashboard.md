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
