# Security Assistant (`nexus/security_assistant/`)

Nexus's security assistant provides passive security monitoring: auth log
watching, fail2ban/UFW log review, listening port enumeration, user/file
integrity checks, certificate inspection, dpkg status, Python package audit,
and local CVE advisory snapshots. All monitoring is **passive and read-only** —
no remote fetching, no exploitation, no active scanning.

All features are **OFF by default** and enabled through `security.yaml`.

## Safety & Default-OFF Policy

`security.yaml` controls security assistant behavior and is **OFF by default**:

```yaml
schema: 1
enabled: false
own_host_confirmed: false
host_label: local
monitoring_enabled: false
watch_enabled: false
scan_enabled: false
responses_enabled: false
analysis_enabled: false
analysis_allow_remote: false
hud_enabled: false
speech_enabled: false
```

`security.yaml` is protected by `Permissions.operator_control()`: agents cannot
edit it.

## Capabilities

### Passive monitors (`monitors.*`)

| Monitor | What it checks | Data source |
|---------|---------------|-------------|
| auth_logs | Authentication events | /var/log/auth.log or /var/log/secure |
| fail2ban_logs | Ban events | /var/log/fail2ban.log |
| ufw_logs | Firewall events | /var/log/ufw.log |
| listening_ports | TCP listeners | psutil socket table (passive) |
| users_files | User account changes | /etc/passwd (never shadow) |
| cron_dirs | Cron jobs | Non-recursive directory listing |
| systemd_dirs | Service units | Link metadata only (targets not followed) |
| integrity_files | File changes | Content hash + ownership/mode |
| certificate_files | Cert expiry | Public PEM/DER only |
| dpkg_files | Installed packages | /var/lib/dpkg/status |
| python_packages | Python packages | Installed metadata only |
| cve_feeds | Known vulnerabilities | Local schema-1 advisory snapshots |

### Targets (`targets`)

IP literals or narrow CIDRs only (<=256 addresses per CIDR). No wildcard, DNS,
URL, scripts, exploits, or nmap.

```yaml
targets: []
scan_ports: [22, 80, 443]
```

### Responses (`responses_enabled`)

Reviewed proposals only — never execution. Separate explicit disclosure +
fresh approval required for remote analysis.

## CLI Integration

```bash
nexus security watch   # explicit foreground watch (watch_enabled: true required)
```

## Known Limits

- This is NOT an OS sandbox — it's a passive monitoring and proposal tool.
- `analysis_allow_remote: false` by default; enabling it requires separate
  explicit disclosure and fresh approval.
- `max_records: 1000` — full timeline/evidence stops, requires manual archive.
- Biometrics, OAuth delivery, MQTT, and multi-device use are not CI-verified.
