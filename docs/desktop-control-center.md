# Desktop Control Center

The original `nexus gui` now opens **Control center** by default. This is a
desktop-first workstation, not a separate app/server. **Run & chat**, Operations,
World, Workbench and Studio retain their existing IDs, APIs and approval behavior.
Direct links `#run`, `#operations`, `#world` and `#control` remain available.
No mobile-specific layout or features were added in this update.

## Find and operate controls

- Search eighteen capability cards by feature name, e.g. privacy, models, MCP or
  Telegram. **Ctrl+Shift+P** opens Control center and focuses search; existing
  **Ctrl+K** Workbench behavior is unchanged.
- Cards open the existing native controls. Opening a model/tool/worker card is
  not consent to download, execute, connect or approve anything.
- Setup readiness reads real local policy and credential **presence only**. An
  enabled policy is **not** a claim that a bot/daemon is running or a key is valid.
  Refresh is explicit and local; there are no background inference/endpoint probes.
- The professional desktop finish reduces decorative terminal effects, uses a
  restrained palette and clearer headings, spacing, borders and card hierarchy.
  Existing functionality is not removed to simplify the screen.

## Desktop setup: new native policy controls

Use the relevant Control center card or **Settings → Desktop setup**.

### Context & privacy

Enable/disable context policy, choose individual reference signals, TTL, additional
blocked-app terms, local vision agent and screenshot policy. Default is OFF.
Save requires exact `SAVE CONTEXT POLICY` consent. This does **not** collect a
window/clipboard, start a watcher, capture an image or approve an upload. Existing
private-app exclusions, emergency stop, short-lived references and separate fresh
vision/upload approvals remain enforced. The original task-reference/context
workflow consumes this policy; native desktop availability is still OS-dependent.

### MCP & plugins

Enable/disable discovery, select a confined plugin-manifest directory and edit
reviewable server declarations. An HTTP example is expandable, not auto-enrolled.
The original catalog validates transport, executable/URL restrictions, declared
JSON tool schemas, budgets and environment/keychain references, even when a server
policy is disabled. Saving connects nothing and does not execute manifests/tools.
Actual invocation still requires the original fresh approval boundary. Do not put
credentials in command arguments or descriptions; env/header secret fields accept
references, not inline values. Secret detection elsewhere is heuristic, not a
promise that every possible unknown secret can be identified.

### Telegram policy

Enroll exact numeric private-chat IDs, configure poll/challenge limits and UTC
quiet hours, and explicitly opt into report-text export if required. Bot token
and approval PIN stay in the server environment; GUI shows only their presence,
not validity or values. Save does not launch polling. The existing foreground
Telegram transport is still started/stopped through its CLI process; no fake
running indicator or silent second bot is created. Its transport rechecks current
policy and credential authority before requests.

### OS user service

Preview and download the original generated Windows Task Scheduler XML or Linux
systemd user manifest. Windows requires an explicit local/domain user. Review
content and instructions, then installation requires the **exact filename/digest
confirmation** returned by the preview. The server regenerates that manifest and
uses the existing bounded installer; installation must target the actual OS.
There is no administrator elevation, password collection, shell command input,
execution-policy change, overwrite or automatic rollback.

An accepted registration is **not** a running-daemon confirmation. Partial manager
outcomes are displayed for manual review; do not retry blindly. Uninstall and OS
manager recovery remain OS-managed using the returned instructions. No OS service
was actually installed during automated validation.

## Persistence, privacy and current limits

The authenticated `/api/workbench/desktop` API is protected by the original
Host/Origin/session boundary. Saves use revision checks, exact consent, existing
validators, bounded regular-file/symlink checks and atomic YAML replacement.
Malformed/private policy errors never echo file content. Discovery/readiness
executes no plugin and reads no ambient context. There is no new learning store.

**Not every conceivable operation is now a GUI button.** Foreground Telegram
transport lifecycle, separately reviewed screen/cloud vision, plugin source-file
authoring, OS uninstall/recovery and environment/OS-vault credential revocation
still use their existing tools/workflows. These are explicit limits, not hidden
behind decorative controls. See [control coverage](gui-control-coverage.md).

![Desktop control center](images/gui-control-center.png)

![Native desktop privacy setup, default OFF](images/gui-desktop-setup.png)
