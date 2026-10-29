# Operations are now inside the original Nexus GUI

Use **`nexus gui -C .` → http://127.0.0.1:8787/dashboard → Operations & approvals**.

There is no additional HUD, console, panel dashboard or Textual interface to
launch. Original Run/chat/models/repositories/settings, background operations,
approvals and World/news/weather are native sections of the same GUI, using one
server, workspace and optional login. No iframe or link to another dashboard.

`nexus hud`, `nexus panel` and deprecated `nexus hud --tui` are compatibility aliases
for that original GUI. Do not follow older port-8790/NEXUS_HUD_TOKEN setup guides.
The main GUI uses `NEXUS_SERVE_TOKEN` and optionally a native `nexus-gui` vault
credential and remembered browser session. `hud.yaml` is legacy adapter config,
not the main GUI server configuration.

See **[GUI launch, Windows setup, security, screenshots and testing](gui.md)**,
[delegation safety](delegation.md), and [World/privacy/source terms](world-widget.md).
