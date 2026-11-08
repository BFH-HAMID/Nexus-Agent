# Long-Term Memory and Personalization (`memory.yaml`)

Nexus keeps curated Markdown notes (`MEMORY.md` / `USER.md`) separate from
opt-in personal profiling, FAISS vector memory, runtime context awareness, and
at-rest encryption. Every new personalization capability is **OFF by default**
until enabled by the operator in `memory.yaml`. Agent file tools cannot edit
`memory.yaml`.

## How to enable

1. Ensure optional dependencies are installed when using vector memory and
   encryption at rest:
   - Vector search (`faiss-cpu`, `numpy`): included in `nexus-agent[orchestrator]` / `nexus-agent[dev]`.
   - Encryption at rest (`cryptography`, `keyring`, `filelock`): included in `nexus-agent[secure-state]`.
2. Edit `memory.yaml` in your workspace (operator-owned):

```yaml
schema: 1
enabled: true
vector_memory:
  enabled: true
  max_entries: 500
  max_chars: 200000
  compaction_threshold: 250
  compaction_keep_recent: 50
  embedding_provider: local-hashed   # or local-ollama
  embedding_dimensions: 256
  top_k: 5
profile:
  enabled: true
  require_confirmation_for_inferred: true
  block_sensitive_categories: true
context_awareness:
  enabled: true
  include_project: true
  include_mission: true
  include_recent_turns: 10
  include_time_of_day: true
  include_active_window: false       # opt-in only; private windows are blocked
privacy:
  encrypt_at_rest: true
  voice_privacy_mode: false
```

## Capabilities

- **User profile store (`nexus/memory.py`)**: tracks `preferences`, `routines`,
  `languages`, `people`, `projects`, `working_hours`, and `tone_preferences`.
  User-stated facts are `confirmed`; inferred facts from `LearningLoop` are
  saved as `suggested` and stay excluded from prompts until explicitly
  confirmed.
- **Vector memory (`faiss.IndexFlatIP`)**: indexes past conversations and
  mission outcomes using offline local embeddings (`LocalHashedEmbedding`, or
  loopback Ollama embeddings when configured), retrieving top-k entries with
  `source` and `date` provenance. Size caps trigger `compact()`, summarizing
  older entries and deleting compacted raw records from both disk and FAISS.
- **Voice & text commands**:
  - `remember that ...`
  - `what do you remember about <topic>`
  - `forget <entry>`
  - `forget everything about <topic>` (always requires confirmation before deleting from files and the vector index)
  - `turn on privacy mode` / `turn off privacy mode` (never speaks stored memory contents aloud in voice privacy mode)
- **Dashboard Memory UI (`/api/knowledge/personal/*` + `nexus/panel/memory.js`)**:
  list, search, add, confirm/reject suggested facts, edit, delete, export as
  JSON, and inspect `source` and timestamp per entry.
- **Privacy & untrusted isolation**:
  - Sensitive categories (`health`, `finance`, `credentials`) are never stored
    automatically unless explicitly instructed (and raw secrets/credentials are
    always rejected).
  - Prompt injections and untrusted/tainted inputs (`<untrusted-data>`, emails,
    web pages, MCP results, OCR) cannot write to memory directly.
  - When `privacy.encrypt_at_rest: true`, payloads are sealed with AES-256-GCM
    (`nexus[secure-state]`) using a workspace key stored in the OS keyring and
    fail closed if `cryptography` or keyring access is unavailable.

## CLI

```bash
nexus memory personal --json
nexus memory remember "Prefer concise pytest output"
nexus memory search "pytest"
nexus memory confirm pf_1234567890
nexus memory forget-topic "old project" --yes
nexus memory compact
nexus memory export > memory-backup.json
```
