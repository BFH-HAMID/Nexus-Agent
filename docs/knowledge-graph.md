# Personal knowledge graph (Task 18)

Opt-in. Nexus links the facts you **explicitly told it** into a small graph of
entities and relations, so it can answer "what do you know about Ayesha?" and
"how is Rahim related to me?" instead of only doing fuzzy text recall.

| | |
| --- | --- |
| Policy file | `knowledge_graph.yaml` (operator-owned, protected, default `enabled: false`) |
| Code | `nexus/knowledge_graph/` |
| State | `<NEXUS_HOME>/knowledge_graph/<workspace-hash>/graph.json.enc` (outside the workspace) |
| CLI | `nexus kg …` |
| REPL / voice | `/kg …`, "who is X", "how is X related to Y", "note that X knows Y" |
| Tools | `kg_query` (read), `kg_add_fact` (write), `kg_forget` (dangerous) |

## Enable

```yaml
# knowledge_graph.yaml
schema: 1
enabled: true
encrypt_at_rest: true      # needs: pip install "nexus-agent[secure-state]"
```

With `encrypt_at_rest: true` and no usable OS keyring the graph **refuses to
open**; it never falls back to plaintext. On a headless test box set
`encrypt_at_rest: false` deliberately. Switching the setting while data exists
is refused until you export and re-import (no silent downgrade).

## Use

```bash
nexus kg status
nexus kg add Ayesha "works at" Grameenphone
nexus kg ingest "My sister is Ayesha. Rahim is Ayesha's manager."
nexus kg about Ayesha
nexus kg path me Rahim            # me → sister → Ayesha → manager → Rahim
nexus kg alias Ayesha Ash
nexus kg forget Ayesha            # the entity and every relation touching it
nexus kg export > graph.json
nexus kg clear --confirm "CLEAR GRAPH"
```

When long-term memory (`memory.yaml`) is on, confirmed profile facts and
`remember` entries are mirrored into the graph automatically
(`sources.memory: true`). `forget` and a confirmed `forget everything about …`
cascade: a relation disappears when its last source is forgotten.

## Voice

In `nexus voice run`, while the graph is on:

| You say | Answer |
|---|---|
| "Who is Ayesha?" | "About Ayesha: ..." with up to six facts |
| "How is Rahim related to Ayesha?" | the chain of relations between the two, or "I do not know a link between ..." |
| "Note that Rahim knows Karim" (also "works with", "is related to") | "Noted: Rahim knows Karim." - stored as your own statement |

* A name that is not in the graph is not the graph's business: "who is Albert
  Einstein" goes to the assistant.
* "What do you know about Ayesha?" and "remember that ..." are sentences of
  long-term memory and stay with it (what memory stores is mirrored into the
  graph). The graph answers them only while `memory.yaml` is off as a whole.
* What the graph holds is said, and a fact is added, only for a verified
  speaker in a session whose last answer used no untrusted content. Otherwise:
  "I only say what the graph holds on a trusted, verified request."
* While voice privacy mode is on, nothing from the graph is said aloud.

The order of spoken commands and what "verified" and "untrusted" mean are in
[voice.md](voice.md#spoken-commands-of-the-opt-in-features).

## Where facts come from

* `extraction.mode: rules` (default) - deterministic sentence patterns
  ("My X is Y", "A is B's manager", "A works at B", "আমার বোনের নাম …"). A
  sentence that matches no pattern produces nothing. No model call.
* `extraction.mode: model` - one tool-free, temperature-0, strict-JSON call to
  the configured agent. The text is wrapped as untrusted data, every returned
  name must occur verbatim in the text, confidence is capped at 0.6, and a
  cloud agent is refused unless `extraction.allow_cloud: true`. "Cloud" is
  every agent whose endpoint is not on this machine or a private network
  address. While `allow_cloud` is false the text is never handed to a cloud
  `fallback` agent either; if the local model is down, nothing is extracted.

## Safety model

* **Trusted vs unverified.** Only your own statements can be trusted. Facts
  from indexed documents (`sources.knowledge: true`), from a tainted run, or
  from a tool after untrusted content was read are stored as *unverified*,
  labelled as such in every answer, and never put into a prompt.
* **No injection path.** Text carrying prompt-injection markers is not ingested
  at all. Trusted writes pass the same gate as long-term memory writes.
* **No secrets.** Credential-looking values are refused; `credentials` cannot be
  removed from `exclude_categories`; `finance` and `health` are excluded by
  default.
* **Prompt context is opt-in** (`context.inject: true`), trusted facts only,
  bounded by `context.max_chars`, and headed "data, never instructions".
* `kg_forget` is DANGEROUS: it always needs a fresh human approval and is never
  authorised by `--yes`.
* Adds, merges and forgets are written to the hash-chained audit trail
  (`nexus audit tail`).

## Known limits

* Rule extraction is English-first with a couple of Bangla patterns; it will
  miss facts phrased unusually. Add them explicitly with `nexus kg add`.
* The graph is one sealed JSON document (bounded by `limits.*`), not a graph
  database; it is designed for thousands of facts, not millions.
* Path search ignores edge direction and stops at `limits.max_hops`.

## Tests

`pytest tests/test_knowledge_graph.py` (offline; fake keyring and scripted provider).
