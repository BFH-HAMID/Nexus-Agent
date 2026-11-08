# Personal assistant: setup, commands and trust boundaries

The assistant is **opt-in**. Importing `nexus.assistant` does not open a microphone or
camera, contact an account, start a scheduler, install a service, or enable browser
automation. Adapter tests are not live hardware, biometric, OAuth, MQTT or OS acceptance.

## Install only what you use

```bash
python -m pip install -e ".[assistant]"       # memory, FAISS, documents, keyring
python -m pip install -e ".[voice]"          # faster-whisper, Edge TTS, microphone I/O
python -m pip install -e ".[voice-wake]"     # openWakeWord / Porcupine
python -m pip install -e ".[voice-offline]"  # Piper / Kokoro Python packages
python -m pip install -e ".[voice-speaker]"  # Resemblyzer speaker templates
python -m pip install -e ".[vision]"         # camera, YOLO and face adapters
python -m pip install -e ".[smart-home]"     # TLS MQTT
```

Use an existing local faster-whisper model directory and local YOLO/Piper/wake-word
models. Nexus does not silently download model weights. `xdotool`/`ydotool` are OS
packages; Windows uses `pywinauto`. Playwright remains in the separate `computer`
extra and is disabled by default.

## Voice

```bash
# Bounded prerecorded STT; language=None enables Bangla/English auto-detection.
nexus assistant voice stt sample.wav --model /models/faster-whisper-small

# Enrol/revoke the workspace-local speaker template (explicit biometric operation).
nexus assistant voice enroll owner-1.wav owner-2.wav --confirm 'ENROLL MY VOICE'
nexus assistant voice verify sample.wav --model /models/faster-whisper-small
nexus assistant voice revoke --confirm 'REVOKE MY VOICE'

# Offline synthesis; set NEXUS_PIPER_MODEL to an existing .onnx voice.
nexus assistant voice speak 'শুভ সকাল' --engine piper --output greeting.wav

# Online synthesis uploads the entered text only after this explicit confirmation.
nexus assistant voice speak 'শুভ সকাল' --engine edge --confirm 'UPLOAD TEXT TO TTS'

# Read-only voice task from a verified prerecorded sample.
nexus assistant voice ask question.wav --model /models/faster-whisper-small

# Live wake-word task and first-sentence streaming speech.
nexus assistant voice ask --wake-model hey-nexus.onnx --wake-engine openwakeword \
  --model /models/faster-whisper-small --speak --engine piper
```

`voice ask` always verifies the enrolled speaker. It is read-only unless `--write`
is present. Every write/danger tool approval then uses a fresh random challenge;
speak **“হ্যাঁ চালাও CODE”** or **“বাতিল CODE”** (English approve/cancel is also
accepted). The same captured utterance is transcribed and speaker-verified. A
browser-supplied `matched` flag is never authority. Press Enter during CLI speech
for barge-in; the text task continues.

The dashboard records a real local waveform and packages WAV in the tab. Audio is
not sent until **Transcribe**, **Verify speaker**, or **Voice approve** is pressed.
Local Piper/Kokoro can speak the first generated sentence from dashboard text
deltas. Online Edge/ElevenLabs requires manual per-request consent rather than
silent streaming upload. Stop/barge-in clears playback and the speech queue.

## Personal memory and current work

```bash
nexus assistant memory enable --confirm 'SAVE PERSONAL MEMORY'
nexus assistant memory add 'I prefer Bangla responses' --kind preference \
  --confirm 'ADD PERSONAL MEMORY'
nexus assistant memory status --query 'language preference'
nexus assistant memory profile --key language --value bn-BD --confirm 'SAVE PERSONAL PROFILE'
nexus assistant memory context 'Implementing the assistant dashboard' \
  --confirm 'SAVE ACTIVE CONTEXT'
nexus assistant memory edit 'Use English for code and Bangla for explanations' \
  --id ENTRY_ID --kind preference --confirm 'EDIT PERSONAL MEMORY ENTRY_ID'
nexus assistant memory forget --id ENTRY_ID --confirm 'FORGET PERSONAL MEMORY ENTRY_ID'
nexus assistant memory forget --all --confirm 'FORGET ALL PERSONAL MEMORY'
```

This separate SQLite store is not enabled by existing task/code learning. Profile,
routines, preferences and active context survive sessions. Enabled, relevant
memories are injected as **data, never commands**. The dashboard provides search,
edit and delete controls. Phone/laptop access uses authenticated revision polling
against the same workspace server, not a third-party cloud sync account.

## Personal data and reviewed email

Set OAuth/account secrets in the launch environment or an unlocked native OS
keyring; no plaintext fallback is used. Supported environment names are
`GOOGLE_OAUTH_ACCESS_TOKEN`, `NOTION_TOKEN`, `ELEVENLABS_API_KEY`,
`HOME_ASSISTANT_TOKEN`, `PORCUPINE_ACCESS_KEY`, and `MQTT_PASSWORD`.

```bash
nexus assistant data gmail --query 'newer_than:1d'
nexus assistant data calendar --days 1
nexus assistant data contacts
nexus assistant data drive --query "name contains 'plan'"
nexus assistant data notion --query 'release checklist'
nexus assistant data obsidian --vault /path/to/vault --query 'morning routine'
nexus assistant data draft 'A reviewed draft body' --to person@example.com --subject 'Plan'
nexus assistant data send 'A reviewed message body' --to person@example.com --subject 'Plan'
```

Draft/send prints a preview and requires its fresh one-use code in the same
interactive process. Non-interactive execution without that code is refused. The
dashboard also supports a typed challenge or server-verified voice approval.
Email, calendar, Drive, Notion, Obsidian, web and uploaded text are wrapped as
untrusted evidence and receive no tool authority.

## Reminders, briefs, monitors and service recovery

```bash
nexus assistant remind add 'Check the deployment' --every 30 --confirm 'ADD REMINDER'
nexus assistant remind list
nexus assistant monitor add --kind server \
  --options '{"metric":"memory","threshold":90}' --every 5 --confirm 'ADD MONITOR'
nexus assistant monitor add --kind website \
  --options '{"url":"https://example.com/health","status":200}' --confirm 'ADD MONITOR'
nexus assistant brief --confirm 'BUILD MORNING BRIEF'
nexus assistant brief --summarize --confirm 'SUMMARIZE MORNING BRIEF'
nexus assistant scheduler --once --confirm 'RUN ASSISTANT SCHEDULER'
nexus assistant scheduler --confirm 'START ASSISTANT SCHEDULER'
```

The scheduler persists reminders and monitor definitions, stores notices before
optional delivery, honours existing quiet/DND policy, and suppresses repeated
unchanged monitor alerts. Morning-brief sources individually report disabled or
unavailable; they are never invented. Native/Telegram delivery uses the existing
opt-in workers. `NEXUS_ASSISTANT_SCHEDULER=1 nexus serve` explicitly enables the
worker alongside the dashboard.

On Linux, a reviewed user service adds restart recovery:

```bash
nexus assistant service status
nexus assistant service install --confirm 'INSTALL ASSISTANT SERVICE'
nexus assistant service remove --confirm 'REMOVE ASSISTANT SERVICE'
```

The service binds loopback by default. Phone access still requires the existing
server token and a reviewed HTTPS proxy. No service is installed implicitly.

## Vision, research and automation

```bash
nexus assistant vision ask document.pdf --query 'What is the deadline?'
nexus assistant vision ask screenshot.png --query 'Explain this error'
nexus assistant vision screen --query 'Describe visible errors briefly.'
nexus assistant vision camera --weights /models/yolov8n.pt --faces \
  --confirm 'CAPTURE CAMERA FRAME'
nexus assistant vision enroll-face owner.jpg --name owner --confirm 'ENROLL FACE owner'
nexus assistant vision recognize-face group.jpg --confirm 'RECOGNIZE FACE'
nexus assistant vision forget-face --name owner --confirm 'FORGET FACE owner'

nexus assistant research 'What changed?' --url https://example.com/source \
  --domain example.com
nexus assistant reason 'Review this architecture and identify concrete risks'
nexus assistant backup create --confirm 'CREATE BACKUP'
nexus assistant backup restore --id BACKUP_ID --output restored \
  --confirm 'RESTORE BACKUP BACKUP_ID'
```

Screen capture uses the existing context enablement, foreground-window checks,
OCR redaction and separate cloud consent. Camera capture is one explicit frame and
is released immediately. Research requires exact HTTPS URLs plus a domain
allowlist and returns source citations. Backups exclude `.nexus`, VCS, caches and
known secrets; restore never overwrites a non-empty destination.

Desktop execution additionally requires `NEXUS_DESKTOP_ENABLED=1` and exact
per-action confirmation. Home Assistant requires an entity allowlist; MQTT uses
TLS and a topic-prefix allowlist. Both require a fresh preview challenge before a
service call or publish. Workflow recording stores reviewed pointer semantics,
not raw keystrokes or passwords; replay reviews every action.

## Dashboard and PWA

Open `http://127.0.0.1:8787/dashboard#assistant`. The Personal assistant section
contains voice/waveform, memory CRUD, reminders/monitors, briefs, upload QA,
allowlisted research, personal-data reads, reviewed email, backup/restore and sync
state. The installable PWA caches only the static application shell. It never
caches API responses, SSE, credentials, audio, email or personal memory. Offline
mode cannot perform live assistant actions.

## Security and verification boundary

* External sends and consequential mutations require explicit fresh approval;
  changed payloads, expired challenges and replay are refused.
* Assistant CLI/API intent and successful actions use the existing chained audit
  store. Content and secrets are not placed in audit details.
* Existing cost budgets and authenticated API rate limits apply to model and web
  endpoints. Account secrets use environment or native keyring only.
* `python -m pytest -q tests/test_assistant_core.py
  tests/test_assistant_integrations.py tests/test_assistant_api.py` covers the
  safety contracts with deterministic adapters.
* Real microphones, wake-word models, cameras, biometric accuracy, OAuth accounts,
  external sends, desktop control, MQTT/Home Assistant delivery and multi-device
  use still require acceptance on your own hardware/accounts. Their presence in
  the capability inventory is not a claim that they ran in CI.
