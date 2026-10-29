"""Mapped GitHub project actions reuse Module 2 previews/CI/scans, never raw Git."""
import json

from ..types import Risk, ToolSpec
from .approvals import Approvals


def project(studio, job, args):
    from .queue import Queue
    policy = Queue(studio).policy()
    cfg = policy['runtime']
    if (cfg['connectivity'] != 'online' or args.get('name') not in cfg['projects']
            or args.get('action') not in {'commit', 'push', 'pr', 'merge'}):
        raise ValueError('Project action outside operator authority or offline')
    service = studio.wb.repositories.github.projects
    preview = service.preview(args)['data']
    spec = ToolSpec('project_reviewed_action', 'Review exact project diff/head/CI and external effects',
                    {'type': 'object'}, risk=Risk.DANGEROUS)
    decision = Approvals(studio).callback(job)(spec, {'preview_id': preview['id']}, Risk.DANGEROUS, json.dumps(preview))
    if not decision.approved or Queue(studio).policy() != policy:
        raise ValueError('Project approval denied or policy changed')
    # Module 2 revalidates credentials, head, mandatory scans and exact CI AFTER consent.
    return json.dumps(service.execute({'preview_id': preview['id'], 'confirm': preview['confirmation']})['data'])
