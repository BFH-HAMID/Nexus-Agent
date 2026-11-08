"""CLI entry points for ``nexus chief``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .chief import (
    ChiefError,
    ChiefOfStaff,
    Mission,
    answer_status_question,
    list_missions,
    load_chief_config,
)
from .redaction import mask
from .redaction import secrets as known_secrets
from .studio import redact


def _print_json(workspace: Path, payload: Any) -> None:
    text = json.dumps(redact(payload), ensure_ascii=False, indent=2, default=str)
    print(mask(text, known_secrets(workspace)))


def _pick_target_id(workspace: Path, explicit_id: str | None, *, wanted_statuses: set[str] | None = None) -> str:
    if explicit_id:
        return explicit_id
    missions = list_missions(workspace, limit=50)
    if wanted_statuses:
        for m in missions:
            if m.status in wanted_statuses:
                return m.id
    if missions:
        return missions[0].id
    raise ChiefError("No Chief-of-Staff missions found in this workspace")


def _print_mission(mission: Mission) -> None:
    print(f"Mission {mission.id} [{mission.status}]")
    print(f"  goal          : {mission.goal}")
    print(f"  plan summary  : {mission.plan_summary}")
    print(f"  autonomy      : {mission.autonomy_level} (max_depth={mission.max_depth})")
    print(
        f"  cost          : ${mission.total_cost_usd:.4f} / cap ${mission.cost_cap_usd:.4f} "
        f"(est ${mission.estimated_cost_usd:.4f})"
    )
    if mission.artifacts:
        print(f"  artifacts     : {', '.join(mission.artifacts[:10])}")
    if mission.error:
        print(f"  error         : {mission.error}")
    print("  steps:")
    for step in mission.steps:
        cost = f"${step.actual_cost_usd:.4f}" if step.actual_cost_usd is not None else f"est ${step.estimated_cost_usd:.4f}"
        deps = f" deps={','.join(step.depends_on)}" if step.depends_on else ""
        print(
            f"    - {step.id:<16} [{step.status:<10}] role={step.role:<16} "
            f"agent={step.agent:<14} {cost}{deps}"
        )


def _interactive_go_prompt(mission: Mission) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{mission.plan_summary} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes", "go", "approve"}


def _speak_if_enabled(workspace: Path, text: str, requested: bool) -> None:
    if not requested or not text:
        return
    try:
        from .voice.audio_io import AudioOutputPlayer
        from .voice.config import load_voice_config
        from .voice.tts import create_tts, sanitize_tts_text

        vcfg = load_voice_config(workspace)
        if not vcfg.enabled:
            return
        safe = sanitize_tts_text(text, workspace=workspace)
        if not safe:
            return
        clip = create_tts(vcfg, workspace=workspace).synthesize(safe)
        AudioOutputPlayer(device=vcfg.output_device).play(clip)
    except Exception:
        pass


def command(args: argparse.Namespace) -> int:
    workspace = Path(getattr(args, "workspace", None) or ".").resolve()
    as_json = bool(getattr(args, "json", False))
    words = [str(w) for w in (getattr(args, "words", None) or []) if str(w).strip()]
    goal_flag = (getattr(args, "goal", None) or "").strip()

    action: str | None = None
    target_id: str | None = None
    goal = goal_flag

    if not goal:
        if not words:
            action = "status"
        elif words[0].lower() in {
            "status", "pause", "resume", "cancel", "approve",
            "history", "replay", "recover", "monitoring",
        } and len(words) <= 2:
            action = words[0].lower()
            target_id = words[1] if len(words) == 2 else getattr(args, "mission_id", None)
        elif words[0].lower() == "schedule":
            action = "schedule"
        elif words[0].lower() == "ask":
            action = "ask"
            target_id = getattr(args, "mission_id", None)
            goal = " ".join(words[1:]).strip()
        else:
            goal = " ".join(words).strip()

    from .cli import interactive_approval

    def _tool_cb(spec, t_args, risk, preview):
        if sys.stdin.isatty():
            return interactive_approval(spec, t_args, risk, preview)
        from .tools import ApprovalDecision

        return ApprovalDecision(False, reason="Non-interactive mode requires explicit tool approval")

    chief = ChiefOfStaff(
        workspace,
        show=None if as_json else print,
        speak=(lambda msg: _speak_if_enabled(workspace, msg, bool(getattr(args, "speak", False)))),
        approve_callback=(
            (lambda _m: True)
            if (getattr(args, "confirm", "") or "").strip().upper() in {"GO", "YES", "APPROVE"}
            else (_interactive_go_prompt if sys.stdin.isatty() and not as_json else None)
        ),
        tool_approval_callback=_tool_cb,
        auto_yes=bool(getattr(args, "yes", False)),
        verbosity=getattr(args, "verbosity", None),
    )

    try:
        if action == "ask":
            question = goal or "what is the status?"
            answer = answer_status_question(workspace, question, mission_id=target_id)
            if as_json:
                _print_json(workspace, {"question": question, "mission_id": target_id, "answer": answer})
            else:
                print(answer)
            return 0
        if action == "status":
            data = chief.status(target_id)
            if as_json:
                _print_json(workspace, data)
            else:
                cfg = data["chief"]
                print(
                    f"Chief-of-Staff enabled={cfg.get('enabled')} "
                    f"autonomy={data['autonomy'].get('level')} "
                    f"missions={len(data.get('missions', [data.get('mission')]) if 'missions' in data else [data['mission']])}"
                )
                if "mission" in data:
                    _print_mission(Mission.from_dict(data["mission"]))
                else:
                    for item in data.get("missions", [])[:15]:
                        print(f"  {item['id']}  [{item['status']:<16}] ${item['total_cost_usd']:.4f}  {item['goal'][:72]}")
            return 0

        if action == "monitoring":
            from .monitoring import collect_self_monitoring

            snap = collect_self_monitoring(workspace)
            if as_json:
                _print_json(workspace, snap)
            else:
                print(
                    f"queue_depth={snap['queue_depth']['total']} "
                    f"running_missions={snap['running_missions']['count']} "
                    f"cost_today=${snap['cost_today']['usd']:.4f} "
                    f"error_rate={snap['error_rate']['rate']:.2%}"
                )
            return 0

        if action == "history":
            items = chief.history(status=getattr(args, "status", None), limit=50)
            if as_json:
                _print_json(workspace, {"items": items})
            else:
                for item in items:
                    print(
                        f"{item['id']} [{item['status']:<16}] "
                        f"{item['steps_done']}/{item['steps_total']} "
                        f"${item['total_cost_usd']:.4f} {item['goal'][:72]}"
                    )
            return 0

        if action == "replay":
            mid = _pick_target_id(workspace, target_id)
            mode = "reexecute" if getattr(args, "execute", False) else ("why" if getattr(args, "why", False) else "inspect")
            step_id = getattr(args, "step_id", None)
            trace_step = getattr(args, "trace_step", None)
            if mode == "reexecute":
                data = chief.replay(mid, step_id=step_id, trace_step=trace_step, mode="reexecute", approval_callback=_tool_cb)
            else:
                data = chief.replay(mid, step_id=step_id, trace_step=trace_step, mode=mode)
            if as_json:
                _print_json(workspace, data)
            else:
                print(json.dumps(redact(data), ensure_ascii=False, indent=2, default=str))
            return 0

        if action == "recover":
            recovered = chief.recover_missions(auto_resume=bool(getattr(args, "auto_resume", False)))
            payload = {"recovered": [m.to_dict() for m in recovered]}
            if as_json:
                _print_json(workspace, payload)
            else:
                print(f"Recovered {len(recovered)} mission(s).")
            return 0

        if action == "schedule":
            from .scheduler import Scheduler

            sched = Scheduler(workspace)
            sub_words = words[1:]
            op = (sub_words[0].lower() if sub_words else "list")
            if op == "list":
                items = [e.to_dict() for e in sched.list()]
                if as_json:
                    _print_json(workspace, {"items": items})
                else:
                    for it in items:
                        print(f"{it['id']} enabled={it['enabled']} kind={it['kind']} tz={it['timezone']} task={it['task'][:60]}")
                return 0
            if op == "add":
                expr = " ".join(sub_words[1:]).strip()
                entry = sched.add(
                    expr,
                    task=(getattr(args, "schedule_task", None) or "").strip(),
                    timezone=getattr(args, "timezone", None) or None,
                    target=getattr(args, "target", None) or None,
                )
                if as_json:
                    _print_json(workspace, entry.to_dict())
                else:
                    print(f"Scheduled {entry.id}: {entry.task} ({entry.kind}, tz={entry.timezone})")
                return 0
            if op in {"pause", "resume"} and len(sub_words) >= 2:
                entry = sched.set_enabled(sub_words[1], op == "resume")
                if as_json:
                    _print_json(workspace, entry.to_dict())
                else:
                    print(f"Schedule {entry.id} enabled={entry.enabled}")
                return 0
            if op == "remove" and len(sub_words) >= 2:
                removed = sched.remove(sub_words[1])
                if as_json:
                    _print_json(workspace, {"removed": removed, "id": sub_words[1]})
                else:
                    print(f"Removed {sub_words[1]}: {removed}")
                return 0
            if op in {"run-due", "tick"}:
                dispatched = sched.tick()
                if as_json:
                    _print_json(workspace, {"dispatched": dispatched})
                else:
                    print(f"Dispatched {len(dispatched)} due schedule(s).")
                return 0
            raise ChiefError("Usage: nexus chief schedule <list|add|pause|resume|remove|run-due> ...")

        if action == "pause":
            mid = _pick_target_id(workspace, target_id, wanted_statuses={"running", "awaiting_approval", "planned"})
            mission = chief.pause(mid)
            if as_json:
                _print_json(workspace, mission.to_dict())
            else:
                _print_mission(mission)
            return 0

        if action == "resume":
            mid = _pick_target_id(workspace, target_id, wanted_statuses={"paused", "interrupted", "running"})
            mission = chief.resume(mid)
            if as_json:
                _print_json(workspace, mission.to_dict())
            else:
                _print_mission(mission)
            return 0 if mission.status == "done" else 1

        if action == "cancel":
            mid = _pick_target_id(
                workspace,
                target_id,
                wanted_statuses={"running", "paused", "awaiting_approval", "planned"},
            )
            mission = chief.cancel(mid)
            if as_json:
                _print_json(workspace, mission.to_dict())
            else:
                _print_mission(mission)
            return 0

        if action == "approve":
            mid = _pick_target_id(workspace, target_id, wanted_statuses={"awaiting_approval", "planned"})
            confirm = (getattr(args, "confirm", None) or "").strip()
            if not confirm and sys.stdin.isatty():
                loaded = chief.get(mid)
                confirm = "GO" if _interactive_go_prompt(loaded) else ""
            mission = chief.approve(mid, approved=bool(confirm), confirm=confirm or "NO")
            if as_json:
                _print_json(workspace, mission.to_dict())
            else:
                _print_mission(mission)
            return 0 if mission.status in {"done", "cancelled"} else 1

        # Goal planning & execution
        if not goal:
            raise ChiefError("Provide a mission goal or subcommand (status|pause|resume|cancel|approve)")

        mission = chief.plan(
            goal,
            pipeline=getattr(args, "pipeline", None),
            cost_cap_usd=getattr(args, "cost_cap", None),
            concurrency=getattr(args, "concurrency", None),
            max_retries=getattr(args, "max_retries", None),
            critic_enabled=False if getattr(args, "no_critic", False) else None,
            source="cli",
        )
        if getattr(args, "plan_only", False):
            chief._say_and_show(mission.plan_summary)
            if as_json:
                _print_json(workspace, mission.to_dict())
            else:
                _print_mission(mission)
            return 0

        confirm_go = (getattr(args, "confirm", None) or "").strip().upper() in {"GO", "YES", "APPROVE"}
        mission = chief.execute(mission, auto_approve=confirm_go)
        if as_json:
            _print_json(workspace, mission.to_dict())
        else:
            _print_mission(mission)
            if mission.status == "awaiting_approval":
                print(f"Awaiting approval. Run: nexus chief approve {mission.id} --confirm GO")
        return 0 if mission.status in {"done", "awaiting_approval", "planned"} else 1
    except ChiefError as exc:
        if as_json:
            _print_json(workspace, {"error": str(exc), "enabled": load_chief_config(workspace).enabled if workspace.exists() else False})
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2


def add_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "chief",
        help="Chief-of-Staff DAG orchestrator: nexus chief '<goal>' | status | pause | resume | cancel | approve",
        description="Chief-of-Staff DAG orchestrator: decompose goals across specialists, run parallel branches, recover from errors, and enforce cost caps.",
    )
    parser.add_argument(
        "words",
        nargs="*",
        help="natural-language goal OR subcommand: status|ask|pause|resume|cancel|approve [mission_id|question]",
    )
    parser.add_argument("--goal", help="explicit natural-language mission goal")
    parser.add_argument("--id", dest="mission_id", default=None, help="target mission ID for status/ask/control")
    parser.add_argument("--workspace", "-C", help="project directory (default: cwd)")
    parser.add_argument("--pipeline", help="base mission steps on a configured pipeline from pipelines.yaml")
    parser.add_argument("--cost-cap", type=float, default=None, help="maximum mission cost budget in USD")
    parser.add_argument("--concurrency", type=int, default=None, help="maximum parallel step workers (1..8)")
    parser.add_argument("--max-retries", type=int, default=None, help="maximum retries per failed step (0..5)")
    parser.add_argument("--no-critic", action="store_true", help="skip phase critic review steps")
    parser.add_argument("--plan-only", action="store_true", help="plan and show summary without executing")
    parser.add_argument("--confirm", help="explicit confirmation ('GO') for non-interactive approval")
    parser.add_argument("--verbosity", choices=("silent", "milestones", "chatty"), default=None, help="override live status narration verbosity")
    parser.add_argument("--speak", action="store_true", help="speak plan summary/narration via configured voice TTS")
    parser.add_argument("--yes", "-y", action="store_true", help="auto-approve non-dangerous file tools (never bypasses permissions.yaml)")
    parser.add_argument("--step", dest="step_id", default=None, help="mission step id for replay")
    parser.add_argument("--trace-step", type=int, default=None, help="recorded trace event step index for replay")
    parser.add_argument("--why", action="store_true", help="show replay decision explanation")
    parser.add_argument("--execute", action="store_true", help="re-execute a safe recorded file tool with fresh approval")
    parser.add_argument("--resume", dest="auto_resume", action="store_true", help="auto-resume recovered interrupted missions")
    parser.add_argument("--status", default=None, help="filter history by status")
    parser.add_argument("--timezone", default=None, help="IANA timezone override for schedule")
    parser.add_argument("--task", dest="schedule_task", default=None, help="explicit task for cron schedules")
    parser.add_argument("--target", choices=("queue", "chief"), default=None, help="schedule dispatch target")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    parser.set_defaults(func=command)


__all__ = ["add_parser", "command"]
