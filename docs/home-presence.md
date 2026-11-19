# Home presence (Task 22)

Opt-in. Nexus works out whether you are **present**, **idle** or **away** from
coarse, content-free signals and lets a rules file react with a small closed
set of actions.

> Not the same thing as the HUD hand-off feature in `docs/presence.md`
> (`hud.yaml`), which shares a conversation between devices.

| | |
| --- | --- |
| Policy files | `presence.yaml`, `presence_rules.yaml` (operator-owned, protected, both default `enabled: false`) |
| Code | `nexus/presence/` |
| State | `<NEXUS_HOME>/presence/<workspace-hash>/state.json` (state, bounded history, proposals, salt) |
| CLI | `nexus presence status\|tick\|watch\|set\|history\|forget\|rules\|proposals\|dismiss\|device-hash` |
| REPL / voice | `/presence [home\|away\|auto]`, "I'm home", "I'm leaving", "am I home?" (spoken: setting presence only for a verified speaker, not after untrusted content; see [voice.md](voice.md#spoken-commands-of-the-opt-in-features)) |
| Tool | `presence_status` (read-only; the model cannot set presence or trigger a rule) |

## Enable

```yaml
# presence.yaml
schema: 1
enabled: true
signals:
  manual: true
  input_idle: true       # needs xprintidle on Linux/X11; built in on Windows and macOS
  lan_devices: true
lan:
  devices: ["<hash from: nexus presence device-hash AA:BB:CC:DD:EE:FF>"]
```

Presence is evaluated when something calls `tick`:

* the Jarvis daemon does it once per cycle (when the daemon is running and not
  at autonomy L0 / emergency stop);
* `nexus presence watch --interval 30` runs it in the foreground;
* `nexus presence tick` runs it once (cron, systemd timer).

## Signals

| Signal | What is read | What is stored |
| --- | --- | --- |
| `manual` | `nexus presence set home\|away\|auto`, "I'm home" | the mark and its time |
| `input_idle` | seconds since the last keyboard/mouse event | a timestamp |
| `vision` | owner-recognised / person events from the opt-in camera pipeline | a timestamp |
| `lan_devices` | this machine's own neighbour (ARP) table - **passive**, nothing is sent on the network | a timestamp |

Device addresses are never written to disk or to the policy file: you configure
salted hashes (`nexus presence device-hash <MAC>`), and the salt lives in Nexus
state, so a hash is meaningless on another machine. No keystrokes, window
titles, frames or locations are read or stored.

A signal that is enabled but unavailable on this machine is reported
(`nexus presence status`, `nexus doctor`) and is **not** read as "away".

## States

```
unknown ──► present ◄──► idle ──► away ──► present
```

* `present` - input within `idle_after_seconds`, a camera sighting, or a manual "home" mark
* `idle` - no input, but a sign of you (known device, camera) within `away_after_seconds`
* `away` - no sign at all for `away_after_seconds`, or a manual "away" mark
* `unknown` - nothing has been observed yet

A new state must hold for `debounce_seconds` before it is accepted. Manual marks
apply at once and expire after `manual_hold_minutes`.

## Rules

```yaml
# presence_rules.yaml
schema: 1
enabled: true
rules:
  - id: welcome-home
    when: arrive                 # arrive | leave | idle | active
    after_away_minutes: 30
    between: "06:00-23:00"       # local time; may wrap midnight
    do: brief                    # see the table
    cooldown_minutes: 120
  - id: route-to-phone
    when: leave
    do: mark_away
```

| `do` | Effect |
| --- | --- |
| `notify` | sends your fixed `text` through the normal notification router (still governed by `autonomy.yaml`) |
| `brief` | notifies "Welcome back. Say 'brief me' for an update." - it does not build or fetch a brief itself |
| `dnd_on` / `dnd_off` | focus mode for `dnd_minutes` / off |
| `mark_present` / `mark_away` | tells the notification router you are at / away from the machine |
| `propose` | records your `text` as a suggestion (`nexus presence proposals`); nothing is executed |

That table is the whole action surface. A rule cannot name a tool, a command, a
mission or a model; any other `do` value makes the file invalid and **no** rule
fires. Guard rails: per-rule cooldown, `actions.max_per_hour` across all rules,
no actions while the emergency stop is latched (state is still tracked), and
every transition and action is audited (`presence.transition`,
`presence.action`).

Start-up (`unknown → …`) is never treated as an arrival.

## Privacy

`nexus presence history` shows what is kept: `from`, `to`, `when`, and which
signal caused the change - bounded by `history.keep_events` and
`history.keep_days`. `nexus presence forget` erases it.

## Known limits

* `input_idle` on Linux needs X11 and the `xprintidle` program; on Wayland the
  signal is reported unavailable.
* ARP entries age: a phone that left can stay listed for a minute or two, and a
  sleeping phone can drop out while you are home. Tune `away_after_seconds`.
  Phones that randomise their address per network keep one address per network,
  so hash the address the phone uses on *your* network.
* Presence is a convenience signal, not a security control: do not use it to
  unlock anything. Nothing in Nexus treats "present" as authorisation.

## Tests

`pytest tests/test_home_presence.py` (offline; injected clock and signal sources).
