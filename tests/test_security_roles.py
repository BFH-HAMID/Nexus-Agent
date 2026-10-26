"""Validate the real, merged security roster without requiring model calls."""
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from nexus.agent import render_system_prompt
from nexus.config import build_registry, load_config
from nexus.router import Router

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "roles.d/security-practitioners.yaml"
DATA = yaml.safe_load(PACK.read_text(encoding="utf-8"))
NEW = {
    "container-kubernetes-security-specialist", "ics-scada-security-specialist",
    "iot-security-tester", "llm-ai-red-teamer", "ctf-mentor",
    "web-application-pentester", "api-security-tester", "mobile-app-pentester",
    "wireless-security-auditor", "active-directory-attack-specialist", "cloud-pentester",
    "social-engineering-analyst", "physical-security-assessor", "soc-analyst",
    "siem-engineer", "purple-team-coordinator", "security-hardening-advisor",
    "zero-trust-architect", "deception-technology-specialist", "soar-engineer",
    "memory-forensics-analyst", "network-forensics-analyst", "mobile-forensics-examiner",
    "dark-web-monitoring-analyst", "attack-surface-management-specialist",
}
EXPERT_ADDITIONS = {
    "coverage-guided-fuzzing-expert",
    "vulnerability-root-cause-researcher",
    "browser-security-researcher",
    "oauth-oidc-security-specialist",
    "saml-federation-security-assessor",
    "network-protocol-security-researcher",
    "firmware-security-researcher",
    "software-supply-chain-security-researcher",
    "ci-cd-pipeline-pentester",
    "saas-tenant-isolation-assessor",
    "hardware-root-of-trust-security-assessor",
    "database-security-pentester",
}
NEW |= EXPERT_ADDITIONS

EXISTING = {
    "Secure Code Reviewer": "application-security-engineer",
    "DevSecOps Engineer": "devsecops",
    "Cryptography Advisor": "cryptography",
    "Threat Modeling Expert": "security-architect",
    "Compliance & Risk Analyst": "compliance",
    "Red Team Operator": "pentesting",
    "Bug Bounty Hunter Coach": "bug-bounty",
    "Threat Hunter": "security-threat-detection-engineer",
    "Incident Responder": "security-incident-responder",
    "Detection Engineer": "blue-team",
    "Digital Forensics Investigator": "forensics",
    "Malware Analyst": "malware-analysis",
    "Reverse Engineer": "reverse-engineering",
    "OSINT Investigator": "osint",
    "Cyber Threat Intelligence Analyst": "security-threat-intelligence-analyst",
}


def layers():
    paths = [ROOT / "roles.yaml", *sorted((ROOT / "roles.d").glob("*.yaml"))]
    return [(yaml.safe_load(p.read_text(encoding="utf-8")), str(p)) for p in paths if p != PACK]


@pytest.fixture(scope="module")
def registry():
    return build_registry([*layers(), (DATA, str(PACK))])


def test_exact_additions_and_all_existing_definitions_preserved(registry):
    before = build_registry(layers())
    assert set(DATA["roles"]) == NEW
    assert len(EXISTING) + len(NEW) == 52
    assert not NEW.intersection(before.roles)
    assert set(registry.roles) - set(before.roles) == NEW
    for name, old in before.roles.items():
        assert asdict(registry.roles[name]) == asdict(old), name
    for name in EXISTING.values():
        assert name in before.roles
    assert before.get("red-team").name == "pentesting"
    assert "threat hunter" in before.get(EXISTING["Threat Hunter"]).persona.lower()


@pytest.mark.parametrize("name", sorted(NEW))
def test_each_specialization_reaches_system_prompt_with_common_rules(registry, name, tmp_path):
    role = registry.roles[name]
    raw = DATA["roles"][name]
    assert role.category == "security"
    assert role.persona == raw["persona"]
    assert role.extra_instructions == DATA["security_practitioner_rules"]
    assert not role.tools  # No additional tool grants.
    prompt = render_system_prompt(role=role, agent_label="test", workspace=tmp_path)
    assert role.persona in prompt
    assert role.extra_instructions.strip() in prompt
    for required in (
        "scope assumption in one line", "approval gates", "Never invent CVEs",
        "verification method", "working malware or ransomware",
        "unauthorized third-party targeting", "at most one clarifying question",
        "Summary -> Findings/Steps -> Evidence/Commands -> Risk rating -> Remediation -> References",
        "MITRE ATT&CK, OWASP, CIS, or NIST",
    ):
        assert required in prompt


def test_real_loader_and_all_specialist_keyword_and_forced_routes(monkeypatch, tmp_path):
    # Deliberately opt into committed packs normally excluded by conftest.
    import nexus.config as config
    monkeypatch.setattr(config, "candidate_role_dirs", lambda _: [ROOT / "roles.d"])
    cfg = load_config(tmp_path, load_pipelines=False)
    router = Router(cfg)
    for name in sorted(NEW):
        role = cfg.roles.roles[name]
        assert role.agent in cfg.agents
        forced = router.route("Review the authorized lab.", forced=name)
        assert forced.role.name == name
        assert forced.method == "manual"
        task = DATA["roles"][name]["keywords"][0]["text"]
        decision = router.keyword_route(task)
        assert decision is not None, name
        assert decision.role.name == name, (name, decision)


def test_distributions_include_existing_and_new_packs():
    tomllib = pytest.importorskip("tomllib")
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = cfg["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"]["force-include"]["roles.d"] == "nexus/_config/roles.d"
    assert "roles.d" in targets["sdist"]["include"]


def test_section_e_personas_preserve_supplied_prompts_verbatim():
    # Frozen digests of the five supplied prompts, not regenerated from the YAML.
    expected = {
        "container-kubernetes-security-specialist": "21aabbfa6486ab68b6073e47dc1328454d3bd0a21216aa6ab267ce8bfbde9348",
        "ics-scada-security-specialist": "e1b81d70fda1a8cd7f0b9d7a5ca66e8af76bdeb2767c53470aba877604c8809b",
        "iot-security-tester": "35d340661ff021b265d7334812f42a4f362bdceccf3ab8a468b9d982a77ec3c4",
        "llm-ai-red-teamer": "e10fa55a3f909b7b532ee2bba67b4b4d305d309d50f1eb3b99d5eed41fd3018c",
        "ctf-mentor": "0c42f3e6acb611858abd2568ec02aea33be807fcee86edc0c09a2fe98100a667",
    }
    for name, digest in expected.items():
        assert hashlib.sha256(DATA["roles"][name]["persona"].encode()).hexdigest() == digest


def test_previous_25_security_definitions_remain_unchanged():
    previous = {k: v for k, v in DATA["roles"].items() if k not in EXPERT_ADDITIONS}
    assert len(previous) == 25
    digest = hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()
    assert digest == "b8ea0f8b06d56bb33acafd782aacc18be45c880a750538b12385b9adce25dcf3"


@pytest.mark.parametrize("name", sorted(EXPERT_ADDITIONS))
def test_expert_personas_have_methodology_and_verifiable_deliverables(name):
    persona = DATA["roles"][name]["persona"]
    assert persona.startswith("You are a senior ")
    assert "Deliver " in persona
    assert len(persona) > 600
    assert "evidence" in persona.lower() or "verification" in persona.lower()
