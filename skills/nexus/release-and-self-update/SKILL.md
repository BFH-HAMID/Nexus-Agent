---
name: release-and-self-update
description: Ship a Nexus version and let users update themselves.
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

# Release And Self-Update Skill

## When to Use
Publishing a new Nexus version, or a user asks "how do I update?".

## Procedure
1. Bump `version` in `pyproject.toml` and `nexus/__init__.py` (they must match).
2. Update `CHANGELOG`/README highlights; keep role counts generated
   (`python scripts/build_roles.py`).
3. Gate: see the `verify-before-commit` skill.
4. Commit, push the branch, open/merge the PR.
5. Users update with:
   ```bash
   nexus update          # git pull --ff-only + pip install -e .
   pip install -U git+https://github.com/BFH-HAMID/Nexus-Agent
   ```

## Pitfalls
- `nexus update` refuses to run on a dirty tree (git pull would fail) - commit
  or stash first.
- A wheel install (non-editable) needs pip, not git.

## Verification
`nexus version` prints the new number and `nexus doctor` is green.
