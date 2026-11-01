# Professional dashboard

`nexus gui` serves one calm, professional workspace for Control center, Run & chat, Operations, World, and every native Workbench, repository and Studio dialog. The overhaul is presentation only: the same controls, endpoints, approval gates, workspace configuration and honest-data rules remain in force. No separate dashboard or mobile build is introduced.

## Layout and navigation

- **Persistent sidebar** (268px) carries the brand, the four sections, appearance preferences and Forget login.
- **Sticky top bar** holds the breadcrumb, configuration chips (workspace, roles, agents, live mode, uplink), the clock and one consistent tool group: Workbench, Studio, Roles, Settings, then the light/dark toggle. Feature scripts append their buttons to the header; `shell.js` normalises them into that single group so nothing lands outside the bar.
- **Run & chat** is a three-column deck that fills one screen: run summary, agents and topology on the left; approval, live activity and the composer in the centre; telemetry, throughput, pipelines, memory, brain, integrations and keys on the right. Each column scrolls on its own; the composer and approval card never leave the viewport.
- **Narrow screens** turn the sidebar into a bottom tab bar with short labels (Control / Run / Approvals / World), keep the tool icons in the header, and stack the deck so activity and the composer come first.
- **Light and dark themes** share one token set. The header toggle is an explicit choice and stores `nexus.ui.theme` in local storage only.

## Finding and opening controls

- **Category chips** combine with text search and **Favourites**. The result count shows how many of the 18 controls match. Contextual empty states explain how to recover.
- **Clear filters** restores all controls and focuses search.
- **Stars** store predefined control titles in browser-local `nexus.ui.favourites`. Unknown titles are ignored; malformed or blocked storage cannot prevent using controls. Favourites apply across workspaces in that browser, not across devices/accounts.
- **Compose a task** opens the original composer and focuses the existing draft without executing it or clearing it.
- **Review approvals** opens the original approval list without approving an action.

## Design system

One token sheet drives colour, spacing, radius, type and elevation. Neutral slate surfaces, a single emerald accent (`#2dd4a4`, darker `#0e9f74` in light mode), 1px hairlines instead of glows, uppercase micro-labels and an Inter/system stack with Bengali fallbacks for the World labels. The previous namespace tokens (`--neon`, `--cyan`, `--edge`, `--panel`, …) are kept as aliases so older module sheets and user overrides keep resolving.

Live charts read `--chart-1/2/3` at draw time and redraw on the `nexus:theme` event, so telemetry follows the theme instead of pinning neon rgba literals.

## Appearance and real state

**Compact layout** reduces spacing. **Reduce effects** disables decorative CSS animations and transitions. OS reduced-motion preferences also take precedence. These two settings store booleans locally; no credentials or workspace policies are stored. Forget login keeps these non-secret appearance preferences. Existing globe rendering/interaction controls are separate: reducing CSS effects is not a worker-stop or global GPU-pause operation.

The configuration snapshot mirrors the existing GUI connection status, registered role count and configured agent count using local DOM observations, with no new polling. Counts are not endpoint health, active workers or model availability. Updated values receive a brief visual highlight. All sections share readable cards, focus rings, native settings surfaces, category icons and short entrance transitions. There are no invented metrics or remote font dependencies.

## Validation and screenshots

`tests/browser/appearance.test.mjs` covers the sidebar/top bar shell, combined filters, empty states, favourite persistence, preserving a composer draft, navigation-only quick actions, appearance persistence, reduced motion, layout overflow and malformed/blocked storage. Run it alongside the existing unified, desktop and management regressions against a disposable authenticated workspace:

```sh
cd tests/browser
NEXUS_GUI_TEST_TOKEN="$NEXUS_SERVE_TOKEN" node --test --test-concurrency=1 appearance.test.mjs unified.test.mjs desktop.test.mjs management.test.mjs
```

Actual local-GUI captures, not mockups or live-service verification:

![Control center](images/gui-smart-control.png)

[Run & chat](images/gui-smart-run.png) · [Operations](images/gui-smart-operations.png) · [World](images/gui-smart-world.png) · [Settings](images/gui-smart-settings.png)

Unavailable feeds and credentials remain explicitly unavailable. No model call or real service registration/removal is required by the visual tests.

Validation on this branch, against a real `nexus gui` server with a disposable authenticated workspace:

- `tests/browser/dashboard.test.mjs` — **31 / 31 passed** (approvals, cancellation, repository review, Studio, Workbench, compact composer, mobile widths).
- `appearance` + `desktop` + `world-settings` + `management` + `desktop-runtime` — **6 / 6 passed**.
- `unified.test.mjs` — **6 / 7 passed**. The remaining case pins the World language fixture to English while this workspace's `widget.yaml` ships `language: bn`, so the assertion fails identically on the baseline commit; it is a fixture expectation, not a rendering regression.
- `pytest tests` — **1364 passed, 1 skipped, 3 failed**. All three failures pre-date this work and reproduce on the baseline commit: two need optional OCR packages (`pytesseract`) and one validates committed importer packs.
- `ruff check nexus tests` and `node --check` on every edited script are clean.

These are real-server UI tests with a disposable workspace; they do not claim live provider credentials or physical Windows service validation.
