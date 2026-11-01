# Role Hub — one thousand roles, one reviewed editor

Prompt 3 of the dashboard build. The roster ships ~1000 roles; `/api/roles`
returns all of them as one 4.4 MB document, which the old Workbench pane could
not render or search. The hub replaces that with a paged index, a single-role
detail call, a versioned edit trail and an offline routing preview.

Code: `nexus/rolehub.py` (service), `nexus/server.py` (routes),
`nexus/panel/rolehub.js` + `shell.css` (page), `nexus/gui_config.py` (records the
replaced layer), `tests/test_rolehub.py`, `tests/browser/roles.test.mjs`.

## Read surfaces

| endpoint | returns |
| --- | --- |
| `GET /api/roles/index?q=&category=&agent=&source=&sort=&dir=&page=&size=` | one page of rows (50 default, 200 max), roster stats and the filter vocabulary |
| `GET /api/roles/detail?name=` | persona, instructions, weighted keywords, aliases, tools, focus, model, temperature, provenance, teams that use it, config revision |
| `GET /api/roles/quality` | static findings with severities, and what the checks do **not** cover |
| `GET /api/roles/history` | versions of the GUI-managed role layer, newest first |
| `GET /api/roles/diff?version=&name=` | unified diff of one role (or the whole layer) against a version |

`/api/roles` is untouched: the legacy panel and its tests still read it.

The index never ships prompt text — a row is `{name, title, category, agent,
keywords, aliases, tools, persona_chars, model, source, override, updated_at}`
and a full page is ~15 KB. Search, filter, sort and paging all happen on the
server, so a 1000-role roster costs one small request per interaction (8 ms
cold, 19 ms for a text search on this machine).

## Actions

| endpoint | consent |
| --- | --- |
| `POST /api/roles/route` | none needed — it only scores keywords |
| `POST /api/roles/export` | none needed — read-only YAML of 1–60 roles |
| `POST /api/roles/restore` | typed `RESTORE ROLES <version>` |

Every action is audited on **every** call (`rolehub.route`, `rolehub.export`,
`rolehub.restore`); the read surfaces are audited once per client per five
minutes so a browse session cannot flood the log. Reads are limited to
120/60 s per client, restores to the same budget with their own bucket, and a
refusal returns `{ok:false, error:{code,message}}` (`rate_limited`,
`invalid_request`).

Editing is deliberately **not** a new write path. The page posts to the existing
`/api/workbench/configuration` with its revision check and typed
`SAVE ROLE <name>` / `REMOVE ROLE <name>` confirmation, so the same validation,
atomic write and rollback that the Workbench uses apply here too.

## Versions

`Configuration.save()` now records the file it is about to replace:
`<workspace>/.nexus/rolehub/history/<UTC stamp>-<sha6>.yaml` plus one line in
`index.jsonl`. A first creation replaces nothing and therefore records nothing —
that is stated in the UI instead of showing an empty "previous version".

Restoring writes the snapshot back through the same atomic replace, reloads the
configuration, re-records the pre-restore file (so a restore is undoable too),
and refuses anything but an exact `RESTORE ROLES <id>` while the current
revision matches. Snapshots are bounded to the newest 20 and the store is
runtime state: `.nexus/rolehub/` is git-ignored.

## Quality checks

Honest and static — no model calls: missing persona, very short or very long
persona, unknown agent, roles with no keywords (never keyword-routed), alias
that collides with another role, alias used by two roles. The panel prints the
`not_checked` list (`prompt quality`, `model fit`, `cost of a run`) next to the
findings so a clean report is not mistaken for a good prompt.

The shipped roster currently reports 7 high findings (alias collisions in the
imported packs) and one note (the `general` role intentionally has no keyword).

## Route preview

`POST /api/roles/route {task}` runs `Router.keyword_route()` with
`providers=None`: the same first-stage scoring a real run uses, no provider is
constructed, `provider_calls: 0` is reported in the response and the panel shows
it. An empty match is reported as "nothing reaches the threshold" with the
scored shortlist, not as a guess.

## Page behaviour

- Virtualised list: a fixed 46 px row, a spacer as tall as the page, and only
  the visible window in the DOM. `role="listbox"` + `aria-activedescendant`;
  ↑/↓/PageUp/PageDown/Home/End move, Enter opens.
- Tabs: Role, History, Quality, Route preview — arrow keys move between them.
- A language switch repaints the stats, the read stamp and every computed
  string from the last answer instead of waiting for another request.
- Saving a role whose name falls on another page keeps its form open (a jump to
  an unrelated first row would be a lie about what was just saved).
- An in-flight detail response can never overwrite a newer selection or the
  New-role form.
- Every read carries `observed_at`; the footer prints
  `Read from the server at HH:MM:SS`.

`window.nexusRoleHub = {load, root, state(), open, switchTab, previewRoute,
history, quality}`; `state()` reports counts, the visible names, the open role
and the last revision token.

## Known limits

- Bulk import of a YAML pack is not built: export is, import needs a reviewable
  merge that Prompt 13's QA pass owns.
- The editor is a textarea with a monospace font, not Monaco (no bundler, must
  work offline; see the inventory decision 4).
- Version history covers the GUI-managed layer (`roles.d/zz-gui-managed.yaml`)
  only. Committed packs are content, not user edits.
- The quality checker does not judge prompt wording, model fit or cost — it
  says so on screen.
- Restoring is per layer, not per role: a snapshot contains every role the
  managed layer held at that moment.
