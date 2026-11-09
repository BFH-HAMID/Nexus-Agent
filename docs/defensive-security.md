# Defensive security assistant — Task 14

**OFF by default. Owned or explicitly authorized systems only.** This is a
read-only host observer and incident/report assistant, **not an autonomous
incident responder, vulnerability scanner suite, exploit runner or OS sandbox**.

The shipped [`security.yaml`](../security.yaml) enables nothing. Import, status,
HUD refresh and installing the package do not start a collector, scan or model.
A foreground operator command is required to collect/watch. No service, firewall
rule, account change, key rotation or automatic Telegram sender is installed.

## 1. Enable in a dedicated, trusted workspace

Prefer a separate security workspace: enabling this mode intentionally narrows
that workspace's **shared tool gate**. Normal coding/desktop workflows should
remain in their own workspace, with their existing policies and fresh approvals.

Install the normal package. Public-certificate and SSH public-key validation share one lazy extra:

```bash
python -m pip install -e '.[security]'
nexus security -C /path/to/security-workspace status
```

Copy the shipped `security.yaml` into that workspace and **edit it yourself**.
Agent file/Office/restore tools cannot edit it, including through hardlink aliases.
No environment variable or `--yes` enables a capability. Missing configuration is
OFF; malformed/unknown settings fail closed. Credentials belong in environment,
`.env` or the existing credential store, never in this YAML or evidence.

A Linux passive-monitor example (merge with the shipped defaults):

```yaml
schema: 1
enabled: true
own_host_confirmed: true          # your actual ownership/explicit authorization
host_label: workstation
monitoring_enabled: true
watch_enabled: false             # enable separately for the foreground watch loop
scan_enabled: false
responses_enabled: false
analysis_enabled: false
analysis_allow_remote: false
hud_enabled: false
speech_enabled: false
monitors:
  auth_logs: [/var/log/auth.log]   # some distributions use /var/log/secure
  fail2ban_logs: [/var/log/fail2ban.log]
  ufw_logs: [/var/log/ufw.log]
  listening_ports: true
  users_files: [/etc/passwd]
  cron_dirs: [/etc/cron.d]
  systemd_dirs: [/etc/systemd/system]
  integrity_files: [/etc/ssh/sshd_config]
  certificate_files: []
  dpkg_files: []
  python_packages: false
  cve_feeds: []
targets: []
```

Choose only inputs you are authorized to read. Grant narrowly scoped read access
or provide operator-produced snapshots; **do not run Nexus as root just to read
logs**. Missing, denied, oversized or unsupported sources are reported as
**coverage gaps**, not as a healthy/secure host. Snapshots describe their source,
not a remote machine that Nexus independently authenticated.

```bash
nexus security -C /path/to/security-workspace monitor
nexus security -C /path/to/security-workspace status
# After explicitly setting watch_enabled: true:
nexus security -C /path/to/security-workspace watch
```

`watch` stays in the foreground, rechecks enablement before each tick and stops on
Ctrl+C, a gate/policy failure or the shared emergency stop. It does not install or
extend the Task 13 OS service. Changing policy starts nothing. Disable
`monitoring_enabled`/`watch_enabled` to stop collection; `enabled: false` turns off
all these capabilities. Existing completed evidence remains until you archive it.

## 2. What is actually observed

| Monitor | Observation and explicit boundary |
|---|---|
| SSH brute force | Common `sshd` failed password/publickey/keyboard-interactive lines; aggregate by IP within the configured window. Passwords/user text and original log lines are not stored. Normalized IP/count, cursor and sample line hashes are evidence. |
| fail2ban / ufw | `Ban`/`Unban` events and `[UFW BLOCK] SRC=…` lines. Informational defensive events are not automatically called intrusions. Other formats need an operator-produced compatible log. |
| Listening ports | Passive `psutil` **TCP LISTEN** table. No connections, subprocesses, service banners, UDP discovery or process-command-line collection. Permission/platform gaps remain explicit. |
| Users | Names, numeric UID/GID and login shells from selected passwd-format files. New/changed UID 0 is critical. No password-field persistence or shadow inspection is needed for supported response proposals. |
| Cron / systemd | Selected directory entries, content hashes, mode and ownership. **Non-recursive**: select spool/child/`.wants` directories explicitly. Links are tracked as link-target hashes, never followed. No unit/cron content is executed. |
| File integrity | Exact selected regular files; content SHA-256, mode/UID/GID; added/changed/missing observations. No arbitrary recursive disk crawling and no file-body archive. |
| Installed-package CVEs | Selected dpkg status files and/or this Python interpreter's ordinary local dist-info/egg-info metadata, matched against local advisory snapshots. Exact versions only; see below. |
| Certificate expiry | Selected **public PEM/DER certificate files**, using lazy `cryptography>=44`. Expired = critical; within seven days = high; otherwise within `certificate_days` = medium. A PEM input checks its first certificate; select each certificate separately. Private-key bundles are refused. No DNS/TLS fetch or trust-chain/hostname validation. |

File/user/directory/port first observations establish baselines, **not proof that
the baseline is safe**. Later diffs become findings. CVE and expiry problems can
alert on the first collection. An event remains historical evidence; disappearance
of a match does not automatically declare an incident resolved.

Logs have persistent inode/device/offset/anchor cursors, complete-line handling
and rotation/copytruncate detection. Initial reads take a bounded tail and report
any skipped-prefix gap. Overlong lines do not advance the cursor. ISO timestamps
are supported; traditional syslog timestamps use the host's local timezone/year.
Unparseable timestamps use arrival time, explicitly labelled. Old/future events
are excluded from the SSH window. Rolling buckets have ten-second granularity;
this is a practical detector, not an exact reconstruction of all authentication.

Severity is `info`, `low`, `medium`, `high`, or `critical`. Unchanged findings do
not produce a new record/alert on every poll. Changed observations update the
incident count and retain timeline evidence even during cooldown. Severity
escalation may alert immediately. **No alert is an approval or an action task.**

### Offline advisory snapshots

No automatic advisory download or installed-inventory upload occurs. Enable
`dpkg_files` and/or `python_packages`, plus one or more `cve_feeds`. Maintain the
feed from a trusted vendor/advisory source yourself. Its timestamp is the actual
snapshot generation time, not a field to refresh merely to suppress warnings.

This is a **synthetic format example**, not a real advisory or vulnerability claim:

```json
{
  "schema": 1,
  "generated_at": "2026-11-09T00:00:00Z",
  "advisories": [
    {
      "id": "CVE-2026-12345",
      "ecosystem": "deb",
      "package": "demo-package",
      "affected_versions": ["1.2-3"],
      "severity": "high",
      "fixed_version": "1.2-4"
    }
  ]
}
```

`ecosystem` is `deb` or `pypi`. Python names are normalized, but versions are
**literal exact matches**, not guessed Debian/backport or PEP 440 ordering.
`fixed_version` is advisory text validated as a version, **not an upgrade command**.
Stale snapshots emit a warning and still show any matches. Missing feeds or
incomplete inventories mean incomplete coverage. There is no NVD/OSV live query,
RPM/APK inventory, automatic remediation or guarantee of complete CVE coverage.
Python zip/custom metadata backends are refused rather than read without bounds.

## 3. Scoped active checks — no nmap/exploits

Set `scan_enabled: true`, exact `targets` and `scan_ports` in operator YAML:

```yaml
scan_enabled: true
targets: [127.0.0.1/32]          # example: your own host only
scan_ports: [22, 443]
```

```bash
nexus security -C /path/to/security-workspace scan 127.0.0.1 --ports 22,443 --preview
nexus security -C /path/to/security-workspace scan 127.0.0.1 --ports 22,443
```

The second command needs a **fresh exact `RUN security_scan …` confirmation** via
the original ApprovalGate. An allowlist is necessary, not sufficient consent.
READ_ONLY/plan/role ceilings, optional second factor, limits and emergency stop
still apply. `--yes`, saved/session approvals and `allow_dangerous` cannot override
scope or replace that fresh decision.

Supported IO is **one IP literal, at most 32 explicitly allowed TCP ports, one
connect per port**, 0.3-second socket timeout and a 12-second loop budget. There is
no payload, banner read, DNS resolution, raw socket, UDP, CIDR expansion, script,
exploit or retry. An open port is not evidence of a vulnerability. Each connection
rechecks scope/cancellation. Revocation stops remaining ports; already initiated
traffic cannot be recalled. Intent and result slots are reserved before sockets.

- Target entries are canonical IPs or narrow CIDRs, at most 256 addresses each.
- No URL, hostname, wildcard, zone ID or ambiguous IPv4-mapped IPv6 target.
- Multi-address subnet endpoints/broadcast addresses are conservatively refused.
- Out-of-scope attempts are refused **before approval/IO** and a request digest is
  recorded as `security.scope_refused` in the existing shared audit trail. Audit
  failure never enables a denied call.
- Raw `nmap`, masscan, nuclei and similar known scanner/exploit invocations are
  refused, even for an allowlisted target: use the typed TCP tool. **No raw scanner
  adapter is offered.**

**Important compatibility boundary:** while root `enabled: true`, the shared gate
allows the defensive tools plus ordinary list/read/grep, skill viewing, recall,
finish and clarification. Opaque shell, Git/gh, MCP, browser/desktop, macro and
other execution/mutation relays are refused, including encoded/custom shell code.
This deliberately avoids pretending a command regex can contain arbitrary code.
Outside this mode, legacy freshly approved code and other processes/workspaces
are not universally inspected or constrained by `security.yaml`. Use OS identity,
network egress controls and isolation for that boundary. No general OS sandbox or
blanket authorization to test another system is claimed.

## 4. Response proposals and rollback

Set `responses_enabled: true`. Choose a recorded incident ID from an export or
monitor result. `--preview` displays the plan **without recording approval**;
without it, fresh approval records only the reviewed proposal.

```bash
nexus security -C /path/to/security-workspace respond INCIDENT_ID block_ip --backend ufw --preview
nexus security -C /path/to/security-workspace respond INCIDENT_ID block_ip --backend iptables
nexus security -C /path/to/security-workspace respond INCIDENT_ID disable_account --account worker
nexus security -C /path/to/security-workspace respond INCIDENT_ID rotate_key --key owner
```

All three classify as **DANGEROUS**, show the complete command **and rollback**,
and use a 60-second one-use, context/payload/policy/evidence/preimage-bound review.
A change during review, caller-supplied review, expired lease, cancellation or
reuse is rejected. There is **no response executor**, `--execute`, automatic
follow-up, saved execution grant or hidden subprocess in this package.

### IP block

The IP must come from recorded SSH/firewall evidence. The proposal uses a uniquely
commented host-input `ufw` or `iptables`/`ip6tables` rule, with a matching delete
command for rollback. It does not flush rules, change persistence or call a remote
firewall. Blocking an observed external IP on **your own firewall** is not permission
to scan that IP; active checks have their separate target allowlist.

Verify existing rules, firewall syntax/backend and an out-of-band console before
any manual application. Even a narrow block can cut off your administrative path.

### Account disable

The account must match a recorded user-file finding **and** `response_accounts`.
UID 0/root is refused. By default, the proposal switches the interactive shell to
`/usr/sbin/nologin`; rollback restores the freshly read original shell exactly.
This does **not** disable forwarding, running sessions, application tokens or every
service. It is clearly labelled **interactive-shell-only** in the review.

For a broader account-login expiry proposal, supply an additional operator-owned,
non-secret expiry snapshot:

```yaml
response_accounts: [worker]
account_state_files:
  worker: account-state.json
```

`account-state.json` has exactly these fields; use the **actual verified current**
UID/expiry and a timezone-aware observation timestamp less than ten minutes old:

```json
{"schema":1,"account":"worker","uid":1001,"expires":"","observed_at":"2026-11-09T12:00:00Z"}
```

`expires: ""` means the verified account does not expire; otherwise use its current
future `YYYY-MM-DD`. This enables a `usermod --expiredate 1` proposal and rollback
to that exact previous expiry. No password lock/unlock assumption or shadow/hash
collection is necessary. The snapshot is operator evidence, **not an independent
host attestation**; verify it before acting. These files are protected against
agent edits and pinned during review. Expiry still depends on services honoring
account/PAM policy and does not kill existing sessions or revoke API tokens.

### Key rotation

Only an explicitly staged **public SSH authorized_keys replacement** is supported:

```yaml
rotation_paths:
  owner:
    current: /home/owner/.ssh/authorized_keys
    replacement: /home/owner/.ssh/authorized_keys.next
```

Prepare the replacement yourself using new public keys and independently verify
recovery access. These operator artifacts are protected from agent editing. Both
files must be bounded regular single-link files, the current file must have
private permissions, and records must be valid simple public SSH keys (validated with `.[security]`),
without embedded command/options or private-key material. No key body enters evidence/review.

The command proposes a new unique public-key backup followed by GNU `install`
with the observed owner/group/mode. Rollback restores the backup. The current and
replacement hashes/identities are revalidated after approval. No backup, key,
account or firewall file is actually written by the proposal. Private-key
creation, export, revocation at arbitrary providers, key-server APIs and universal
ACL/SELinux/native rollback guarantees are outside this implementation.

## 5. Spoken alerts and the existing HUD

Enable `hud_enabled` and, separately, `speech_enabled` in `security.yaml`. The
existing `proactive.yaml` must also have `enabled: true`; its focus mode, scheduled
DND, explicit urgent overrides and speech-per-hour budget remain authoritative.
Other proactive monitors/briefs do not need to be enabled. Existing Jarvis
quiet/DND is read without starting a ServerState or any repository/workflow
service. Unreadable/encrypted Inbox policy is treated as quiet; there is no
plaintext/keyring fallback in this notification reader.

Spoken delivery additionally requires:

- `voice.yaml` explicitly enabled with **installed local Piper** and its model;
- the existing presence policy reporting the user present;
- the shared speaker target being `local`, when handoff is enabled.

There is **no cloud/auto TTS fallback**. Policy, presence, emergency stop, current quiet/DND and
speaker selection are checked before synthesis and again before playback. Already
playing audio cannot be recalled by these checks. The normal `[voice]` /
`[voice-offline]` dependencies, model and audio output need actual host setup.
Tests fake these adapters; they do not certify a speaker or microphone.

A spoken alert asks about a response proposal, never treats a reply or a log line
as approval. Example: “Repeated SSH authentication failures … Review an IP-block
proposal and its rollback? Fresh approval is required; nothing will be blocked
automatically.” Follow through the existing tool/CLI review; no new spoken
approval shortcut is added.

Per-incident cooldowns persist. At most eight alert dispatches are attempted per
tick, plus the existing hourly speech cap. Suppressed/failed delivery leaves the
incident evidence intact. There is no external-delivery retry/fanout queue.

Local notices are retained in the existing proactive HUD store. Recent unread,
non-suppressed notices from a foreground/watch process bridge into the existing
**authenticated owner Mission Control `alert.new` SSE feed**, once per server
instance. That bridge reads notices only, never runs collectors or responses.
Operations snapshot includes status-only `defensive_security`. CLI exports remain
the detailed incident interface; no additional security HTTP mutation endpoint or
new mobile device scope was added. **No automatic Telegram, push or desktop send**
is inherited from generic severity routing. Any external sending remains a
separate fresh-reviewed action outside this defensive mode.

## 6. Existing security analysis/report roles

See [the existing role registry and authority boundaries](security-roles.md).
There is no duplicate persona registry or permission expansion. The configured
`analysis_roles` must name installed defensive roles (for example `security-audit`,
`blue-team`, `forensics`, `soc-analyst`, `security-incident-responder`, or
`security-hardening-advisor`). An unavailable role/model is refused, **not replaced
with the general role or another provider**.

```yaml
analysis_enabled: true
analysis_roles: [blue-team, forensics]
analysis_agent: ollama          # existing configured agents.yaml name
analysis_allow_remote: false
```

```bash
nexus security -C /path/to/security-workspace roles
nexus security -C /path/to/security-workspace analyze INCIDENT_ID --role blue-team --preview
nexus security -C /path/to/security-workspace analyze INCIDENT_ID --role blue-team
```

Every analysis request also receives fresh DANGEROUS review because it may disclose
incident metadata and incur model cost. The preview identifies endpoint, model,
role, exact normalized incident and prompt/evidence hashes. Remote inference is refused by default: select an existing loopback
OpenAI-compatible agent as above. A role mapped to a cloud agent is refused, not
automatically rerouted to a local model. `localhost` is pinned to numeric loopback. Explicit
`analysis_allow_remote: true` permits a reviewed HTTPS provider. Known `:cloud`
aliases are refused; an operator must still ensure a local service is not itself
a remote proxy.

One no-tool request reuses the existing provider adapter, with no fallback,
redirect, ambient proxy, automatic retry, memory/learning, or tool execution.
Untrusted evidence is delimiter-escaped. Tool-bearing replies are rejected.
Inputs/output, transport bytes/deadline and response tokens are bounded. Existing
budget/request guards apply and metadata-only usage goes into the existing
session/cost ledger; unknown billing is **not reported as zero**. No raw logs,
file bodies, private keys, screenshots or faces enter this path.

Ordinary Nexus chat is still an ordinary model conversation: if an operator asks
its enabled defensive tools for a report, their normalized tool results can enter
that selected model's context. Use this foreground CLI or an appropriately
configured local agent for private investigations. The dedicated analysis flag is
not a process-wide DLP system for every legacy model conversation.

## 7. Evidence, exports, capacity and recovery

```bash
nexus security -C /path/to/security-workspace export --format markdown > incident-report.md
nexus security -C /path/to/security-workspace export --format json > incident-report.json
nexus security -C /path/to/security-workspace export --format json --incident INCIDENT_ID
nexus audit -C /path/to/security-workspace tail -n 20
```

Exports go to stdout; **the operator's shell**, not Nexus, chooses the destination.
Protect redirected reports yourself. Reports include severity, timestamps,
occurrence counts, proposed next review, collection gaps, linked timeline events
and normalized evidence with SHA-256 references. Markdown escapes embedded HTML,
backticks and table delimiters. JSON includes an explicit untrusted-data label.
Neither export is executable authority.

State is private and workspace-scoped at
`$NEXUS_HOME/defensive-security/<workspace-sha256>/` (outside the repository by
default). POSIX directories request 0700, state/lock files 0600 and evidence files
0400. Symlinked/non-private/multilink state is refused. Evidence filenames are
content hashes; hash/chain mismatches stop exports. Originals are opened read-only
and are never modified. Known environment secrets/token/private-key patterns are
masked **before JSON escaping**, including nested values/keys. Newly known secrets
are masked again on reads; masking is not proof that arbitrary sensitive data was
identified or erased from prior backups.

**Evidence is normalized metadata, not byte-for-byte forensic acquisition.** No
raw log/file-body archive is made. Hashes/modes help detect accidental changes;
the host owner can still modify files, recompute hashes or replace a whole chain.
There is no WORM, signed timestamp, secure-erasure, root-compromise resistance or
legal chain-of-custody certification.

Default bounds:

- 48 explicit source paths, at most 16 per path-based monitor;
- 2 MB per file read, 8 MB total file reads per tick; configurable maxima 8 MB /
  32 MB. Large dpkg/Python inventories may require an intentional increase;
- 256 entries per selected directory, 128 rolling log-IP groups, 512 findings per
  collection pass; excess work reports/refuses rather than scanning without bounds;
- 1,000 timeline/evidence records, 25 MB of evidence, 8 MB journal state;
- 64 KB per evidence artifact; 32 pending one-use reviews; no policy hot-reload
  grants or automatic replay;
- analysis: 12 KB incident input, 16 K-character role prompt, 1,024 output tokens,
  256 KB uncompressed response cap and a 20-second response-loop deadline.
  Blocking transport calls can take their timeout to return after revocation.

**Full means stop, not delete old evidence.** Orphan artifacts count against
capacity. Active checks/reports reserve intent/result slots under the journal
lock before IO. A crash can still leave an intent without a result; inspect it
manually, never infer success or auto-retry. Collection interruptions/partial log
rotation and same-user filesystem races are not atomically solved. Archive/export
and reset storage yourself only after preserving what you need; no purge command
or silent baseline reset is supplied.

## 8. Offline tests and host acceptance

```bash
pytest -q tests/test_defensive_security.py tests/test_mission.py
pytest -q
ruff check nexus tests
```

Tests use fake logs/package inventories, generated public certificates/SSH public
keys, temporary private storage, real gates/leases/exports and fake sockets,
models, TTS/playback and HUD delivery. They cover every monitor, baselines,
rotation/partial lines, severity/dedup/cooldowns, coverage/quotas, symlink/hardlink
refusal, redaction/hash verification, non-allowlisted target refusal and audit,
no `--yes`/saved-approval bypass, policy/preimage/review revocation, opaque executor
refusal, role/model fallback denial, response rollback previews and **no response
execution**, local-only speech, existing DND and cross-process HUD projection, watch-loop
revocation, model budget stops, and transport byte/deadline/revocation bounds.

No network/GPU/microphone/API key is required for these tests. Passing fixtures do
not certify real auth-log formats, exhaustive package CVEs, Linux distribution
firewall/usermod syntax, native file ACLs, audio devices, remote model billing or
real incident response. Test on your explicitly authorized host with a recovery
console before manually applying any command. No live security scan, exploit,
service installation, account/firewall mutation or key rotation was performed to
validate this implementation.
