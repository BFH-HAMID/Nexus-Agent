---
name: add-an-agent
description: Add a model endpoint to agents.yaml with a fallback.
version: 1.0.0
author: NXRD
license: MIT
metadata:
  hermes:
    category: nexus
    tags:
      - nexus
      - workflow
---

# Add An Agent Skill

## When to Use
A new provider/model should be callable by roles, or an existing model id is dead.

## Procedure
1. Add an entry to `agents.yaml` (never inside `roles.yaml`):
   ```yaml
   my-agent:
     provider: openai-compatible          # gemini | deepseek | openrouter | openai-compatible | anthropic
     model: ${MY_MODEL:-vendor/model-id}
     base_url: ${MY_BASE_URL:-https://host/v1}
     api_key_env: MY_API_KEY
     max_tokens: 8192
     supports_tools: true
     tool_mode: native                    # text = JSON tool contract in the prompt
     fallback: gemini                     # must be another agent name
     description: Short, factual.
   ```
2. Point at least one role at it (or set `NEXUS_AGENT`), otherwise it is dead weight.
3. Verify without burning tokens:
   ```bash
   nexus models my-agent        # asks the provider what it serves
   nexus agents                 # confirms it loads + key presence
   nexus ask "say hi" --agent my-agent
   ```
4. HTTP wire tests live in `tests/test_provider_http.py` - extend them if you
   add a provider family.

## Pitfalls
- Do not invent model ids; `nexus models` is the ground truth.
- Keys go in `~/.config/nexus/keys.json` (`nexus keys set <family>`), never a
  committed `.env`.
- A `fallback` loop (A -> B -> A) makes `call_with_fallback` spin; keep chains acyclic.

## Verification
`nexus doctor` is green for the new family and one real call returns text.
