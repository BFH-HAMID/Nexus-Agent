# Computer Control — per-capability trust, reviewed file writes, one panic latch

Prompt 5 of the dashboard build. The workspace already shipped guarded desktop
adapters in `nexus/computer.py` (headless browser with an exact origin allowlist,
Office reader/writer, VS Code launcher, Docker shell backend) and a
`nexus/computer_control.py` emergency stop. What was missing was one page where
an operator can **see** every capability's real state, lower the two surfaces the
dashboard may own, write a file with a typed token, and stop everything at once.

Two rules shape the whole page:

* **Narrowing only.** The studio store can lower what `permissions.yaml` and the
  adapter opt-ins already allow; it can never raise them. A capability disabled
  in `permissions.yaml` stays disabled here whatever this store says.
* **No second execution path.** Terminal, Office, the screen, the app launcher and
  the VS Code bridge are *reported* from their real policy and probed from the
  real environment. The page never runs a command, launches an app or drives the
  screen. Files and browser inspection are the only surfaces it executes, and
  every mutation carries its own exact typed confirmation.

Code: `nexus/computerstudio.py` (service and store), `nexus/server.py` (routes),
`nexus/panel/computer.js` + `shell.css` (page), `tests/test_computerstudio.py`,
`tests/browser/computer.test.mjs`.

## The policy store

Everything lives under `<workspace>/.nexus/computer/` — workspace runtime state,
git-ignored, never part of a role pack:

| path | contents |
| --- | --- |
| `settings.json` | `{schema, profile, levels, scopes, updated_at}`, mode `0600` |
| `history/<UTC>-<sha8>.json` | pre-change settings with the reason, newest 20 kept |
| `trash/<id>-<name>` + `<id>.json` | moved files with their original path, time and size |

Writes are atomic (temp file + `os.replace`) and the store is read through a
small cached loader; a damaged file reads as `default_settings()` (everything
off) instead of stopping the page. The revision is
`sha256(profile, levels, scopes)[:16]`; every settings write must quote the
current one, so a stale tab cannot overwrite a newer decision.

The defaults are **locked**: `files: off`, `browser: off`, profile `locked`.

## Levels, profiles and the ceiling

`files` and `browser` are the two owned capabilities. Each has a level:

| level | meaning |
| --- | --- |
| `off` | the page refuses to read or write; lists render their off explanation |
| `read` | tree, preview, snapshots and trash are readable; mutations are refused |
| `ask` | every mutation needs its exact typed token (the review-first default) |
| `auto` | mutations are allowed, still with the same typed token |

Three profiles write a whole level set at once — `locked` (`off`/`off`),
`read-only` (`read`/`off`) and `review` (`ask`/`read`). A profile is a shortcut,
not a policy of its own: the individual selectors stay editable before saving.

`effective(state, capability)` returns the **lower** of the store level and the
`permissions.yaml` ceiling (`computer.browser`, `computer.editor`,
`computer.office`, `computer.shell_backend`). The panel shows the resulting level
and names the cause when the ceiling is what limits it.

The other five capabilities are read-only facts, each with a real probe:

| capability | source |
| --- | --- |
| `files` | studio store ∩ `permissions.yaml` |
| `browser` | `computer.browser` + allowlisted origin + studio origins + Playwright + worker probe |
| `terminal` | `computer.shell_backend` and the configured backend |
| `office` | `computer.office` and the installed reader/writer |
| `screen` | `DISPLAY` / `WAYLAND_DISPLAY` — otherwise "Unavailable", never "Off" |
| `apps` | desktop session detection |
| `vscode` | the `code` CLI on `PATH` |

## Endpoints

Reads (GET, `computer:` bucket, audited once per five minutes):

| endpoint | returns |
| --- | --- |
| `/api/computer/state` | capability matrix, policy, levels, scopes, revision, latch, queue, local-only flag |
| `/api/computer/files?path&depth` | one directory level (or `depth` up to 4), capped at 400 entries |
| `/api/computer/trash` | trash entries with original path, time and size |
| `/api/computer/snapshots` | snapshot versions touching the workspace, newest first |
| `/api/computer/browser` | `{available, level, enabled, origins, allowlist, reason}` |

Writes (POST, `computer-write` bucket at 120 per 60 s, typed token, revision
where a whole document is replaced):

| endpoint | body | consent |
| --- | --- | --- |
| `/api/computer/settings` | `{profile, levels, scopes, revision}` | `SAVE COMPUTER POLICY` |
| `/api/computer/file/preview` | `{path}` | none — read-only, `read` level |
| `/api/computer/file/write` | `{path, text, revision}` | `WRITE <path>` |
| `/api/computer/file/undo` | `{id}` | `RESTORE <id>` |
| `/api/computer/file/trash` | `{path}` | `TRASH <path>` |
| `/api/computer/trash/restore` | `{id}` | `RESTORE <id>` |
| `/api/computer/browser/open` | `{url}` | none extra — `read` level, origin must be allowlisted |
| `/api/computer/panic` | `{confirmation}` | `PANIC STOP` |
| `/api/computer/resume` | `{confirmation, resume_queue}` | `RESUME COMPUTER` |

Anything else under `/api/computer/` is a 404, an over-budget client gets 429
`rate_limited`, bad input 400 `invalid_request`, and a POST-only route reached by
GET is a 404 — all as `{ok:false, error:{code,message}}`. Responses are redacted
against the workspace secret values; no token, path outside the scope roots or
key ever reaches the browser.

## Files

`tree()` walks one level (or `depth` levels) under a resolved scope root,
skipping `.git` internals, symlinks that leave the workspace and any path that
resolves outside a root. Each row is `{name, path, kind, depth, size, modified,
hidden}`. Dotfiles are listed with `hidden: true` — an operator can see and edit
`.gitignore`, and the browser test relies on it.

`preview()` returns at most 120 000 characters plus `revision`
(`sha256(text)[:16]`, or `missing`), `binary`, `size` and `truncated`. A write
must quote the revision it saw; a stale revision is a 400 `changed since
previewed` refusal rather than a silent overwrite.

`write()` (max 400 000 characters) runs the text through
`github_safety.scan_text`, records a `Snapshots.change(..., 'write_file')` entry
so the change can be undone, then writes atomically with mode `0600`. `undo()`
restores that snapshot through the existing lineage-checked `Snapshots.undo()`.
`trash()` moves the file into the studio trash (100 entries) and `restore()`
moves it back, refusing to overwrite a file that appeared in the meantime.

## Browser inspection

`browser_view()` answers *why* the surface is unavailable, in cause order:

1. `computer.browser` is not enabled in `permissions.yaml`;
2. no origin is allowlisted there;
3. the allowlisted origin is not in the studio's origin list;
4. Playwright cannot be imported;
5. the browser worker probe fails.

Only then is `available` true. `browser_open()` fetches one allowlisted origin
with the existing worker, returns title, final URL, status and at most 24 000
characters of text, and never follows a redirect off the allowlist. On this host
it honestly reports Playwright missing rather than pretending a browser exists.

## Panic and resume

`panic(state, {confirmation: 'PANIC STOP'})` returns
`{latch, queue_paused, sessions_cancelled, daemon, daemon_error}`:

* sets the process-wide `EmergencyStop` latch;
* cancels every `running` / `awaiting_approval` session through its own `cancel`
  event and reports how many;
* pauses the mission queue (`state.queue_paused = True`);
* asks the daemon to stop (`Operations.action('control', {action: 'kill'})`) and
  reports `daemon_error` when that backend is not available — it does not
  pretend the process died.

`resume(state, {confirmation: 'RESUME COMPUTER', resume_queue})` clears the latch.
The queue is only unpaused when `resume_queue` is exactly `true`, so resuming
inspection never silently restarts a queue the operator paused. A non-boolean is
refused. Both actions require the server on a writable store; demo mode refuses.

## UI

Five tabs (`overview`, `permissions`, `files`, `browser`, `panic`) as an ARIA
tablist with Arrow/Home/End support. The overview lists the seven capabilities
with their real level and source, the policy facts from `permissions.yaml`, the
scope chip, the latch and the read time; no capability is shown as "Off" when it
is really unavailable.

The permissions tab holds the profile select, the two level selects, the roots
and origins textareas and a single typed confirmation for the whole save. The
files tab is a scoped tree on the left and a preview/editor on the right, with
the snapshot and trash lists underneath; every mutation opens the same inline
confirmation row that names the exact token it expects.

Desktop is two columns at ≥1080 px and a single column at 360 px; all labels
exist in English and Bangla (124 `computer.*` keys per language) and the page
re-paints on a language switch without refetching. The page never widens the
document (`scrollWidth − innerWidth = 0`).

## Manual test

```bash
.venv/bin/python -m nexus gui --port 8787 --no-open --host 0.0.0.0
# open http://127.0.0.1:8787/dashboard, log in with the serve token
```

1. Sidebar → **Computer Control**. The overview shows seven capabilities with
   their real levels, `Workspace files: Off`, and the read time.
2. Permissions tab → choose levels `files: ask`, `browser: off`, press **Save**.
   Nothing is written until `SAVE COMPUTER POLICY` is typed exactly; a wrong
   token leaves the revision unchanged.
3. Files tab → open `tests/browser`, click a file to preview it; the footer shows
   its size and revision.
4. Edit the text, press **Write**: the confirmation row names
   `WRITE tests/browser/<file>`. A wrong token is refused; the right one saves
   and the snapshot list gains an `applied` entry.
5. Press **Undo** on that snapshot with `RESTORE <id>`; the previous content is
   back and the file itself reports the restored text.
6. Press **Trash** with `TRASH <path>`; the file disappears from the tree, the
   trash list records its original path, and **Restore** brings it back.
7. Browser tab → the reason is named (here: Playwright missing) and **Inspect**
   is disabled; no browser is launched.
8. Panic tab → `PANIC STOP` latches; the header chip says **Latched** and the
   queue shows **Paused**. **Resume** stays disabled until `RESUME COMPUTER` is
   typed; with *resume the mission queue* ticked the queue restarts.
9. Switch to Bangla; the page translates. Shrink to 360 px; the layout is one
   column and the document does not scroll sideways.

## Tests

* `tests/test_computerstudio.py` — 28 tests: default store, revision and stale
  writes, level/profile validation, narrowing against `permissions.yaml`,
  capability matrix, tree/preview/write/undo/trash/restore round trips,
  scope refusals, panic and resume semantics, audit throttling, and the HTTP
  surface (401, 404, 429, POST-only, redaction).
* `tests/browser/computer.test.mjs` — 7 tests: policy painted from the real
  store, typed save with the exact token, scoped files written and undone through
  the UI, trash and restore, the honest browser reason, panic/resume with the
  queue checkbox, and Bangla at 360 px.

## Known limits

* Only `files` and `browser` are owned by this page; terminal, Office, screen,
  apps and the VS Code bridge are reported, never driven.
* Browser inspection needs Playwright *and* an origin allowlisted in both
  `permissions.yaml` and the studio; on a host without Playwright it stays
  unavailable by design.
* Writes are capped at 400 000 characters and are recorded as snapshots, so the
  workspace history grows with each save (20 versions kept per file).
* The store is per workspace and not synced between machines.
* Panic cancels running sessions and pauses the queue; killing the daemon is
  delegated to the Operations backend and reported as `daemon_error` when that
  backend is not reachable.
