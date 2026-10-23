<!--
Nexus Agent fills the two placeholders below automatically (nexus/tools.py: pr_body)
and appends a "Nexus run" footer with role, branch, commits and diffstat.
-->

## What changed

<!-- summary -->

## Why

<!-- motivation -->

## How it was done

<!-- approach, alternatives considered, anything a reviewer should verify by hand -->

## Role / agent

<!-- e.g. backend -> hermes (hermes3:8b) -->

## Testing

- [ ] `nexus doctor` is green
- [ ] `python -m pytest -q` passes
- [ ] Manual check performed (describe it)

## Checklist

- [ ] No secrets, tokens or `.env` content committed
- [ ] Docs/README updated if behaviour changed
- [ ] Breaking changes called out above
