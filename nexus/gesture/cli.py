"""``nexus gesture`` - status, bindings, classifier check and a foreground watch loop."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path


def add_parser(sub) -> None:
    parser = sub.add_parser("gesture", help="hand-gesture control: stop / deny / approve by pose (opt-in)")
    parser.add_argument("action", choices=["status", "bindings", "classify", "watch"])
    parser.add_argument("value", nargs="?", default="", help="classify: JSON file with 21 [x, y] landmarks, or with "
                                                             '{"image": [...], "world": [...]}')
    parser.add_argument("-C", "--workspace", default=".")
    parser.add_argument("--seconds", type=float, default=0.0, help="watch: stop after N seconds (0 = until Ctrl+C)")
    parser.add_argument("--dry-run", action="store_true", help="watch: print what would fire, do nothing")
    parser.add_argument("--poses", action="store_true", help="watch: also print each pose the camera sees, for tuning")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=command)


def command(args) -> int:
    from . import policy, status

    workspace = Path(args.workspace).resolve()
    try:
        if args.action == "status":
            info = status(workspace)
            print(json.dumps(info, indent=2) if args.json else "\n".join(f"{k}: {v}" for k, v in info.items()))
            return 0
        cfg = policy.require(workspace)
        if args.action == "bindings":
            print(json.dumps({"arming": cfg["arming"], "bindings": policy.bindings(workspace, cfg),
                              "approve": cfg["approve"]}, indent=2))
            return 0
        if args.action == "classify":
            from ..workbench import safe_path
            from .classify import classify, finger_scores

            landmarks = json.loads(safe_path(workspace, args.value).read_text(encoding="utf-8")[:100_000])
            world = None
            if isinstance(landmarks, dict):     # picture points and, optionally, MediaPipe's world landmarks
                landmarks, world = landmarks.get("image"), landmarks.get("world")
            pose, confidence = classify(landmarks, world)
            print(json.dumps({"pose": pose, "confidence": confidence, "fingers": finger_scores(landmarks, world)}, indent=2))
            return 0 if pose != "none" else 1
        from .camera import GestureWatcher
        from .controller import GestureController

        stop = threading.Event()
        last: list[tuple[str, bool]] = []

        def show(pose: str, confidence: float) -> None:
            """One line whenever what the camera sees changes: the pose, and whether it is sure enough to count."""
            seen = (pose, confidence >= cfg["min_confidence"])
            if last[-1:] != [seen]:
                last[:] = [seen]
                print(f"sees {pose} ({confidence:.2f}{'' if seen[1] or pose == 'none' else ', below min_confidence'})",
                      file=sys.stderr, flush=True)

        watcher = GestureWatcher(GestureController(workspace, dry_run=args.dry_run), stop,
                                 on_event=lambda event: print(json.dumps(event), flush=True),
                                 on_pose=show if args.poses else None)
        print("Watching the camera for gestures (frames stay in memory). Ctrl+C to stop.", file=sys.stderr)
        watcher.start()
        try:
            watcher.join(args.seconds if args.seconds > 0 else None)
        except KeyboardInterrupt:
            pass
        stop.set()
        watcher.join(5)
        if watcher.error:
            raise ValueError(watcher.error)
        return 0
    except (ValueError, OSError) as exc:
        print(f"nexus gesture: {exc}", file=sys.stderr)
        return 2
