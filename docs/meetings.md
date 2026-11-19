# Meeting assistant (Task 26)

Opt-in. Nexus keeps notes for meetings you tell it about, summarises them
offline and lists action items. It works from two sources: notes you dictate
or type while the meeting runs, and transcript files you import afterwards.

It does **not** open the microphone or listen to the room, and it never sends
a transcript or a summary anywhere by itself. There is no tool that mails,
posts or uploads meeting content.

| | |
| --- | --- |
| Policy file | `meetings.yaml` (operator-owned, protected, default `enabled: false`) |
| Code | `nexus/meetings/` |
| Extras | `pip install "nexus-agent[secure-state]"` for encryption at rest; `"nexus-agent[meetings]"` only for audio import |
| State | `<NEXUS_HOME>/meetings/<workspace-hash>/` - `index.json.enc` and one `mtg-<id>.json.enc` per meeting |
| CLI | `nexus meetings status\|start\|note\|stop\|import\|list\|show\|summary\|export\|delete\|purge` |
| Tools | none by default; with `agent_tools: true`: `meeting_list` (READ), `meeting_summary` (READ) |
| Slash | `/meeting`, `/meeting start TITLE \| CONSENT`, `/meeting note TEXT`, `/meeting stop`, `/meeting summary [ID]` |

## Enable

```yaml
# meetings.yaml
schema: 1
enabled: true
encrypt_at_rest: true     # needs an OS keyring; set false to store plain JSON
retention_days: 30
```

```bash
nexus meetings start "Weekly sync" --consent "Told Rahim and Ayesha at the start; both agreed"
nexus meetings note "Ayesha will send the billing export by Friday." --speaker Ayesha
nexus meetings stop
nexus meetings summary            # the latest meeting

nexus meetings import ~/Downloads/standup.vtt --consent "Recording announced by the host; nobody objected"
nexus meetings export mtg-20261110-101500-1a2b3c4d > notes.md
```

## Consent

Every meeting needs a consent statement of at least 8 characters: who was told
that notes are taken, and that they agreed. Without it nothing is stored and
no file is read. The statement is saved with the meeting and printed at the
top of every summary.

Nexus cannot check the statement. It records what you said; getting real
consent, and knowing what the law where you live requires for recordings and
transcripts, is up to you.

When notes start, Nexus shows (or, by voice, speaks) `consent_notice`, so that
people in the room hear it.

## Taking notes

- **CLI / REPL**: `start`, then `note` for each line, then `stop`. One meeting
  can be open at a time.
- **Voice**: "start meeting notes for the budget review" only makes Nexus ask
  whether everyone agreed. "Everyone agreed, start meeting notes for the
  budget review" starts them. Then "meeting note: ...", "stop meeting notes",
  "summarize the meeting". Other speech is not stored. Starting notes, adding
  a note and having the summary read aloud are refused for an unverified
  speaker and after the assistant's last answer used untrusted content;
  anybody present can stop the notes. While voice privacy mode is on, the
  summary is not read aloud (see
  [voice.md](voice.md#spoken-commands-of-the-opt-in-features)).

Credential-like text (tokens, keys) is masked before a line is stored, and the
summary says in how many places.

## Importing a transcript

| File | What is read |
| --- | --- |
| `.vtt` (WebVTT) | cue times, `<v Name>` speakers, text; `NOTE`/`STYLE` blocks are skipped |
| `.srt` (SubRip) | cue times and text; formatting tags are removed |
| `.txt`, `.md` | one line per entry; `Name: text` gives the speaker; a leading `[00:01:05]` gives the time |

Files must be UTF-8 and at most `limits.max_import_bytes`. In text files a
time like `12:30` is read as minutes and seconds.

### Audio files (optional)

```yaml
audio:
  enabled: true
  model: small        # faster-whisper model size, or a folder with a model
  language: auto
```

`nexus meetings import call.wav --consent "..."` then transcribes the file on
this machine with faster-whisper (the same engine the voice feature uses).
Cloud speech-to-text is never used here. Two things to know: faster-whisper
downloads the model the first time unless `model` is a local folder, and there
is no speaker separation, so the transcript has no names.

## Summaries

`nexus meetings summary [ID]` always gives the **offline summary**:

- **Key points** - the sentences whose words come up most across the meeting.
  Sentences that repeat a better one are dropped.
- **Decisions** - sentences with words such as "decided", "agreed", "approved".
- **Action items (candidates)** - commitments and requests: "I will ...",
  "Ayesha should ...", "please ...", "can you ...?", "action item: ...". The
  owner and due date are filled in only when the sentence states them.
- **Questions raised**, speakers with word counts, and the length.

Every line is a quote from the transcript, so the offline summary can pick the
wrong sentences but it cannot invent content. The cue words are English and
Bengali; a transcript in another language gets key points and questions only.

### Model summary (optional)

```yaml
summary:
  mode: model
  agent: "ollama"      # "" = your default agent
  allow_cloud: false
```

The transcript is sent to the model as untrusted data, in one call. A
transcript over about 24,000 characters is sent as its best sentences, and the
summary says what share the model saw. The result is shown above the offline
summary, labelled "unverified", and stored so that later reads cost nothing.

- If the call fails, you still get the offline summary, with a note.
- If the transcript contains instruction-like text ("ignore previous
  instructions ..."), the meeting and the summary are marked. A model can be
  misled by such text; compare its summary with the quotes below it.
- `allow_cloud: false` (the default) refuses a cloud agent, and a local
  agent's cloud `fallback` is not used either. A cloud agent is any agent
  whose endpoint is not on this machine or a private network.
- `--offline` skips the model for one call.

## Retention and deletion

- `retention_days` (default 30, `0` = keep): a meeting is deleted that many
  days after it ended. An open meeting that was never stopped counts from its
  start.
- Retention is applied whenever the meeting assistant is used (any command,
  tool or voice request) and by `nexus meetings purge`. There is no background
  job: if Nexus is not run, nothing is deleted.
- `nexus meetings delete ID` removes one meeting at once.
- Deleting removes the sealed file. It does not overwrite disk blocks, and it
  does not reach backups or files you exported.

## At rest

With `encrypt_at_rest: true` the index and each meeting are AES-256-GCM sealed
with a key from the OS keyring. If no keyring is available, meetings refuse to
open; nothing is written in plain text. Changing the setting later does not
convert existing data: Nexus refuses to read until you restore the setting.

## Agent tools

Off by default. With `agent_tools: true` the agent can list meetings and read
the offline summary (plus a stored model summary). What a tool returns goes to
the model that runs the session, which may be a cloud model, so turn this on
only if that is acceptable. Tool output is wrapped as untrusted data and marks
the session as tainted, which stops saved approvals from authorising
high-impact actions afterwards.

The agent cannot start a meeting, add notes, import, delete, or trigger a
model summary.

## Policy keys

| Key | Default | Meaning |
| --- | --- | --- |
| `encrypt_at_rest` | `true` | seal state with a keyring key |
| `retention_days` | `30` | 0-3650; 0 keeps meetings until you delete them |
| `consent_notice` | "Notes are being taken for this meeting." | shown or spoken at start |
| `limits.max_meetings` | `200` | stored meetings |
| `limits.max_transcript_chars` | `400000` | per meeting |
| `limits.max_import_bytes` | `2000000` | transcript file size |
| `summary.mode` | `extractive` | `model` adds the model summary |
| `summary.agent`, `summary.allow_cloud` | `""`, `false` | which model, and whether a cloud model may see transcripts |
| `summary.max_tokens_per_call` | `900` | output limit of the model call |
| `summary.max_points` | `6` | key points in the offline summary |
| `audio.enabled`, `audio.model`, `audio.language`, `audio.max_megabytes` | `false`, `small`, `auto`, `200` | audio import |
| `agent_tools` | `false` | register the two read-only tools |

## Audit

`meetings.start`, `meetings.stop`, `meetings.import`, `meetings.summary`,
`meetings.delete`, `meetings.purge`. Rows carry the meeting id and counts,
never titles or transcript text.

## Known limits

- No live transcription and no microphone capture. Live notes are what you
  dictate or type.
- Action items are pattern matches. Expect misses and false hits; that is why
  they are called candidates.
- Speakers come from labels in the file. Audio import has none.
- Audio import was tested with a stand-in for faster-whisper, not with the
  real library.
- Consent is your statement; Nexus does not verify it.
