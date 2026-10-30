# Operations are now inside the original Nexus GUI

Use **`nexus gui -C .` → http://127.0.0.1:8787/dashboard → Operations & approvals**.

There is no additional HUD, console, panel dashboard or Textual interface to
launch. Original Run/chat/models/repositories/settings, background operations,
approvals and World/news/weather are native sections of the same GUI, using one
server, workspace and optional login. No iframe or link to another dashboard.

`nexus hud`, `nexus panel` and deprecated `nexus hud --tui` are compatibility aliases
for that original GUI. Do not follow older port-8790/NEXUS_HUD_TOKEN setup guides.
The browser suites `tests/browser/hud.test.mjs` and `tests/browser/world.test.mjs`
drive the retired standalone page (its `#token`/`#connect` form and port 8790)
and are therefore not part of the regression set; the same behaviour is
covered by `tests/browser/unified.test.mjs` (auth, sockets, scrolling),
`tests/browser/world-settings.test.mjs` (Bangla toggle, layers, weather) and
`tests/browser/shell.test.mjs` (shell, palette, phone layout).
The main GUI uses `NEXUS_SERVE_TOKEN` and optionally a native `nexus-gui` vault
credential and remembered browser session. `hud.yaml` is legacy adapter config,
not the main GUI server configuration.

See **[GUI launch, Windows setup, security, screenshots and testing](gui.md)**,
[delegation safety](delegation.md), and [World/privacy/source terms](world-widget.md).
