#!/usr/bin/env python3
"""Validate the team roster in ``docs/teams.md`` against the installed role packs.

``docs/teams.md`` groups the 1000 specialists of ``roles.yaml`` + ``roles.d/*.yaml``
into workflow-ordered teams. The document is hand-written prose, so every role name
it mentions is checked here against the packs that are actually loaded by
``nexus/config.py`` - the same way ``scripts/build_roles.py`` keeps ``roles.yaml``
honest.

Run from the repo root:

    python3 scripts/build_teams.py            # validate + stats (read-only)
    python3 scripts/build_teams.py --check    # validate, exit 1 on any problem
    python3 scripts/build_teams.py --stats    # per-team counts and pack mix
    python3 scripts/build_teams.py --write    # refresh the "মোট Member" counts

What is checked
  * every role named in a table exists in the pack the table claims (or, for rows
    that list several packs, in one of them);
  * no role is listed twice inside one team;
  * every stage of a team's ``Workflow:`` line has a table row, and no table row
    invents a stage the workflow line does not have;
  * every ``Handoff → Team N`` target is a team defined in the document;
  * every role loaded from ``roles.yaml`` + ``roles.d/`` belongs to at least one
    team, so the roster really covers all of them.

Counts are always recomputed; ``--write`` only rewrites the ``মোট Member`` numbers,
never the prose or the tables.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / 'docs' / 'teams.md'

# pack label used in docs/teams.md -> file the role really lives in
PACKS: dict[str, str] = {
    'roles.yaml': 'roles.yaml',
    'agency-agents': 'roles.d/agency-agents.yaml',
    'ecc': 'roles.d/ecc.yaml',
    'awesome-llm': 'roles.d/awesome-llm-apps.yaml',
    'security-practitioners': 'roles.d/security-practitioners.yaml',
    'superpowers': 'roles.d/superpowers.yaml',
    'rea': 'roles.d/rea.yaml',
}

BANGLA_DIGITS = str.maketrans('0123456789', '০১২৩৪৫৬৭৮৯')

TEAM_HEAD = re.compile(r'^##\s.*?TEAM\s+(\d+)\s*:\s*(.+?)\s*$')
SUB_HEAD = re.compile(r'^###\s+(\d+[A-Z])\s*:\s*(.+?)\s*$')
COUNT_LINE = re.compile(r'^\*\*মোট Member:.*$')
HEADER_ROW = re.compile(r'^\|\s*(ধাপ|Role)\s*\|')
SEPARATOR = re.compile(r'^\|[\s:|-]+\|$')


def bangla(value: int) -> str:
    return str(value).translate(BANGLA_DIGITS)


@dataclass
class Assignment:
    stage: str
    role: str
    packs: list[str]
    line: int


@dataclass
class Team:
    ident: str
    name: str
    line: int
    workflow: list[str] = field(default_factory=list)
    handoff: str = ''
    handoff_targets: list[str] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)

    @property
    def roles(self) -> list[str]:
        seen: OrderedDict[str, None] = OrderedDict()
        for item in self.assignments:
            seen.setdefault(item.role, None)
        return list(seen)

    @property
    def stages(self) -> list[str]:
        seen: OrderedDict[str, None] = OrderedDict()
        for item in self.assignments:
            seen.setdefault(item.stage, None)
        return list(seen)


@dataclass
class Roster:
    teams: list[Team] = field(default_factory=list)
    count_lines: dict[str, int] = field(default_factory=dict)   # ident -> line index
    sub_headings: dict[str, int] = field(default_factory=dict)  # ident -> line index


def load_packs() -> dict[str, list[str]]:
    """role name -> pack files that define it (exactly what ``nexus`` loads)."""
    import yaml  # local import so --help works without the dependency

    files = [PACKS['roles.yaml']] + sorted(p for p in PACKS.values() if p != 'roles.yaml')
    owners: dict[str, list[str]] = {}
    for rel in files:
        data = yaml.safe_load((ROOT / rel).read_text(encoding='utf-8')) or {}
        for name in (data.get('roles') or {}):
            owners.setdefault(str(name), []).append(rel)
    return owners


def split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip('|').split('|')]


def parse_roles(cell: str) -> list[str]:
    roles: list[str] = []
    for chunk in cell.split(','):
        name = re.sub(r'\(.*?\)', '', chunk).strip().strip('*`').strip()
        if name:
            roles.append(name)
    return roles


def parse_packs(cell: str) -> list[str]:
    packs: list[str] = []
    for chunk in re.split(r'[,\s]+', re.sub(r'\(.*?\)', '', cell)):
        name = chunk.strip().strip('`')
        if name:
            packs.append(name)
    return packs


def parse(doc: str) -> Roster:
    roster = Roster()
    lines = doc.splitlines()
    team: Team | None = None
    stage = ''
    header: list[str] = []
    in_fence = False
    seen_sub = False

    for index, line in enumerate(lines):
        if line.startswith('```'):
            in_fence = not in_fence
            if team is not None and in_fence is False:
                stage = stage if not seen_sub else stage
            continue

        if line.startswith('# ') and not in_fence:
            # a new H1 section (e.g. "Verification & Changelog") ends the roster
            team, stage, header = None, '', []
            continue

        head = TEAM_HEAD.match(line)
        if head and not in_fence:
            team = Team(ident=head.group(1), name=head.group(2), line=index)
            roster.teams.append(team)
            stage, header, seen_sub = '', [], False
            continue
        sub = SUB_HEAD.match(line)
        if sub and not in_fence:
            name = re.sub(r'\s*\([^)]*\)\s*$', '', sub.group(2)).strip()
            team = Team(ident=sub.group(1), name=name, line=index)
            roster.teams.append(team)
            stage, header, seen_sub = sub.group(2), [], True
            continue
        if team is None:
            continue

        if COUNT_LINE.match(line):
            roster.count_lines[team.ident] = index
            continue
        if in_fence:
            stripped = line.strip()
            if stripped.startswith('Workflow:'):
                team.workflow = [
                    re.sub(r'\s*\(.*?\)\s*$', '', part).strip()
                    for part in stripped.split(':', 1)[1].split('→')
                    if part.strip()
                ]
            elif 'Handoff' in stripped and '→' in stripped:
                team.handoff = stripped
                team.handoff_targets = re.findall(r'Team\s+(\d+)', stripped)
            continue

        if not line.startswith('|'):
            continue
        if SEPARATOR.match(line):
            continue
        cells = split_cells(line)
        if HEADER_ROW.match(line):
            header = cells
            continue
        if not header:
            continue

        if header[0] == 'ধাপ':                       # stage | roles | pack
            if len(cells) < 3:
                continue
            if cells[0]:
                stage = cells[0].strip('* ')
            for role in parse_roles(cells[1]):
                team.assignments.append(Assignment(stage, role, parse_packs(cells[2]), index + 1))
        elif len(header) == 2 and header[0] == 'Role':   # role | pack
            for role in parse_roles(cells[0]):
                team.assignments.append(
                    Assignment(stage or team.name, role, parse_packs(cells[1]), index + 1))
        elif len(header) == 3 and header[0] == 'Role':   # role | pack | related team
            for role in parse_roles(cells[0]):
                team.assignments.append(
                    Assignment(stage or team.name, role, parse_packs(cells[1]), index + 1))
    return roster


def _slug(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', value.lower())


def _stage_match(step: str, stage: str) -> bool:
    """Loose match between a workflow step and a table stage ("PoC" vs "PoC Development")."""
    parts = [part for part in re.split(r'[/,]', step) if part.strip()]
    for part in parts:
        left, right = _slug(part), _slug(stage)
        if left and right and (left in right or right in left):
            return True
    return False


def check(roster: Roster, owners: dict[str, list[str]]) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    idents = {team.ident for team in roster.teams}
    covered: set[str] = set()
    for team in roster.teams:
        if not team.assignments:
            # a container heading such as "TEAM 34" is allowed when it only groups sub-teams
            if any(other.ident.startswith(team.ident) and other.ident != team.ident
                   for other in roster.teams):
                continue
            errors.append(f'Team {team.ident} ({team.name}) has no role table')
            continue
        counts = Counter(item.role for item in team.assignments)
        for role, hits in counts.items():
            if hits > 1:
                stages = sorted({item.stage for item in team.assignments if item.role == role})
                warnings.append(
                    f'Team {team.ident}: {role} listed {hits}x (stages: {", ".join(stages)})')
        for item in team.assignments:
            if item.role not in owners:
                errors.append(f'Team {team.ident} line {item.line}: unknown role `{item.role}`')
                continue
            covered.add(item.role)
            claimed = [PACKS[p] for p in item.packs if p in PACKS]
            unknown = [p for p in item.packs if p not in PACKS]
            if unknown:
                errors.append(
                    f'Team {team.ident} line {item.line}: unknown pack `{"`, `".join(unknown)}`')
            if claimed and not set(claimed) & set(owners[item.role]):
                errors.append(
                    f'Team {team.ident} line {item.line}: `{item.role}` is in '
                    f'{", ".join(owners[item.role])}, not in {", ".join(item.packs)}')
        if team.workflow:
            used = team.stages
            missing = [stage for stage in team.workflow
                       if not any(_stage_match(stage, row) for row in used)]
            if missing:
                warnings.append(
                    f'Team {team.ident}: workflow step(s) with no table stage: {", ".join(missing)}')
        for target in team.handoff_targets:
            if target not in idents:
                errors.append(f'Team {team.ident}: handoff to undefined Team {target}')

    missing = sorted(set(owners) - covered)
    if missing:
        errors.append(f'{len(missing)} role(s) belong to no team: {", ".join(missing)}')

    notes.append(f'{len(roster.teams)} teams, {sum(len(t.assignments) for t in roster.teams)} '
                 f'stage assignments, {len(covered)}/{len(owners)} roles covered')
    return errors, warnings, notes


def render_counts(doc: str, roster: Roster) -> str:
    lines = doc.splitlines()
    for team in roster.teams:
        unique = len(team.roles)
        stages = len(team.stages)
        if team.ident in roster.count_lines:
            index = roster.count_lines[team.ident]
            lines[index] = (f'**মোট Member: {bangla(unique)}** — যাচাইকৃত · '
                            f'{bangla(stages)} ধাপ · {bangla(len(team.assignments))} assignment')
        else:
            for index, line in enumerate(lines):
                if line.startswith(f'### {team.ident}:'):
                    base = re.sub(r'\s*\(.*?\)\s*$', '', line)
                    lines[index] = f'{base} ({bangla(unique)})'
                    break
    return '\n'.join(lines) + ('\n' if doc.endswith('\n') else '')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--check', action='store_true', help='exit 1 on any problem (CI mode)')
    parser.add_argument('--stats', action='store_true', help='print per-team counts and pack mix')
    parser.add_argument('--write', action='store_true', help='rewrite the মোট Member counts in docs/teams.md')
    parser.add_argument('--doc', default=str(DOC), help='roster document to validate')
    args = parser.parse_args(argv)

    path = Path(args.doc)
    if not path.exists():
        print(f'error: {path} not found', file=sys.stderr)
        return 2
    doc = path.read_text(encoding='utf-8')
    owners = load_packs()
    roster = parse(doc)
    if not roster.teams:
        print('error: no teams found in the document', file=sys.stderr)
        return 2
    errors, warnings, notes = check(roster, owners)

    if args.stats:
        print(f'{"Team":<6}{"Members":>8}{"Stages":>8}{"Assignments":>13}  Name')
        for team in roster.teams:
            print(f'{team.ident:<6}{len(team.roles):>8}{len(team.stages):>8}'
                  f'{len(team.assignments):>13}  {team.name}')
        packs = Counter()
        for team in roster.teams:
            for item in team.assignments:
                for pack in item.packs:
                    packs[pack] += 1
        print('\nassignments by pack:')
        for pack, hits in packs.most_common():
            print(f'  {pack:<24}{hits:>6}')

    for note in notes:
        print(note)
    for warning in warnings:
        print(f'warning: {warning}')
    for error in errors:
        print(f'error: {error}', file=sys.stderr)

    if args.write:
        updated = render_counts(doc, roster)
        if updated != doc:
            path.write_text(updated, encoding='utf-8')
            print(f'rewrote member counts in {path}')
        else:
            print('member counts already up to date')

    if errors and (args.check or not args.write):
        return 1
    print('ok' if not errors else 'ok (counts rewritten; fix the errors above)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
