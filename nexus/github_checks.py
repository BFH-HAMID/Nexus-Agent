"""Fresh GitHub test evidence for an exact immutable PR head, not model prose."""
from __future__ import annotations

import datetime as dt
import re


def require_tests(api, repository, head):
    if not re.fullmatch(r'[a-f0-9]{40}', head):
        raise ValueError('Invalid test-evidence commit identity')
    checks = api(f'repos/{repository}/commits/{head}/check-runs?per_page=100&filter=latest')
    runs = checks.get('check_runs') if isinstance(checks, dict) else None
    if not isinstance(runs, list) or checks.get('total_count') != len(runs) or len(runs) > 100:
        raise ValueError('Incomplete test evidence; no PR action allowed')
    status = api(f'repos/{repository}/commits/{head}/status')
    if not isinstance(status, dict) or status.get('sha') != head or not isinstance(status.get('statuses'), list):
        raise ValueError('Status evidence does not match the reviewed commit')
    successes = []
    now = dt.datetime.now(dt.timezone.utc)
    for run in runs:
        if (run.get('head_sha') != head or run.get('status') != 'completed'
                or run.get('conclusion') != 'success'):
            raise ValueError('Checks are pending/failed/skipped or belong to another commit')
        if re.search(r'(?i)(?:^|[^a-z])tests?(?:[^a-z]|$)', str(run.get('name', ''))):
            try:
                completed = dt.datetime.fromisoformat(run['completed_at'].replace('Z', '+00:00'))
                age = (now - completed).total_seconds()
            except (KeyError, ValueError, TypeError):
                raise ValueError('Test evidence has no valid completion time') from None
            if not 0 <= age <= 86400:
                raise ValueError('Test evidence must be fresh (within 24 hours)')
            successes.append({'id': run.get('id'), 'name': run['name'], 'head': head,
                              'completed_at': run['completed_at']})
    # Legacy statuses can block, but cannot masquerade as independently observed test runs.
    if len(status['statuses']) > 100 or status.get('total_count') != len(status['statuses']):
        raise ValueError('Incomplete combined status evidence')
    if any(s.get('state') != 'success' for s in status['statuses']):
        raise ValueError('Commit statuses are pending/failed')
    if not successes:
        raise ValueError('No successful test check for this exact head; push CI tests before creating/merging a PR')
    return successes
