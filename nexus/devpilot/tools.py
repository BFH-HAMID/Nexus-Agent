"""Agent tools for DevPilot. Registered only while ``devpilot.yaml`` is ON.

``devpilot_run`` executes the operator's test command over model-written code
and ``devpilot_apply`` writes to the working tree, so both are declared
DANGEROUS: a fresh human approval every time, never ``--yes``. Each approval
prompt carries a ``review`` block computed at call time: the exact commands a
run will start, or the exact files an apply will write.
"""
from __future__ import annotations

import json

from . import policy


def register(reg, ctx) -> None:
    if not policy.enabled(ctx.workspace) or ctx.config is None:
        return
    from ..tools import ToolFailure
    from ..types import Risk
    from . import core

    def pilot():
        return core.DevPilot(ctx.workspace, ctx.config, ctx.providers, cancel=ctx.cancel_event)

    def flagged(record) -> None:
        if record.get("injection_flags"):
            ctx.mark_tainted("devpilot", list(record["injection_flags"]))

    def prepare_run(args):
        try:
            task, files = str(args.get("task") or ""), args.get("files") or []
            return {"task": task, "files": files, "review": pilot().preview(task, files)}
        except ValueError as exc:
            raise ValueError(str(exc)) from None  # the registry shows plain ValueError messages only

    def run(args):
        approved = args.get("review") if isinstance(args.get("review"), dict) else {}
        try:
            record = pilot().run(args.get("task"), args.get("files") or [], source="agent",
                                 expect=approved.get("runs_on_this_machine"))
        except ValueError as exc:
            raise ToolFailure(str(exc)) from None
        flagged(record)
        info = core.brief(record)
        info["next"] = ("Show the user the diff (devpilot_show). If they want it, call devpilot_apply with this run id "
                        "and patch_sha256." if info["files"] and info["status"] != "error" else "Nothing to apply.")
        return json.dumps(info, ensure_ascii=False)

    def show(args):
        try:
            record = core.load_run(ctx.workspace, args.get("run_id"))
            text = core.diff(ctx.workspace, record["id"], 16_000)
        except ValueError as exc:
            raise ToolFailure(str(exc)) from None
        flagged(record)
        return json.dumps({**core.brief(record), "diff": text}, ensure_ascii=False)

    def prepare_apply(args):
        try:
            return {"run_id": args.get("run_id"), "patch_sha256": args.get("patch_sha256"),
                    "review": core.review(ctx.workspace, args.get("run_id"), args.get("patch_sha256"))}
        except ValueError as exc:
            raise ValueError(str(exc)) from None

    def apply(args):
        try:
            return json.dumps(core.apply(ctx.workspace, args.get("run_id"), args.get("patch_sha256"), source="agent"))
        except ValueError as exc:
            raise ToolFailure(str(exc)) from None

    reg.register(
        "devpilot_run",
        "Ask DevPilot to make one small code change. A model writes a patch; DevPilot applies it to a throw-away "
        "checkout of the last commit and runs the project's test command there, for a few attempts. The working "
        "tree is not changed. Returns a run id, the changed files and patch_sha256. The test command executes on "
        "this machine, so every call needs a fresh human approval.",
        {"type": "object", "properties": {
            "task": {"type": "string", "description": "the change to make, in one or two sentences; name the files"},
            "files": {"type": "array", "items": {"type": "string"}, "description": "files the model should read first"}},
         "required": ["task"]},
        run, risk=Risk.DANGEROUS, prepare=prepare_run,
        approval_context={"effect": "runs the listed commands on this machine over model-written code; not sandboxed"})
    reg.register(
        "devpilot_show",
        "Show a DevPilot run: status, check results, changed files and the unified diff of its patch.",
        {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
        show, risk=Risk.READ, read_only=True)
    reg.register(
        "devpilot_apply",
        "Write the reviewed patch of a DevPilot run into the working tree. Needs the run id and the patch_sha256 the "
        "user reviewed. Refused when a touched file is not the committed version. Nothing is committed or pushed; "
        "use git_prepare afterwards if the user wants a commit. Needs a fresh human approval.",
        {"type": "object", "properties": {"run_id": {"type": "string"}, "patch_sha256": {"type": "string"}},
         "required": ["run_id", "patch_sha256"]},
        apply, risk=Risk.DANGEROUS, prepare=prepare_apply,
        approval_context={"effect": "writes the listed files into the working tree; no commit, no push"})
