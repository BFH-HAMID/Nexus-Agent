# Security model & responsible use

Nexus runs an LLM with shell and git access inside your repository. This page is the honest
account of what protects you, what does not, and where the offensive-security roles fit.

## Threat model

| Risk | Mitigation in Nexus | Residual risk |
| --- | --- | --- |
| Agent deletes or overwrites something important | approval gate, `danger` class for deletes, diff in every `write_file` result, git as the undo button | `--yes` removes the human from writes; uncommitted work is not recoverable |
| Agent runs a destructive shell command | deny-list regexes (`rm -rf /`, `mkfs`, `dd of=/dev/`, fork bomb, `git push --force`, `curl \| sh`, `npm publish`, `drop database`, …) | a determined model can phrase damage differently; the deny list is a floor, not a ceiling |
| Agent escapes the workspace | `PathGuard` resolves every path and refuses escapes | `--allow-outside` disables it deliberately |
| Agent rewrites shared history | `git push --force` denied; `gh_merge_pr` always prompts even under `--yes` | you can still approve it |
| Secret exfiltration | `.env` values are never printed by tools; `run_command` output is clipped; deny list blocks `curl \| sh` | any tool with network access can be told to POST somewhere — review commands you approve |
| Unattended server | `nexus serve` binds loopback by default; token auth when exposed; warnings on `0.0.0.0` without a token | an exposed tokenless server = remote code execution as you |
| Prompt injection from repo content | plan mode, read-only roles, tool whitelists per role, three-error nudge | a poisoned README can still steer an approved run — treat `--yes` on untrusted repos as unsafe |

## Rules of thumb

1. **Commit before you run.** Nexus assumes git is your undo history.
2. **Start with `nexus ask` / `--plan`.** Read-only, mutations refused at the registry level.
3. **Read the preview before `y`.** Every prompt shows the exact command or the file + size.
4. **`--yes` only on repos you own and can reset.** Danger-class tools still block unless you also
   pass `--allow-dangerous`.
5. **Use `never_allow_tools`** for things you never want an agent to do (e.g. `gh_merge_pr`).
6. **Keep the server loopback.** If you must expose it, set `NEXUS_SERVE_TOKEN` and put TLS in
   front of it.
7. **Audit role whitelists.** A role with `tools: [read_file, grep, finish]` cannot hurt you.

## Offensive-security roles

`pentesting`, `osint`, `malware-analysis`, `reverse-engineering`, `ctf-player` and `bug-bounty`
are routed to `ollama-uncensored` (`dolphin-mixtral`) because mainstream models refuse legitimate
authorized security research — a lab you own, a CTF box, an engagement with written permission.

What that routing **is**:

* a way to get direct, technical answers about standard methodology on **your own** targets;
* local-only: those models run on your machine, so your engagement details do not leave it;
* still gated by every Nexus safety layer — the deny list, the approval gate, the path guard.

What it **is not**:

* authorization. A persona prompt grants no legal permission. Testing a system you do not own or
  have written permission to test is a crime in most jurisdictions, and no system prompt changes
  that.
* a jailbreak guarantee. The personas themselves say to refuse anything that clearly targets real
  third parties or causes real-world harm; models vary in how reliably they hold that line, which
  is exactly why the approval gate exists — **you** are the last control.
* a substitute for scope. Keep a scope document, work inside it, and log what you ran.

The personas in `roles.yaml` are written to be blunt about this:

> You are an offensive-security specialist doing authorized penetration testing / CTF work in a
> lab the user owns or has written permission to test. … Still refuse anything clearly illegal
> (targets the user doesn't control/own, real third-party harm).

If you want a stricter setup, point those roles at `claude` instead
(`roles.yaml` → `agent: claude`, or a project-local `.nexus/roles.yaml` override), or add them to
`settings.never_allow_tools` equivalents at the role level with a `tools:` whitelist.

## Supply chain & dependencies

* Runtime deps: `anthropic`, `httpx`, `PyYAML`, `python-dotenv` — nothing else. The server,
  panel and approval machinery are stdlib.
* `gh_create_pr` prefers the local `gh` CLI and its existing auth session; the REST fallback needs
  a `GITHUB_TOKEN` you provide.
* CI (`.github/workflows/ci.yml`) runs on ubuntu with `permissions: contents: read`, tests three
  Python versions, validates the role data and smoke-tests the CLI. No secrets are needed for the
  test suite — it is entirely offline.

## Reporting a problem

If you find a way to make Nexus damage a repo or leak a secret in a way these controls should have
prevented, open an issue with the exact task, flags and tool sequence. Deny-list gaps are treated
as bugs.
