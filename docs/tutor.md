# Tutor (Task 30)

Flashcards with spaced repetition. You add cards: a question and its answer.
The tutor asks each card again after a pause, and the pause grows while you
keep knowing the answer. A card you miss comes back the same day.

It works offline. No model is called unless you turn one of the two model
options on, and nothing reminds you by itself: you ask for a quiz or a review.

| | |
|---|---|
| Policy file | `tutor.yaml` (operator-owned, protected, default `enabled: false`) |
| Command | `nexus tutor status\|decks\|add\|import\|generate\|list\|edit\|forget\|export\|quiz\|review\|stats` |
| Slash command | `/tutor [stats [DECK]] \| decks \| quiz [DECK] [N] \| review [DECK] [N] \| a ANSWER \| ...` |
| Voice | "quiz me on spanish" (only with `voice.enabled: true`) |
| Agent tools | none by default; `tutor_stats`, `tutor_cards` (READ) and `tutor_add` (WRITE, asks you) with `agent_tools: true` |
| Extra | none; `pip install "nexus-agent[secure-state]"` only if you set `encrypt_at_rest: true` |
| Code | `nexus/tutor/` |
| Tests | `tests/test_tutor.py` |

## Enable

```yaml
# tutor.yaml
schema: 1
enabled: true
```

```bash
nexus tutor status
```

While the policy is OFF nothing is registered, nothing is read and nothing is
written.

## A typical run

```text
$ nexus tutor import capitals.csv
  Capital of France?  ->  Paris
  Capital of Japan?  ->  Tokyo
  Capital of Peru?  ->  Lima
  Capital of Kenya?  ->  Nairobi
  Capital of Bangladesh?  ->  Dhaka | ঢাকা
  Largest city of Australia?  ->  Sydney (not the capital)
not used: line 8: a row needs a question and an answer
6 new card(s) for 'capitals'
Add 6 card(s) to 'capitals'? [y/N] y
added 6 card(s) to 'capitals'

$ nexus tutor quiz capitals -n 4
Type your answer. Enter alone: I do not know. Ctrl-D or /stop ends.
[1/4] capitals: Capital of France?
> paris
Right. Back in 1 day.
[2/4] capitals: Capital of Japan?
> tokio
The card says: Tokyo
Were you right? [y/N] y
Right (on your word). Back in 1 day.
[3/4] capitals: Capital of Peru?
>
Skipped. The card says: Lima
It comes back in this session.
[4/5] capitals: Capital of Kenya?
> nairobi.
Right. Back in 1 day.
[5/5] capitals: Capital of Peru?
> lima
Right. Back in 1 day.
Quiz finished: 4 of 5 right.

$ nexus tutor stats
all decks: 6 card(s): 2 new, 4 learning, 0 well known
due today: 0 review(s), 2 new; next review on 2026-11-11
answered (all decks): today 5, last 7 days 5, 80% right over 30 days; 1 day in a row
```

(The run above was made on 2026-11-10.)

## Cards

A card has a question (the front) and an answer (the back). Cards are kept in
decks. A deck is named with letters, digits, spaces, `-` or `_` (at most 40
characters, the first a letter or digit); upper and lower case are the same
deck. A deck exists as long as it has a card.

```bash
nexus tutor add spanish "dog" "perro | el perro"   # one card
nexus tutor import words.csv --deck spanish        # many cards; shown first, then you confirm
nexus tutor list spanish --search perro            # ids, state and due date
nexus tutor edit c12 --back "el perro"             # correct a card; what you learned is kept
nexus tutor forget c12                             # remove a card (asks first; --yes skips the question)
nexus tutor forget --deck spanish                  # remove a deck and what you learned in it
nexus tutor export spanish > spanish.tsv           # question<TAB>answer, one card per row
nexus tutor decks
```

Two cards with the same question in the same deck are the same card. An
import or an `add` never changes a card that is already there: if the new
answer is different, the row is reported and left out, and you change the card
with `nexus tutor edit`.

### Writing the answer

- `perro | el perro`: either answer is right.
- `to run (irregular)`: text in brackets may be left out.
- Upper and lower case, extra spaces and punctuation at the edges of a word
  are ignored: `Paris.` is `paris`.
- Everything inside a word is compared: `-5` is not `5`, `3.14` is not `314`,
  `x+1` is not `x-1`.

### Card files

`nexus tutor import FILE [--deck NAME]` reads these files. Without `--deck`,
the deck is named after the file.

| File | Layout |
|---|---|
| `.csv` | One card per row: question, answer. The separator (`,` `;` or tab) is the one the first row uses. A first row of column titles such as `question,answer` or `front,back` is skipped. |
| `.tsv` | One card per row: question, tab, answer. `nexus tutor export` writes this. |
| `.txt`, `.md` | Either `Q:` / `A:` blocks (a question and an answer may run over several lines; an empty line or the next `Q:` ends the card), or one card per line as `question :: answer` or `question<TAB>answer`. |

In the row and line layouts, a line that starts with `#` and has no answer is
a comment, and columns after the second are not used. In a file of `Q:` / `A:`
blocks, everything outside a block is ignored.

The import shows the new cards (the first 20) and the rows it could not use
(the first 10), each with its line number, and stores nothing until you
confirm. Without a terminal it needs `--yes`.

A file is refused when it is a link, larger than `limits.max_import_bytes`,
not UTF-8 text, has more than 40,000 lines, holds more than 2,000 cards, or
would take the collection over `limits.max_cards`. A side of a card that is
longer than `limits.max_card_chars` or 12 lines, or that looks like a
credential, is left out and reported. Control characters and invisible
formatting are removed from card text when it comes in.

`nexus tutor export DECK` writes the cards only, not what you learned about
them.

## When a card comes back

The tutor uses the SM-2 method (SuperMemo 2). Every card has an ease, which
starts at `schedule.starting_ease` (2.5) and never falls below 1.3.

| Your answer | The pause until the card is due again | Ease |
|---|---|---|
| `again` (you missed it) | none: the card is due again today and starts from the first pause | unchanged |
| `hard` | as for `good`, with the lower ease | minus 0.14 |
| `good` | first right answer: `first_interval_days` (1). Second in a row: `second_interval_days` (6). After that: the last pause times the ease, rounded | unchanged |
| `easy` | as for `good`, with the higher ease | plus 0.10 |

With the default settings a card that is always answered `good` comes back
after 1, 6, 15, 38 and 95 days. From the third right answer on, a pause grows
by at least one day; it never goes beyond `schedule.max_interval_days`.

A card is **new** until its first answer, then **learning**, and **well
known** once its pause is three weeks or more.

The day changes at midnight, by the clock of this machine.

## Quiz

```bash
nexus tutor quiz [DECK] [-n N]      # N questions; without -n: quiz.questions (10)
```

You type the answer and the tutor checks it against the card. `/stop` or
Ctrl-D ends the quiz; what you answered until then is saved.

- The answer matches the card: right. The card is graded `good`.
- Enter alone: you do not know. The card is graded `again` and the answer is
  shown.
- Anything else: the tutor shows the card's answer and asks whether you were
  right. Nothing is recorded until you say yes (`good`) or no (`again`). The
  tutor never decides by itself that a different answer is wrong.

A quiz takes the cards that are due first (the one that has waited longest
first), then today's new cards, then cards that are not due yet (the one that
was asked longest ago first). For a card that is not due yet, a right answer
changes nothing: a quiz never pushes a card further away before its time. A
miss always brings the card back today.

A missed card is asked again at the end of the same session, at most twice.
After that it stays due today.

## Review

```bash
nexus tutor review [DECK] [-n N]
```

The tutor shows a question. You think of the answer, press Enter to see it,
and grade yourself: `1` again, `2` hard, `3` good (or Enter), `4` easy.

A review holds the cards that are due today and then today's new cards, and
nothing else. When nothing is due it says when the next card is.

## The day's limits

- `schedule.new_per_day` (10): how many cards are shown for the first time in
  a day, in quizzes and reviews together. The other new cards wait.
- `schedule.reviews_per_day` (100): how many answers to cards you have seen
  before a review will take in a day. Every answer to such a card that was due
  counts, in a quiz too, and also when a missed card is asked again. A quiz
  itself is not held back by this limit.

The limits are checked when a session starts.

## Statistics

```bash
nexus tutor stats [DECK]
nexus tutor decks
```

`stats` shows how many cards are new, learning and well known, how many are
due today, and, when none is, the next due date. The line about answers
(today, the last 7 days, the share of right answers over 30 days, days in a
row) is counted for all decks together, also when you name a deck. The
counters are kept for 400 days of use.

## Optional: a model

Both switches are off in the shipped file, and then no model is called.

```yaml
model:
  grade: true        # judge quiz answers that do not match the card word for word
  generate: true     # nexus tutor generate
  agent: ""          # agents.yaml name (empty = workspace default)
  allow_cloud: false
```

A cloud agent is used only with `model.allow_cloud: true`. Otherwise the call
is refused, also when a local agent would fall back to a cloud one.
`model.calls_per_hour` (120) bounds the calls; a reply that cannot be used
counts as a call.

### `model.grade`

When a quiz answer does not match the card, the model is asked whether it
says the same thing. It is sent the question, the card's answer and your
answer, and nothing else: no deck name, no ids, no dates.

- The model says it is the same: right, shown as "accepted by the model".
- Anything else (it says no, it does not answer, it is not reachable, the
  hour's calls are used up): you are asked, as without a model.

So the model can only spare you the question "Were you right?". It can accept
a wrong answer; it cannot reject a right one.

### `model.generate`

```bash
nexus tutor generate notes.md --deck biology -n 10
```

The model reads a `.txt` or `.md` file (the first 12,000 characters) and
proposes up to N cards (at most 30). It is sent the text and the number of
cards, not the file name. The proposals are shown and nothing is stored until
you confirm. A proposal whose answer is not word for word in your notes is
marked with `?`: read it before you learn it. Proposals are cleaned like any
other card, and a question that is already in the deck is left out.

## Voice

With `voice.enabled: true`, in `nexus voice run`:

- "quiz me", "quiz me on spanish", "test me on spanish", "start a quiz"
  start a spoken quiz.
- Then everything you say is an answer, until the quiz is over. The tutor
  says "Right" and the next question, or reads the card's answer and asks
  "Were you right? Say yes or no."
- "I don't know", "skip", "pass", "next": the answer is read out and the card
  counts as missed.
- "repeat", "say that again", "again": the question again.
- "stop the quiz", "end the quiz": the quiz ends.

A leading "um", "I think" or "it's" is not part of the answer. When what you
say is the answer on the card, it is taken as the answer, also when it is one
of the words above ("pass", "again").

The questions and replies are spoken by the tutor itself. They are not sent
to the assistant's model and do not become part of the conversation.

While a spoken quiz runs, the tutor is asked before the other spoken commands,
so "remember the Alamo" is an answer and not a note for long-term memory. Only
the pipeline's own commands still come first ("stop everything", a pending
spoken approval).

A spoken quiz is started and answered only by a verified speaker, and not
after the assistant's last answer used untrusted content; otherwise the reply
is "I only run a quiz on a trusted, verified request." and nothing is
recorded. Without a speaker check nobody can be told apart. Both terms are
explained in [voice.md](voice.md#spoken-commands-of-the-opt-in-features).

A spoken quiz that gets no answer for `voice.idle_minutes` (3) ends by itself.
While the tutor waits for yes or no, nothing else you say is taken for an
answer and the waiting time is not extended. A quiz typed at the terminal is
not answered by voice.

Every turn starts with the wake word, every answer too. Push-to-talk or
`wake.enabled: false` in `voice.yaml` is the practical setting for a quiz.

Voice privacy mode does not stop a quiz: asking for one is asking to hear the
cards.

With `model.grade` on, a spoken answer that does not match the card is sent
to that agent like a typed one.

## Slash command

```text
/tutor                      statistics for all decks
/tutor stats DECK
/tutor decks
/tutor quiz [DECK] [N]      then: /tutor a YOUR ANSWER | /tutor skip | /tutor yes | /tutor no
/tutor review [DECK] [N]    then: /tutor show, and /tutor again | hard | good | easy
/tutor stop
/tutor add DECK :: QUESTION :: ANSWER
```

A session goes on from one command to the next and ends with the day. What
the command prints is shown in the terminal and is not sent to the model.

One session runs at a time in a workspace. Starting a quiz or a review closes
the one that was open; its answers are already saved.

## Agent tools

None by default. With `agent_tools: true` the agent gets:

| Tool | Risk | What it does |
|---|---|---|
| `tutor_stats` | READ | Counts per deck and the statistics above. No card text. |
| `tutor_cards` | READ | Cards of a deck (question, answer, state, due date), at most 50 per call. |
| `tutor_add` | WRITE | Adds up to 20 new cards to a deck. You are shown exactly the cards that would be stored and decide. |

There is no tool that grades, edits or removes a card, and none that starts a
session.

- What `tutor_cards` returns goes to the model that runs the session. If that
  is a cloud model, your cards leave this machine.
- Card text is wrapped as untrusted data: a card may come from a file or from
  a model, and what it says is text to show, not an order.
- After `tutor_cards` has run, the session counts as tainted: actions with an
  outside effect (shell commands, `git push`, `gh`, the browser, MCP tools
  that write) need a fresh approval from you and cannot be auto-approved. A
  tool that only reads from outside is not stopped by this (see "Known
  limits").
- Cards added by the agent are stored with `source: agent`, and the addition
  is in the audit trail.

## Where the cards are kept

`<NEXUS_HOME>/tutor/<workspace>/cards.json`, readable by you only (mode 0600),
outside the workspace, so the agent's file tools cannot reach it. Every
answer is saved at once.

With `encrypt_at_rest: true` the file is `cards.json.enc`, sealed with
AES-256-GCM; the key is in the OS keyring (`pip install
"nexus-agent[secure-state]"`). When the keyring cannot be used, the tutor says
so before anything is shown or stored: `nexus tutor status` reports "the
cards cannot be sealed", and commands stop with the same message. Changing
`encrypt_at_rest` later does not convert the file: the tutor stops and tells
you to restore the setting. (To change it anyway, export each deck first and
import it afterwards; the schedule is not carried over.)

A file that was changed by hand and no longer has the expected shape is
reported as damaged and is not written over.

## Policy keys

| Key | Default | Range | Meaning |
|---|---|---|---|
| `enabled` | `false` | | Master switch. |
| `encrypt_at_rest` | `false` | | Seal the card file (needs a keyring). |
| `schedule.new_per_day` | 10 | 0–200 | New cards per day. 0 = no new cards. |
| `schedule.reviews_per_day` | 100 | 1–2000 | Answers to known cards a review takes per day. |
| `schedule.starting_ease` | 2.5 | 1.3–3.5 | Ease of a new card. |
| `schedule.first_interval_days` | 1 | 1–30 | Pause after the first right answer. |
| `schedule.second_interval_days` | 6 | 1–60 | Pause after the second right answer in a row. Not shorter than the first. |
| `schedule.max_interval_days` | 365 | 7–3650 | Longest pause. Not shorter than the second. |
| `quiz.questions` | 10 | 1–100 | Length of a quiz when you give no number. |
| `limits.max_decks` | 50 | 1–200 | |
| `limits.max_cards` | 5000 | 10–50000 | A batch that does not fit is refused as a whole. |
| `limits.max_card_chars` | 600 | 40–2000 | Per side of a card. |
| `limits.max_import_bytes` | 500000 | 1000–5000000 | Size of a card file or a notes file. |
| `model.grade` | `false` | | See above. |
| `model.generate` | `false` | | See above. |
| `model.agent` | `""` | | Agent name; empty = the workspace default. |
| `model.allow_cloud` | `false` | | Allow a cloud agent. |
| `model.max_tokens_per_call` | 900 | 100–4000 | Length of a model reply. |
| `model.calls_per_hour` | 120 | 1–5000 | Model calls per hour. |
| `voice.enabled` | `false` | | The spoken quiz. |
| `voice.idle_minutes` | 3 | 1–30 | A spoken quiz without an answer ends after this. |
| `agent_tools` | `false` | | Give the agent the three tools. |

An unknown key, a value out of range or a credential in the file makes the
whole policy invalid, and then the tutor is off.

## Audit

| Action | When | Recorded |
|---|---|---|
| `tutor.add` | cards were added | who (cli, slash, agent), the deck or file name, how many, the source |
| `tutor.edit` | a card was changed | the card id |
| `tutor.forget` | a card or deck was removed | which, how many |
| `tutor.session` | a quiz or review started, ended, was stopped or replaced | who (cli, slash, voice), the deck, the number of cards or of right and wrong answers |

No question and no answer is written to the audit trail.

## Known limits

- Tested with made-up decks and scripted model replies. The schedule is
  checked against values worked out by hand; it has not been tried over
  months of real use.
- Text cards only: no pictures, no sound, no fill-in-the-gap cards. A card is
  asked in one direction; for both directions add two cards.
- The answer check is literal. A typo, a synonym or `four` for `4` is not
  accepted by itself: you are asked whether you were right, and the tutor
  takes your word.
- A spoken quiz is as good as the speech recognition. Names and words of
  another language are often heard wrong, and then you are asked. It was
  tested with scripted speech recognition, not with a microphone.
- The counters of answers per day are kept for all decks together.
- One session at a time in a workspace.
- `export` writes the cards without their schedule, and `import` reads text
  files only (no Anki `.apkg`).
- An exported file is plain text. A spreadsheet program reads a card that
  begins with `=` as a formula.
- Nothing reminds you that cards are due: there is no background job.
- With `model.grade`, the model can accept an answer that is wrong.
- With `agent_tools`, card text goes to the session's model. A later
  read-only tool call that reaches outside (for example a read-only MCP tool)
  is not blocked by the approval gate.
- Tested on Linux only.

## Tests

```bash
python -m pytest tests/test_tutor.py -q
```

The tests are offline: a clock moved by hand, an in-memory keyring and
scripted model replies.
