# Permissioned MCP and manifest plugins

`mcp.yaml` is operator-owned and **disabled by default**. Install `.[mcp]` (or
`.[dev]`), explicitly enable the root policy, then declare servers/tools or copy
reviewed `*.mcp.json` files into the configured workspace plugin directory.
Catalog discovery/hot reload reads bounded manifests only: it never imports plugin
Python, starts a server, opens a socket or resolves a secret. The next tool-surface
build observes additions/changes; changing/disabling a manifest revokes a previously
registered capability before further RPCs. Role allowlists and read-only/plan mode
remain authoritative. No second tool permission system or learning KB is added.

```powershell
# From the repository, copy the runnable template into YOUR workspace/plugins.
# Review its Python and manifest first; operator-trusted code is NOT sandboxed.
python -m pip install -e ".[mcp]"
nexus extensions -C WORKSPACE list
nexus extensions -C WORKSPACE run words count '{"text":"one two three"}'
```

The runnable template is `examples/mcp/plugins/word_count.py` with
`words.mcp.json`. The stdlib plugin actually implements initialization, tool discovery
and `tools/call` over newline-delimited JSON-RPC. It can be added without core edits;
only the root operator enable flag and plugin files are needed. The configured plugin
directory (and default `plugins/`) is operator-owned: agent file/Office/snapshot
restore APIs cannot modify it, even with AUTO_ALLOW or a hardlink alias. Install
plugins yourself, not by asking the agent to rewrite this trust boundary. Explicit
workspace argv files up to 1 MB are hashed into the reviewed capability; edits
revoke prebuilt tools. This is not a complete imported-library dependency hash. `@python` resolves
to the current Python executable; other executables must be absolute installed
paths, not PATH lookups, shell command strings or downloads at discovery time.

## Authority, secrets and budgets

Every exposed `mcp_SERVER_TOOL` is **DANGEROUS** and needs a fresh existing
ApprovalGate decision, even if the server advertises read-only annotations,
`permissions.yaml` says AUTO_ALLOW, `--yes` is set or earlier consent was saved.
The human preview includes actual argv/endpoint/cwd, manifest/explicit workspace entry-file SHA, secret-reference
names and deadline. Model schemas do not receive that human review context as
extra authority. The CLI requires an exact `RUN EXTENSION ... DIGEST` phrase.
Existing permission audit and credential pre-log scans apply. Outputs are wrapped
as **untrusted data**, not instructions, executable follow-up or approval authority.

Env/header values must be references, e.g. `{"env":"MY_SERVICE_TOKEN"}` or
`{"keychain":"service-account"}`. Keychain reads use `keyring` service `nexus-mcp`;
missing/locked references fail, never fall back to inline credentials. HTTP header
names are limited to Authorization/X-API-Key. For bearer auth, the referenced value
must include the required `Bearer ` prefix. Child env inherits only basic runtime
variables, not ambient provider/GitHub keys; only explicit references are injected.
Sensitive interpreter/library-search overrides are refused. Known/reference secrets
are masked from outputs; child stderr and raw HTTP errors are never echoed.

Input object schemas are operator-declared and validated (no `$ref` downloads).
Initialization negotiates MCP `2025-03-26`, advertises **no** client sampling,
elicitation or file capabilities, and checks actual `tools/list` schema against the
reviewed declaration before calling. Extra server tools are never registered.
Unknown server-initiated stdio requests are declined, not executed by Nexus.

Session deadline 1–30 seconds, bounded input/output/messages, and per-capability
minute/day call quotas persist in existing Studio state. Calls reserve before IO;
failed attempts do not refund. There are **no retries** of an uncertain tools/call.
Stdout overflow/deadline/cancel/emergency/revocation fails closed; owned stdio groups
are terminated/reaped. HTTP uses explicit HTTPS or numeric/localhost loopback HTTP,
no URL credentials/query, redirects, ambient proxy or cookies; JSON and bounded
response SSE are supported. Legacy two-endpoint SSE transport, pagination, OAuth
flows and protocol versions beyond the declared version are not implied.

This executes explicitly operator-trusted code/network under the current OS user,
**not an OS sandbox**. A third-party program can have wider host/network effects
than its description; review it before consent. Blocking HTTP cannot be unsent;
cancellation is checked at request/chunk boundaries. Hard process death and hostile
self-detaching descendants are not universal cleanup guarantees. Background Jarvis
keeps its pre-existing narrow tool ceiling: it does **not** expose MCP subprocesses
until parent-owned cleanup/isolation is designed and tested. Foreground agents and
the explicit CLI use the tested capability surface today.

Tests include actual child JSON-RPC and a real loopback HTTP/SSE fixture, fresh
approval despite AUTO_ALLOW, schema/secret denial, changed manifest, persistent
quotas, child timeout/overflow/reaping, clean environment and redirect refusal.
They do not certify arbitrary vendor MCP servers or live account permissions.
