"""Review-only HTTP actions for workflow/runtime/security/data lifecycle."""
from __future__ import annotations

import hashlib
import json
import time

from .workbench import observation


def idle(studio):
    if studio.state.active_task or studio.state.task_queue or getattr(studio.state, 'workflow_busy', False):
        raise ValueError('Stop active/queued runs and workflows first')


def extra_get(studio, path, query):
    if path == 'dev-apps':
        return observation(studio.execution.list(), 'actual managed processes; bounded redacted logs')
    if path == 'workflow-versions':
        family = (query.get('family') or [''])[0]
        return observation([w for w in studio.items('workflow') if not family or w['family'] == family], 'immutable workflow versions')
    if path == 'hardware-fit':
        import psutil
        try:
            inventory = studio.wb.command(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits']) if __import__('shutil').which('nvidia-smi') else ''
            gpu = sum(float(n) * 1024**2 for n in inventory.splitlines()) if inventory else None
        except ValueError:
            gpu = None
        params = float((query.get('parameters_b') or ['0'])[0])
        bits = float((query.get('bits') or ['4'])[0])
        context = int((query.get('context') or ['8192'])[0])
        if not 0 < params <= 1000 or bits not in {2, 3, 4, 5, 6, 8, 16, 32} or not 1 <= context <= 2000000:
            raise ValueError('Supply parameters_b (0–1000), quantization bits and context')
        weights = params * 1e9 * bits / 8
        # KV is architecture-dependent; report an explicitly labelled range, never a guarantee.
        overhead_low, overhead_high = weights * .15 + context * 65536, weights * .5 + context * 524288
        available = psutil.virtual_memory().available
        return observation({'parameters_b': params, 'bits': bits, 'context': context, 'weight_bytes_estimate': weights,
            'total_bytes_range_estimate': [weights + overhead_low, weights + overhead_high], 'available_ram_bytes': available,
            'free_gpu_bytes': gpu, 'ram_fit_estimate': 'likely' if weights + overhead_high < available else 'uncertain/not recommended',
            'warning': 'Heuristic, not a guarantee. Architecture, KV precision, runtime, offload and batch size vary.'}, 'user model dimensions + actual free RAM/optional nvidia-smi')
    return None


def migrate(backup):
    if not isinstance(backup, dict):
        raise ValueError('Backup must be an object')
    schema = backup.get('schema')
    if schema == 0:
        backup = {'schema': 1, 'studio': {'conversation': backup.get('conversations', []),
            'template': backup.get('templates', []), 'profile': backup.get('profiles', []),
            'memory-draft': backup.get('memory_drafts', [])}}
    elif schema == 2:
        backup = {**backup, 'schema': 1}
    elif schema != 1:
        raise ValueError('Supported migration schemas: legacy 0, 1, 2')
    return backup


def extra_post(studio, path, payload):
    if path == "memory-review" and payload.get("action") == "approve" and studio.encrypted():
        record = studio.find("memory-draft", str(payload.get("id", "")))
        text = str(payload.get("text", record.get("text", ""))).strip()
        if record.get("status") != "draft" or record.get("expires", 0) < time.time() or not 0 < len(text) <= 4000 or studio.redact_text(text) != text:
            raise ValueError("Valid non-secret pending memory (1–4000 characters) required")
        active = [r for r in studio.items("memory-draft") if r.get("status") == "approve" and r.get("scope") == "encrypted-dashboard"]
        if sum(len(r.get("approved_text", "")) for r in active) + len(text) > 4000:
            raise ValueError("Encrypted dashboard memory cap reached; revoke/consolidate entries first")
        record.update(status="approve", approved_text=text, scope="encrypted-dashboard", active_expires=time.time() + 30 * 86400)
        return observation(studio.put("memory-draft", record, record["id"]), "approved encrypted-dashboard memory; no plaintext memory file written")
    if path in {"skill-preview", "skill-check", "skill-promote", "skill-reject"}:
        return skill_action(studio, path, payload)
    if path == 'workflow-save':
        return studio.workflows.save(payload)
    if path == 'workflow-dry-run':
        definition, order = studio.workflows.validate(payload.get('definition'))
        return observation({'valid': True, 'order': order, 'definition': definition,
            'writes': [n['id'] for n in definition['nodes'] if not n['read_only'] and n['kind'] == 'agent'],
            'network': 'model calls follow configured routing; tool egress follows isolation policy',
            'note': 'Structural dry-run only: no commands, model calls or writes executed'}, 'DAG/roles/conditions/budgets validator')
    if path == 'workflow-run':
        return studio.workflows.run(payload)
    if path == 'workflow-cancel':
        return studio.workflows.cancel(str(payload.get('id', '')))
    if path == 'clarification-answer':
        with studio.question_lock:
            record = studio.find('clarification', str(payload.get('id', '')))
            if record['status'] != 'pending' or record['id'] not in studio.questions:
                raise ValueError('Question expired, cancelled or no live waiter (server restart never resumes it)')
            answer = payload.get('answer')
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 20000 or studio.redact_text(answer) != answer:
                raise ValueError('A non-secret answer up to 20 KB is required')
            if record['approval'] and answer not in {'APPROVE', 'DENY'}:
                raise ValueError('Approval answer must be APPROVE or DENY')
            record.update(status='answered', answer=answer, answered_at=time.time())
            studio.put('clarification', record, record['id'])
            studio.questions[record['id']].set()
        return observation(record, 'explicit human clarification/approval')
    if path == 'dev-start':
        if getattr(studio.state, 'workflow_busy', False):
            raise ValueError('Do not start concurrent host writers during a workflow')
        return observation(studio.execution.start(payload), 'actual trusted host process started')
    if path == 'dev-stop':
        return observation(studio.execution.stop(str(payload.get('id', ''))), 'actual process group stopped')
    if path == 'sandbox-run':
        if payload.get('confirm') != 'RUN IN ISOLATED CHECKOUT':
            raise ValueError('Explicit isolation execution confirmation required')
        return observation(studio.execution.isolated(payload), 'actual Docker output; no host fallback')
    if path == 'domain-fetch':
        return observation(studio.allowed_fetch(payload), 'actual domain-enforced HTTPS egress')
    if path == 'encryption-enable':
        idle(studio)
        if payload.get('confirm') != 'ENCRYPT DASHBOARD STATE' or payload.get('single_server') is not True:
            raise ValueError('Confirm ENCRYPT DASHBOARD STATE and that no other server uses the shared DB')
        from .studio_crypto import enable, enabled
        with studio.wb.lock:
            with studio.wb.connect():
                pass
            enable(studio.wb.path)
        return observation({'enabled': enabled(studio.wb.path), 'scope': 'dashboard SQLite; OS-keystore AES-GCM, not project or CLI files'}, 'actual encrypted file header')
    if path == 'backup-migrate':
        result = migrate(payload.get('backup'))
        # Validation occurs without writes through a temporary pure validator mode.
        return observation({'backup': result, 'target_schema': 1, 'note': 'Review normalized import; restore still validates every record atomically'}, 'deterministic data-only schema migration')
    if path == 'record-delete':
        idle(studio)
        kind, ident = payload.get('kind'), str(payload.get('id', ''))
        allowed = {'conversation', 'template', 'context', 'evidence', 'verification', 'checkpoint', 'workflow', 'workflow-run', 'clarification', 'skill-test', 'transcript', 'workflow-live', 'archived-run', 'profile', 'memory-draft', 'skill-draft'}
        if kind not in allowed or payload.get('confirm') != 'DELETE ' + ident:
            raise ValueError('Choose a supported collection and confirm DELETE <id>')
        record = studio.find(kind, ident)
        if kind == 'memory-draft' and record.get('status') == 'approve':
            raise ValueError('Revoke active memory before deleting its provenance')
        with studio.wb.lock, studio.wb.connect() as conn:
            conn.execute('DELETE FROM studio WHERE workspace=? AND kind=? AND id=?', (studio.wb.key, kind, ident))
        return observation({'deleted': ident, 'note': 'Logical deletion; filesystem backups/SQLite remnants may remain'}, 'explicit record deletion')
    if path == 'retention-all':
        idle(studio)
        if payload.get('confirm') != 'DELETE EXPIRED STUDIO HISTORY':
            raise ValueError('Explicit Studio retention confirmation required')
        cutoff = time.time() - studio.settings()['retention_days'] * 86400
        protected = {'settings', 'workflow', 'profile', 'template', 'checkout'}
        with studio.wb.lock, studio.wb.connect() as conn:
            rows = conn.execute('SELECT id,kind,body FROM studio WHERE workspace=?', (studio.wb.key,)).fetchall()
            ids = [ident for ident, kind, body in rows if kind not in protected and json.loads(body).get('updated_at', 0) < cutoff
                   and not (kind == 'memory-draft' and json.loads(body).get('status') == 'approve')]
            for ident in ids:
                conn.execute('DELETE FROM studio WHERE workspace=? AND id=?', (studio.wb.key, ident))
            count = conn.execute('DELETE FROM runs WHERE workspace=? AND created<?', (studio.wb.key, cutoff)).rowcount
        return observation({'studio_deleted': len(ids), 'runs_deleted': count, 'protected': sorted(protected),
            'note': 'Active memory provenance retained until revoked; saved configuration/templates/workflows require explicit record deletion'}, 'explicit workspace-wide history retention')
    if path == 'memory-revoke':
        idle(studio)
        from .memory import MemoryStore
        record = studio.find('memory-draft', str(payload.get('id', '')))
        if payload.get('confirm') != 'REVOKE ' + record['id'] or record.get('status') != 'approve':
            raise ValueError('Confirm REVOKE <active draft id>')
        store = MemoryStore()
        text = record.get('approved_text', '')
        entries = store.block('memory').entries
        removed = record.get('scope') != 'encrypted-dashboard' and text in entries
        if removed:
            entries.remove(text)
            store._save(store.block('memory'))
        record.update(status='revoked', revoked_at=time.time(), exact_entry_removed=removed)
        return observation(studio.put('memory-draft', record, record['id']), 'exact owned entry only; manually modified entries are not overwritten')
    if path == 'skill-runtime-test':
        idle(studio)
        if payload.get('confirm') != 'RUN DRAFT SKILL TEST':
            raise ValueError('Type RUN DRAFT SKILL TEST; executes trusted repository tests, not arbitrary model claims')
        draft = studio.find('skill-draft', str(payload.get('id', '')))
        if draft['status'] != 'draft':
            raise ValueError('Only inactive draft skills can be tested')
        result = studio.run_check(str(payload.get('kind', 'tests')), 180)
        record = studio.put('skill-test', {'draft_id': draft['id'], 'proposal_hash': hashlib.sha256(json.dumps(draft['proposal'], sort_keys=True).encode()).hexdigest(),
            'verification_id': result['id'], 'exit_code': result['exit_code'], 'fingerprint': result['fingerprint'],
            'warning': 'These are actual repository checks. Human review must assess whether they cover the proposed procedure.'})
        return observation(record, 'actual runtime check linked to immutable proposal hash')
    return None


def skill_action(studio, path, payload):
    from . import skills
    draft = studio.find('skill-draft', str(payload.get('id', '')))
    if draft['status'] != 'draft':
        raise ValueError('Skill proposal is no longer pending')
    if path == 'skill-reject':
        draft['status'] = 'rejected'
        return observation(studio.put('skill-draft', draft, draft['id']), 'explicit draft rejection')
    args = draft['proposal']
    action = args.get('action', 'create')
    name = str(args.get('name', ''))
    if action not in {'create', 'patch', 'delete'} or not skills.NAME_RE.fullmatch(name):
        raise ValueError('Invalid skill action/name')
    existing = skills.get(name, studio.root)
    if action in {'patch', 'delete'}:
        if not existing or existing.source in {'hermes', 'bundled'}:
            raise ValueError('Only existing user/project-owned skills can be changed')
        # Restrict edits/deletes to the two mutable skill roots, never bundled files.
        roots = [skills.user_skills_root().resolve(), skills.project_skills_root(studio.root).resolve()]
        if not any(existing.path.resolve().is_relative_to(root) for root in roots):
            raise ValueError('Skill is not in a mutable user/project root')
    elif existing:
        raise ValueError('New skill name already exists; stage an explicit patch')
    before = existing.path.read_text() if existing else ''
    before_hash = hashlib.sha256(before.encode()).hexdigest()
    body = str(args.get('body', existing.body if existing else ''))
    if args.get('append'):
        body += '\n' + str(args['append'])
    description = str(args.get('description', existing.description if existing else ''))
    warnings = [] if action == 'delete' else skills.lint(name, description, body, tags=args.get('tags', []))
    import difflib
    diff = ''.join(difflib.unified_diff(before.splitlines(True), ('' if action == 'delete' else body).splitlines(True), fromfile='current-skill', tofile='proposed-body'))
    proposal_hash = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
    if path in {'skill-preview', 'skill-check'}:
        if path == 'skill-preview':
            draft.update(reviewed_before_hash=before_hash, reviewed_proposal_hash=proposal_hash)
            studio.put('skill-draft', draft, draft['id'])
        return observation({'action': action, 'warnings': warnings, 'diff': diff, 'before_hash': before_hash,
            'scope': 'Review preview + structural lint. Promotion also requires actual linked runtime checks (except deletion).'}, 'current installed skill vs proposed draft')
    idle(studio)
    if payload.get('confirm') != 'PROMOTE REVIEWED SKILL' or draft.get('reviewed_before_hash') != before_hash or draft.get('reviewed_proposal_hash') != proposal_hash:
        raise ValueError('Preview the current proposal, then explicitly confirm; stale skill changes are refused')
    if warnings:
        raise ValueError('Resolve skill lint warnings before promotion: ' + '; '.join(warnings))
    if action != 'delete':
        tests = [t for t in studio.items('skill-test') if t['draft_id'] == draft['id'] and t['proposal_hash'] == proposal_hash
                 and t['exit_code'] == 0 and t['fingerprint'] == studio.verification_fingerprint()]
        if not tests:
            raise ValueError('A passing runtime check for this exact draft and unchanged workspace is required; human must assess coverage')
    if action == 'create':
        skills.create_skill(name=name, description=description, body=body, tags=args.get('tags', []), workspace=studio.root,
            provenance={'source': draft['source'], 'reviewed_at': str(time.time())})
    elif action == 'patch':
        skills.patch_skill(existing.path, body=body, description=description, tags=args.get('tags'))
    else:
        # Remove only the reviewed SKILL.md, never unrelated references/scripts recursively.
        existing.path.unlink()
    draft.update(status='promoted', promoted_action=action, promoted_at=time.time())
    return observation(studio.put('skill-draft', draft, draft['id']), 'human-reviewed, stale-checked skill promotion with linked test evidence')
