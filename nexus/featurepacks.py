"""One registry for the opt-in feature packages of Tasks 18-30.

Each package is governed by its own operator-owned YAML (default OFF) and
exposes the same small surface, so the CLI, the tool registry, ``doctor``, the
HUD and the voice pipeline can treat them uniformly:

``<package>.status(workspace)``            never-raising summary
``<package>.cli.add_parser(sub)``          ``nexus <command>``
``<package>.tools.register(reg, ctx)``     agent tools (only while enabled)
``<package>.commands.handle(ws, text, ctx)`` spoken/typed command -> ``(reply, intent)``
``<package>.commands.exclusive(ws)``       optional: a mode that owns every utterance is active
``<package>.commands.slash(ws, arg, ctx)`` ``/<slash>`` in the REPL

``ctx`` is what the caller knows about the request. The voice pipeline passes
``source`` ("voice"), ``tainted`` (the assistant's last turn read untrusted
content), ``speaker_verified`` (True, False, or None when no speaker check is
in use), ``privacy_mode`` (the voice privacy mode is on: stored personal data
is not spoken) and ``keyring_module``; the REPL passes ``config`` and
``providers``. A package loads what is missing by itself.

Nothing here enables a feature: with every YAML absent, no tool is registered,
no command is intercepted and no state is written.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Feature:
    key: str            # short id, also the doctor row suffix
    package: str        # python package under nexus/
    policy: str         # operator-owned YAML in the workspace root
    title: str
    task: int
    command: str        # ``nexus <command>``
    slash: str          # ``/<slash>`` in the REPL ('' = none)
    extra: str = ""     # optional pip extra for heavy dependencies
    doc: str = ""


FEATURES: tuple[Feature, ...] = (
    Feature("knowledge_graph", "knowledge_graph", "knowledge_graph.yaml", "Personal knowledge graph", 18,
            "kg", "kg", "secure-state", "docs/knowledge-graph.md"),
    Feature("council", "council", "council.yaml", "Model council", 19, "council", "council", "", "docs/council.md"),
    Feature("whatif", "whatif", "whatif.yaml", "What-if simulator", 20, "whatif", "whatif", "", "docs/whatif.md"),
    Feature("explain", "explain", "explain.yaml", "Explainable decisions", 21, "explain", "why", "",
            "docs/explain.md"),
    Feature("presence", "presence", "presence.yaml", "Home presence", 22, "presence", "presence", "",
            "docs/home-presence.md"),
    Feature("gesture", "gesture", "gesture.yaml", "Gesture control", 23, "gesture", "", "gesture",
            "docs/gesture.md"),
    Feature("hardware", "hardware", "hardware.yaml", "Hardware bridge", 24, "hardware", "", "hardware",
            "docs/hardware.md"),
    Feature("devpilot", "devpilot", "devpilot.yaml", "DevPilot", 25, "devpilot", "devpilot", "", "docs/devpilot.md"),
    Feature("meetings", "meetings", "meetings.yaml", "Meeting assistant", 26, "meetings", "meeting", "meetings",
            "docs/meetings.md"),
    Feature("translate", "translate", "translate.yaml", "Translator", 27, "translate", "translate", "",
            "docs/translate.md"),
    Feature("docs_auto", "docs_auto", "docs_auto.yaml", "Document automation", 28, "docs-auto", "docs", "docs-auto",
            "docs/docs-auto.md"),
    Feature("finance", "finance", "finance.yaml", "Personal finance", 29, "finance", "finance", "secure-state",
            "docs/finance.md"),
    Feature("tutor", "tutor", "tutor.yaml", "Tutor", 30, "tutor", "tutor", "", "docs/tutor.md"),
)

_ERRORS = (ValueError, OSError, RuntimeError, TypeError, KeyError, LookupError)


class Context(dict):
    """What a caller knows about a request, as the ``ctx`` of ``handle_text``.

    ``lazy`` maps a key to a function without arguments. The value is worked
    out the first time a package reads the key, and only then: the voice
    pipeline uses this for ``speaker_verified``, which costs a voice comparison
    that no turn should pay for unless a package wants to know.
    """

    def __init__(self, values: dict[str, Any] | None = None, *, lazy: dict[str, Any] | None = None) -> None:
        super().__init__(values or {})
        self._lazy = dict(lazy or {})

    def __missing__(self, key: str) -> Any:
        if key not in self._lazy:
            raise KeyError(key)
        self[key] = self._lazy.pop(key)()
        return self[key]

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key) or key in self._lazy

    def __bool__(self) -> bool:
        return bool(len(self) or self._lazy)


def _module(feature: Feature, name: str = ""):
    """Import ``nexus.<package>[.<name>]``; ``None`` only when that exact module does not exist."""
    package = f"nexus.{feature.package}"
    path = package + (f".{name}" if name else "")
    try:
        return importlib.import_module(path)
    except ModuleNotFoundError as exc:
        if exc.name in (path, package):
            return None
        raise  # a genuinely missing dependency inside the package must stay visible


def by_key(key: str) -> Feature | None:
    return next((f for f in FEATURES if key in (f.key, f.command, f.slash) and key), None)


def add_parsers(sub) -> None:
    """Register every package's ``nexus <command>`` sub-parser."""
    for feature in FEATURES:
        module = _module(feature, "cli")
        if module is not None:
            module.add_parser(sub)


def register_tools(reg, ctx) -> None:
    """Let each *enabled* package add its tools. A broken policy registers nothing."""
    for feature in FEATURES:
        module = _module(feature, "tools")
        if module is None:
            continue
        try:
            module.register(reg, ctx)
        except _ERRORS:
            continue  # invalid operator policy: the feature simply stays unavailable


def status(workspace: Path | str) -> dict[str, dict[str, Any]]:
    """``{key: summary}`` for every package; never raises."""
    out: dict[str, dict[str, Any]] = {}
    for feature in FEATURES:
        module = _module(feature)
        if module is None:
            continue
        try:
            info = dict(module.status(workspace))
        except _ERRORS as exc:
            info = {"policy": feature.policy, "valid": False, "enabled": False, "error": str(exc)[:240]}
        info.setdefault("enabled", False)
        info.update(title=feature.title, task=feature.task, command=f"nexus {feature.command}")
        out[feature.key] = info
    return out


def doctor_rows(workspace: Path | str) -> list[tuple[str, bool | None, str, str]]:
    """``(name, ok, detail, hint)`` rows for ``nexus doctor`` (``ok=None`` = optional and off)."""
    rows: list[tuple[str, bool | None, str, str]] = []
    for key, info in status(workspace).items():
        feature = by_key(key)
        assert feature is not None
        name = f"opt:{key}"
        if info.get("valid") is False or (info.get("enabled") and info.get("error")):
            rows.append((name, False, str(info.get("error", "invalid policy"))[:160], f"fix {feature.policy}"))
        elif not info.get("present"):
            rows.append((name, None, f"{feature.policy} not present", f"copy {feature.policy} to workspace to opt in"))
        elif not info.get("enabled"):
            rows.append((name, None, f"OFF in {feature.policy}", f"set enabled: true in {feature.policy}"))
        else:
            missing = info.get("missing") or []
            detail = "enabled" + (f"; missing optional: {', '.join(missing)}" if missing else "")
            hint = f'pip install "nexus-agent[{feature.extra}]"' if missing and feature.extra else ""
            rows.append((name, True, detail, hint))
    return rows


def _modal(workspace: Path | str, module) -> bool:
    """True while the package is in a mode that owns every utterance (the interpreter, a spoken quiz)."""
    try:
        return bool(module.exclusive(workspace)) if hasattr(module, "exclusive") else False
    except _ERRORS:
        return False


def handle_text(workspace: Path | str, text: str, ctx: dict[str, Any] | None = None, *,
                modal: bool | None = None) -> tuple[str, str] | None:
    """Offer a spoken/typed utterance to each enabled package in order.

    Returns ``(reply, intent)`` from the first package that recognises it, or
    ``None`` so the caller continues with its normal handling.  A package only
    answers while its operator policy is ON.

    ``modal`` is for a caller that has commands of its own (the voice pipeline):
    ``True`` asks only the packages that are in a mode which owns every
    utterance, so that they can be asked before the caller's other commands;
    ``False`` asks every package in registry order.  ``None`` does both in one
    call: the packages in such a mode first, then the others.
    """
    spoken = " ".join(str(text or "").split())
    if not spoken or len(spoken) > 2000:
        return None
    offers = [(feature, module) for feature in FEATURES
              if (module := _module(feature, "commands")) is not None and hasattr(module, "handle")]
    if modal is True:
        offers = [offer for offer in offers if _modal(workspace, offer[1])]
    elif modal is None:
        offers.sort(key=lambda offer: not _modal(workspace, offer[1]))  # stable: registry order otherwise
    for feature, module in offers:
        try:
            # a Context is shared, so that a value worked out for one package is not worked out again
            answer = module.handle(workspace, spoken, ctx if isinstance(ctx, Context) else dict(ctx or {}))
        except _ERRORS as exc:
            return f"{feature.title} is unavailable: {str(exc)[:160]}", f"{feature.key}_error"
        if answer:
            return answer
    return None


def slash(workspace: Path | str, command: str, argument: str, ctx: dict[str, Any] | None = None) -> str | None:
    """Run ``/<command> <argument>`` for the package that owns it; ``None`` when no package does."""
    feature = next((f for f in FEATURES if f.slash and f.slash == command.lower()), None)
    if feature is None:
        return None
    module = _module(feature, "commands")
    if module is None or not hasattr(module, "slash"):
        return None
    try:
        return str(module.slash(workspace, argument, dict(ctx or {})))
    except _ERRORS as exc:
        return f"{feature.title}: {str(exc)[:240]}"


def slash_help() -> list[tuple[str, str]]:
    return [(f"/{f.slash}", f"{f.title.lower()} (opt-in: {f.policy})") for f in FEATURES if f.slash]
