# Voice core & human conversation layer (`nexus/voice/`): setup, commands, and safety boundaries

Nexus voice control and its persona policy are **opt-in and disabled by default** (`enabled: false` in `voice.yaml` and `persona.yaml`).
Importing `nexus.voice` never opens audio hardware, downloads model weights, or contacts cloud endpoints.

## Installation (optional extras)

Install only the voice capabilities you need:

```bash
python -m pip install -e ".[voice]"           # sounddevice, faster-whisper, silero-vad, edge-tts, numpy
python -m pip install -e ".[voice-wake]"      # openWakeWord, Porcupine, pynput push-to-talk hotkey
python -m pip install -e ".[voice-offline]"   # Piper / Kokoro local offline TTS
python -m pip install -e ".[voice-speaker]"   # Resemblyzer speaker verification
python -m pip install -e ".[voice-realtime]"  # Optional Gemini Live API adapter (google-genai)
python -m pip install -e ".[voice-livekit]"   # Optional LiveKit Agents adapter
python -m pip install -e ".[voice-pipecat]"   # Optional Pipecat pipeline adapter
```

## Configuration (`voice.yaml` & `persona.yaml`)

Both `voice.yaml` and `persona.yaml` are **operator-owned** (protected by `Permissions.operator_control()`; agent file tools cannot modify them). Inline secrets in either YAML file are rejected — store any cloud STT/TTS/realtime keys in `.env` or the unlocked OS keyring (`OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, `GEMINI_API_KEY`, `LIVEKIT_*`), never in YAML or Git.

Minimal offline + conversation configuration example (`voice.yaml`):

```yaml
schema: 1
enabled: true

sample_rate: 16000
channels: 1
input_device: null
output_device: null

wake:
  enabled: true
  engine: openwakeword       # openwakeword | ptt | disabled
  word: "hey nexus"
  model_path: "/path/to/hey_nexus.onnx"
  sensitivity: 0.6
  cooldown_seconds: 1.5
  push_to_talk_key: "ctrl+space"

vad:
  engine: silero             # silero | energy
  threshold: 0.5
  min_speech_ms: 250
  min_silence_ms: 700
  speech_pad_ms: 200
  max_utterance_seconds: 30

stt:
  provider: faster-whisper   # faster-whisper | cloud
  model_size: small
  language: auto             # auto (Bangla, English, mixed) | bn | en

tts:
  provider: piper            # piper | kokoro | edge | elevenlabs | auto
  speaking_rate: 1.0
  piper_model: "/path/to/en_US-lessac-medium.onnx"
  edge_voice_bn: bn-BD-NabanitaNeural
  edge_voice_en: en-US-AriaNeural

speaker:
  enabled: false             # set true after `nexus voice enroll`
  require_for_dangerous: true
  threshold: 0.75

duplex:
  enabled: true              # keep listening while speaking; barge-in <200ms
  barge_in_threshold: 0.55
  barge_in_min_speech_ms: 96
  cancel_budget_ms: 200

turn_detection:
  semantic_eot: true         # extend silence after trailing 'and', 'um', open punctuation
  adaptive_silence: true
  min_silence_ms: 450
  max_silence_ms: 2400
  incomplete_extension_ms: 900

streaming:
  enabled: true
  fast_first_clause: true
  target_first_audio_ms: 1000

realtime:
  enabled: false             # local pipeline remains default
  provider: local            # local | gemini-live | livekit | pipecat

acknowledgements:
  enabled: true              # rate-limited 'Okay, on it.' when LLM takes > min_delay_ms
  min_delay_ms: 450
  cooldown_seconds: 12.0
  max_per_minute: 4

conversation:
  max_turns: 10
  resolve_references: true   # resolve 'that one', 'run it again', 'send it to him'
  one_question_clarification: true
  spoken_approvals: true     # 'yes do it', 'no cancel', 'wait'
```

Persona configuration (`persona.yaml`, disabled by default with `enabled: false`):

```yaml
schema: 1
enabled: true
name: Nexus
tone:
  style: calm
  concise: true
  light_humour: true
language_policy:
  mirror_user: true
  allow_bangla_english_mixing: true
spoken_style:
  short_sentences: true
  strip_markdown: true
  read_lists_aloud: false
  read_urls_aloud: false
  summarize_numbers: true
  summarize_code: true
  long_output_route: hud     # hud | telegram | both
  offer_summary_on_long: true
```

## CLI commands

```bash
nexus voice devices                     # list input/output devices (read-only)
nexus voice test-mic --seconds 2        # record a short mic diagnostic sample
nexus voice test-tts "Hello Nexus"      # synthesize secret-redacted speech
nexus voice enroll s1.wav s2.wav --confirm "ENROLL MY VOICE"
nexus voice run                         # run full-duplex wake -> VAD/EOT -> STT -> agent -> streaming TTS loop
nexus voice run --wav cmd.wav           # run one prerecorded WAV through the pipeline
nexus voice run --no-ack                # disable spoken acknowledgements for this run
nexus voice run --no-duplex             # disable full-duplex barge-in monitoring
nexus voice run --realtime gemini-live  # opt into a realtime adapter instead of local pipeline
```

## Architecture & conversation layer

1. **Audio I/O (`nexus/voice/audio_io.py`)**: 16 kHz mono PCM16 capture and low-latency 32 ms chunked playback over `sounddevice` (with `ffplay`/`aplay`/`afplay` fallback), device index/name resolution, and WAV helpers.
2. **Wake word (`nexus/voice/wake.py`)**: `OpenWakeWordDetector` (default `'hey nexus'`, sensitivity + cooldown) and `PushToTalkDetector` hotkey fallback.
3. **Adaptive VAD + semantic end-of-turn (`nexus/voice/vad.py`, `nexus/voice/turn_detection.py`)**: `SileroVAD` plus `SemanticTurnDetector` and `AdaptiveSilenceTracker` in `TurnTakingSegmenter`. Trailing fillers (`"um"`, `"uh"`, `"উম"`), trailing conjunctions/prepositions (`"and"`, `"but"`, `"এবং"`), or unclosed punctuation extend the silence threshold so speakers are not cut off mid-thought.
4. **Full duplex & streaming (`nexus/voice/duplex.py`, `nexus/voice/tts.py`, `nexus/voice/pipeline.py`)**:
   - `StreamingSentenceBuffer` flushes LLM tokens into TTS sentence-by-sentence while `TurnMetrics` records `stt_ms`, `llm_first_token_ms`, `tts_first_audio_ms`, and `barge_in_cancel_ms`.
   - `FullDuplexMonitor` listens while Nexus speaks; user speech triggers `interrupt_speech()` within `< 200 ms`, cancelling active TTS (`PlaybackQueue.barge_in`) and setting `llm_cancel_event` (aborting active `NexusAgent` token streaming via `StreamCancelled`).
   - Subsequent user speech is classified via `classify_barge_in()` into `resume` (`"continue"`, `"keep going"`), `discard` (`"stop"`, `"cancel"`), or `new_turn`.
5. **Persona & long-output routing (`nexus/voice/persona.py`)**: `format_for_speech()` enforces short sentences, strips markdown, summarizes lists/URLs/numbers/code blocks, mirrors Bangla/English/Bangla-English mixing, and routes full redacted long output to `$NEXUS_HOME/voice/<ws_hash>/hud_messages.json` (surfaced via `HUD.snapshot()['voice']['messages']`) or whitelisted `telegram.yaml` recipients (`Bot`).
6. **Conversation state & rate-limited acknowledgements (`nexus/voice/conversation.py`)**: `ConversationState` resolves references (`"that one"`, `"run it again"`, `"send it to him"`) over the last N turns and active mission context, asking at most ONE short clarifying question when ambiguous; `AcknowledgementManager` emits rate-limited short acks (`"Okay, on it."`) when slow and can be disabled via `acknowledgements.enabled: false` or `--no-ack`.
7. **Spoken approvals (`nexus/voice/approvals.py`)**: `SpokenApprovalManager` formats the exact planned action and risk class (`SAFE` / `MEDIUM` / `DANGEROUS`), treats `"yes do it"`, `"no cancel"`, and `"wait"` as first-class spoken intents layered strictly *inside* `ApprovalGate` (`permissions.yaml`, speaker verification on `DANGEROUS`, Task 2 second-factor PIN/Telegram confirmation when configured on `DANGEROUS` actions, and fresh `once` confirmation remain intact; `--yes` never bypasses `permissions.yaml`).
8. **Live status Q&A & narration routing (`nexus/chief.py`, `nexus/notifications.py`)**: Voice turns record user presence (`mark_user_present`) and answer live questions (`"what is the status?"`, `"what are you doing?"`, `"why did that fail?"`) strictly from live mission state and logs via `answer_status_question()`. When `status_narration` / `notifications` are enabled in `autonomy.yaml`, progress milestones speak when the user is present and fall back to Telegram/desktop notifications when away.
9. **Optional realtime adapters (`nexus/voice/realtime.py`)**: unified `RealtimeVoiceAdapter` interface with `LocalPipelineRealtimeAdapter` as the default, plus lazy-imported `GeminiLiveAdapter`, `LiveKitAdapter`, and `PipecatAdapter`.

## Known limits

- Live microphone capture, wake word inference, neural VAD, local STT/TTS models, Resemblyzer embeddings, and realtime adapters require installing optional extras and local model/hardware/credential support; offline test suites verify contracts deterministically with fake audio frames and scripted providers.
- Real-time acoustic echo cancellation (AEC) is not built into `sounddevice` capture; use headphones or push-to-talk if speaker playback feeds back into the microphone.
- Cloud/realtime providers (`stt.provider: cloud`, `tts.provider: edge` / `elevenlabs`, `realtime.provider: gemini-live` / `livekit` / `pipecat`) transmit audio/text (after secret redaction on local TTS) to the configured provider; use local `faster-whisper` + `piper` / `kokoro` + `realtime.provider: local` for strictly offline operation.
