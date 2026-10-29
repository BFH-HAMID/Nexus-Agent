"""Measured telemetry/cost aggregation; unknown pricing and failed wires stay unknown."""
from collections import defaultdict


def measured(store, root, days=7):
    bodies, truncated = store.observations(root, days=days, summaries=True)
    groups = {}
    totals = defaultdict(int)
    known_cost = 0.0
    unknown = 0
    for body in bodies:
        totals['scopes_observed'] += 1
        totals[body.get('kind', 'agent_run') + '_scopes'] += 1
        totals['unfinished_runs'] += body['state'] == 'running'
        totals['wire_retries'] += body.get('wire_retries', 0)
        totals['fallback_transitions'] += body.get('fallback_transitions', 0)
        totals['exact_tool_retries'] += body.get('exact_tool_retries', 0)
        totals['provider_errors'] += body.get('provider_errors', 0)
        totals['wire_attempts'] += body.get('wire_attempts', 0)
        totals['approval_allowed'] += body.get('approvals', {}).get('allowed', 0)
        totals['approval_denied'] += body.get('approvals', {}).get('denied', 0)
        humans = [r for r in body.get('reviews', []) if r['source'] == 'explicit operator assessment']
        totals['human_reviews'] += len(humans)
        totals['human_accepts'] += sum(r['approved'] for r in humans)
        totals['peer_reviews'] += len(body.get('reviews', [])) - len(humans)
        unknown += body.get('uncertain_model_calls', 0)
        totals['commands_finished'] += body.get('command_outcomes', {}).get('finished', 0)
        totals['commands_succeeded'] += body.get('command_outcomes', {}).get('succeeded', 0)
        for call in body.get('models', []):
            key = (call['day'], call['agent'], call['model'])
            group = groups.setdefault(key, {'day': key[0], 'agent': key[1], 'model': key[2], 'calls': 0,
                'known_input_tokens': 0, 'known_output_tokens': 0, 'unknown_usage_calls': 0,
                'known_calculated_model_cost_usd': 0., 'unknown_price_calls': 0})
            group['calls'] += 1
            if call['input_tokens'] is None:
                group['unknown_usage_calls'] += 1
            else:
                group['known_input_tokens'] += call['input_tokens']
                group['known_output_tokens'] += call['output_tokens']
            cost = call['calculated_model_cost_usd']
            if cost is None:
                group['unknown_price_calls'] += 1
                unknown += 1
            else:
                group['known_calculated_model_cost_usd'] += cost
                known_cost += cost
    return {'source': 'per-run observed telemetry in existing sessions.db; command success is not independent test verification',
            'coverage': 'instrumented scopes started in the selected window only; legacy and truncated journals are not invented',
            'cost_exclusions': ['actual invoices', 'electricity', 'external tool fees', 'embedding transports outside provider registry'],
            'totals': dict(totals), 'models_by_utc_day': list(groups.values())[:200], 'groups_truncated': len(groups) > 200,
            'observations_truncated': truncated, 'known_calculated_model_cost_usd': known_cost,
            'calculated_model_cost_usd': None if unknown or truncated or not bodies or any(not b['complete'] or b['state'] == 'running' for b in bodies) else known_cost,
            'unknown_cost_calls': unknown, 'actual_cost_usd': None, 'project': str(root)}
