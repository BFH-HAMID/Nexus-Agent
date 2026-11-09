# Proactive Assistant (`nexus/jarvis/proactive.py`)

Nexus's proactive assistant **proposes** actions — morning briefs, voice reminders,
monitoring alerts, DND/focus mode, and channel routing — but **never executes**
without following the existing `autonomy.yaml`, `permissions.yaml`, `Queue`, and
`ApprovalGate` policies. All features are **OFF by default** and are enabled
strictly through the operator-owned `proactive.yaml`.

Text from emails, calendar events, RSS feeds, web endpoints, or logs is untrusted
data and is never executed as an instruction or used to change permissions.

## Safety & Default-OFF Policy

`proactive.yaml` at the workspace root controls proactive behavior and is **OFF
by default**:

```yaml
schema: 1
enabled: false
timezone: ""
```

`proactive.yaml` is protected by `Permissions.operator_control()` alongside
`permissions.yaml`, `autonomy.yaml`, `voice.yaml`, and `chief.yaml`: agents
cannot write, edit, or delete it via workspace tools. Setting `enabled: true`
is an explicit operator edit.

## Capabilities

### Morning brief (`morning_brief`)

Delivers a spoken/HUD/Telegram summary at a scheduled time or on greeting.
Sources include news, weather, calendar, email (read-only), tasks, CI status,
and system health — all read-only, all local-first where possible.

```yaml
morning_brief:
  enabled: false        # flip to true to opt in
  time: "08:00"
  deliver_on_greeting: true
  deliver_on_schedule: true
  spoken_max_chars: 320
  news_topics: []
  max_news_items: 5
  include_weather: true
  include_calendar: true
  include_email: true
  include_tasks: true
  include_system: true
  include_ci: true
  ci_repo: ""
  ci_ref: "HEAD"
```

### Voice reminders (`voice_reminders`)

Speaks scheduled reminders when the user is present.

```yaml
voice_reminders:
  enabled: false
  default_recurring_target: "queue"
```

### Monitors (`monitors`)

Passive watches on auth logs, fail2ban, UFW, listening ports, users, cron,
systemd, file integrity, certificates, dpkg, Python packages, and CVE feeds.
All passive reads only — no remote fetching, no exploitation.

```yaml
monitors:
  enabled: false
  items: []
```

### Suggestions (`suggestions`)

Proactively suggests actions based on context, rate-limited to avoid speech
spam.

```yaml
suggestions:
  enabled: false
  max_speech_per_hour: 3
  meeting_soon_minutes: 30
  deadline_hours: 24
  cooldown_seconds: 900
```

### DND / focus mode (`dnd`)

Do-not-disturb schedule with urgent-override routing.

```yaml
dnd:
  enabled: false
  schedule:
    enabled: false
    start: "22:00"
    end: "07:00"
    days: []
  focus_default_minutes: 60
  urgent_override:
    enabled: false
    min_severity: "critical"
    allowed_sources: []
```

### Channels (`channels`)

Routes notifications by importance: HUD toast, voice, Telegram, desktop.

```yaml
channels:
  enabled: false
  away_fallback: true
  severity_routing:
    info: ["hud_toast"]
    low: ["hud_toast"]
    medium: ["hud_toast", "voice"]
    high: ["hud_toast", "voice", "telegram", "desktop"]
    critical: ["hud_toast", "voice", "telegram", "desktop"]
```

## CLI Integration

Proactive features surface through:

- `nexus chief ask "what is the status?"` — live status Q&A
- Morning brief via `ProactiveEngine.run_morning_brief()` (programmatic)
- HUD snapshot exposes `monitoring` state (`queue_depth`, `running_missions`,
  `cost_today`, `error_rate`)

## Known Limits

- Morning brief requires `proactive.yaml enabled: true` AND
  `morning_brief.enabled: true` — both must be set by the operator.
- No email/calendar/news fetching happens without explicit source
  configuration; all sources default to empty/disabled.
- Voice reminders require `voice.yaml enabled: true` plus a TTS provider.
- All proactive proposals route through the normal approval gate — a suggestion
  is never an action until the operator approves it.
