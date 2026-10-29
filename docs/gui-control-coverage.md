# Original dashboard — control coverage audit

Updated 2026-10-30, desktop-first follow-up. **The goal of every agent control being GUI-managed is not yet
fully met.** This table distinguishes working native controls from remaining
terminal-only setup. There is no replacement dashboard, iframe panel or extra
server. Open **Control center** (default desktop landing) or **Settings → Overview → Dashboard control shortcuts** to discover
existing controls. These shortcuts navigate; they do not start tasks/services.

| Capability | Native original-dashboard surface | Status / safety boundary |
|---|---|---|
| Chat, role/model routing, task lifecycle | Run & chat, composer, activity | Available; existing task/approval behavior retained |
| Roles, model profiles, tool modes, Red/Blue teams, ordered pipelines | Roles, agents & teams | Available; revision/schema checks and explicit save confirmation |
| Provider credentials | Original API Keys card | Supported-provider key setup; GitHub remains environment/.env-only |
| Optional GitHub attachment | Run repository bar | Off/deselect persists; reconnect requires selection; does not revoke token/cancel old work |
| GitHub task branch, PR and merge workflow | Existing Run/repository workflow | Available with permission gates; external/destructive approvals unchanged |
| Queued local work, worker start/pause/resume/stop/kill | Operations & approvals | Available; starting a worker is not execution approval |
| Local model availability/install and execution settings | Workbench models / Operations | Available; local execution requires explicit setup; no paid/cloud fallback implied |
| Model routing/safety policy, planning/history, diffs | Workbench policy/runs/files | Available; shortcuts reuse the original panes |
| Autonomy, quiet/DND, usage, learning, diagnostics | Operations & approvals | Available; no second learning store; no destructive-action approval bypass |
| Studio workflow configuration | Original Studio dialog | Available; original workflow store retained |
| Bangladesh news via RSS | World default Bangladesh tab; News & weather settings | Eight Bangladesh publisher choices, original URLs/times when supplied; source failures visible |
| WeatherAPI forecast, city lookup and key setup | World; News & weather settings | Available; server-side key, explicit confirmation; missing key/plan limits visible |
| OS user-service registration | Desktop setup → OS service | Native preview/download and exact manifest-confirmed install via existing installer; uninstall/recovery remains OS-managed; registration is not proof of running |
| Context activation/privacy policy | Desktop setup → Context & privacy | Native policy/signal/TTL/blocklist editing; stays OFF by default, no automatic collection; screen/cloud vision still separately reviewed |
| MCP/plugin endpoint enrollment | Desktop setup → MCP & plugins | Native reviewed declarations, schemas, budgets and secret references; save/discovery executes nothing; plugin source editing remains file-based |
| Telegram enrollment/policy | Desktop setup → Telegram policy | Native whitelist/limits/export policy; credentials remain environment-only; foreground transport lifecycle remains CLI-managed |
| Environment/native-vault credential revocation | Environment/OS/provider tools | Clearing a session key is not global credential deletion/revocation |

See [desktop setup, safety and remaining limits](desktop-control-center.md). No mobile-specific work is included in this desktop update.

## Validation boundary

Provider adapters/parser/security/settings are tested with explicit mock responses;
actual-server browser tests exercise real authenticated settings persistence and
native navigation. Only the public news/weather *display* in the new settings
browser test is scripted, explicitly labelled as test data. There is no fake
production headline/forecast fallback. Real external RSS availability, account
plan access and Windows physical desktop/vault behavior were not verified here.
See [World setup and provider terms](world-widget.md) and [GUI guide](gui.md).
