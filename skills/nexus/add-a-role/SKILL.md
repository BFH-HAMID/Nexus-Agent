---
name: add-a-role
description: Add a new specialist role to Nexus.
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

# Add A Role Skill

## When to Use
The user wants a new specialist ("a K8s reviewer", "a copywriter", "a SRE") or the
router keeps picking the wrong existing role.

## Procedure
1. Open `roles.yaml` and pick the closest existing category block.
2. Add a block with these keys:
   ```yaml
   my-role:
     category: engineering
     agent: gemini            # must exist in agents.yaml
     description: One sentence on when to use it.
     system_prompt: |
       You are ... (repeat the role, the scope, the output format)
     keywords: ["three to six", "distinct", "routing", "phrases"]
     aliases: ["short name"]
   ```
3. Keep `keywords` DISJOINT from neighbouring roles - overlapping keywords make
   routing flip-flop.
4. Verify:
   ```bash
   python scripts/build_roles.py --check   # generated docs/README stay in sync
   nexus roles --search <keyword>
   nexus ask "..." --role my-role
   ```
5. Add a `tests/` assertion only if the role is load-bearing (counts change).

## Pitfalls
- `Risk`/agent typos fail at load time, not at call time.
- Adding a role changes the role count advertised in README/docs/CI: run the
  build script instead of editing those numbers by hand.

## Verification
`nexus roles --json | jq '.[] | select(.name=="my-role")'` returns the role, and
`nexus system --role my-role` shows the prompt it actually sends.
