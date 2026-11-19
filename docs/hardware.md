# Hardware control (Task 24)

Opt-in. Nexus can talk to devices you list, using only the commands you write
down. The model picks a device name, a command name and bounded parameters; it
never composes a payload, a port, a topic or a pin.

| | |
| --- | --- |
| Policy file | `hardware.yaml` (operator-owned, protected, default `enabled: false`, `dry_run: true`) |
| Code | `nexus/hardware/` |
| Extra | `pip install "nexus-agent[hardware]"` (pyserial, paho-mqtt, gpiozero - only what your transports need) |
| State | `<NEXUS_HOME>/hardware/<workspace-hash>/` (rate window, cooldown stamps) |
| CLI | `nexus hardware status\|devices\|send DEVICE COMMAND [-p NAME=VALUE] [--yes]\|safe [DEVICE]\|log` |
| Tools | `hardware_read` (READ), `hardware_actuate` (WRITE), `hardware_critical` (DANGEROUS) |

Unlike the Home Assistant / TLS-MQTT adapter in `permissions.yaml`
(`computer.smart_home`), this talks to devices directly.

## Enable

```yaml
# hardware.yaml
schema: 1
enabled: true
dry_run: true            # keep this on until `nexus hardware send` shows what you expect
devices:
  desk_lamp:
    transport: serial
    port: /dev/ttyUSB0
    baud: 9600
    settle_ms: 2000      # boards that reset when the port is opened
    safe: "off"
    commands:
      "on":   {payload: "LAMP ON\n", class: actuate}
      "off":  {payload: "LAMP OFF\n", class: actuate}
      dim:    {payload: "DIM {level}\n", class: actuate, params: {level: {min: 0, max: 100}}}
      status: {payload: "LAMP?\n", class: read}
```

> YAML reads bare `on`, `off`, `yes`, `no` as booleans. Quote them (`"on"`,
> `safe: "off"`). Nexus rejects the file instead of guessing, so an unquoted
> `safe: off` can never silently mean "no safe command".

Then:

```bash
nexus hardware devices
nexus hardware send desk_lamp dim -p level=40      # dry run: shows byte count, sends nothing
# set dry_run: false when you are satisfied
nexus hardware send desk_lamp dim -p level=40 --yes
```

## Dry run (the default)

With `dry_run: true` every command is still checked against the allowlist,
approved, rate-limited and written to the audit trail (result `dry-run`), but
nothing is transmitted and no transport library is needed. The tool
descriptions the model sees say "DRY RUN is on", and each result carries the
note "DRY RUN: nothing was transmitted".

## CLI

```text
nexus hardware status                 policy, device count, missing libraries, devices without a safe command
nexus hardware devices                the allowlist as the model sees it
nexus hardware send DEVICE COMMAND [-p NAME=VALUE ...] [--yes]
nexus hardware safe [DEVICE]          send the safe command of one device or of all
nexus hardware log [-n N]             recent hardware.command audit rows
```

All take `-C WORKSPACE` and `--json`. With dry run off, `send` needs `--yes`
for an actuate or critical command. `safe` exits 1 if any device failed.
There are no voice or slash shortcuts.

## Command classes

| `class` | Tool | Approval |
| --- | --- | --- |
| `read` | `hardware_read` | none (a query you declared harmless) |
| `actuate` | `hardware_actuate` | the normal approval mode (`--yes` / auto mode may approve it) |
| `critical` (default) | `hardware_critical` | a fresh human approval every time; `--yes` never approves it |

A tool refuses commands of another class, so the read tool cannot be used to
actuate. Anything that can hurt someone or damage something - heaters, locks,
motors, garage doors - should stay `critical`, which is why it is the default.

## What can reach a wire

* The device and command must be listed. Unknown names are refused.
* The payload is your literal text (`payload`) or bytes (`payload_hex`).
* Parameters fill `{name}` placeholders only, and only when declared: whole
  numbers within `min`..`max`, or one of up to 16 plain-word `choices`.
  Placeholders and declared parameters must match exactly.
* Per command `cooldown_seconds` (protects relays and compressors from rapid
  cycling) and a global `rate.max_per_minute`. A cooldown starts when the
  command is attempted, so it also applies after a failed exchange. Safe
  commands are exempt from both.
* One exchange, bounded by `response.timeout_ms` and `response.max_bytes`.
  **Never retried**: after a failure the physical outcome is unknown.
* Every attempt is audited as `hardware.command` with result `sent`,
  `dry-run`, `refused` or `failed`. The row holds the device, command, class,
  transport, byte count and the first 16 hex characters of the payload's
  SHA-256 - never the payload text or the reply.

## Transports

| Transport | Device settings | Notes |
| --- | --- | --- |
| `serial` | `port`, `baud`, `settle_ms` | Local device paths only (`/dev/tty…`, `/dev/serial/by-id/…`, `COM3`); network serial URLs are rejected. A reply is read up to a newline. |
| `mqtt` | `host`, `port`, `tls`, `topic`, `state_topic`, `username_env`, `password_env`, `qos` | Publishes to the one exact topic (no wildcards), never retained. A `read` command takes the first message that arrives on `state_topic` (a retained one, or the next one published before the timeout) and ignores every other topic. Each exchange uses a new client id and a clean session. TLS is on by default; `tls: false` is accepted only for a broker on this machine or the local network. Credentials come from the named environment variables. |
| `gpio` | `pin`, `active_high` | Commands use `state: "on"\|"off"` and optional `pulse_ms`; a `read` command returns `1` or `0`. See the limit below. |
| `mock` | `reply` | In-memory device for trying a configuration. |

## Replies are untrusted

Device output is data, never instructions. A bare number or one of a few fixed
status words (`ON`, `OFF`, `OK`, `HIGH`, `LOW`, `TRUE`, `FALSE`, `OPEN`,
`CLOSED`) is passed through as is. Anything else is wrapped as untrusted
content and marks the session tainted, so later write actions need a fresh
approval.

## Emergency stop and safe state

* While the emergency stop is latched, actuate and critical commands are
  refused, and the agent cannot call any tool at all. From the CLI, reads and
  `nexus hardware safe` still work.
* `safe:` names the command that puts a device in its safe state.
  `nexus hardware safe [DEVICE]` sends it.
* With `safe_on_stop: true` (default), `nexus stop` and the gesture emergency
  stop also send every device's safe command. `nexus stop` prints one
  `hardware safe: ...` line per device (`hardware_safe` in `--json` output).
  Other ways of latching the stop (HUD, API) block further actuation but do
  not send safe commands - run `nexus hardware safe`.
* `nexus hardware status` / `nexus doctor` list devices that can actuate but
  define no safe command.

## Known limits

* **Transmission is not confirmation.** "Sent" means the bytes left this
  machine (or the broker acknowledged them). Use a `read` command to check.
* **GPIO levels last only as long as the process.** gpiozero releases pins when
  the Nexus process that set them exits, so a level set by a one-off
  `nexus hardware send` does not persist; `pulse_ms` is the operation that is
  complete within one call. For state that must outlive the process, put a
  microcontroller on serial or MQTT.
* **No de-duplication.** A retried mission step sends the command again. Prefer
  idempotent commands (`on` / `off`, not `toggle`) or class them `critical`.
* No device discovery, no free-form payloads and no voice shortcuts - the agent
  uses the tools through the normal approval flow.
* Checked against pyserial's loopback port, gpiozero's mock pin factory and a
  local plain-TCP MQTT broker. **Not** checked against physical devices, a TLS
  broker or a broker that requires credentials. Keep `dry_run: true` until you
  have watched your own device respond.

## Install

`pip install "nexus-agent[hardware]"` brings pyserial, paho-mqtt and gpiozero.
Only the library for a transport you use has to be present;
`nexus hardware status` and `nexus doctor` list the missing ones.

## Tests

`pytest tests/test_hardware.py` (offline; stand-in modules, plus a pyserial
loopback test that runs when pyserial is installed).
