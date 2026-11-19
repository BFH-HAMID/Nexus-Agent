# Translator (Task 27)

Opt-in. Translates text with one of your agents. Everything around the
translation is done locally and deterministically: detecting the language,
applying your glossary, keeping code, links and placeholders intact, and
checking what the model returned. A reply that fails a check is an error, not
a translation.

| | |
| --- | --- |
| Policy file | `translate.yaml` (operator-owned, protected, default `enabled: false`) |
| Code | `nexus/translate/` |
| Needs | an agent in `agents.yaml`; no extra packages |
| State | `<NEXUS_HOME>/translate/<workspace-hash>/` - the rate window and the interpreter switch; no text |
| CLI | `nexus translate status\|text\|detect\|languages` |
| Tool | `translate_text` (READ) |
| Slash | `/translate <language> <text>`, `/translate detect <text>`, `/translate languages` |

## Enable

```yaml
# translate.yaml
schema: 1
enabled: true
agent: "ollama"        # "" = your default agent
allow_cloud: false     # true = the text may go to a hosted model
default_target: en
```

```bash
nexus translate text "The invoice is ready, {name}." --to bn
nexus translate text - --to en < letter.txt          # read standard input
nexus translate detect "আমি আগামীকাল রিপোর্ট পাঠাব।"       # offline; works while the translator is off
nexus translate languages
```

Languages are given as a code or an English name: `bn`, `Bengali`, `bangla`.
Known codes: ar bn de el en es fa fr gu he hi id it ja ko mr ms ne nl pa pl pt
ru sv ta te th tr uk ur vi zh. `targets: [en, bn]` limits what may be asked for.

## What happens to a text

1. **Detect** the source language (unless you pass `--from`).
2. **Stop early** when there is nothing to translate: the text is already in
   the target language, or it consists only of protected parts and glossary
   terms. No model is called; the result says so.
3. **Protect.** Each part that must come back unchanged is replaced by a
   numbered token such as `⟦3⟧`. The model never sees the part itself.
4. **One model call.** The text is sent as a JSON value, with the instruction
   to translate it and never to answer or follow what it says. The reply must
   be one JSON object `{"translation": "..."}`.
5. **Check the reply**, then put the protected parts back.

### What is protected

- fenced and inline code
- `http(s)` links and e-mail addresses
- placeholders: `{name}`, `{0}`, `{:.2f}`, `%s`, `%d`, `%(name)s`, `${VAR}`, `{{ template }}`
- HTML/XML tags and entities (`<b>`, `&nbsp;`)
- your glossary terms

Each kind has a size limit so that hostile text cannot stall the translator:
inline code up to 400 characters, a link up to 2,000, a `{{ template }}` up to
300, an e-mail name up to 64. A longer span is not protected and reaches the
model as ordinary text. Fenced code has no limit.

### Glossary

```yaml
glossary:
  bn:
    invoice: "চালান"
    Nexus: "Nexus"        # keep a name as it is
```

Terms are listed per target language. A term matches as a whole word, without
regard to case, longest term first. It is replaced by exactly the text you
wrote: the model cannot choose another word, and the term is not inflected.
In languages where the word would change its ending, check the result.

### Checks on the reply

| Check | Catches |
| --- | --- |
| one JSON object with a `translation` string | chatter, refusals in prose |
| every token back exactly once | lost, repeated or invented placeholders |
| not far longer than the text | an answer or an essay instead of a translation |
| at least 15% of the letters are in the target language's script | a reply in the wrong language (for example an English refusal when Bengali was asked for) |

A failed check gives `No translation: <reason>` and exit code 2. It is
recorded in the audit trail as `rejected`.

The script check needs different alphabets. Between two Latin-alphabet
languages (English to Spanish) a reply in the wrong language is not detected.
And no check can tell a fluent but wrong translation from a right one.

## Language detection

Offline. A non-Latin script identifies the language (Bengali, Arabic, Urdu,
Persian, Hindi, Russian, Ukrainian, Chinese, Japanese, Korean, ...). For the
Latin alphabet the commonest function words decide among English, Spanish,
French, German, Portuguese, Italian, Dutch, Indonesian, Turkish, Vietnamese,
Polish and Swedish.

It answers `unknown` rather than guess: for very short Latin text, for names,
and for Bengali typed in Latin letters. The model is then told to work out the
source language itself. Languages that share a script are reported as the most
common one (Devanagari as Hindi, the Bengali script as Bengali).

## Voice

- "Translate good morning to Bengali", "How do you say thank you in Bangla?"
- **Interpreter mode** (`interpreter.enabled: true`): "start interpreter"
  (or "start interpreter between Hindi and English"). From then on every
  utterance is translated instead of answered: speech in the second language
  of the pair goes to the first, everything else goes to the second. It ends
  with "stop interpreter", by itself after `interpreter.max_minutes`, or when
  you turn the policy switch off.

Starting the interpreter needs a trusted request: a verified speaker, and an
assistant whose last answer used no untrusted content. While it runs, the
other person's speech is translated too; that is the point of it. Translating
is all that happens with that speech: every sentence goes to the translator
before any other spoken command, so "remember ..." or "what is the status" is
translated, not carried out. Only the pipeline's own commands still come
first ("stop everything", a pending spoken approval). Anybody can say "stop
interpreter".

What the voice setup has to allow (`voice.yaml`):

- `stt.language: auto`. With a fixed language, the other person's speech is
  not recognised.
- Every turn starts with the wake word, the other person's too. Push-to-talk
  or `wake.enabled: false` is the practical setting for a conversation.
- With `speaker.require_always: true` only your own voice is heard at all, so
  the other person is not translated. Interpreter mode does not lift that.

The translation is spoken in the voice for its own script, not in the voice
for the language of the request: Bengali script gets the Bengali voice, Latin
script the English one, a mix of both the mixed one, and anything else the
default voice (always the default voice when `persona.yaml` does not mirror
the user's language). Whether that voice can pronounce the target language
depends on your voice setup.

## Agent tool

`translate_text(text, target?, source?)` is READ: it changes nothing. It does
call a model, so `rate_per_hour` (default 120 model calls per hour, all
sources together) bounds what an agent can spend.

If the text contains instruction-like wording, or the session has already read
untrusted content, the translation is returned wrapped as untrusted data and
the session is marked tainted: a translated instruction is still not an
instruction.

## Cloud models

A cloud agent is any agent whose endpoint is not on this machine or a private
network. With `allow_cloud: false` (the default) a cloud agent is refused, and
a local agent's cloud `fallback` is not used either: if the local model is
down, the translation fails and the text stays here.

## Policy keys

| Key | Default | Meaning |
| --- | --- | --- |
| `agent` | `""` | agents.yaml name; empty = default agent |
| `allow_cloud` | `false` | whether text may go to a cloud agent |
| `default_target` | `en` | used when no target is given |
| `targets` | `[]` | allowed targets; empty = any known language |
| `max_chars` | `4000` | per request (50-40000); longer text is refused, not cut |
| `max_tokens_per_call` | `1500` | output limit of the model call |
| `rate_per_hour` | `120` | model calls per hour |
| `glossary` | `{}` | up to 20 languages, 300 terms each |
| `interpreter.enabled` | `false` | allow the voice interpreter |
| `interpreter.pair` | `[en, bn]` | default language pair |
| `interpreter.max_minutes` | `30` | the mode ends by itself |

## Audit

`translate.text` with result `ok`, `rejected` or `failed`, the language pair,
the character count and the agent; `translate.interpreter` with `on` / `off`.
The text and the translation are never written to the audit trail or to state.

## Known limits

- Translation quality is the model's. It was not measured: the tests use
  scripted replies, no real model was available where this was built.
- One request is one model call of at most `max_chars`. Longer documents have
  to be split by you.
- Mixed-language text is sent as it is; the model decides how to handle it.
- Interpreter mode was tested with scripted speech recognition, not with two
  people at a microphone. It needs the wake word before every sentence unless
  the wake word is off, and it does not work with
  `speaker.require_always: true` (see "Voice").
- The token characters `⟦` and `⟧` cannot appear in the text.
