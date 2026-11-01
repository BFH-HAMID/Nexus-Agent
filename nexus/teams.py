"""The team roster: 48 workflow-ordered teams over every installed role.

``teams.yaml`` is generated from ``docs/teams.md`` by
``python3 scripts/build_teams.py --yaml``, so the roster the CLI reads and the
document a human reads cannot disagree. This module only *reads* it: a team is a
grouping of existing roles with an ordered ``stages`` list, never a new role, a
new agent or an executable pipeline (that is ``pipelines.yaml``).

Look up:
    nexus teams                 every team, with member/stage counts
    nexus teams show red-team   workflow + each stage's roles
    nexus teams role bengali-nlp  which teams and stages use a role
    nexus teams check           validate the roster against installed roles
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError, RoleRegistry, load_yaml_layers

FILENAME = "teams.yaml"
SCHEMA_VERSION = 1


def slugify(title: str) -> str:
    """Same rule as ``scripts/build_teams.py`` - one slug space, no surprises."""
    import re

    base = re.sub(r"\s*\([^)]*\)\s*$", "", title)
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")


@dataclass(frozen=True)
class Stage:
    """One workflow step of a team and the roles the roster lists for it."""

    name: str
    roles: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "roles": list(self.roles)}


@dataclass(frozen=True)
class Team:
    """A grouping of installed roles - data only, mirroring ``docs/teams.md``."""

    ident: str
    number: str
    title: str
    workflow: tuple[str, ...] = ()
    stages: tuple[Stage, ...] = ()
    emoji: str = ""
    handoff: str = ""
    handoff_teams: tuple[str, ...] = ()
    parent: str = ""
    children: tuple[str, ...] = ()

    # -- lookups ----------------------------------------------------------- #
    @property
    def roles(self) -> tuple[str, ...]:
        """Unique role names, in document order (a role may sit in two stages)."""
        seen: dict[str, None] = {}
        for stage in self.stages:
            for role in stage.roles:
                seen.setdefault(role, None)
        return tuple(seen)

    @property
    def size(self) -> int:
        return len(self.roles)

    @property
    def assignments(self) -> int:
        return sum(len(stage.roles) for stage in self.stages)

    def stage(self, name: str) -> Stage | None:
        wanted = name.strip().lower()
        for stage in self.stages:
            if stage.name.strip().lower() == wanted:
                return stage
        return None

    def stage_of(self, role: str) -> list[str]:
        return [stage.name for stage in self.stages if role in stage.roles]

    def label(self) -> str:
        return f"{self.emoji} {self.title}".strip()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.ident,
            "number": self.number,
            "title": self.title,
            "emoji": self.emoji,
            "workflow": list(self.workflow),
            "handoff": self.handoff,
            "handoff_teams": list(self.handoff_teams),
            "stages": [stage.to_dict() for stage in self.stages],
            "roles": list(self.roles),
            "size": self.size,
        }
        if self.parent:
            data["parent"] = self.parent
        if self.children:
            data["children"] = list(self.children)
        return data


@dataclass
class TeamBook:
    """Every team, in document order, plus the checks that keep it honest."""

    teams: dict[str, Team] = field(default_factory=dict)
    sources: list[Path] = field(default_factory=list)

    # -- container protocol ------------------------------------------------- #
    def __len__(self) -> int:
        return len(self.teams)

    def __iter__(self) -> Iterator[Team]:
        return iter(self.teams.values())

    def __contains__(self, query: object) -> bool:
        return isinstance(query, str) and self.get(query) is not None

    # -- lookups ------------------------------------------------------------ #
    def get(self, query: str) -> Team | None:
        """Resolve ``red-team``, ``1``, ``34a`` or a title/slug to one team."""
        needle = (query or "").strip().lower()
        if not needle:
            return None
        if needle in self.teams:
            return self.teams[needle]
        for team in self.teams.values():
            if (needle == team.number.lower() or needle == slugify(team.title)
                    or needle == team.title.strip().lower()):
                return team
        return None

    def find(self, query: str) -> list[Team]:
        """Every team whose id, number or title contains ``query``."""
        needle = (query or "").strip().lower()
        if not needle:
            return []
        return [team for team in self.teams.values()
                if needle in team.ident or needle in team.number.lower()
                or needle in team.title.lower()]

    def for_role(self, role: str) -> list[tuple[Team, list[str]]]:
        needle = (role or "").strip().lower()
        hits: list[tuple[Team, list[str]]] = []
        for team in self.teams.values():
            stages = [stage.name for stage in team.stages
                      if any(r.lower() == needle for r in stage.roles)]
            if stages:
                hits.append((team, stages))
        return hits

    def roles(self) -> set[str]:
        return {role for team in self.teams.values() for role in team.roles}

    def stage_names(self) -> list[str]:
        seen: dict[str, None] = {}
        for team in self.teams.values():
            for stage in team.stages:
                seen.setdefault(stage.name, None)
        return list(seen)

    def to_dict(self) -> dict[str, Any]:
        return {"teams": [team.to_dict() for team in self.teams.values()]}

    # -- integrity ---------------------------------------------------------- #
    def check(self, registry: RoleRegistry) -> list[str]:
        """Unknown roles and dangling references - the roster must name real roles."""
        errors: list[str] = []
        known = set(registry.names())
        for team in self.teams.values():
            if not team.stages and not team.children:
                errors.append(f"team '{team.ident}' has no stages")
            for stage in team.stages:
                for role in stage.roles:
                    if role not in known:
                        errors.append(
                            f"team '{team.ident}' stage '{stage.name}' names unknown role '{role}'")
            for target in team.handoff_teams:
                if target not in self.teams:
                    errors.append(f"team '{team.ident}' hands off to unknown team '{target}'")
            if team.parent and team.parent not in self.teams:
                errors.append(f"team '{team.ident}' names unknown parent '{team.parent}'")
            for child in team.children:
                if child not in self.teams:
                    errors.append(f"team '{team.ident}' names unknown child '{child}'")
        return errors

    def uncovered(self, registry: RoleRegistry) -> list[str]:
        """Installed roles the roster does not place (expected for project-local roles)."""
        return sorted(set(registry.names()) - self.roles())


def _stage_list(raw: Any, source: Path, ident: str) -> tuple[Stage, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{source}: team '{ident}' stages must be a list")
    stages: list[Stage] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError(f"{source}: team '{ident}' has a non-mapping stage")
        name = str(entry.get("name") or "").strip()
        roles = entry.get("roles") or []
        if not name:
            raise ConfigError(f"{source}: team '{ident}' has a stage with no name")
        if not isinstance(roles, list) or any(not isinstance(r, str) or not r.strip() for r in roles):
            raise ConfigError(f"{source}: team '{ident}' stage '{name}' roles must be role names")
        stages.append(Stage(name=name, roles=tuple(r.strip() for r in roles)))
    return tuple(stages)


def _str_list(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def parse_teams(data: dict[str, Any], source: Path) -> TeamBook:
    """Turn one ``teams.yaml`` layer into a :class:`TeamBook`."""
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a mapping")
    version = data.get("version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ConfigError(f"{source}: unsupported roster version {version!r}")
    raw_teams = data.get("teams")
    if not isinstance(raw_teams, dict) or not raw_teams:
        raise ConfigError(f"{source}: `teams` must be a non-empty mapping")
    book = TeamBook(sources=[source])
    for ident, cfg in raw_teams.items():
        slug = str(ident).strip()
        if not slug:
            raise ConfigError(f"{source}: a team has an empty id")
        if not isinstance(cfg, dict):
            raise ConfigError(f"{source}: team '{slug}' must be a mapping")
        title = str(cfg.get("title") or "").strip()
        if not title:
            raise ConfigError(f"{source}: team '{slug}' has no title")
        book.teams[slug] = Team(
            ident=slug,
            number=str(cfg.get("number") or "").strip(),
            title=title,
            workflow=_str_list(cfg.get("workflow")),
            stages=_stage_list(cfg.get("stages"), source, slug),
            emoji=str(cfg.get("emoji") or "").strip(),
            handoff=str(cfg.get("handoff") or "").strip(),
            handoff_teams=_str_list(cfg.get("handoff_teams")),
            parent=str(cfg.get("parent") or "").strip(),
            children=_str_list(cfg.get("children")),
        )
    return book


def load_teams(workspace: Path | str | None = None) -> TeamBook:
    """Every ``teams.yaml`` layer, lowest precedence first (project overrides repo)."""
    ws = Path(workspace).resolve() if workspace else Path.cwd()
    merged, used = load_yaml_layers(FILENAME, ws)
    if not merged:
        return TeamBook()
    return parse_teams(merged, used[-1] if used else Path(FILENAME))
