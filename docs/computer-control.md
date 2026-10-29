# Module 3 — permissioned computer-control implementation

Existing adapters, roles, learning and Studio remain intact. New computer tools are
registered in the **existing tool registry** only when operator-enabled; existing
role whitelists/Studio ceilings still apply. No second privileged executor or KB.

The supported core is implemented and offline/integration-tested. Native Windows
VS Code/hotkey/Office interoperability and an actual Docker daemon are **not yet
live-certified here**. Bounded adapters are not unrestricted computer autonomy.

## Enable deliberately

```powershell
python -m pip install -e ".[computer]"
python -m playwright install chromium
```

Review workspace `permissions.yaml` as the operator (agent tools cannot edit it):

```yaml
schema: 1
default: ASK_EVERY_TIME
computer:
  office: true
  editor: true
  browser: true
  browser_origins: ["http://localhost:3000"]
  shell_backend: host
  image: ""
```

Defaults leave all three new adapters OFF. An explicit role whitelist must also
include the new tool name; enabling an adapter does not silently widen that role.
Policy is reloaded, so disabling an adapter prevents later calls through a previously
built registry. Tool dry-run does not run the adapter or command:

```powershell
nexus agent -C . tool office_file '{"path":"notes.docx","action":"write","text":"Project notes"}'
nexus agent -C . tool office_file '{"path":"notes.docx","action":"write","text":"Project notes"}' --execute
nexus agent -C . tool office_file '{"path":"budget.xlsx","action":"set","cell":"B2","value":120}' --execute
nexus agent -C . tool office_file '{"path":"review.pptx","action":"write","text":"Review findings"}' --execute
nexus agent -C . tool browser_action '{"url":"http://localhost:3000","action":"inspect"}' --execute
nexus agent -C . tool vscode_open '{"path":"README.md","line":1}' --execute
```

Mutations/launches need the exact freshly displayed RUN confirmation, even under
AUTO_ALLOW. READ_ONLY/plan mode cannot launch apps or mutate files. Office reads
are real read-only actions; their outputs are data, never permission instructions.

## Implemented bounded surfaces

| Surface | Actual behavior | Deliberate safety limits |
|---|---|---|
| Terminal/files | Existing terminal/list/read/grep/write/edit/delete reused; CLI dry-run | Fresh command approval; file/private/runtime boundaries; host programs are not an OS sandbox |
| Playwright | Real isolated-interpreter ephemeral Chromium inspect/click/fill | Exact operator origins; no non-GET/HEAD, WebSockets, service workers, persisted profile, downloads, file upload, JS eval or screenshot capture; sensitive controls refused/manual |
| VS Code | One existing file, line navigation, fresh isolated profile, extensions disabled, CLI --wait | No folder/workspace-file opening or extension/task installation; VS Code must exist; timeout/cancel via runner; native detached-GUI lifetime requires platform acceptance |
| Word | Real macro-free DOCX read/write/append paragraphs | Bounded plain paragraphs, not full layout/table/track-change automation |
| Excel | Real XLSX read/set existing-sheet literal cells | No formulas, macros, external links, unbounded grids or financial execution |
| PowerPoint | Real PPTX read/write/append text slides | No macros/external/embedded objects; generated template printer settings removed; not full design fidelity |
| Recovery | Pre-image snapshots and byte/mode exact undo for typed text + accepted OOXML files | Later edits/identity changes block; only proven agent lineage rebased; no irreversible external-action rollback |

Office uses python-docx/openpyxl/python-pptx and validates archive members, expanded
size, XML entities, formulas, macros/embedded/external relationships before processing.
Files stay within workspace and 500 KB compressed / 5 MB expanded / 200 members.
Unsupported files fail closed rather than being silently converted or overwritten.
Local OOXML snapshots are private **plaintext backups**, not encrypted archives;
embedded image text is not OCR-scanned. See [snapshot privacy and quotas](permissions.md).

All browser actions are fresh-approved, including inspection. Password/payment,
submit/delete/transfer and similar recognized sensitive controls require manual
interaction; there is no hidden checkout, password upload or payment automation.
DOM text is explicitly tagged UNTRUSTED_BROWSER_DATA, not instruction authority.
Origin routing is application policy, not a browser-exploit/OS network sandbox. Use
only trusted allowed origins. No passive context capture or screenshots were enabled.

## Emergency stop and hotkey

```powershell
nexus agent kill
nexus agent control-status
nexus agent resume-tools
nexus agent hotkey
```

Kill latches a user-scoped runtime flag. New registry/approval operations refuse it;
running bounded command/worker process groups poll it and terminate. Resume is an
explicit operator action, not a model-exposed tool. The Windows listener registers
**Ctrl+Alt+Pause** with RegisterHotKey, does not record keys, and must be kept running.
It polls without a busy loop and unregisters on exit. On non-Windows use CLI kill
or terminal Ctrl+C; there is no pretend global hotkey implementation for every OS.
Existing server per-task cancel/approval endpoints remain in place. This latch is
not represented as killing arbitrary OS apps, the server, future Jarvis daemons,
all detached GUI processes or every legacy Studio execution path.

## Optional actual Docker confinement

Set `shell_backend: docker` and an explicitly installed trusted
`image: "REPOSITORY@sha256:64_LOWERCASE_HEX_DIGITS"`. There is no pull or host fallback.
The image must already exist on the **local** Docker engine. The tool makes a bounded
credential-scanned text copy (200 files / 2 MB), excluding private/runtime paths, and
mounts that copy **read-only**, never the real source or daemon socket. It uses a
non-root UID, no network, no capabilities/new privileges, read-only container root,
PID/memory/CPU limits, a bounded tmpfs and container cleanup attempted in finally on cancellation; daemon failure requires
manual inspection.
The command may build in /tmp; source changes are disposable, never applied behind
the user's back. Apply desired outputs via reviewed typed file tools and snapshots.
This is not a VM and not a guarantee against kernel/container-runtime vulnerabilities.

`host` is retained for explicitly approved legacy commands and is clearly **not**
confined, universally undoable or guaranteed to use guarded Git APIs. Arbitrary host
programs can read/write beyond cwd and encode data; neither redaction nor a path
guard proves otherwise. Choose Docker for untrusted code. Literal configured
credentials are now masked across output chunk boundaries; credential-bearing
write/Office/browser/editor/shell arguments are refused before approval/logging.
Heuristic redaction is not a general exfiltration firewall.

## Acceptance evidence

26 computer tests and 6 stream-redaction tests execute real local headless Chromium and genuine Office ZIP
read/write/undo; test policy denial, scoped command construction, no host fallback,
CLI dry-run/exact consent, hotkey message handling, emergency termination and secret
redaction. VS Code/Docker transport and Win32 messages are scripted, not claimed as
live desktop/container acceptance. Source tests are under `tests/test_computer.py`
and `tests/test_stream_redaction.py`; permission/recovery suites remain in place.

Operational gates before broader unattended use: native Windows installation/ACLs,
GUI process lifecycle, Office interoperability, real Docker cancellation and host
runtime review. External/app-specific effects that cannot be reversed remain manual
or require fresh approval; no claim of complete arbitrary-computer undo is made.

Latest combined regression with the Module 4 admission foundation: **952 Python
tests passed, 29 browser dashboard tests passed**; Ruff, diff checks and wheel build
passed. Real local Chromium/Office tests ran here; no native desktop/Docker test
result was fabricated. The transient regression server was stopped after testing.
