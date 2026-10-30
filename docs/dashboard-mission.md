# Mission Control — summary API and live feed

Prompt 2 of the dashboard build. Prompt 1 laid out the shell and a Mission
Control page that polled; this one gives that page one honest projection and a
server-push feed with exactly five event names. No new dependency, no second
process, no database: the projection is built from records the server already
owns.

Code: `nexus/mission.py` (service), `nexus/server.py` (two routes),
`nexus/panel/mission.js` (renderer), `tests/test_mission.py` (backend),
`tests/browser/mission.test.mjs` (browser).

## `GET /api/mission/summary`

Small (≈2 KB, 15–30 ms warm). Same authentication as every other `/api` route
(session cookie, `X-Nexus-Token` or `?token=`), rate limited per client
(`mission:<ip>`, 120 requests / 60 s), and audited once per client per five
minutes (`mission.summary`) so a 4-second poll cannot fill the audit log.

```json
{
  "schema": 1, "observed_at": 1730000000.0, "source": "local records",
  "daemon":    {"state": "ok", "reason": null, "status": "stopped", "lock_held": false,
                "execution_enabled": true, "dispatch_configured": false, "observed_at": 1730000000.0},
  "tasks":     {"state": "ok", "active": 0, "queued": 0, "failed_24h": 0, "total": 4,
                "recent": [], "observed_at": 1730000000.0},
  "approvals": {"state": "ok", "pending": 0, "items": [], "observed_at": 1730000000.0},
  "resources": {"state": "ok", "cpu_percent": 12.0, "ram_percent": 31.0,
                "disk_free_bytes": 19900000000, "disk_total_bytes": 63000000000,
                "gpu": null, "gpu_reason": "nvidia-smi not installed", "observed_at": 1730000000.0},
  "usage":     {"state": "ok", "tokens_in": 0, "tokens_out": 0, "cost_usd": null,
                "cost_reason": "no verified price for the models used", "observed_at": 1730000000.0},
  "agents":    {"state": "ok", "total": 13, "with_keys": 4, "reachability_checked": false,
                "observed_at": 1730000000.0},
  "activity":  [], "health": {}, "alerts": [], "stream": {"subscribers": 1},
  "build_ms": 18.4
}
```

Rules the projection obeys:

- **`state` is the section's own status** — `ok` or `unavailable` with a
  `reason`. The daemon's *value* is `status`, so a stopped daemon is
  `{"state": "ok", "status": "stopped"}`, never a section that looks broken.
- **No invented numbers.** A missing reading is `null` plus a sentence saying
  why (`gpu_reason`, `cost_reason`) or the section is `unavailable` with the
  exception type. `agents.reachability_checked` is always `false` until a
  reachability probe exists — configured is not reachable.
- **Every value carries `observed_at`**, and the page prints the age
  (`state read 0s ago`) under the tile.
- **Nothing slow is on the request path.** GitHub (~0.5 s) and `nvidia-smi`
  run in the feed's slow loop (60 s) or in `ensure_health()`, never inside a
  summary call.

`NEXUS_MISSION_TICK` (default 5 s, floor 0.5 s) bounds both the summary cache
and the metrics cadence.

## `GET /api/mission/stream`

A server-sent event feed shared by every open tab. `retry: 5000`,
`X-Accel-Buffering: no`, `Cache-Control: no-cache`. Limit `MAX_SUBSCRIBERS`
(32) and `mission-stream:<ip>` (120 / 60 s); refuse with `503 too_many_streams`
rather than silently dropping events.

| event | when | payload |
| --- | --- | --- |
| `heartbeat` | on connect, then `NEXUS_MISSION_HEARTBEAT` (15 s) | `server_time`, `uptime_s`, `subscribers`, `health`, `usage`, `agents`, `daemon` |
| `task.update` | on connect, then when the fingerprint changes | `active`, `queued`, `failed_24h`, `recent[]`, `observed_at` |
| `approval.new` | on connect, then when the pending set changes | `pending`, `items[]`, `new_ids[]`, `first` |
| `metrics.tick` | `NEXUS_MISSION_TICK` (5 s) | `cpu_percent`, `ram_percent`, `disk_free_bytes`, `observed_at` |
| `alert.new` | a source changes state | `id`, `severity`, `title`, `detail`, `source`, `observed_at` |

`NEXUS_MISSION_SLOW` (60 s) is the slow-check cadence. Alert ids are stable
(`ollama-off`, `github-ok`, `disk-low`, `daemon-down`, `gpu-*`), and each
transition fires once: the first reading of a source is a baseline, not an
incident.

The producer thread only exists while somebody is listening — the last
unsubscribe stops it, which the tests assert. A client that stops reading has
its oldest frame dropped rather than growing a backlog.

### Authentication without headers

`EventSource` cannot send `X-Nexus-Token`, so the page uses
`nexusGui.streamUrl(path)`: the session cookie when the browser was told to
remember the session, otherwise `?token=…`. Both are the same checks every other
route applies, and neither value is logged.

## Browser behaviour

`nexus/panel/mission.js` paints once from the summary, then follows the feed:

- `connecting` → `live` on the first frame; `polling` after three failed
  reconnects (1.5 s · attempt, capped at 30 s); `offline` when the tab is hidden.
- While the feed is not live the page polls the summary every 15 s, and only
  while the tab is visible.
- A language switch re-renders the numbers already on screen from the cached
  summary instead of waiting for the next tick.
- The footer (`#sbSse`) and the pill (`#msStream`) tell the truth: with the
  stream blocked the page says *polling*, not *live*.
- `window.nexusMission = {load, root, state(), streamUrl()}`;
  `state()` returns `{stream, attempts, lastEventAt}`.

Alerts are appended to the activity list with `data-kind="alert"` and a
severity tint, so a warning never looks like a queued run.

## Known limits

- The feed is per-process: a GUI restart drops subscribers (they reconnect with
  backoff), and two server processes do not share one sequence space.
- `metrics.tick` is a `psutil` sample of the whole machine, not of the agent.
- GPU text is whatever `nvidia-smi` prints; there is no history, only the last
  reading.
- `usage` reads the ledger's day bucket only. A verified price table arrives
  with Prompt 10; until then the tile says cost unavailable instead of guessing.
- Reachability of configured agents is not probed (Prompt 3 owns the role hub).
