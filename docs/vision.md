# Vision and perception — opt-in, local-first

Vision does **not** start with Nexus, the HUD, Telegram, or a voice session. The
operator must edit `vision.yaml`; every switch ships **OFF**. Installing an extra
is not consent. `--yes`, saved approval scopes, model output and API booleans
cannot enable features or replace fresh approval.

## Install only what you need

From an editable checkout (or replace `-e ".[extra]"` with `"nexus-agent[extra]"`):

```bash
python -m pip install -e ".[vision-documents]"  # Pillow + PDFium; image/PDF Q&A
python -m pip install -e ".[context]"           # foreground screenshots + OCR adapters
python -m pip install -e ".[vision]"            # OpenCV + Ultralytics YOLOv8 camera
python -m pip install -e ".[vision-faces]"       # SEPARATE biometric extra + encryption/keyring
```

Heavy modules are imported only when used. No camera, face model, PDF renderer,
VLM model, Tesseract, or OS package is installed/downloaded by a vision command.
Install Tesseract yourself for screen OCR; its executable must be on `PATH`.
Face recognition uses native dlib/face-recognition and may require platform build
prerequisites. Enrollment requires a working **native** Secret Service, macOS
Keychain, or Windows Credential Vault; plaintext keyring backends are refused.

Run your own trusted local Ollama service and install a vision-capable model
separately. Set the **exact installed tag** in YAML: the example `qwen2-vl:7b`
is not an assertion that this tag is available on every Ollama installation.
The model must accept images through `/v1/chat/completions` and structured JSON
answers. A text-only model or unsupported structured response fails; Nexus does
not switch models/providers or download a replacement.

## Local image / PDF questions

Minimal operator configuration; unspecified values retain the bounded defaults:

```yaml
# vision.yaml — edit yourself, never through agent file tools
schema: 1
enabled: true
vlm:
  enabled: true
  provider: ollama
  model: qwen2-vl:7b                 # replace with your installed vision model tag
  base_url: http://127.0.0.1:11434
  allow_cloud: false
files:
  enabled: true
  hud: false
  telegram: false
  voice: false
```

```bash
nexus vision -C . status
nexus vision -C . ask screenshot.png "What does this error mean?"
nexus vision -C . ask report.pdf "Explain the trend on page 3" --pages 3
nexus vision -C . ask chart.png "Explain this region" --region 0.1 0.2 0.9 0.8
```

Only explicit regular workspace files are read by path. Private/runtime paths,
symlinks, unsupported formats and excessive inputs fail closed. Supported inputs:
single-frame PNG/JPEG/WebP, PDF, and UTF-8 TXT/MD/LOG/CSV/JSON notes. Existing
standalone Office text-extraction helpers remain available; Office documents are
not implicitly converted/uploaded by this new VLM path.

Images are reencoded as JPEG without EXIF. PDFs are rendered **in memory**, without
initializing JavaScript/forms or following embedded links. Default limits are
10 MB input, 16 million decoded pixels, eight pages and 1,600-pixel rendered image
edges. Policy can select at most 20 pages from a document of at most 500 pages.
Larger-than-default documents require an explicit `--pages` selection; encrypted
or unsupported PDFs fail. Text notes are limited to 50,000 characters.

Answers contain `references: [{page, region}]`. Page numbers are one-based;
regions are normalized `[left, top, right, bottom]`. Crop-relative references
are translated back to the original page. Invented page IDs, invalid regions,
tool calls and malformed answers are rejected. An answer may explicitly report
`insufficient_evidence`. Valid coordinates **do not verify the model's claim**.

## On-demand screen understanding

In addition to the VLM settings, enable `screen.enabled: true` in `vision.yaml`,
and explicitly enable both `enabled: true` and `screenshots: true` in the existing
`context.yaml`. Keep unrelated context signals OFF if you do not need them.

```bash
nexus vision -C . screen "What is on my screen?"
nexus vision -C . screen "Fix this error"
nexus vision -C . screen "Explain this chart"
```

Literal screen requests also resolve through `Context.resolve` when the new
screen/VLM policy is enabled. The existing `nexus context vision`,
`VisionQuestioner.screen`, and assistant CLI names remain available. The legacy
`context.yaml`-selected adapter retains its own explicit opt-in when the new path
is not enabled; the hard-off latch applies to **both** paths.

Every screenshot needs a fresh `context_capture` approval through the existing
ApprovalGate, including in `--yes` mode. Plan/read-only runs cannot capture.
Only the foreground window is considered. Existing password-manager, banking,
private-window and conservative browser exclusions remain mandatory;
`screen.denylist` **extends**, never replaces, them. Title/app/geometry are
rechecked around capture. Unknown native foreground metadata is refused.

Existing mss/X11 and Win32 capture constraints apply. Wayland/restricted desktop
sessions may be unsupported. Screen OCR is mandatory before resizing, with
sensitive recognized lines blacked out. Missing/failed OCR refuses analysis.
OCR is imperfect, and another window's overlay can still be visible: close
sensitive overlays first. Screen payloads retain the existing 1,280 × 720 / 512 KB
cap and context TTL. Tesseract manages temporary OCR files; an OS crash can leave
those behind even though Nexus does not archive screenshots.

“Fix this error” produces an **advisory explanation** on the dedicated vision
path, not an automatic shell command or repair. A subsequent agent action keeps
all its usual permission and fresh-approval requirements.

## Explicit Gemini uploads — non-person material only

Use local vision for people, identity documents, photographs of faces, uncertain
content, or anything you cannot safely disclose. Camera/object/recognition
sessions, enrollment images, embeddings and the encrypted face store have **no
VLM/cloud route**.

For an intentional non-person upload, the operator must set:

```yaml
vlm:
  enabled: true
  provider: gemini
  model: YOUR_GEMINI_VISION_MODEL_ID  # choose an available model; no fallback
  allow_cloud: true
```

Put `GEMINI_API_KEY` in the workspace/user `.env`, environment, or native keyring
(`nexus-agent` service, `GEMINI_API_KEY` account), **never YAML or Git**. Also
install `vision-faces` for the local face-detection preflight. Using that detector
for upload safety does not enroll anyone or enable recognition.

```bash
nexus vision -C . ask diagram.png "Explain this diagram" --cloud --no-people
nexus vision -C . screen "Explain this chart" --cloud --no-people
# Legacy context route also requires attestation and the local preflight:
nexus context -C . vision --cloud-agent YOUR_EXISTING_AGENT --no-people
```

The per-request `--cloud` choice, no-people attestation, local face check and a
**new exact dangerous-action approval** are all required. Detected faces, an
unavailable detector or failed detection refuse the upload. Approvals bind the
source/evidence hashes, question hash, pages/regions, model, endpoint and policy.
Capture approval does not approve an upload. Gemini uses a fixed HTTPS endpoint;
Ollama is pinned to numeric loopback. No redirects, environment proxies,
automatic retry, tool execution or cloud fallback are permitted.

**Important limit:** face detection is not a proof of absence. Tiny, occluded,
stylized or otherwise missed faces can evade a detector. The operator's
no-people attestation is part of the boundary; use the local path whenever unsure.
No software claim is made that arbitrary uploads can be certified face-free.

Provider-reported token counts are recorded in the existing cost/session ledger
without questions, pixels, OCR or answers. Direct VLM dollar costs are **unknown,
not zero**; there is no automatic price lookup. Existing daily-token/action
limits apply, and Cost Center's unknown-price blocking can refuse subsequent
unpriced calls. Cloud approval includes a possible-charge warning. These are
not a guarantee of a provider-side spending ceiling, especially for concurrent
or failed/in-flight requests.

## HUD / dashboard, Telegram and voice

### HUD and main dashboard

Enable `files.hud: true`. Both the main **Operations** view and assistant upload
panel support image/PDF questions, page selection and normalized region crops.
The legacy authenticated FastAPI HUD uses the same implementation. Uploads are
bounded base64 JSON, decoded in memory, and are **not** saved to an uploads folder
or service-worker cache.

New HTTP routes require actual authentication, even when other local dashboard
routes are running without a token. Configure the existing `NEXUS_SERVE_TOKEN`
(or legacy `NEXUS_HUD_TOKEN`) locally. Same-origin checks and rate/concurrency
limits still apply. Do not put credentials in URLs or screenshots.

- `GET /api/vision/status` — metadata only; no device opens.
- `POST /api/vision/ask` — bounded, explicitly enabled analysis.
- `POST /api/vision/preview` — one-use cloud review; no model call.
- `POST /api/vision/off` — durable stop; no approval or inference slot needed.
- Existing `/api/assistant/vision/ask` delegates to the same guarded service.

Cloud UI reviews expire after 60 seconds and bind identical bytes/question/
pages/region, model policy, authenticated client and hard-off epoch. The browser
must resubmit the same upload with the exact one-use confirmation; there is no
server-side media stash. Replay, changed payloads and revocation are refused.
Permission-configured second factors still apply.

### Private Telegram attachment Q&A

Keep the existing explicit `telegram.yaml` enable, numeric private-chat whitelist,
bot token, PIN, freshness/replay checks and rate limits. Also enable
`files.enabled`, `files.telegram` and `vlm.enabled` in `vision.yaml`.

Attach one image or PDF with a **literal caption**:

```text
/look Explain this chart and cite the visible region
/look --cloud --no-people Explain this non-person diagram
```

Only after private identity/channel checks may the bot use bounded `getFile` and
fixed-origin file download. It accepts no arbitrary URLs, redirects, forwarded
messages or group attachments. Local analysis is the default. No output becomes
a Telegram command or queued task.

A local result stays in bounded process memory pending a **fresh PIN confirmation
of the exact outbound answer digest**. Nothing from that answer is sent until
approved. Cloud mode first requires a separate PIN-bound upload approval, then
a second approval for the answer send. Control/review prompts retain the bot's
existing metadata-only behavior. Extra `permissions.yaml` restrictions/second
factors are not bypassed; unsupported second-factor flows fail closed.

Pending content expires with the Telegram challenge, is limited to ten reviews /
20 MB total, and is lost on process restart. Failed/uncertain sends are never
retried automatically. `/vision-status` is metadata only; `/vision-off` latches
stop and clears pending reviews in that bot. Your original Telegram attachment
is already held by Telegram; Nexus cannot undo that upload or its retention.

### Voice

Enable `files.voice: true` in addition to the relevant files/screen/VLM switches
and existing voice policy. Say **“look at this chart.png”** for a workspace file,
or **“what is on my screen?”**, **“fix this error”**, or **“explain this chart”**.
“Look at this” without a path/attachment asks for clarification instead of
silently opening a camera or taking a screenshot.

The voice handler returns the answer and references directly, not through the
agent tool loop. Read-only, speaker-verification and fresh screen-capture gates
remain intact. Automatic vision speech requires explicitly local Piper/Kokoro;
cloud/auto TTS is refused to avoid exporting private answers. In the dashboard,
**Use voice question** copies the user's transcript into the selected upload's
question field; it does not execute the transcript as a task.

## Camera + YOLOv8 and local proactive events

Enable `camera.enabled` and select an **existing trusted local** `.pt` weight
file, for example `vision-weights/yolov8n.pt`. That directory and `.pt` files are
ignored by Git. Agent file tools cannot modify configured weights or policy,
including hardlink aliases. Do not load untrusted weights: PyTorch model
serialization and native dependencies are not an OS sandbox. YOLO auto-install,
offline/network behavior and telemetry switches are restricted; use OS egress
controls as well if third-party code/model provenance is not trusted.

```yaml
camera:
  enabled: true
  weights: vision-weights/yolov8n.pt
  classes: [person, dog]  # must exist in the selected model
  confidence: 0.6
  fps: 1.0
  max_frames: 300
  max_seconds: 300
  location: door
  events: false
  store_frames: false
```

```bash
nexus vision -C . camera --frames 10 --seconds 20
```

A fresh review binds the device, weights hash, classes, FPS, session limits,
face recognition, events and storage choices. One session owns the camera per
workspace. There is no automatic restart. CPU inference is selected. Frames,
boxes, classes and confidence scores are bounded/validated; actual FPS can be
lower, never a catch-up burst. `finally` releases the owned camera on normal
exit, failure, interrupt and cooperative cancellation.

Frames are not persisted by default. If you explicitly set `store_frames: true`,
JPEGs go under `$NEXUS_HOME/vision/<workspace-hash>/frames/` with private file
permissions. The default quota is 100 frames / 20 MB. **A full quota stops the
session**; there is no implicit deletion or rolling archive. Operator archival/
deletion needs its own reviewed action. Stored frames are not encrypted; do not
enable storage unless that is acceptable.

For “person at the door” notifications, additionally enable `camera.events: true`
and the master switch in `proactive.yaml`. Structured, untrusted event candidates
enter `ProactiveEngine.receive_vision_event`; the persisted per-class/kind
cooldown defaults to 60 seconds. Only **local HUD toasts** are allowed here.
Existing DND applies; voice/Telegram/desktop routing, tool execution, spending,
queueing and external sends are not inherited from normal proactive alerts.

## Consensual, encrypted local face recognition

Install `vision-faces`, enable `faces.enabled: true`, and optionally set
`faces.owner` to an enrolled display name. Each person must freely consent to
the intended local use. Enroll a workspace image containing **exactly one face**:

```bash
nexus vision -C . faces enroll Owner owner.jpg --consent
nexus vision -C . faces list
nexus vision -C . faces delete Owner
```

Enrollment and deletion each require a new ApprovalGate decision. The `--consent`
flag is an explicit operator attestation, not an automated determination that
someone consented. At most five 128-dimensional templates are retained per
person, with a default maximum of 20 people. Templates, names and consent times
are encrypted together using AES-GCM; the key is held only in the native OS
keyring. Missing/locked keys and authentication failures stop processing. There
is no plaintext fallback, automatic legacy `faces.json` import, or cloud API.
Existing plaintext legacy stores require separate operator-reviewed removal and
fresh consenting re-enrollment; they are not silently deleted.

With faces and camera events enabled, a local owner match yields a local HUD
greeting; an unmatched detected face yields an unknown-person advisory. Neither
result is authentication, authorization, reliable identification, nor a reason
to unlock doors, run tools or bypass speaker verification. False matches/misses
are possible. Deletion is allowed with a fresh approval even when recognition
or the vision-specific latch is OFF; it does not claim secure erasure from OS
backups, swap or storage remanence. Original user-provided images remain yours
to manage; Nexus does not delete them.

Legacy `nexus assistant vision ask|screen|camera|enroll-face|recognize-face|forget-face`
and public perception names remain guarded compatibility delegates. Legacy
`--confirm` strings do not bypass the new operator policy, consent or fresh
interactive review. Prefer the commands above for new workflows.

## Indicators, hard off, and trust boundary

Both HUDs expose actual **capture leases**, not an inference that a device is
active just because YAML is enabled. Each attempted frame read is preceded by a
hash-chained audit record and followed by a captured/failed/revoked result.
Records contain capture IDs/kind/timestamps, not pixels, OCR, embeddings or face
names. A stale lease is **unknown**, never falsely reported idle. A failed camera
release also latches hard off and leaves the indicator **unknown**, requiring an
OS/physical-device check. Brief screenshot
captures may finish between UI polls; the last-capture timestamp remains visible.

```bash
nexus vision -C . off
nexus vision -C . status
nexus vision -C . resume  # fresh approval; resets latch ONLY, starts no device
```

The off latch is independent of YAML, durable across processes and honored by
legacy screen capture too. It cancels in-process leases and invalidates old
capture/upload/approval epochs. The HUD off button does not wait for an inference
slot and still works with malformed vision YAML. Global Emergency Stop is also
honored. Resume never overrides `permissions.yaml` or starts a device.

A synchronous native camera read, OCR, decoder, GPU/CPU inference, or HTTP system
call may take time to return. Hard off blocks subsequent steps/transmission and
discards results, but **cannot unsend bytes already transmitted**, promise an
instant driver shutdown, or securely erase all Python/native/OS copies. Physical
camera indicators and OS-level controls remain the ultimate check.

All file/OCR/image/VLM text remains untrusted. It cannot alter YAML, grant
permissions, enroll people, call tools or confirm an approval. Parent agent
sessions consuming vision evidence are tainted; high-impact actions still need
fresh approval. A trusted loopback Ollama server could itself forward data;
Nexus validates its destination, not that server's internals.

## Offline acceptance tests

```bash
python -m pip install -e ".[dev]"
pytest -q tests/test_vision.py tests/test_vision_channels.py tests/test_context.py
pytest -q
ruff check nexus tests
```

Tests generate fixture images/PDF pages in memory, use HTTP mock transports,
fake camera/YOLO/face modules and an ephemeral fake keyring key, while exercising
real approval gates, image/PDF decoding, AES-GCM, capture audits and both HTTP
transports. They cover default OFF, protected YAML/weights, bounded resources,
references, injection handling, revocation, one-use approvals, private Telegram,
voice constraints and local-only proactive events. No API key, external service,
GPU, microphone, camera or native face model is needed. The tests are not live
acceptance of desktop permissions, a specific installed model, recognition/OCR
accuracy, camera drivers, Telegram delivery, or cloud billing.
