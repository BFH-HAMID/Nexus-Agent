"""Explicit vision subcommands; no device is started by status or policy edits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def operator_gate(workspace):
    from ..permissions import Permissions
    from ..redaction import mask, secrets
    from ..tools import ApprovalDecision, ApprovalGate
    def review(spec, args, risk, preview):
        digest = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:16]
        phrase = f'ALLOW {spec.name} {digest}'
        print(mask(json.dumps({'operation': spec.name, 'review': args, 'confirmation': phrase},
                              ensure_ascii=False, indent=2), secrets(workspace)))
        return ApprovalDecision(input('Exact one-time confirmation: ') == phrase, scope='once')
    gate = ApprovalGate(callback=review)
    gate.permissions = Permissions(workspace)
    return gate


def command(args):
    from .camera import CameraPipeline
    from .faces import FaceRegistry
    from .service import VisionService
    from .state import hard_off, resume, status
    root = Path(args.workspace or '.').resolve()
    gate = operator_gate(root)
    try:
        if args.operation == 'status':
            result = status(root)
        elif args.operation == 'off':
            result = hard_off(root)
        elif args.operation == 'resume':
            result = resume(root, gate)
        elif args.operation == 'screen':
            result = VisionService(root).screen(args.question, gate=gate, cloud=args.cloud, no_people=args.no_people)
        elif args.operation == 'ask':
            result = VisionService(root).ask_path(args.path, args.question, gate=gate, cloud=args.cloud,
                                                 no_people=args.no_people, pages=args.pages, crop=args.region)
        elif args.operation == 'camera':
            result = CameraPipeline(root).run(gate=gate, frames=args.frames, seconds=args.seconds)
        else:
            faces = FaceRegistry(root)
            if args.face_operation == 'list':
                result = {'people': faces.list(), 'encrypted': True, 'local_only': True}
            elif args.face_operation == 'enroll':
                if not args.consent:
                    raise ValueError('Obtain the person\'s consent, then use --consent and fresh interactive review')
                result = faces.enroll(args.name, faces.load_image(args.image), consent=args.consent, gate=gate)
            else:
                result = {'deleted': faces.forget(args.name, gate=gate)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print('Vision cancelled; owned camera released. No automatic restart.')
        return 130
    except (ValueError, OSError, TimeoutError, EOFError, ImportError) as exc:
        from ..redaction import mask, secrets
        print(json.dumps({'error': mask(str(exc), secrets(root))[:500], 'automatic_retry': False}))
        return 2


def add_parser(sub):
    parser = sub.add_parser('vision', help='opt-in local-first screen/image/PDF/camera perception; vision.yaml')
    parser.add_argument('-C', '--workspace')
    actions = parser.add_subparsers(dest='operation', required=True)
    for name in ('status', 'off', 'resume'):
        actions.add_parser(name)
    for name in ('ask', 'screen'):
        action = actions.add_parser(name)
        if name == 'ask':
            action.add_argument('path', help='regular workspace image/PDF/notes')
            action.add_argument('--pages', nargs='+', type=int, help='one-based PDF pages')
            action.add_argument('--region', nargs=4, type=float, help='normalized left top right bottom')
        action.add_argument('question', nargs='?', default='What is visible? Cite pages and regions.')
        action.add_argument('--cloud', action='store_true', help='explicit Gemini upload; YAML and fresh approval also required')
        action.add_argument('--no-people', action='store_true', help='attest that the upload contains no people; local face preflight also required')
    camera = actions.add_parser('camera')
    camera.add_argument('--frames', type=int, help='bounded session, no automatic restart')
    camera.add_argument('--seconds', type=float)
    faces = actions.add_parser('faces')
    face_actions = faces.add_subparsers(dest='face_operation', required=True)
    face_actions.add_parser('list')
    enroll = face_actions.add_parser('enroll')
    enroll.add_argument('name')
    enroll.add_argument('image')
    enroll.add_argument('--consent', action='store_true', help='attest the person freely consented to local enrollment')
    face_actions.add_parser('delete').add_argument('name')
    parser.set_defaults(func=command)
