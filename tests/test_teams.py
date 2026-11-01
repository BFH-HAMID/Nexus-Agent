"""Tests for the team roster in ``docs/teams.md`` and its validator.

Everything here runs offline: the roster is checked against the role packs that
ship in this repo (``roles.yaml`` + ``roles.d/*.yaml``), which is exactly what
``nexus/config.py`` loads.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_teams as bt  # noqa: E402


@pytest.fixture(scope="module")
def roster() -> bt.Roster:
    return bt.parse(bt.DOC.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def owners() -> dict[str, list[str]]:
    return bt.load_packs()


def test_document_structure(roster: bt.Roster) -> None:
    idents = [team.ident for team in roster.teams]
    assert len(idents) == len(set(idents)), "duplicate team id"
    assert sum(1 for ident in idents if ident.isdigit()) == 48
    assert "34" in idents and sum(1 for i in idents if i.startswith("34")) == 11  # 34 + 34A..34J
    assert all(team.assignments for team in roster.teams if not _is_container(team, idents))


def _is_container(team: bt.Team, idents: list[str]) -> bool:
    return any(other.startswith(team.ident) and other != team.ident for other in idents)


def test_roster_is_clean(roster: bt.Roster, owners: dict[str, list[str]]) -> None:
    errors, _warnings, _notes = bt.check(roster, owners)
    assert errors == []


def test_every_role_is_in_a_team(roster: bt.Roster, owners: dict[str, list[str]]) -> None:
    covered = {item.role for team in roster.teams for item in team.assignments}
    assert covered == set(owners)
    assert len(owners) == 1000


def test_counts_are_up_to_date(roster: bt.Roster) -> None:
    doc = bt.DOC.read_text(encoding="utf-8")
    assert bt.render_counts(doc, roster) == doc, (
        "member counts in docs/teams.md are stale - run "
        "`python3 scripts/build_teams.py --write`"
    )


def test_unknown_role_is_rejected(roster: bt.Roster, owners: dict[str, list[str]]) -> None:
    broken = bt.parse(bt.DOC.read_text(encoding="utf-8").replace(
        "| **Scoping** | pentesting, security-audit, bug-bounty | roles.yaml |",
        "| **Scoping** | pentesting, security-audit, made-up-role | roles.yaml |",
    ))
    errors, _warnings, _notes = bt.check(broken, owners)
    assert any("made-up-role" in error for error in errors)


def test_wrong_pack_is_rejected(roster: bt.Roster, owners: dict[str, list[str]]) -> None:
    broken = bt.parse(bt.DOC.read_text(encoding="utf-8").replace(
        "| **Retest** | ctf-player, ctf-mentor | roles.yaml, security-practitioners |",
        "| **Retest** | ctf-player, ctf-mentor | rea |",
    ))
    errors, _warnings, _notes = bt.check(broken, owners)
    assert any("rea" in error for error in errors)


def test_missing_handoff_target_is_rejected(roster: bt.Roster,
                                            owners: dict[str, list[str]]) -> None:
    broken = bt.parse(bt.DOC.read_text(encoding="utf-8").replace(
        "🔁 Handoff → Blue Team (Team 2)", "🔁 Handoff → Ghost Team (Team 99)"
    ))
    errors, _warnings, _notes = bt.check(broken, owners)
    assert any("Team 99" in error for error in errors)


def test_main_check_mode(roster: bt.Roster) -> None:
    assert bt.main(["--check"]) == 0
