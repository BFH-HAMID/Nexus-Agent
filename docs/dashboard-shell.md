# Dashboard shell — navigation, command palette, Mission Control

Prompt 1 of the dashboard build. Everything here is served by the existing GUI
server (`nexus gui`) at `/dashboard`; there is no second application, no CDN and
no build step. All assets are local files under `nexus/panel/`.

## Sections

`nexus/panel/nav.js` owns the registry. Thirteen rows, two groups (`operate`,
`configure`), each row either switches a `data-gui-view` page, deep-links inside
the Workbench/Studio drawers, or opens a dialog:

| id | label (en / bn) | target |
| --- | --- | --- |
| `mission` | Mission Control / মিশন কন্ট্রোল | `view-mission` |
| `run` | Run & chat | `view-run` |
| `roles` | Roles & router | `view-roles` (Role Hub, Prompt 3) |
| `computer` | Computer Control | Workbench → *desktopSetup* |
| `workflows` | Workflows | Studio → *Workflows* |
| `approvals` | Approvals | `view-operations`, anchor `#reviews`, badge |
| `security` | Security & logs | `view-operations`, anchor `#audit` |
| `memory` | Memory & knowledge | `view-run`, anchor `#memCard`, focuses `#brainQuery` |
| `cost` | Cost & models | Workbench → *models* |
| `world` | World & news | `view-world` |
| `integrations` | Integrations | `view-run`, anchor `#integrations` |
| `controls` | All controls | `view-control` (18 cards) |
| `settings` | Settings | Workbench → *layout* (opened by `#guiSettings`) |

Keyboard: `Alt+1` … `Alt+4` = Mission Control, Run, Approvals, World;
`Alt+0` = All controls. The hash (`#run`, `#mission`, `#operations`, `#world`,
`#control`) is a deep link and is kept in sync both ways.

Accessibility: at desktop widths `#sidebarNav` is a `role="tablist"` and each row
is a `role="tab"` with `aria-selected`; at ≤1080 px those roles move to the
bottom `#tabbar`, and the closed drawer is `inert` so it cannot be tabbed into.
The approval badge exposes a stable `aria-label` with the count in
`aria-description`, so its accessible name never changes under a test or a
screen reader.

## Command palette

`Ctrl+K` or `Ctrl+Shift+P` (global). `Ctrl+Alt+K` is the older Workbench-only
list and stays available inside the Workbench drawer. Modes:

* `>` actions (compose, refresh, appearance toggles, kill, …)
* `@` roles, `#` tasks, `/` pages
* plain text = fuzzy over roles, agents, pipelines, pages and actions

`Enter` opens in the current page, `Shift+Enter` opens a new panel (a tab),
`Ctrl+Enter` pins, recents and pins live in `localStorage`
(`nexus.ui.palette.recents`, `nexus.ui.palette.pins`).

Index source: **`GET /api/palette`** (`nexus/palette.py`) returns a compact
projection of the whole roster — `{built_at, elapsed_ms, default_agent, roles[],
agents[], pipelines[]}`. With 1000 roles it is ~14 KB and fuzzy search over it
takes 2–3 ms, which is why the palette does not fetch `/api/roles` (4.4 MB).
The endpoint needs the same token as every other `/api` route, is rate limited
per client (`palette:<ip>`, 120 requests / 60 s), writes one audit line
(`palette.index`) and refuses to answer (`503 audit_unavailable`) rather than
serve without an audit trail. Any failure degrades gracefully: the palette
falls back to the five built-in pages instead of disappearing.

## Mission Control (landing page)

`#view-mission` is the default view. Every tile states where its number came
from and when it was read: `state read 3s ago · lock held: no`, `GPU:
Unavailable`, `Reachability not checked`, `Read from the server at 10:31:02`.
Nothing is invented: when a source is missing the tile says so. Prompt 2
replaced the polling loop with one summary call plus the global feed — see
`docs/dashboard-mission.md`.

Tiles: `#msDaemonState #msDaemonAge #msDaemonHint #msTasksActive #msTasksQueued
#msTasksFailed #msApprovals #msApprovalTone #msCpu #msRam #msDisk #msGpu
#msTokensIn #msTokensOut #msCost #msAgents #msAgentsHint #msFeed
#msActivityMeta`, plus the health strip and `#missionUpdated`.

Quick actions: *Compose task* only focuses `#taskInput` (it never submits),
*Review approvals* jumps to the review queue, *DND 60 minutes* posts
`/api/operations/action/delivery {quiet: true, minutes: 60}`, *Open last run*
opens the newest recorded run, *Refresh* re-reads everything.

## Emergency kill

`#emergencyKill` opens `#killDialog`: step 1 explains what the kill does, step 2
requires the word `KILL`. Only then does `#killConfirm` post
`/api/operations/action/control {action: "kill"}` and cancel every
`queued | running | awaiting_approval` task. Opening the dialog never runs
anything; cancelling leaves the daemon untouched.

## Script contracts

```js
window.nexusNav     = {sections, go(id), active, setActive(id), detail};
window.nexusPalette = {open, close, search(text), lastMs, count, data, loadIndex};
window.nexusMission = {load, root, state(), streamUrl()};   // Prompt 2: see docs/dashboard-mission.md
window.nexusShell   = {setNav(open), closeNav};
window.nexusToast   = (message, {tone, title, timeout}) => void;
window.nexusI18n    = {t(key, vars), setLang(lang), missing()};
```

`nexusI18n.missing()` returns every key used in the DOM that has no Bangla
string; the shell test asserts it is empty, so a half-translated label cannot
ship. In Bangla the bundled Bengali face leads the font stack (see the *Bangla
type* block in `shell.css`) — otherwise Latin-only substitutes render Bengali as
`▯▯▯`.

Server helpers used by the new endpoints live in `nexus/webapi.py`: `ok()`,
`failed()` (with the legacy `error_message` key kept for older clients),
`Limiter`, `audit_api()`, `read_audit()` and `audit_path()`.

## Tests

* `tests/test_dashboard_shell.py` — palette projection size/content, secret
  redaction in the audit file, limiter window, auth/rate-limit/503 behaviour.
* `tests/browser/shell.test.mjs` — sections and hash deep links, palette speed
  with 1000 roles, "compose never runs", typed kill, 360 px phone, Bangla
  switch, bell and status bar.
* `tests/browser/dashboard.test.mjs`, `appearance.test.mjs`, `desktop.test.mjs`,
  `desktop-runtime.test.mjs`, `unified.test.mjs`, `world-settings.test.mjs`,
  `management.test.mjs` keep the pre-shell behaviour honest (they now start on
  Mission Control and navigate to Controls themselves).

Known limits: the palette index is rebuilt per request (cheap at 1000 roles, but
prompt 3 will memoise it behind a version counter); Mission Control reads
`/api/operations/state` on demand rather than from a pushed stream (prompt 2
adds the global SSE feed); the docs screenshots in `docs/images/` still show the
pre-shell layout and are refreshed in prompt 13.
