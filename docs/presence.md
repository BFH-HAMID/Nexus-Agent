# Shared HUD, mobile presence and always-on service (Task 13)

The **Shared HUD** is a page in the original authenticated Nexus dashboard, not a
second agent or permission system. Open `/dashboard#presence` from the sidebar.
It has English/Bangla labels, a voice orb and real local microphone waveform,
bounded live conversation text, Chief mission dependencies/step status and recorded
costs, exact approval cards, capture indicators, briefs and device handoff.

**All new capabilities ship OFF in operator-owned `hud.yaml` and `telegram.yaml`.**
Opening the page never starts a microphone, camera, face model, Telegram poller,
OS service or a task. No installation or real-device delivery is implied by tests.

## 1. Enable only the capabilities you need

Edit `hud.yaml` yourself, preserving any existing top-level legacy HUD settings.
Agents/tools cannot change this policy file. For example, merge these blocks for
text-only paired devices; these are opt-in examples, **not shipped defaults**:

```yaml
presence:
  enabled: true
  devices_enabled: true
  conversation_enabled: true
  handoff_enabled: true
  voice_enabled: false
  push_enabled: false
  world_enabled: false
  device_scopes: [read, chat, approve, handoff]
  default_speaker: none
  max_devices: 12
  device_days: 30
  history_turns: 20
  retention_hours: 24
  max_streams: 16
  push_origins: []
service:
  enabled: false
```

The policy parser rejects unknown keys, string booleans and unbounded limits.
`presence.enabled` is the master switch; each subordinate switch is also required.
`service.enabled` is independent: starting the HTTP service does not enable sharing.

| Scope | Device may do, subject to the corresponding YAML switch |
| --- | --- |
| `read` | Observe the shared conversation, exact pending actions, missions, briefs and device metadata |
| `chat` | Review and submit a new shared turn; this is admission, not tool approval |
| `approve` | Decide existing exact approval requests with **once-only** scope |
| `handoff` | Review a change of active mission/speaking device |
| `voice` | Report input/playback state, explicitly transcribe a draft, claim assigned speech |
| `push` | Review a subscription or a generic push send; unsubscribe itself |

The default scope is only `read`. Enrolled devices capture the configured scope
ceiling. Effective scopes are their enrollment scopes intersected with the current
YAML ceiling: removing a scope takes effect on revalidation; adding one does not
silently expand old tokens. Re-enroll a device when deliberately expanding access.
A `read` device can see potentially sensitive exact action text. Pair trusted
personal devices only; scopes are not per-conversation or per-mission isolation.

### Authentication and pairing

1. Configure a **separate, random GUI access token** in private `NEXUS_SERVE_TOKEN`
   environment/ignored `.env`, or enroll the existing native-vault GUI credential
   with `nexus gui --remember-token`. Never reuse a provider/GitHub key. See
   [GUI authentication](gui.md). Do not put a token in YAML, a URL, a screenshot,
   a committed file or a shell command argument.
2. Start the existing server, for example:
   `nexus serve -C /absolute/workspace --host 127.0.0.1 --port 8787`.
   Shared presence requires authentication **even on loopback**. Non-loopback
   binds without a token are refused. Use TLS for another device (below).
3. Log in as the operator. In Shared HUD, name a device, choose **Review device pairing**,
   inspect the exact review and type its confirmation. Copy the newly returned
   device token privately to that device's login dialog. Dismiss it afterwards.
4. The device opens Shared HUD only. It cannot enroll other devices, edit policy,
   call general administration APIs, or use the original live World API. Its
   `nd1_…` token works in `X-Nexus-Token` or a separate HttpOnly, SameSite=Strict
   cookie, **never as a query parameter**. Trusted-browser persistence is optional.
5. Revoke a device in the HUD; a device may revoke itself, not another device.
   Requests revalidate expiry/scopes/revocation and the realtime stream closes.
   Explicit operator re-login clears the device cookie; an explicit bad device
   credential cannot fall back to an owner cookie. Rotating the serve token and
   restarting the server invalidates old device credentials too.

Only keyed token hashes are stored. Subscriptions, shared text and metadata live
under `${NEXUS_HOME}/presence/<workspace-sha256>/`, not in Git. On POSIX the files
are private mode 0600 in a 0700 directory; symlinks, hard-linked state files and
oversized files are refused. On Windows use a private operator profile and ACLs;
POSIX mode checks are not a Windows ACL sandbox. Keep `NEXUS_HOME` consistent
between your HTTP server, voice process and service.

## 2. Shared state, exact approvals and handoff

HUD/mobile use **one authenticated fetch-SSE stream**,
`GET /api/presence/events`. The existing local voice pipeline publishes/reads the
same locked workspace state. There is no second model executor. Full snapshots
arrive about once a second, with a revision cursor; reconnect is a read and **does
not replay a command**. Slots and state size are bounded. The UI clears private
content on observed revocation/OFF, stops local input/playback on disconnect and
requires new interaction for playback after reconnection.

Shared conversation history is bounded text/status, not tool calls, audio, pixels,
face templates or permission grants. Default retention is 20 turns / 24 hours;
pruning is persisted when the enabled store is accessed. A stable conversation ID
survives empty snapshots and service restarts. A turn idle for 15 minutes is marked
interrupted, not resumed. Streaming text is masked before publication; a trailing
word may be held briefly to avoid exposing a partial credential. Redaction is
best-effort defense, not permission to place secrets in prompts.

- **Review & send** previews the exact text and read-only choice. The review expires
  after 60 seconds and is bound to actor, payload and policy. It is consumed before
  admission. A queued response is not proof of successful execution or testing.
- Existing live/Jarvis approvals use the original Approval Center. Cards show full
  exact tool arguments, not an inferred paraphrase. Oversized, truncated or
  redacted actions are **deny-only** here: use the original local review if needed.
- Approvals cannot transfer session-wide grants. Shell/git/gh and sends, deletes,
  spending and deployment retain their original fresh gates and role ceilings.
  `--yes`, voice text and imported context cannot bypass them.
- Choose an observed active mission and `none`, `local`, or a live voice-scoped
  device as speaker, then review the handoff. The selection and bounded text
  context follow you; an executing job does **not** migrate to the phone.
- The CLI voice reference resolver imports completed shared text and selected
  mission context. It does not import executed-command authority, voice identity,
  policy or approval grants. Revoked/expired context is dropped on refresh.

The cost meter uses Chief's recorded cost estimate/cap, not a provider invoice.
Conversation cost without a verified price is **unknown**, not a fabricated zero.
Camera/screen indicators reuse the existing vision capture status. They start no
capture and never weaken [local-only face protection or Vision hard off](vision.md).

### Microphone and speech

Set `presence.voice_enabled: true` and include `voice` in the device scope ceiling;
re-enroll old read-only devices if appropriate. Install optional local speech
adapters and obtain their model files yourself:

```sh
python -m pip install -e '.[voice,voice-offline]'
```

Set `NEXUS_WHISPER_MODEL` to an existing local faster-whisper directory and
`NEXUS_PIPER_MODEL` to an existing compatible Piper model. No implicit model
download or cloud TTS fallback is used by the shared HUD. Existing CLI voice
capture still needs its own explicit `voice.yaml` enablement and launch. Turning
shared presence OFF does not stop an independently enabled legacy CLI voice
process; stop that process or change its own voice policy separately.

**Hold to talk** records at most 30 seconds in the browser tab. Releasing before a
late permission grant stops the granted tracks. The waveform is measured input,
not simulated animation. Transcription is a separate explicit **TRANSCRIBE AUDIO**
review and produces a draft only: it never approves a tool or submits chat. Edit
and review the draft separately. Browser microphone access requires a secure
context and a user gesture.

For output, choose a paired speaking device and click **Enable / mute this tab** on
that device. Speech claims are assigned, generation-bound, expire after 30 seconds
and are consumed before local synthesis. Revocation, voice OFF or a handoff during
synthesis withholds the response. Local queued playback also checks the selected
speaker before and after synthesis. There is no retry or fallback to another
speaker after uncertain output. Autoplay/OS suspension may prevent playback;
**claimed/submitted does not mean heard**. Already playing audio or already sent
bytes cannot be recalled instantaneously.

### Maps and charts

Enable `presence.world_enabled` as well as the existing `widget.yaml` configuration
if wanted. **Show cached World widget** reparents the existing World widget; it is not a duplicate
chart/map implementation. Its shared-HUD adapter reads bundled geometry and the
existing public cache only. Both cache hits and misses avoid publisher fetches.
Stale/missing data is labelled; an empty cache never invents news or prices.

Refresh/configuration stays on the original operator World surface. Public
news/map text remains untrusted display data, never conversation instructions or
approval authority. World assets/data are intentionally not in the mobile offline
cache. See [World source terms and configuration](world-widget.md).

## 3. Mobile PWA, TLS and generic push

The manifest starts at `/dashboard#presence`, supplies 192/512 PNG install icons
(including a maskable icon), and preserves the existing installed app identity.
Use **Install app** where supported, or your browser's Install/Add to Home Screen
menu. iOS generally needs Add to Home Screen and a user interaction for web push.
Installation eligibility depends on the browser/OS and a valid HTTPS origin.

The service worker caches only an explicit **static app shell**. API/SSE responses,
query-bearing URLs, credentials, audio, transcripts, approvals and World requests
are excluded. Offline loading is not offline authentication or execution. There
is no background-send queue, approval from a notification, or automatic resend.

### TLS: Tailscale (private access preferred)

Keep Nexus on `127.0.0.1:8787`. On the operator's host, after explicitly configuring
Tailscale, its device ACLs and HTTPS certificates, use the installed CLI's Serve
command (check `tailscale serve --help` for your version), for example:

```sh
tailscale serve --bg http://127.0.0.1:8787
```

Use the exact HTTPS hostname it reports. In your private launch environment set
`NEXUS_ALLOWED_HOSTS` to that hostname **without a scheme, port or wildcard**, and
`NEXUS_GUI_HTTPS_PROXY=1`. Restart Nexus after environment changes. This sets Secure
cookies behind the trusted TLS proxy; it does not create TLS by itself. Allow
access only to your intended tailnet users/devices. **Do not enable Funnel or
expose an unauthenticated public bind.** Nexus authentication is still mandatory.

### TLS: Cloudflare Tunnel (optional alternative)

Provision a named tunnel and hostname explicitly in your own Cloudflare account;
point its origin service at `http://127.0.0.1:8787`, and run the already configured
named tunnel with `cloudflared tunnel run <your-tunnel-name>`. Keep tunnel
credentials outside the repository and chat. Restrict the hostname with Cloudflare
Access; keep the Nexus token/device login as an additional, mandatory boundary.
Set the same exact-host allowlist and HTTPS-proxy flag described above.

For either proxy: preserve the external Host and same-origin Origin, disable SSE
buffering/caching, and allow long-lived streamed responses. Do not use wildcard
CORS/hosts or trust arbitrary forwarded headers. Test login, SSE, microphone,
revocation and logout from the actual phone. This repository does not install a
tunnel, open firewall ports, provide certificates or verify remote reachability.

### Optional Web Push

1. Install `python -m pip install -e '.[mobile]'`.
2. Generate your own VAPID pair locally with your chosen Web Push tooling. Supply
   `NEXUS_VAPID_PUBLIC_KEY`, `NEXUS_VAPID_PRIVATE_KEY` and a `mailto:`
   `NEXUS_VAPID_SUBJECT` through the private launch environment/ignored `.env`.
   The public key is the URL-safe base64 uncompressed P-256 point; private key
   material must be in a format supported by pywebpush. Never commit keys or
   subscription endpoints. These push variables are environment-resolved; a
   native-vault deployment must explicitly inject them at launch.
3. Enable `push_enabled`, add `push` to the enrollment ceiling, and explicitly list
   the actual browser provider(s), for example:

   ```yaml
   push_origins:
     - https://fcm.googleapis.com
     - https://updates.push.services.mozilla.com
     - https://web.push.apple.com
   ```

4. On a paired device click **Pair push notifications**, grant the browser permission, and
   confirm the subscription review. To send, select a subscribed device in the
   speaker selector and use **Review push to selected device**. Selection alone sends nothing.

Each send requires a fresh, exact one-use review bound to the subscription digest.
The provider receives only a generic "open Nexus" notification, never task/approval
text, audio, faces or credentials. The worker ignores payload links/actions/text
and opens a fixed same-origin HUD. Redirects, ambient HTTP proxies and unsupported
push origins are refused; request/exception logging masks private endpoint/key
material. No automatic retry occurs on uncertain delivery. Unsubscribe/revoke
removes the server subscription; a revoked device cannot approve via push.
**Provider acceptance is not proof of delivery.** No automatic approval-notification
fanout is enabled by this implementation.

## 4. Always-on, without unattended authority

Set `service.enabled: true` in `hud.yaml` yourself. Review any **existing** workflow
schedules before enabling auto-start: the original server retains their configured
startup behavior and original gates. The new supervisor enables no Telegram
poller, microphone, capture, new schedule or autonomous approval.

The optional service block defaults to loopback port 8787, initial crash backoff
2 seconds, maximum 60 seconds, and 1 MiB lifecycle logs with three backups. The
Python environment referenced in the manifest must already have Nexus installed.
The supervisor loads the workspace's private `.env` without overriding an existing
launch environment. A GUI credential is required; an unavailable locked native
vault is not silently replaced with an unauthenticated server.

### Linux systemd user service

```sh
nexus service -C /absolute/workspace export --platform systemd
nexus service -C /absolute/workspace install --platform systemd
nexus service -C /absolute/workspace status --platform systemd
nexus service -C /absolute/workspace health
nexus doctor -C /absolute/workspace --offline --json
```

Export prints the manifest and instructions only. Install shows the exact manifest
and requires its digest-bound confirmation before writing the unit and calling
`systemctl --user`. It never uses sudo or overwrites an existing unit. User service
startup normally follows login; running before login/after logout may require the
operator to separately configure systemd lingering. No linger policy is changed
here.

### Windows scheduled task

```powershell
nexus service -C C:\absolute\workspace export --platform windows --user 'COMPUTER\Operator'
nexus service -C C:\absolute\workspace install --platform windows --user 'COMPUTER\Operator'
nexus service -C C:\absolute\workspace status --platform windows --user 'COMPUTER\Operator'
```

The reviewed task uses the exact user's logon trigger, InteractiveToken,
LeastPrivilege and duplicate-instance prevention. It stores no password and does
not elevate. Registration is not proof of a running process: use the returned
explicit Run instruction or log on, then inspect `health`. This is a logon task,
**not a pre-login Windows system service**.

For foreground diagnostics use `nexus service -C /absolute/workspace run`.
`remove --platform …` requires a fresh observed-state removal confirmation;
foreign, modified or overridden registrations are refused. Disabling YAML stops
the owned child on the next supervisor check, but does not remove OS registration.
Use the reviewed removal command if you also want to remove auto-start.

### Restart, logging and health semantics

- One supervisor lock per workspace. Exponential child-crash backoff is capped and
  resets after a stable minute. A normal child exit stops rather than restarts.
  The OS manager has a separate bounded supervisor-restart policy.
- A restart restores HTTP availability, not in-flight execution, pending reviews,
  claimed speech or approval grants. Interrupted work requires inspection/fresh
  approval, not automatic replay.
- Private rotating `service.log` files contain lifecycle metadata only; child
  stdout/stderr are deliberately not collected into these logs. Existing Nexus
  audit/history systems retain their original behavior. Partial manager outcomes
  are reported for manual review, without automatic rollback/retry.
- `health` and `doctor --offline` inspect a fresh supervisor heartbeat and owned
  child observation. They do not signal a stored PID or probe a model/network/phone.
  A fresh heartbeat alone is not "healthy" if the child is stopped/backing off.
  These checks do not prove that TLS, a provider or audio output works.
- Stop/cleanup signals only the child process group launched by this supervisor,
  not arbitrary recorded PIDs or unrelated Jarvis/voice processes. Already sent
  external actions are not undone.

The existing `nexus jarvis …` service commands/manifests remain available with
unchanged default targets; the new `nexus service …` target does not replace them.

## 5. Telegram voice, approvals, status and briefs

Telegram is still explicit private-chat long polling with exact numeric user-ID
allowlisting and the existing environment bot token / 8–12 digit approval PIN.
Run `nexus telegram -C /absolute/workspace` separately, or use the existing reviewed
Telegram runtime control. The always-on GUI does not launch it automatically.

New `telegram.yaml` flags are all OFF. When explicitly desired, enable the main bot
and whitelist first, then merge:

```yaml
approval_details: true   # exports exact action arguments to the private Telegram chat
allow_report_text: false # opt in separately if brief/report text may leave the host
voice:
  enabled: true
  send_enabled: false    # enable separately for PIN-reviewed outgoing voice notes
  max_seconds: 30
  max_bytes: 2000000
```

Install `.[telegram-voice,voice-offline]` and configure the local Whisper/Piper model
paths. PyAV decodes bounded OGG input to local PCM and encodes local WAV output to
OGG/Opus; no media shell command or cloud STT/TTS fallback is constructed. Incoming
notes are size/path/duration checked. Their text becomes a **shared HUD draft only**
when shared voice is enabled, otherwise it is discarded. Audio/text saying
`/confirm`, "yes run" or "হ্যাঁ চালাও" is never a command or approval.

- `/voice EXACT TEXT`: with `send_enabled`, shows the exact private recipient/text
  and an expiring one-use PIN challenge. Only `/confirm ID PIN` consumes that intent
  and sends local synthesized audio. Pending text is memory-only, timer-bounded,
  lost on restart and removed before send; uncertain sends are not retried.
- `/approvals`, `/review ID`, `/approve ID`, `/reject ID`: existing durable Jarvis
  action/job controls. Exact detail export is separate opt-in; oversized/redacted
  action text requires local review. Live HTTP-session-only approvals remain in
  the authenticated HUD rather than being recreated in a separate Telegram process.
- `/status`: daemon status plus shared conversation/mission/speaker identifiers.
  `/brief`: existing Inbox briefs; metadata-only unless `allow_report_text` is set.

Telegram is an external service. User-sent voice notes and typed commands already
pass through Telegram; "local STT" does not make Telegram end-to-end private or
recall uploaded bytes. Existing [Telegram boundaries](telegram.md), Task 11 face
privacy and Task 12 exact computer gates remain in force.

## 6. Offline validation and remaining acceptance

From an editable development install:

```sh
python -m pip install -e '.[dev]'
pytest -q
ruff check nexus tests
pytest -q tests/test_presence.py tests/test_presence_service.py tests/test_telegram_voice.py
cd tests/browser
npm ci --no-audit --no-fund
node --test presence.test.mjs environment.test.mjs
```

The browser tests need a local Chromium executable/shared libraries; see
[`tests/browser/README.md`](../tests/browser/README.md). These two suites load real
panel/shell scripts with fake APIs/audio and intercepted module/network fixtures;
**no Nexus server, microphone, credential, model, Telegram account, push provider
or OS-service installation is required**. Python transport tests use loopback
fixtures; supervisor/provider/codec behavior is faked. Build the shipped World
bundle after source changes with `cd frontend/world && npm ci && npm run build`.

Tests cover OFF/default policy, exact reviews, scope/revocation/auth cookies,
SSE/reconnect bounds, masking and retention, shared references and speaker claims,
cache-only World hit/miss behavior, static shell/manifest/icons, local Telegram
voice and PIN replay, supervisor backoff/log ownership/rotation and service export.
Browser checks include responsive English/Bangla, exact approvals, real shell
navigation, split UTF-8 SSE, mic permission races/errors, playback opt-in, OFF and
World adapter handoff. These are not Lighthouse or real-device acceptance results.

**Still require operator testing:** Android/iOS installability and background
suspension, actual local model/language quality, microphones/audio devices, VAPID
provider delivery, Telegram/PyAV/Piper interoperability, TLS tunnel access, and native
Linux/Windows service-manager behavior. No Android wrapper is included. Browser
suspension, revocation propagation and OS audio cancellation are not instantaneous;
secure erasure of RAM/clipboard/backups is not claimed. Disabling sharing prevents
access/publication but does not securely erase previously stored history. Inspect
private state under your own retention/backup policy; do not commit it.
