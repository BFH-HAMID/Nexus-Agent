# Explicit Telegram private-chat long polling

This adapter uses the same Jarvis queue, delegation, one-use approvals and daemon
controls. **Disabled by default.** It never registers/deletes a webhook or executes
arbitrary chat/shell text. Live Telegram network/account acceptance is separate
from the scripted HTTP contract tests used here.

Set `TELEGRAM_BOT_TOKEN` and an independent **8–12 digit** `TELEGRAM_APPROVAL_PIN`
in the launch environment; never print/commit/share them. Edit `telegram.yaml` to
explicitly enable and whitelist up to ten **numeric sender IDs**. Private chat ID
must equal sender ID. Usernames, group/admin names, forwarded messages, bot senders,
stale/future messages and arbitrary supplied chat IDs cannot grant authority.

```powershell
nexus telegram -C WORKSPACE
```

The foreground runner is single-instance per workspace, owned by an OS file lock.
Ctrl+C stops polling; it does not implicitly start/stop the Jarvis worker. The
existing daemon must be configured and explicitly started separately. A previously
configured Telegram webhook causes API refusal; the adapter does not remove it
silently. Network failures stop with a generic diagnostic instead of printing the
bot-token URL, PIN, raw response/error or automatically retrying a sensitive action.

Commands: `/status`, `/tasks`, `/delegate TASK`, `/approve ID`, `/reject ID`,
`/pause`, `/kill`, `/report ID`. Unknown text is never treated as a terminal command.
An exact ID belongs to this workspace, not a caller-selected path/repository.
Delegation always begins approval-pending. No model or cloud fallback is launched
by polling or task creation.

`/approve` prepares a short-lived user-bound challenge, bound to the exact task
signature or action-argument digest. `/kill` also needs a second challenge/PIN.
Read the **actual task/diff in the local HUD**, then send the separate
`/confirm CHALLENGE_ID PIN` command. The bot does not export code/diffs as a shortcut.
A challenge expires, locks after three bad attempts, and is consumed **before**
its side effect; there is also a persistent five-failed-PIN/hour user limit and
20-command/minute limit. Another user cannot transfer/replay it. Changed whitelist,
policy, task signature, terminal state or lease fails the existing enforcement.
A task PIN approval never authorizes individual file/GitHub/extension actions.

Update offsets persist **before** handling, so restart never replays an already
reserved command. A crash can lose a command/reply, not guarantee delivery/exactly-once
external outcomes. Inspect actual status and create a fresh intent rather than
blindly retrying. PIN text/messages are not stored in the challenge or command log;
only hashed bindings, counters and offsets are retained in existing Studio. Bot
account token fingerprints isolate offsets across token changes. Keys/PIN are
masked; operator rotation requires restarting the runner.

`quiet_utc_hours: [22, 7]` is explicitly UTC, not inferred local time. Existing
inbox quiet/DND also suppresses background count notifications, not direct command
responses. Notifications are counts only; unread/pending state stays in the inbox.
`allow_report_text: false` (default) sends only ID/status/attempt metadata. Turning
it on is explicit operator export opt-in for a bounded redacted agent summary,
not test proof; screenshots/context/code files are never uploaded by this adapter.
No Telegram callback button, username authentication, URL/PIN logging or arbitrary
method passthrough exists. The HTTP transport only allows getUpdates/sendMessage,
bounds responses, disables redirects/proxies and rechecks whitelist/enable state
before sending. Same-OS-user compromise, stolen bot account or PIN messages retained
by Telegram are not protections this application can eliminate.

## Original dashboard lifecycle controls

Open **Control center → Telegram policy**. Policy saves do not start a bot.
After saving an enabled whitelist and configuring server environment credentials,
use separately confirmed **Start Telegram polling**, **Stop Telegram polling**
and **Refresh polling status**. Start remains disabled when prerequisites are
missing, and the backend independently checks consent/current policy revision.
The GUI-owned thread uses this same Bot and workspace file lock, so it refuses
a concurrent CLI runner. It never automatically retries/restarts after failure.

Status reports observed GUI-thread liveness/phase and last completed poll, not
account validity or independent CLI process health. Stop prevents new handling
and sends after cancellation checks; an already-started action/request cannot be
recalled. Wait for `alive:false` before restarting. GUI server shutdown requests
cancellation. Closing/logging out of a browser tab does **not** stop the server
or revoke environment credentials; explicitly Stop polling when required.

No live account or external Telegram traffic was used in automated validation.
Bot transport/cancellation/duplicate-lock tests use scripted HTTP; the browser
test exercises real authenticated controls and refused disabled-policy startup.
