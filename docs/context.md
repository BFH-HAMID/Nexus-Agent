# Context: explicit opt-in, on demand, never permission

`context.yaml` is **OFF by default**; every signal and screenshots are separately
OFF. Importing Nexus, opening the HUD or starting Jarvis does not start an ambient
watcher, capture pixels, read clipboard or invoke a model. The HUD/TUI reports the
actual policy, **not** a claim that a collector is continuously running.

Enable only the signals you need in the operator-owned workspace policy. The
existing permission and snapshot guards forbid agent edits to this policy.
`ttl_seconds` is 5–120; editor/file/terminal references reuse existing Studio state,
are workspace-bound, expire on lookup and are revoked by policy changes. There is
one bounded record per signal, not a second learning KB. Expired rows are physically
removed on collection; `context clear` purges them immediately. Database backups
are not a secure-erasure mechanism. Clipboard/window contents are not saved by the
collector. Context or model summaries used in a run can appear in that run's normal
local history/learning; opt-in does not promise erasure of an already-used answer.

```powershell
nexus context -C . status
nexus context -C . collect
nexus context -C . signal file '{"path":"src/view.py","line":12}'
nexus context -C . resolve 'fix this file'
nexus context -C . clear
```

## Functional signal consumers

- **Window/application:** real Win32 foreground process/title/window geometry, or
  X11 `xprop` + `xwininfo`. Missing identity/geometry, Wayland-only/headless/macOS
  sessions fail closed for ambient collection; no fake foreground values.
- **Project:** the actual selected workspace name, not a guessed GitHub checkout.
- **Git:** actual bounded tracked porcelain status; no shell, project hooks,
  fsmonitor, text conversion, filters, ambient credentials or project execution.
- **VS Code:** explicit command **Nexus: Share Active Editor Context Reference
  (Opt-in)** in the existing extension. Sends path/line only, no document text,
  selection text or clipboard. The Python consumer rechecks opt-in, workspace,
  secret/private file exclusions, TTL and policy. No cloned repository is assumed.
- **File:** explicit local path/line signal, validated again at resolution; a
  replacement symlink cannot turn an old public reference into `.env`/outside data.
- **Terminal:** actual agent command exit events and bounded failed-tool previews
  automatically feed the signal **only when opted in**. No shell history/keystroke
  capture. External terminals can explicitly publish JSON exit/error metadata via
  `context signal terminal`; this is operator input, not independent test evidence.
- **Clipboard:** opted-in, bounded Unicode text using Win32 clipboard or X11
  `xclip`, only after private-window checks and a stable foreground recheck.

Private password managers, banking/payment/email titles/apps and built-in private
browser markers are blocked. **Known browser apps are conservatively blocked
altogether** because native titles do not reliably identify incognito windows.
Operator blocklist entries extend, never remove, the built-ins. This is an explicit
blocklist, not a universal detector for renamed apps, private notifications or all
possible sensitive content. Unknown credentials cannot be universally identified.
Known credentials, labelled passwords/PINs/OTPs and 13–19 digit card patterns are
masked before signal/summary use. The emergency stop also blocks collection.

## Reference resolution in the existing agent

Opted-in phrases such as “fix this file”, “this error”, “this project”, “this
window”, “this clipboard” (and supported Bengali file/error/project/window phrases)
resolve locally before routing. Missing/expired/conflicting references produce **one
clarification**, not an invented file or execution target. The existing agent gets
bounded JSON as explicitly **untrusted reference metadata**, never instructions or
permission. Normal tool/role/autonomy/approval limits remain in force.

A reference-bearing run installs a per-wire local-agent guard: remote classifiers,
learning calls and hosted fallbacks cannot receive the context silently. Select
loopback HTTP OpenAI-compatible/Ollama agents explicitly. Privacy-policy changes
revoke further context-bearing wires. A later unrelated run restores its previous
guard; no global provider rewrite or new adapter is introduced. The operator's local
server can itself proxy elsewhere; Nexus cannot certify its internals.

## Screenshots and vision are distinct consent boundaries

Install `.[context]` and the native **Tesseract OCR executable** yourself. No implicit
OS package install, privilege elevation or screenshot upload occurs. For local vision,
set `local_vision_agent` to an existing loopback HTTP OpenAI-compatible vision model.

```powershell
nexus context -C . vision
# Separately authorized hosted vision, only when explicitly requested:
nexus context -C . vision --cloud-agent YOUR_EXISTING_VISION_AGENT
```

Each capture uses the existing DANGEROUS ApprovalGate with a fresh exact CLI phrase,
regardless of `--yes`, saved scope or AUTO_ALLOW. Capture is restricted to the
foreground window, ≤16M input pixels, resized to ≤1280×720 and ≤512 KB JPEG. Win32
uses native window capture; X11 captures the window rectangle (visible overlays can
still appear). OCR is required **before** resizing: recognized sensitive lines are
blacked out, and missing/failed OCR refuses the operation. It is best effort, not
a guarantee that all private pixels were detected. Close sensitive overlays first.

Images stay in memory, never a Nexus screenshot/log/history file. A bounded-lifetime
buffer is cleared after use/failure/expiry; no secure RAM/swap erasure guarantee is
made. Tesseract's managed temporary OCR input is cleaned on normal completion/error;
a hard OS crash can leave OS temporary files. Capture and foreground/policy checks
are repeated. `this window` can invoke the same locally configured vision path through
the agent after fresh capture approval; plan/read-only runs do not auto-capture.

Cloud mode requires **another** fresh approval bound to the exact JPEG SHA, size,
agent and endpoint; approving local capture never grants upload permission. Both
local and cloud use the **existing model adapter**, with a short private HTTP client,
no ambient proxies/redirects, and a per-wire guard that refuses retries/fallbacks,
expired images or revoked policy. An in-flight request cannot be unsent. Only
OpenAI-compatible vision endpoints are enabled here (Gemini/OpenRouter/Ollama as
configured); a non-vision model can refuse, never silently switch providers.

## Acceptance and limits

41 dedicated tests cover OFF/no probing, explicit signals, TTL/policy revocation,
private/outside/symlink references, actual Git/process execution, private-app denial,
clipboard/secret/card masking, real pixel redaction/JPEG encoding with scripted
native inputs, separate cloud consent, actual existing adapter HTTP contracts,
no retry/fallback, cleanup, terminal event ingestion and truthful HUD status.
Native Win32/X11/VS Code UI, actual OCR quality and live model/account delivery need
host acceptance. Scripted desktop fixtures are not claims of native Windows success.

## One-time collection in the original GUI

**Control center → Context & privacy** edits this same policy. Collection remains
default OFF. Saving a policy is not a collection request. **Collect local context
once** needs a saved enabled policy, its current revision and fresh exact
`COLLECT LOCAL CONTEXT` consent. It does not capture screenshots or invoke local/
cloud vision. All existing private-window/browser exclusions, native dependency
checks and emergency stop remain in force; the GUI does not weaken them. Project/
Git reference collection can be used without enabling window/clipboard signals.

**Clear cached context references** separately requires `CLEAR LOCAL CONTEXT` and
clears only this workspace's existing reference records. It does not disable the
policy, delete unrelated Studio records or revoke/delete exported screen images.
One-time output is plain text/JSON and clears on pane switch/dialog close; it is
not persisted in browser storage or used as agent instructions.
