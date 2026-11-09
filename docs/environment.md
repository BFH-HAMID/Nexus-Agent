# Environment Control (`docs/computer-control.md` reference)

Nexus's environment control features (Module 12 / computer control) provide
opt-in Linux X11/Windows UIA app control, scriptless origin-locked browser
search/draft/extract, scoped file organization, and smart-home integration.
All environment controls are **OFF by default** and require explicit operator
configuration in `permissions.yaml`.

## Safety & Default-OFF Policy

`permissions.yaml` controls environment behavior and is **OFF by default**:

```yaml
computer:
  browser: false
  editor: false
  office: false
  browser_origins: []
  shell_backend: host
  image: ""
  desktop:
    enabled: false
    backend: auto
    apps: {}
  browser_controls:
    enabled: false
    forms: false
    downloads: false
    download_dir: browser-downloads
    max_download_bytes: 2000000
    search_url: ""
  files:
    enabled: false
    roots: []
    max_files: 200
    max_bytes: 20000000
  workflows:
    enabled: false
    voice: false
    directory: computer-macros
    approved: {}
    allowed_tools: []
    max_steps: 30
  smart_home:
    enabled: false
    voice: false
    home_assistant:
      enabled: false
      url: ""
      devices: {}
    mqtt:
      enabled: false
      host: ""
      port: 8883
      devices: {}
    voice_commands: {}
```

Nothing starts merely by importing Nexus. External sends need fresh approval;
source text is untrusted data.

## Capabilities

### Desktop control (`desktop.enabled`)

Linux X11 (xdotool + wmctrl) or Windows UIA (pywinauto) app control. Only
operator-pinned executables + window classes + actions are allowed.

### Browser control (`browser_controls.enabled`)

Origin-locked browser search, form draft, and data extraction. Form submission
requires a separate digest + fresh submit approval. Downloads are bounded and
never auto-opened.

### File roots (`files.enabled`)

Scoped organize/rename/backup/duplicates/recoverable trash within
workspace-relative roots only. No symlinks, no private/operator files.

### Smart home (`smart_home.enabled`)

Home Assistant (HTTPS, loopback HTTP allowed) and scoped MQTT (TLS mandatory).
Credentials in env/keyring, never in YAML. Voice commands map exact phrases to
device/actions — no inferred authority.

## CLI Integration

```bash
nexus computer        # check Docker sandbox configuration and invariants
nexus sandbox check   # verify sandbox invariants
```

## Known Limits

- Host shell execution is explicitly approved, not claimed to be sandboxed.
- Docker sandbox requires a digest-pinned image (`python:3.12-slim@sha256:...`).
- Live Windows/desktop/Docker acceptance is not claimed — scripted transport
  tests are not host certification.
- Phone support uses authorized companion notifications, not invasive hooks.
