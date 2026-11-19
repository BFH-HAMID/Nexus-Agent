"""Deterministic "what would happen" analysis. Nothing in this module executes anything.

``simulate_action``  one tool call  -> files touched, diff preview, reversibility, approvals
``simulate_command`` a shell line    -> classified effects (never run, never parsed by a shell)
``simulate_plan``    plan steps      -> per-step effects, waves, irreversible/external counts, worst case

The module imports no tool handler and no subprocess API.  File access is
read-only, confined to the workspace by ``workbench.safe_path`` and bounded.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from ..types import RiskClass

READ_COMMANDS = frozenset({"ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "pwd", "echo", "stat", "file",
                           "which", "whoami", "date", "df", "du", "tree", "diff", "env", "printenv", "ps", "uname"})
RUNS_CODE = frozenset({"python", "python3", "pytest", "node", "npm", "npx", "yarn", "pnpm", "make", "bash", "sh",
                       "cargo", "go", "mvn", "gradle", "ruby", "php", "deno", "bun", "tox", "uv", "pipx"})
NETWORK = frozenset({"curl", "wget", "ssh", "scp", "rsync", "ftp", "nc", "ncat", "telnet", "ping", "dig", "nslookup"})


class _Later:
    """``start`` followed by ``target`` before the next ``stops`` character (or exactly at it).

    Written as one pattern (``start[^stops]*target``) this is quadratic on a line that repeats
    ``start``: 30,000 characters of "dd dd dd ..." took 1.3 s and ten times that two minutes.
    Here every stretch of the line is looked at once.
    """

    def __init__(self, start: str, stops: str, target: str, *, at_stop: bool = False) -> None:
        self.start, self.target, self.at_stop = re.compile(start), re.compile(target), at_stop
        self.stops = re.compile("[" + re.escape(stops) + "]")

    def search(self, text: str) -> bool:
        seen = 0
        for match in self.start.finditer(text):
            if match.start() < seen:
                continue  # inside a stretch that was already looked at
            stop = self.stops.search(text, match.end())
            end = stop.start() if stop else len(text)
            if self.target.match(text, end) if self.at_stop else self.target.search(text, match.end(), end):
                return True
            seen = end
        return False


_DESTRUCTIVE = (
    (re.compile(r"\brm\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf]", re.I), "deletes files recursively/forcibly"),
    (re.compile(r"\brm\b"), "deletes files"),
    (re.compile(r"\b(?:mkfs|fdisk|parted|wipefs)\b"), "overwrites a disk or partition"),
    (_Later(r"\bdd\s+", "|", r"\bof="), "overwrites a disk or partition"),
    # a flag of its own (--force, -f, -uf) or a "+refspec"; a branch called "feature-f" is not a force push
    (_Later(r"\bgit\s+push\b", "|;&", r"(?<!\S)(?:--force(?:-with-lease)?\b|-[a-zA-Z]{0,8}f[a-zA-Z]{0,8}(?!\S)|\+[\w./-])"),
     "rewrites remote git history"),
    (re.compile(r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s|restore\s)"), "discards uncommitted work"),
    (re.compile(r"\bgit\s+(?:branch\s+-D|tag\s+-d)"), "deletes a branch or tag"),
    (_Later(r"\bgit\s+push\s+", "|;&", r"--delete"), "deletes a branch or tag"),
    (re.compile(r"\b(?:drop|truncate)\s+(?:table|database|schema)\b|\bdelete\s+from\b", re.I), "destroys database rows"),
    (re.compile(r"\b(?:shutdown|reboot|halt|poweroff|init\s+0)\b"), "stops or restarts the machine"),
    (re.compile(r"\b(?:kill|pkill|killall)\b"), "terminates processes"),
    (re.compile(r"\bchmod\s+(?:-R\s+)?[0-7]*7[0-7]{2}\b|\bchown\s+-R\b"), "changes permissions broadly"),
    (_Later(r"curl|wget", "|;&", r"\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b", at_stop=True), "downloads and executes remote code"),
    (re.compile(r"\bsudo\b"), "runs with elevated privileges"),
    (re.compile(r">\s*/dev/sd|>\s*/etc/|\btee\s+/etc/"), "writes to a system path"),
    (re.compile(r"\b(?:docker|podman)\s+(?:system\s+prune|rm|rmi|volume\s+rm)\b"), "removes containers, images or volumes"),
    (re.compile(r"\b(?:kubectl|helm)\s+(?:delete|uninstall)\b|\bterraform\s+(?:destroy|apply)\b"), "changes live infrastructure"),
)
_EXTERNAL = (
    (re.compile(r"\bgit\s+push\b"), "publishes commits to a remote"),
    (re.compile(r"\b(?:npm|yarn|pnpm)\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bgh\s+release\b"), "publishes a package or release"),
    (re.compile(r"\b(?:docker|podman)\s+push\b"), "pushes an image to a registry"),
    (re.compile(r"\bgh\s+pr\s+(?:create|merge|close)\b|\bgh\s+issue\s+(?:create|close)\b"), "changes GitHub state"),
    (re.compile(r"\b(?:mail|sendmail|mutt)\b"), "sends e-mail"),
    (re.compile(r"\b(?:kubectl|helm)\s+(?:apply|install|upgrade|rollout)\b|\b(?:vercel|netlify|fly|heroku)\b"), "deploys to a live environment"),
)
_INSTALL = re.compile(r"\b(?:pip3?|npm|yarn|pnpm|apt(?:-get)?|brew|cargo|gem|go)\s+(?:install|add|i|upgrade|update)\b")
_STEP_WORDS: tuple[tuple[str, re.Pattern[str], bool, bool], ...] = (
    # effect, pattern, external, irreversible
    ("deploys or publishes", re.compile(r"\b(?:deploy|publish|release|ship to prod|go live|rollout|push to (?:main|prod))", re.I), True, True),
    ("sends a message", re.compile(r"\b(?:send|e-?mail|notify|post to|tweet|message|sms|telegram|slack)\b", re.I), True, True),
    ("spends money", re.compile(r"\b(?:pay|purchase|buy|charge|transfer|invoice|subscribe|order)\b", re.I), True, True),
    ("deletes data", re.compile(r"\b(?:delete|remove|drop|wipe|purge|destroy|truncate|uninstall)\b", re.I), False, True),
    ("changes a database", re.compile(r"\b(?:migrat\w+|schema change|alter table|database|backfill)\b", re.I), False, True),
    ("changes access or secrets", re.compile(r"\b(?:rotate|revoke|grant|permission|credential|secret|api key|token)\b", re.I), True, True),
    ("opens a pull request", re.compile(r"\b(?:pull request|open a pr|create a pr|merge)\b", re.I), True, False),
    ("installs dependencies", re.compile(r"\b(?:install|upgrade|bump|dependency|dependencies)\b", re.I), False, False),
    ("edits files", re.compile(r"\b(?:implement|write|edit|refactor|fix|create|add|update|generate|build|code)\b", re.I), False, False),
    ("reads only", re.compile(r"\b(?:review|analy[sz]e|audit|inspect|read|research|plan|design|test|verify|check|lint|summari[sz]e)\b", re.I), False, False),
)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def simulate_command(command: str) -> dict[str, Any]:
    """Classify a shell line by inspection only."""
    text = str(command or "").strip()
    effects: list[str] = []
    for pattern, effect in _DESTRUCTIVE:
        if pattern.search(text) and effect not in effects:
            effects.append(effect)
    destructive = bool(effects)
    external = False
    for pattern, effect in _EXTERNAL:
        if pattern.search(text):
            external = True
            if effect not in effects:
                effects.append(effect)
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    programs = [Path(word).name for word in words if word and not word.startswith("-") and "=" not in word[:1]]
    first = programs[0] if programs else ""
    chained = bool(re.search(r"[;&|`]|\$\(", text))
    network = external or bool(set(programs) & NETWORK) or bool(_INSTALL.search(text))
    if _INSTALL.search(text):
        effects.append("installs packages (network, changes the environment)")
    runs_code = first in RUNS_CODE or (chained and bool(set(programs) & RUNS_CODE))
    if runs_code and not destructive:
        effects.append("executes project code; its effects depend on that code")
    read_only = bool(first) and first in READ_COMMANDS and not chained and not destructive and not external and ">" not in text
    if first == "git" and len(programs) > 1 and not destructive and not external:
        read_only = programs[1] in {"status", "diff", "log", "show", "branch", "remote", "describe", "ls-files", "rev-parse"}
        if not read_only:
            effects.append(f"changes the local repository (git {programs[1]})")
    if read_only:
        effects = ["reads only"]
    reversible = "yes" if read_only else "no" if destructive or external else "unknown"
    return {"command": _clip(text, 300), "program": first, "read_only": read_only, "destructive": destructive,
            "external": external, "network": network, "runs_code": runs_code, "chained": chained,
            "effects": effects or ["effect can not be determined statically"], "reversible": reversible}


def _file_view(workspace: Path, path: str, max_bytes: int) -> tuple[Path, str | None, str]:
    """``(resolved, text|None, note)`` - read-only, inside the workspace, bounded."""
    from ..workbench import safe_path

    target = safe_path(workspace, path)
    if not target.exists():
        return target, None, "does not exist"
    if target.is_symlink() or not target.is_file():
        return target, None, "not a regular file"
    if target.stat().st_size > max_bytes:
        return target, None, f"larger than {max_bytes} bytes; not diffed"
    try:
        return target, target.read_text(encoding="utf-8"), ""
    except (UnicodeDecodeError, OSError):
        return target, None, "binary or unreadable; not diffed"


def simulate_action(workspace: Path | str, tool: str, args: dict[str, Any] | None, *, max_diff_lines: int = 60,
                    max_file_bytes: int = 400_000, registry: Any = None) -> dict[str, Any]:
    """Predict one tool call. Returns a plain dict; never raises for a bad path, it reports ``blocked``."""
    from ..autonomy import _PROTECTED_POLICY_NAMES, classify_tool_risk
    from ..tools import unified_diff_snippet

    workspace = Path(workspace).resolve()
    args = dict(args or {})
    name = str(tool or "")
    risk = classify_tool_risk(name, args, registry=registry)
    out: dict[str, Any] = {
        "tool": name, "risk_class": risk.value if hasattr(risk, "value") else str(risk), "would_change": [],
        "effects": [], "external": False, "network": False, "reversible": "unknown", "blocked": "", "preview": "",
        "approval": {RiskClass.SAFE: "none", RiskClass.MEDIUM: "notify or ask (per autonomy level)"}.get(
            risk, "fresh human approval every time"),
        "executed": False,
    }
    path = str(args.get("path") or "")
    if name in {"write_file", "edit_file", "delete_file"}:
        if Path(path.replace("\\", "/")).name.casefold() in _PROTECTED_POLICY_NAMES:
            out.update(blocked="operator-owned policy file: the permission gate refuses this", reversible="yes")
            return out
        try:
            target, text, note = _file_view(workspace, path, max_file_bytes)
        except (ValueError, OSError) as exc:
            out.update(blocked=f"path refused: {str(exc)[:120]}", reversible="yes")
            return out
        relative = str(target.relative_to(workspace)) if target.is_relative_to(workspace) else path
        out["would_change"] = [relative]
        if name == "write_file":
            new = str(args.get("content") or "")
            if args.get("append") and text is not None:
                new = text + new
            exists = target.exists()
            out["effects"] = [("overwrites" if exists else "creates") + f" {relative} ({len(new.encode())} bytes)"]
            out["preview"] = (unified_diff_snippet(relative, text or "", new, max_diff_lines)
                              if text is not None or not exists else note)
        elif name == "edit_file":
            old, new = str(args.get("old_text") or ""), str(args.get("new_text") or "")
            if text is None:
                out.update(blocked=f"edit would fail: file {note}", reversible="yes")
                return out
            count = text.count(old) if old else 0
            if count == 0:
                out.update(blocked="edit would fail: old_text not found in the file", reversible="yes")
                return out
            replaced = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
            out["effects"] = [f"edits {relative}: {count if args.get('replace_all') else 1} of {count} occurrence(s) replaced"]
            out["preview"] = unified_diff_snippet(relative, text, replaced, max_diff_lines)
        else:
            if not target.exists():
                out.update(blocked="delete would fail: path does not exist", reversible="yes")
                return out
            size = target.stat().st_size if target.is_file() else 0
            out["effects"] = [f"deletes {relative}" + (f" ({size} bytes)" if size else "")]
        out["reversible"] = "snapshot or git (if tracked/enabled)"
        return out
    if name == "run_command":
        info = simulate_command(str(args.get("command") or ""))
        out.update(effects=info["effects"], external=info["external"], network=info["network"],
                   reversible=info["reversible"], command=info)
        return out
    if name == "git":
        raw = args.get("args")
        parts = [str(part) for part in raw] if isinstance(raw, list) else str(raw or "").split()
        info = simulate_command("git " + " ".join(parts))
        out.update(effects=info["effects"], external=info["external"], network=info["network"],
                   reversible=info["reversible"], command=info)
        return out
    low = name.casefold()
    external = bool(re.search(r"send|post|publish|deploy|push|pay|spend|email|message|telegram|mcp_|gh_|browser|home_|hardware", low))
    destructive = bool(re.search(r"delete|remove|drop|wipe|destroy|forget|revoke", low))
    out.update(
        external=external, network=external,
        effects=[("acts outside this machine" if external else "") or ("removes data" if destructive else "")
                 or ("reads only" if risk is RiskClass.SAFE else "changes local state")],
        reversible="yes" if risk is RiskClass.SAFE else "no" if external or destructive else "unknown")
    return out


def _waves(steps: list[dict[str, Any]]) -> list[list[str]]:
    """Dependency layers; steps in a cycle or with unknown dependencies end in a final layer."""
    remaining = {str(step["id"]): {str(d) for d in step.get("depends_on") or []} for step in steps}
    done: set[str] = set()
    waves: list[list[str]] = []
    while remaining:
        ready = sorted(key for key, deps in remaining.items() if deps <= done)
        if not ready:
            waves.append(sorted(remaining))
            break
        waves.append(ready)
        done.update(ready)
        for key in ready:
            del remaining[key]
    return waves


def simulate_plan(steps: list[dict[str, Any]], *, goal: str = "", max_steps: int = 40) -> dict[str, Any]:
    """Static forecast of a plan. ``steps`` are dicts with id/title/instruction/risk_level/read_only/depends_on."""
    rows: list[dict[str, Any]] = []
    for step in list(steps)[:max_steps]:
        text = f"{step.get('title', '')} {step.get('instruction', '')}"
        risk = str(step.get("risk_level") or "medium")
        effects, external, irreversible = [], False, False
        if step.get("read_only"):
            effects = ["reads only"]
        else:
            for effect, pattern, is_external, is_irreversible in _STEP_WORDS:
                if pattern.search(text) and effect != "reads only":
                    effects.append(effect)
                    external = external or is_external
                    irreversible = irreversible or is_irreversible
            if not effects:
                effects = ["reads only"] if _STEP_WORDS[-1][1].search(text) else ["changes local state"]
        if risk == "critical" and not step.get("read_only"):
            irreversible = True
        rows.append({
            "id": str(step.get("id")), "title": _clip(str(step.get("title") or ""), 120), "risk_level": risk,
            "effects": effects[:4], "external": external, "irreversible": irreversible,
            "approval": "none" if effects == ["reads only"] else
                        "fresh human approval" if irreversible or external or risk in {"high", "critical"} else
                        "notify or ask (per autonomy level)",
        })
    irreversible_rows = [row for row in rows if row["irreversible"]]
    external_rows = [row for row in rows if row["external"]]
    high = [row for row in rows if row["risk_level"] in {"high", "critical"}]
    score = min(100, 12 * len(irreversible_rows) + 10 * len(external_rows) + 8 * len(high)
                + 2 * sum(1 for row in rows if row["effects"] != ["reads only"]))
    worst = irreversible_rows or external_rows or high
    worst_case = (f"'{worst[0]['title']}' {worst[0]['effects'][0]} and can not be undone by Nexus"
                  if irreversible_rows else
                  f"'{worst[0]['title']}' {worst[0]['effects'][0]} outside this machine" if external_rows else
                  f"'{worst[0]['title']}' is marked {worst[0]['risk_level']} risk" if high else
                  "local, reversible changes only")
    return {
        "goal": _clip(str(goal), 300), "steps": rows, "waves": _waves(list(steps)[:max_steps]),
        "truncated": len(steps) > max_steps,
        "counts": {"steps": len(rows), "read_only": sum(1 for row in rows if row["effects"] == ["reads only"]),
                   "irreversible": len(irreversible_rows), "external": len(external_rows), "high_risk": len(high)},
        "risk_score": score, "worst_case": worst_case,
        "rollback": ("file edits: `nexus` snapshots / `git restore`; commits: `git revert`; "
                     + ("the irreversible steps above have no automatic rollback" if irreversible_rows
                        else "no step is irreversible")),
        "executed": False,
    }
