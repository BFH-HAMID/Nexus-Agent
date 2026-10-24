"""The learning loop: a finished run becomes a skill / memory / profile."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nexus import skills as skilllib
from nexus.learning import LearningLoop, extract_json, run_digest
from nexus.memory import MemoryStore


class StubAgent:
    """Enough of NexusAgent for the learning loop (no provider, no tools)."""

    def __init__(self, workspace: Path, reply: str, *, memory: MemoryStore | None = None):
        self.ws = workspace
        self.reply = reply
        self.events: list[tuple[str, dict]] = []
        self.messages: list[str] = []
        self.memory_store = memory or MemoryStore(workspace / "nexus-home")
        self.active_role = SimpleNamespace(name="backend", agent="fake", model="")
        self.skills_enabled = True
        self.config = SimpleNamespace(
            settings=SimpleNamespace(
                learning=True, learning_agent="", router_agent="", learning_min_tools=4,
                max_turns=24, default_agent="fake",
            ),
            default_agent="fake",
            agents={"fake": SimpleNamespace(name="fake")},
            agent_for_role=lambda role: SimpleNamespace(name="fake"),
        )
        self.providers = SimpleNamespace(call_with_fallback=self._call)

    # -- doubles ---------------------------------------------------------- #
    def _call(self, name, system, messages, tools, **kwargs):
        self.messages.append(system)
        return SimpleNamespace(name=name), SimpleNamespace(text=self.reply, content=[])

    def event(self, type_, **data):
        self.events.append((getattr(type_, "value", str(type_)), data))

    def status(self, message, **data):
        pass

    def emit(self, event):
        pass


def _outcome_agent(tmp_path: Path, body: dict) -> tuple[StubAgent, LearningLoop]:
    import json

    agent = StubAgent(tmp_path, json.dumps(body))
    return agent, LearningLoop(agent)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_extract_json_tolerates_fences_and_prose():
    data = extract_json('sure!\n```json\n{"skill": {"name": "x"}}\n```\ndone')
    assert data == {"skill": {"name": "x"}}
    assert extract_json('{"a": {"b": 1}} trailing') == {"a": {"b": 1}}
    assert extract_json("no json here") is None
    assert extract_json("") is None


def test_run_digest_summarises_the_run():
    result = SimpleNamespace(
        role="backend", tool_calls=5, turns=4, changed_files=["nexus/cli.py"],
        summary="added the flag", text="done", messages=[],
    )
    digest = run_digest(result, "add a --json flag")
    assert "TASK: add a --json flag" in digest
    assert "FILES CHANGED: nexus/cli.py" in digest
    assert "TOOLS CALLED: 5" in digest


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #


def test_should_review_only_non_trivial_successes(tmp_path: Path):
    agent, loop = _outcome_agent(tmp_path, {})
    trivial = SimpleNamespace(ok=True, tool_calls=1)
    meaty = SimpleNamespace(ok=True, tool_calls=9)
    failed = SimpleNamespace(ok=False, tool_calls=9)
    assert not loop.should_review(trivial)
    assert loop.should_review(meaty)
    assert not loop.should_review(failed)

    agent.config.settings.learning = False
    assert not loop.should_review(meaty)


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #


BODY = (
    "# Add A Json Flag Skill\n\n## When to Use\nYou need machine-readable output.\n\n"
    "## Procedure\n1. add the argument\n2. print json.dumps\n\n## Verification\n1. run it\n"
)


def test_apply_creates_a_skill_and_memory(tmp_path: Path):
    agent, loop = _outcome_agent(tmp_path, {})
    outcome = loop.apply(
        {
            "skill": {"name": "Add JSON Flag", "description": "Add a --json flag to a CLI command.",
                      "body": BODY, "tags": ["cli"]},
            "memory": ["nexus/cli.py holds every subcommand"],
            "profile": ["prefers JSON output"],
        },
        # fresh outcome object
        __import__("nexus.learning", fromlist=["LearningOutcome"]).LearningOutcome(),
    )
    assert outcome.skill == "add-json-flag"
    assert outcome.memory and outcome.profile
    path = Path(outcome.skill_path)
    assert path.exists()
    skill = skilllib.load_skill(path)
    assert skill.description.startswith("Add a --json flag")
    assert skill.tags == ["cli"]
    assert skill.provenance.get("created_by") == "learning-loop"
    assert agent.memory_store.entries("user") == ["prefers JSON output"]
    assert any(name == "skill" for name, _ in agent.events)


def test_apply_patches_an_existing_local_skill(tmp_path: Path):
    from nexus.learning import LearningOutcome

    path = skilllib.create_skill(name="deploy-it", description="Deploy the service.", body=BODY)
    agent, loop = _outcome_agent(tmp_path, {})
    outcome = LearningOutcome()
    loop.apply({"patch": {"name": "deploy-it", "description": "Deploy the service safely."}}, outcome)
    assert outcome.patched == "deploy-it"
    assert skilllib.load_skill(path).description == "Deploy the service safely."


def test_imported_skills_are_never_patched(tmp_path: Path):
    # imported skills live under <user root>/hermes-*/ ; patch them by writing the
    # same tree the importer produces into the isolated NEXUS_HOME.
    import os

    from nexus.learning import LearningOutcome

    home = Path(os.environ["NEXUS_HOME"]) / "skills"
    dest = home / "hermes-vendor"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(
        "---\nname: vendor-skill\ndescription: Vendor skill.\nmetadata:\n  nexus:\n"
        "    imported_from: hermes\n---\n\n# Vendor\n",
        encoding="utf-8",
    )
    agent, loop = _outcome_agent(tmp_path, {})
    found = skilllib.get("vendor-skill")
    assert found is not None
    outcome = LearningOutcome()
    loop.apply({"patch": {"name": "vendor-skill", "body": "# hacked\n"}}, outcome)
    assert outcome.patched == ""
    assert "# hacked" not in (dest / "SKILL.md").read_text(encoding="utf-8")


def test_bad_review_is_a_noop(tmp_path: Path):
    agent = StubAgent(tmp_path, "I could not decide.")
    loop = LearningLoop(agent)
    outcome = loop.review(SimpleNamespace(ok=True, tool_calls=6, messages=[], summary="s",
                                          role="backend", text="t"), "task")
    assert not outcome.changed
    assert outcome.notes


def test_review_writes_when_the_model_answers_json(tmp_path: Path):
    import json

    agent = StubAgent(tmp_path, json.dumps({
        "skill": {"name": "ship-small-diffs", "description": "Ship small reviewable diffs.",
                  "body": BODY, "tags": []},
        "patch": None, "memory": [], "profile": [],
    }))
    loop = LearningLoop(agent)
    outcome = loop.review(SimpleNamespace(ok=True, tool_calls=7, messages=[], summary="s",
                                          role="backend", text="t"), "refactor the router")
    assert outcome.skill == "ship-small-diffs"
    assert Path(outcome.skill_path).exists()
