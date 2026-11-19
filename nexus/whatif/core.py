"""What-if service: run a simulation, optionally ask a model an open question, keep a record."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .. import policyfile
from . import policy
from .simulate import simulate_action, simulate_command, simulate_plan

AREA = "whatif"
_RUN = re.compile(r"^(?:what (?:if|happens if|would happen if) (?:i|we|you) )?(?:run|ran|execute|executed)\s+[`'\"]?(?P<cmd>.+?)[`'\"]?\??$", re.I)
_DELETE = re.compile(r"^(?:what (?:if|happens if|would happen if) (?:i|we|you) )?(?:delete|deleted|remove|removed)\s+(?:the file\s+)?[`'\"]?(?P<path>[^\s`'\"?]+)[`'\"]?\??$", re.I)
LIKELIHOODS = ("low", "medium", "high")


class WhatIfError(policy.WhatIfPolicyError):
    """A simulation could not be produced."""


class WhatIf:
    def __init__(self, workspace: Path | str, *, config: Any = None, providers: Any = None, clock=time.time) -> None:
        self.workspace = Path(workspace).resolve()
        self.cfg = policy.require(self.workspace)
        self.config = config
        self.providers = providers
        self.clock = clock

    # -- deterministic simulations ---------------------------------------------- #
    def action(self, tool: str, args: dict[str, Any] | None, *, registry: Any = None) -> dict[str, Any]:
        if not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", tool):
            raise WhatIfError("Name one tool to simulate")
        if args is not None and not isinstance(args, dict):
            raise WhatIfError("Tool arguments must be a JSON object")
        result = simulate_action(self.workspace, tool, args, max_diff_lines=self.cfg["max_diff_lines"],
                                 max_file_bytes=self.cfg["max_file_bytes"], registry=registry)
        return self._record("action", f"{tool}", result)

    def command(self, command: str) -> dict[str, Any]:
        return self._record("command", command[:80], {**simulate_command(command), "executed": False})

    def plan(self, steps: list[dict[str, Any]], *, goal: str = "", subject: str = "") -> dict[str, Any]:
        if not isinstance(steps, list) or not steps or not all(isinstance(step, dict) and "id" in step for step in steps):
            raise WhatIfError("A plan simulation needs a list of steps with ids")
        return self._record("plan", subject or goal[:80], simulate_plan(steps, goal=goal, max_steps=self.cfg["max_steps"]))

    def mission(self, mission: Any) -> dict[str, Any]:
        steps = [{"id": s.id, "title": s.title, "instruction": s.instruction, "risk_level": s.risk_level,
                  "read_only": s.read_only, "depends_on": list(s.depends_on)} for s in mission.steps
                 if getattr(s, "kind", "work") != "critic"]
        return self.plan(steps, goal=mission.goal, subject=mission.id)

    # -- questions ------------------------------------------------------------------ #
    def ask(self, question: str) -> dict[str, Any]:
        """Answer "what if ...". Command/delete questions are simulated; open ones need ``mode: model``."""
        text = " ".join(str(question or "").split())
        if not 3 <= len(text) <= 2000:
            raise WhatIfError("Ask a what-if question of 3-2000 characters")
        match = _RUN.match(text)
        if match:
            return self.command(match.group("cmd"))
        match = _DELETE.match(text)
        if match:
            return self.action("delete_file", {"path": match.group("path")})
        if self.cfg["mode"] != "model":
            raise WhatIfError("Static mode simulates tools, commands and plans. For open questions set mode: model "
                              "in whatif.yaml, or use: nexus whatif action|command|plan")
        return self._forecast(text)

    def _forecast(self, question: str) -> dict[str, Any]:
        from ..untrusted import flag_names, wrap

        if self.config is None:
            from ..config import load_config

            self.config = load_config(self.workspace)
        agent = policyfile.pick_agent(self.config, self.cfg["agent"])
        if policyfile.is_cloud_agent(self.config, agent) and not self.cfg["allow_cloud"]:
            raise WhatIfError("whatif.yaml keeps forecasts local: choose a local agent or set allow_cloud: true")
        providers = self.providers or policyfile.providers_for(self.config)
        wrapped, _ = wrap(question, source="whatif", kind="question")
        try:
            value, usage = policyfile.ask_json(
                providers, agent,
                "You forecast consequences of a hypothetical. You can not act and nothing will be executed. "
                'Answer as JSON: {"outcomes": [{"outcome": str, "likelihood": "low"|"medium"|"high", "why": str}], '
                '"assumptions": [str], "unknowns": [str], "safer_alternative": str}. Give 2-4 outcomes; '
                "say plainly what you can not know.",
                {"question": wrapped}, allow_cloud=self.cfg["allow_cloud"],
                max_tokens=self.cfg["max_tokens_per_call"], workspace=self.workspace)
        except (RuntimeError, ValueError) as exc:  # provider down, or the reply was not JSON
            raise WhatIfError(f"No forecast: agent '{agent}' gave no usable answer ({str(exc)[:200]})") from None
        outcomes = []
        for row in value.get("outcomes") if isinstance(value.get("outcomes"), list) else []:
            if isinstance(row, dict) and str(row.get("likelihood", "")).lower() in LIKELIHOODS and row.get("outcome"):
                outcomes.append({"outcome": " ".join(str(row["outcome"]).split())[:300],
                                 "likelihood": str(row["likelihood"]).lower(),
                                 "why": " ".join(str(row.get("why") or "").split())[:300]})
        if not outcomes:
            raise WhatIfError("The model returned no usable forecast")

        def texts(key):
            raw = value.get(key)
            return [" ".join(str(item).split())[:240] for item in raw[:5]] if isinstance(raw, list) else []

        return self._record("forecast", question[:80], {
            "question": question, "outcomes": outcomes[:4], "assumptions": texts("assumptions"),
            "unknowns": texts("unknowns"),
            "safer_alternative": " ".join(str(value.get("safer_alternative") or "").split())[:300],
            "estimate": True, "injection_flags": flag_names(question), "usage": usage, "executed": False})

    # -- records ---------------------------------------------------------------------- #
    def _record(self, kind: str, subject: str, result: dict[str, Any]) -> dict[str, Any]:
        from ..redaction import redact_text

        record = {"id": policyfile.new_id("whatif"), "kind": kind, "subject": redact_text(str(subject), self.workspace)[:120],
                  "at": self.clock(), "result": json.loads(redact_text(json.dumps(result, ensure_ascii=False, default=str),
                                                                       self.workspace))}
        if self.cfg["store_scenarios"]:
            folder = policyfile.state_dir(self.workspace, AREA) / "scenarios"
            policyfile.write_json(folder / f"{record['id']}.json", record)
            for stale in sorted(folder.glob("whatif-*.json"))[:-self.cfg["keep_scenarios"]]:
                stale.unlink(missing_ok=True)
        policyfile.audit(self.workspace, "whatif.simulate", who="whatif", target=f"{kind}:{record['subject']}"[:200],
                         result="simulated", risk="low", detail={"id": record["id"], "executed": False})
        return record


def scenarios(workspace: Path | str, limit: int = 20) -> list[dict[str, Any]]:
    folder = policyfile.state_dir(workspace, AREA) / "scenarios"
    rows = []
    for path in sorted(folder.glob("whatif-*.json"), reverse=True)[:max(1, min(int(limit), 200))]:
        try:
            row = policyfile.read_json(path, {}, limit=600_000)
        except ValueError:
            continue
        rows.append({k: row.get(k) for k in ("id", "kind", "subject", "at")})
    return rows


def scenario(workspace: Path | str, ident: str) -> dict[str, Any]:
    ident = policyfile.safe_id(ident, label="scenario id", error=WhatIfError)
    row = policyfile.read_json(policyfile.state_dir(workspace, AREA) / "scenarios" / f"{ident}.json", None, limit=600_000)
    if not isinstance(row, dict):
        raise WhatIfError(f"No what-if scenario '{ident}'")
    return row


def render(record: dict[str, Any]) -> str:
    result, kind = record["result"], record["kind"]
    lines = [f"What-if ({kind}; nothing was executed)"]
    if kind == "plan":
        counts = result["counts"]
        lines.append(f"{counts['steps']} step(s): {counts['read_only']} read-only, {counts['irreversible']} irreversible, "
                     f"{counts['external']} external, {counts['high_risk']} high-risk. Risk score {result['risk_score']}/100.")
        lines.append("Worst case: " + result["worst_case"])
        for row in result["steps"]:
            mark = "!" if row["irreversible"] else "→" if row["external"] else " "
            lines.append(f" {mark} {row['id']}: {row['title']} - {', '.join(row['effects'])} [{row['approval']}]")
        lines.append("Rollback: " + result["rollback"])
    elif kind == "forecast":
        lines.extend(f" - ({row['likelihood']}) {row['outcome']}" + (f" - {row['why']}" if row["why"] else "")
                     for row in result["outcomes"])
        if result["unknowns"]:
            lines.append("Unknown: " + "; ".join(result["unknowns"]))
        if result["safer_alternative"]:
            lines.append("Safer: " + result["safer_alternative"])
        lines.append("This is a model estimate, not a measurement.")
    else:
        if result.get("blocked"):
            lines.append("Would not happen: " + result["blocked"])
        lines.extend(" - " + effect for effect in result.get("effects", []))
        lines.append(f"Reversible: {result.get('reversible', 'unknown')}; external: {bool(result.get('external'))}"
                     + (f"; approval: {result['approval']}" if result.get("approval") else ""))
        if result.get("preview"):
            lines.append(result["preview"])
    return "\n".join(lines)
