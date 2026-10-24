---
name: verify-before-commit
description: Run tests, ruff and config checks before committing.
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

# Verify Before Commit Skill

## When to Use
Before every commit/PR in this repository, and after any change to
`nexus/*.py`, `agents.yaml`, `roles.yaml` or `pyproject.toml`.

## Prerequisites
- `pip install -e '.[dev]'` (pytest + ruff)

## How to Run
```bash
python -m pytest -q
python -m ruff check nexus tests scripts
python scripts/build_roles.py --check
printf '/help\n/exit\n' | python -m nexus --demo >/dev/null
```

## Procedure
1. Run pytest - failures first, in the file you touched.
2. Run ruff; the repo is warning-free, so any new warning is a regression
   (watch for B023 closures in loops).
3. `build_roles.py --check` guarantees README/docs role counts still match
   `roles.yaml`.
4. Smoke the REPL in demo mode (`--demo` never needs a key and never prompts).
5. Only then `git add -A && git commit`.

## Pitfalls
- `nexus chat --demo` auto-denies approvals unless you pass `--yes`.
- Long-running servers must be started as background processes, not inline.

## Verification
All four commands exit 0 and `git status` shows only intended files.
