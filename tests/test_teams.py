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


# --------------------------------------------------------------------------- #
# the runtime roster (`nexus teams` reads teams.yaml)
# --------------------------------------------------------------------------- #

from nexus.config import load_config  # noqa: E402
from nexus.teams import Stage, Team, load_teams, slugify  # noqa: E402


@pytest.fixture(scope="module")
def book():
    return load_teams(REPO_ROOT)


@pytest.fixture()
def config(monkeypatch):
    """The full installed roster.

    ``tests/conftest.py`` deliberately hides the committed ``roles.d`` packs from
    routing tests; the roster check has to see them, so this fixture puts them
    back for the test that needs the whole 1000-role registry.
    """
    import nexus.config as config_module

    real = config_module.candidate_role_dirs
    committed = config_module.REPO_ROOT / config_module.DEFAULT_ROLES_DIR

    def with_committed_packs(workspace):
        dirs = list(real(workspace))
        if committed not in dirs:
            dirs.append(committed)
        return dirs

    monkeypatch.setattr(config_module, "candidate_role_dirs", with_committed_packs)
    return load_config(REPO_ROOT)


def test_roster_file_matches_document() -> None:
    doc = bt.DOC.read_text(encoding="utf-8")
    roster = bt.parse(doc)
    _data, expected = bt.roster_data(roster)
    assert (REPO_ROOT / "teams.yaml").read_text(encoding="utf-8") == expected, (
        "teams.yaml is stale - run `python3 scripts/build_teams.py --yaml`"
    )


def test_runtime_roster_shape(book) -> None:
    top = [team for team in book if team.number.isdigit()]
    assert len(top) == 48
    assert len(book) == 58  # + the 10 language sub-teams of Team 34
    assert len(book.roles()) == 1000
    red = book.get("red-team")
    assert red is not None and red.number == "1"
    assert isinstance(red, Team)
    assert all(isinstance(stage, Stage) for stage in red.stages)
    assert red.stages[0].name == "Scoping"
    assert "pentesting" in red.roles


def test_roster_lookups(book) -> None:
    assert book.get("1") is book.get("red-team")
    assert book.get("34a").number == "34A"            # number, case-insensitive
    assert book.get("qa-testing-team").number == "7"  # slug
    assert book.get("QA & Testing Team").number == "7"  # title
    assert book.get("language-specific-engineering-teams").children
    assert book.get("34A").number == "34A"
    assert [team.ident for team in book.find("security")]


def test_role_lookup_across_teams(book) -> None:
    hits = book.for_role("bengali-nlp")
    numbers = {team.number for team, _stages in hits}
    assert {"8", "9", "28"} <= numbers
    assert book.for_role("definitely-not-a-role") == []


def test_roster_check_is_clean(book, config) -> None:
    assert book.check(config.roles) == []
    assert book.uncovered(config.roles) == []


def test_stage_lookup_and_helpers(book) -> None:
    team = book.get("red-team")
    assert team.stage("scoping").roles[0] == "pentesting"
    assert team.stage("nope") is None
    assert team.stage_of("forensics") == ["Post-Exploit"]
    assert team.size == len(team.roles)
    assert "stages" in team.to_dict()


def test_slugify_matches_generator() -> None:
    assert slugify("Red Team (Offensive Security)") == "red-team"
    assert slugify("Platform & Infrastructure Team") == "platform-infrastructure-team"


def test_project_roster_overrides_repo_roster(tmp_path) -> None:
    """A project layer merges on top, exactly like roles.yaml / pipelines.yaml."""
    pack = tmp_path / ".nexus"
    pack.mkdir()
    (pack / "teams.yaml").write_text(
        "version: 1\nteams:\n  my-team:\n    number: '99'\n    title: My Team\n"
        "    stages:\n    - name: Build\n      roles: [backend]\n",
        encoding="utf-8",
    )
    book = load_teams(tmp_path)
    assert book.get("my-team") is not None
    assert book.get("my-team").stages[0].roles == ("backend",)
    assert book.get("red-team") is not None, "repo roster must still load underneath"


def test_cli_teams_list_and_check(capsys, config) -> None:
    from nexus.cli import main

    assert main(["teams", "check"]) == 0
    out = capsys.readouterr().out
    assert "48" in out and "1000" in out
    assert main(["teams", "show", "red-team"]) == 0
    assert "Scoping" in capsys.readouterr().out
    assert main(["teams", "role", "bengali-nlp"]) == 0
    assert "nlp-computer-vision-team" in capsys.readouterr().out
