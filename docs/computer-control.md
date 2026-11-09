# Environment and desktop control

Task 12 extends the existing computer tools, **not** a second privileged agent.
Desktop, file-management, high-level browser, macro, and smart-home controls all
ship **OFF** in operator-owned `permissions.yaml`. Importing a module, opening
Computer Studio, setting an environment flag, receiving an email/webpage/image,
or learning a routine cannot enable them.

## Shared boundaries

- Tools are `desktop_action`, `file_action`, `browser_action`, `workflow_run`, and
  `home_action`. Every invocation uses the existing `ToolRegistry`, `ApprovalGate`,
  permissions, action limits, optional second factor, cancellation and emergency
  stop. These tools are conservatively **DANGEROUS**, including inspections and
  duplicate searches. `READ_ONLY`/plan mode cannot execute them.
- `--yes`, `AUTO_ALLOW`, saved session decisions, recorded approvals, device state,
  and macro membership **never** replace fresh approval. Macro steps are separately
  reviewed even when the underlying tool normally allows automatic reads/writes.
- Preparation binds the exact arguments, current policy and relevant targets
  (window/process identity, file pre-images, macro digest). They are rechecked
  after approval and before effects. Changed state requires a new review.
- Agent role whitelists still apply. Macro replay uses the **same pruned registry**;
  it cannot reconstruct a broader registry or call missing tools.
- Chained audit events record decisions and action start/completion/failure.
  Computer action audit bodies contain metadata/digests, not typed text, screenshot
  pixels or credentials. Exact arguments/results remain visible in the authenticated
  **live** session. Durable Computer Studio run/evidence copies omit payloads,
  output, process arguments and diffs; they keep status, digests and changed-file
  receipts, and cannot replay actions. Prepared tool trace events are metadata-only
  too. Ordinary agent conversation/model transcripts follow their existing memory
  policy; this is not a guarantee that typed prose is erased from an app or every
  transcript. Known credentials in arguments/configuration are refused before review;
  never type secrets into an automation request.
- Web/DOM/OCR/device/file results are untrusted data, never tool instructions,
  authorization or memory/skill promotion authority. No automatic retries after
  uncertain external effects. Inspect the real app/device before trying again.

**This is not an OS sandbox.** App allowlists, origin routing and path guards do
not confine a compromised native app, browser exploit, another process with the
same OS privileges, or an explicitly approved host shell command. App plugins,
macros inside third-party apps, and application-specific effects need operator
review. Native input has a residual focus/geometry race; prefer precise APIs and
isolated accounts/desktops for high-impact work.

## Install only the optional pieces you use

```bash
python -m pip install -e ".[computer]"      # Playwright + existing Office adapters
python -m playwright install chromium       # explicit operator installation
python -m pip install -e ".[desktop]"       # lazy Windows pywinauto dependency
python -m pip install -e ".[context]"       # optional foreground screenshot/OCR
python -m pip install -e ".[smart-home]"    # optional TLS MQTT
```

Linux desktop control needs native **xdotool + wmctrl on X11**. Capture also needs
`xprop`, `xwininfo` and Tesseract. Windows uses pywinauto UI Automation and the
existing foreground capture boundary. There is **no** unsafe ydotool global-input
fallback when window identity cannot be verified. Wayland, macOS native control,
headless displays, elevated/inaccessible windows and unsupported controls fail
closed; they are not advertised as working native backends.

## Desktop: exact applications, not arbitrary launch strings

Manually merge only the scopes you need into `permissions.yaml`. Example for a
Linux host **after verifying the actual executable and WM_CLASS there**:

```yaml
computer:
  desktop:
    enabled: true
    backend: xdotool
    apps:
      editor:
        executable: /usr/bin/gedit
        args: []
        window_class: gedit.Gedit
        actions: [open, close, focus, type, hotkey, click, move, scroll, screenshot]
```

The path above is an example, not an installation/readiness claim. Supply an
existing regular executable, not a shell string or symlink launcher. Window
matching requires its real process executable, process start identity and (on
X11) exact class. Fixed launch arguments cannot be supplied/changed by a tool.
Workspace launch-input files and executables are operator-protected too. On
Windows use `backend: pywinauto`, an actual executable such as a verified classic
Notepad installation, and its actual class (or omit the class and require one
unambiguous window of that executable). UWP/launcher indirection may not match.

```bash
# Default: preview only. Add --execute, then type each freshly displayed RUN phrase.
nexus agent -C . tool desktop_action '{"app":"editor","action":"open"}'
nexus agent -C . tool desktop_action '{"app":"editor","action":"focus"}' --execute
nexus agent -C . tool desktop_action '{"app":"editor","action":"type","text":"Literal text, not a keyboard script"}' --execute
nexus agent -C . tool desktop_action '{"app":"editor","action":"hotkey","keys":["ctrl","s"]}' --execute
nexus agent -C . tool desktop_action '{"app":"editor","action":"click","x":120,"y":80}' --execute
nexus agent -C . tool desktop_action '{"app":"editor","action":"close"}' --execute
```

Coordinates are **window-relative**, bounded by the reviewed window. Input requires
that app to be **already foreground**; focusing is a separate approved action.
Multiple windows require an explicit decimal `window` handle. Hotkeys use a bounded
list of key names, not an interpreter string. Windows text escapes pywinauto's
keyboard mini-language. Exact accessible **button** labels are available on Windows
with `{"action":"click","app":"editor","label":"Save"}`; unsupported/ambiguous
labels are refused rather than converted into guessed coordinates. Close requests
the app/window to close normally, never a blanket process kill.

### Screenshots stay local and preserve Task 11 privacy

`desktop_action` with `action: screenshot` requires all of:

1. Desktop opt-in and that app's `screenshot` allowlist entry.
2. `context.yaml` `enabled: true` and `screenshots: true`.
3. Verified foreground identity, existing context/vision private-window denylists,
   available OCR redaction, and an unlatched vision/emergency hard-off.
4. A fresh desktop action review **and** the existing context-capture consent.

It calls `nexus.context.vision.capture`, not a raw full-screen screenshot API.
Bounded, OCR-redacted JPEGs are saved mode 0600 below private
`.nexus/computer/captures/`; at most 20 artifacts are admitted, with no automatic
purge. The tool returns a local path/receipt, **not image bytes for a model**. No
cloud/VLM/face upload is added. Open/remove those files deliberately on the local
host; the dashboard run result reports the path rather than embedding pixels.
`nexus vision -C . off` prevents subsequent capture. OCR is best effort, local
captures are plaintext, and hard-off cannot erase already saved/sent data.
See [vision privacy and hard-off limits](vision.md).

## Browser: scriptless, ephemeral, exact origins

The existing `computer.browser` boolean and canonical `browser_origins` remain.
Use exact `scheme://host:port`, including default ports; no credentials, paths,
wildcards or prefix matching. IPv6 origins use brackets. New high-level actions
also need `browser_controls.enabled`:

```yaml
computer:
  browser: true
  browser_origins: ["https://intranet.example:443"]
  browser_controls:
    enabled: true
    forms: false
    downloads: false
    download_dir: browser-downloads
    max_download_bytes: 2000000
    search_url: "https://intranet.example:443/search?q={query}"
```

```bash
nexus agent -C . tool browser_action '{"action":"open","url":"https://intranet.example/"}' --execute
nexus agent -C . tool browser_action '{"action":"search","query":"release notes"}' --execute
nexus agent -C . tool browser_action '{"action":"extract_data","url":"https://intranet.example/","selectors":{"headings":"h2","links":"a"}}' --execute
nexus agent -C . tool browser_action '{"action":"draft_form","url":"https://intranet.example/contact","form":"form#contact","fields":{"name":"Operator","note":"A reviewed draft"}}' --execute
```

Each call creates an isolated, headless Chromium context with **JavaScript disabled**,
no persistent user profile/passwords/autofill, permission grants, file uploads,
service workers, sockets, popup interaction or browser-managed downloads. All
requests stay inside the origin allowlist; ordinary requests are GET/HEAD only.
Navigation itself may have effects on a badly designed server, so even inspection
requires review. Only use trusted origins; this is not an OS network firewall.

Legacy `inspect` and `fill` remain. A raw `click` now only follows an anchor whose
resolved `target` URL exactly matches the reviewed URL. It cannot click a form
button (including a button with HTML's implicit submit type). Scriptless pages
prevent `oninput`/`onload` handlers from submitting forms while drafting.

### Separate approval for every form submission

A `draft_form` result supplies `draft.target`, `draft.method`, the proposed named
fields and `form_sha256`. Drafting submits **nothing**, and does not save a grant.
Only after operator-enabling `browser_controls.forms` can a separate request run:

```json
{
  "action": "submit_form",
  "url": "https://intranet.example/contact",
  "form": "form#contact",
  "fields": {"name": "Operator", "note": "A reviewed draft"},
  "target": "https://intranet.example/submit",
  "method": "POST",
  "form_sha256": "USE_THE_EXACT_64_HEX_DIGEST_FROM_THE_REVIEWED_DRAFT"
}
```

This request needs fresh approval of the full target/method/fields. The worker
reloads the page, reconstructs the form and rejects changed digests/targets/methods.
Only the exact one-use GET/POST serialization is admitted; POST redirects/retries
cannot retransmit it. There is no persistent authenticated browser session.

Supported forms are deliberately simple, URL-encoded, with unique visible/enabled
plain text/email/search/tel/URL/number inputs or textareas and every field explicitly
supplied. Password/token/payment controls, hidden fields (including CSRF tokens),
file inputs, selects, overrides, external associated controls and complex JS forms
require manual interaction. No credential autofill or payment automation is added.
Extraction is bounded plain text, never caller-supplied JavaScript.

### Sandboxed download storage

After separately enabling `browser_controls.downloads`, `action: download` accepts
one exact allowlisted URL. A fresh unauthenticated HTTP stream (no browser cookies,
ambient proxies, redirects or compressed response decoding) enforces byte/time
budgets. It writes a new random `.download` file, mode 0600, only inside the
workspace-relative `download_dir`. Server filenames cannot choose paths. At most
20 files are admitted; quota exhaustion refuses rather than auto-deletes. Nothing
is opened, executed or extracted. `browser-downloads/` is ignored by Git; add an
ignore rule yourself if configuring another directory. Location confinement does
not make downloaded data safe.

## Files: narrow roots, no clobber, recoverable deletion

```yaml
computer:
  files:
    enabled: true
    roots: [work, backups]
    max_files: 200
    max_bytes: 20000000
```

Source roots must contain the actual data you intend to manage. All paths stay
inside both the workspace and these roots; `--allow-outside` cannot widen them.
Private/runtime/VCS paths, symlinks/junctions, hardlinked files, Windows device
names/alternate data streams, policy files, operator macro/plugin directories and
trusted app artifacts are refused.

```bash
nexus agent -C . tool file_action '{"action":"rename","path":"work/old.txt","destination":"work/new.txt"}' --execute
nexus agent -C . tool file_action '{"action":"organize","moves":[{"source":"work/new.txt","destination":"work/notes/new.txt"}]}' --execute
nexus agent -C . tool file_action '{"action":"duplicates","path":"work"}' --execute
nexus agent -C . tool file_action '{"action":"backup","path":"work","destination":"backups/work.zip"}' --execute
nexus agent -C . tool file_action '{"action":"restore_backup","path":"backups/work.zip","destination":"work/recovered"}' --execute
nexus agent -C . tool file_action '{"action":"delete","path":"work/unused.txt"}' --execute
nexus agent -C . tool file_action '{"action":"restore","id":"t-RETURNED_ID"}' --execute
```

- Organize is an explicit bounded move list, not a heuristic that silently chooses
  files to delete. Rename/moves never overwrite. Cross-filesystem moves fail closed.
- Duplicate detection groups content hashes and deletes **zero** files.
- Backups are bounded ZIPs; restores accept regular safe members and new paths only,
  reject traversal/symlinks/duplicate members/encryption/oversized expansion, and
  never overwrite a non-empty destination or set executable permissions.
- Delete is **DANGEROUS** and moves one regular file to private workspace trash with
  a hash-bound receipt. Restore revalidates bytes, current roots and destination
  absence. There is no permanent-delete tool, automatic trash purge, or directory
  deletion. Trash holds at most 100 receipts; operator cleanup is explicit.
- Known secret text is refused and private names are excluded from traversal.
  UTF-8 secret scanning has an additional 2 MB per-file ceiling. Opaque binary
  content is not OCR/decrypted/guaranteed secret-free. Backups/trash are local
  **plaintext**, not encrypted secure storage. Keep backups out of Git;
  `**/nexus-backups/` is ignored for the compatibility backup API.
- Multi-file operations are not transactions. Each pre-image is checked; revocation,
  cancellation or a later failure stops further steps but does not undo earlier
  effects. Review the action receipts/changed files before retrying.

Existing `write_file`, `edit_file`, `delete_file`, Office snapshots and
`nexus agent undo` keep their existing recovery semantics. The file manager's
bounded binary trash is separate; it does not broaden snapshot support.

## Record and replay operator-reviewed macros

```yaml
computer:
  workflows:
    enabled: true
    voice: false
    directory: computer-macros
    approved: {}
    allowed_tools: [file_action, desktop_action]
    max_steps: 30
```

```bash
nexus agent -C . workflow start organize
# Execute explicitly reviewed normal tools. Denied/failed calls are NOT recorded.
nexus agent -C . tool file_action '{"action":"rename","path":"work/a.txt","destination":"work/b.txt"}' --execute
nexus agent -C . workflow show
nexus agent -C . workflow stop
```

This records **semantic successful tool calls**, not OS mouse events, raw keystrokes,
password capture or background hooks. Typed bodies/form values become required
parameters instead of recorded literal text. Stop writes an **unreviewed draft**
under `.nexus/computer/drafts/`; it is not executable and does not grant anything.

The operator must inspect/edit the draft, manually copy it to a protected named
YAML file (for example `computer-macros/organize.yaml`), and pin its actual SHA-256
in `permissions.yaml` `workflows.approved.organize`. Agent file tools cannot
perform that promotion or change a pinned macro. Both default and configured macro
directories are protected even when disabled; hardlink aliases are not a workaround.

Example operator-reviewed file (choose appropriate real paths before pinning):

```yaml
schema: 1
parameters:
  destination: {type: string, max_length: 200}
steps:
  - tool: file_action
    args:
      action: rename
      path: work/a.txt
      destination: "${destination}"
```

```bash
# Compute the digest locally, inspect the file, then manually update operator YAML.
sha256sum computer-macros/organize.yaml
nexus agent -C . workflow run organize --parameters '{"destination":"work/reviewed/a.txt"}'
nexus agent -C . workflow run organize --parameters '{"destination":"work/reviewed/a.txt"}' --execute
```

Parameters replace **whole JSON values**, never object keys, tool names or embedded
shell strings. Types are bounded string/integer/boolean; unknown/missing values,
YAML aliases/cycles and recursive macros are refused. Shell/Git/gh macro steps, if
explicitly listed by the operator, must use fixed reviewed arguments with **no
parameter substitution** and still receive their own normal fresh approvals.
Use the existing Docker backend for untrusted commands; a macro does not sandbox
host commands or make deployment reversible.

Replay checks the pin and policy before every step, uses the same role registry,
reviews each step separately, and stops at denial/failure/cancellation. It does not
reuse a recorded grant, replay a persisted external result as approval, roll back
external effects, or silently resume after a crash.

## Smart home and authorized phones

Home Assistant uses its documented **REST** API. MQTT uses TLS, exact topics,
operator-fixed payloads, no retained messages and no automatic reconnect/retry.
Neither adapter discovers devices or exposes an arbitrary API-path/body tool.

```yaml
computer:
  smart_home:
    enabled: true
    voice: false
    home_assistant:
      enabled: true
      url: "https://home.example:443"
      devices:
        desk: {entity: light.desk, services: [turn_on, turn_off]}
        front: {entity: lock.front, services: [lock, unlock]}
        garage: {entity: cover.garage, services: [open_cover, close_cover]}
        alarm: {entity: alarm_control_panel.house, services: [alarm_arm_away, alarm_disarm]}
        phone: {entity: notify.mobile_app_owner, services: [notify]}
    mqtt:
      enabled: false
      host: "broker.example"
      port: 8883
      devices:
        desk: {topic: "house/desk/set", kind: light, commands: {"off": "OFF", "on": "ON"}}
    voice_commands:
      lights off: {adapter: home_assistant, device: desk, action: turn_off}
```

Use environment/optional `.env` or the existing native keyring accounts for
`HOME_ASSISTANT_TOKEN` and `MQTT_PASSWORD`; set `MQTT_USERNAME` in the environment.
Never put credentials into YAML, arguments, logs or Git. Use minimum-privilege
user-authorized API credentials. Home Assistant requires HTTPS, or exact loopback
HTTP such as `http://127.0.0.1:8123`; no arbitrary insecure LAN URL or redirect.

```bash
nexus agent -C . tool home_action '{"adapter":"home_assistant","device":"desk","action":"state"}' --execute
nexus agent -C . tool home_action '{"adapter":"home_assistant","device":"desk","action":"turn_off"}' --execute
nexus agent -C . tool home_action '{"adapter":"home_assistant","device":"front","action":"unlock"}' --execute
nexus agent -C . tool home_action '{"adapter":"home_assistant","device":"phone","action":"notify","message":"Your reviewed job finished."}' --execute
nexus agent -C . tool home_action '{"adapter":"mqtt","device":"desk","action":"off"}' --execute
```

All calls are conservatively DANGEROUS. **Locks, alarms and garage doors always
require confirmation**, with configured second-factor checks still enforced.
There is no YAML setting to downgrade them. Code-required alarm services that need
secret PIN fields are unsupported rather than accepting a PIN in a tool payload.
HA state reads address one allowed entity at a time; they do not fetch all household
states then filter. Arbitrary attributes are withheld and returned state is untrusted.
An HA success response or MQTT QoS 1 acknowledgement is **not proof of physical
state**. QoS 1 may deliver duplicates; use appropriate device-side idempotency and
inspect uncertain outcomes manually.

Phone support is ordinary notification delivery through a specifically configured
Home Assistant companion notification service, plus the existing opt-in,
authenticated [Telegram interface](telegram.md). No ADB, invasive hooks, screen
scraping, account takeover, companion `command_*`, broadcast intents, or arbitrary
remote phone control is added. Notification delivery still needs fresh send approval.

### Literal voice shortcuts

Enable `voice.yaml` normally, then separately enable `workflows.voice` and/or
`smart_home.voice` in operator YAML. **“run my deploy routine”** selects only the
pinned macro `deploy`; **“lights off”** selects only the exact configured alias above.
Voice replay currently supplies no macro parameters; use a reviewed parameter-free
routine or the CLI for explicit parameters. Existing enrolled-speaker checks,
spoken approvals, READ_ONLY and second factor are preserved. The literal dispatcher
never interprets webpage/OCR/device output as a voice command. Normal chat outside
these exact aliases still has the usual agent approval/role boundaries.

## Computer Studio and compatibility

Launch the existing server with a configured private `NEXUS_SERVE_TOKEN` and use
`/dashboard#computer`. All `/api/computer/*` reads and writes require authentication
and the existing host/same-origin checks. Opening the page starts no devices.

- The new Environment actions form queues a typed computer action in the existing
  **Run queue and Approval Center**, not through an LLM. Each macro step and capture
  request appears as a separate live approval. Expand **Exact arguments** to inspect
  the complete bounded request; the center cannot grant a computer request whose
  arguments exceed its display budget. Stale/missing approval IDs are refused.
  Panic/cancel stops further effects; no automatic recovery/replay is attempted.
- File and browser page settings only **narrow** YAML roots/origins. The file page
  now also needs `computer.files.enabled` and allowed roots; saving a page setting
  cannot enable it. Per-tool READ_ONLY, second factor, the emergency latch and
  operator-file protection apply to legacy page operations as well.
- Legacy synchronous write/trash/restore/undo/browser endpoints first need
  `POST /api/computer/action/review` with `operation` and the exact `payload`. The
  returned random confirmation is one use, 60 seconds, and bound to the client,
  target/pre-image, policy and page scope. Send its `id` as `approval_id` and typed
  confirmation as `approval` to the original endpoint. The UI does this sequence.
  Static `WRITE …`, `TRASH …`, or `RESTORE …` phrases alone no longer authorize it.
- `DesktopController`, `WorkflowRecorder`, `BackupManager`, `HomeAssistant`,
  `MqttHome`, and existing `nexus assistant` command names remain. They delegate to
  the same YAML/registry boundary. `NEXUS_DESKTOP_ENABLED` cannot enable desktop
  control. Legacy MQTT “prefix” arguments now only accept exact configured topics.
  Assistant dashboard automation queues the same live approval sessions. A returned
  task/backup ID is an admission receipt, **not** proof the backup or restore ran.
- Old unreviewed recordings, old backup archives without current receipts, and
  legacy trash layouts are **not automatically trusted/imported**. Inspect and
  recover them manually; an old file/confirmation cannot become execution authority.

## Existing Office, editor, Docker and emergency controls

The independent OFF-by-default `computer.office` and `computer.editor` opt-ins,
`office_file`, `vscode_open`, and their public CLI names remain. Office supports
bounded macro-free DOCX/PPTX text and literal XLSX cells; it rejects formulas,
external relationships/embedded executables, oversized archives and unsafe paths.
Its existing snapshots cover approved text/OOXML writes, not arbitrary app effects.
The VS Code bridge opens one existing file in a fresh isolated profile with
extensions disabled; it does not grant arbitrary desktop-app access.

`computer.shell_backend: docker` still requires an already installed digest-pinned
image, no pull or host fallback, a bounded credential-scanned read-only source copy,
no network/capabilities/daemon socket, non-root execution and resource limits.
`host` remains explicitly approved **unconfined** execution, not universally undoable.

```bash
nexus agent kill
nexus agent control-status
nexus agent resume-tools
nexus agent hotkey
```

The emergency latch blocks new calls and the bounded worker runner cancels owned
process groups. The Windows optional hotkey is Ctrl+Alt+Pause, not keylogging.
Resume only resets the tool latch; it does not restart apps, replay macros, resume
vision hard-off or undo a physical action. A native GUI process already launched or
an API request already on the wire cannot be recalled reliably.

## Tests and acceptance limits

```bash
python -m pytest -q
python -m ruff check nexus tests
# Focused, offline environment-control coverage:
python -m pytest -q tests/test_environment_*.py tests/test_computer.py tests/test_computerstudio.py tests/test_snapshots.py
```

Optional panel-only DOM regressions, without a running server or credentials:

```bash
cd tests/browser
npm ci
node --test environment.test.mjs
```

The tests use fake X11/Windows/browser/HA/MQTT transports, real temporary-file
operations, fake voice/speaker decisions, and loopback authenticated HTTP where
needed. They require no internet, GPU, microphone, API keys, device accounts or
installed optional desktop/broker/browser runtimes. The existing opt-in live
Chromium acceptance test skips when Playwright/Chromium is absent.

Native OS app interoperability, desktop focus races, live browser sites, HA/MQTT
physical state, companion/Telegram delivery and end-to-end live dashboard interaction
still need operator acceptance on the actual hardware/accounts. Fixture-based panel
DOM tests do not exercise a live desktop or authenticated browser worker. Tests do not certify hardware,
provide a universal rollback, or claim unrestricted computer autonomy.
