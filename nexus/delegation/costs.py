"""Operator-priced local inference bounds. These are NOT provider invoices/electricity."""
import math
from decimal import Decimal


def cap(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1000:
        raise ValueError('Finite 0–1000 USD model-cost cap required')
    return value


def price(studio, model):
    data = studio.wb.settings()['pricing'].get(model)
    if not isinstance(data, dict) or not isinstance(data.get('source'), str) or not data['source'].strip():
        return None
    for key in ('input_per_million', 'output_per_million'):
        value = data.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return None
    return {key: data[key] for key in ('source', 'input_per_million', 'output_per_million')}


def amount(rates, input_units, output_units):
    return (Decimal(str(rates['input_per_million'])) * Decimal(input_units)
            + Decimal(str(rates['output_per_million'])) * Decimal(output_units)) / Decimal(1000000)


def alert(studio, job, reserved):
    from ..jarvis.queue import Queue
    limit = (job.get('delegation_spec') or {}).get('max_cost_usd')
    if limit is None or reserved < Decimal(str(limit)) * Decimal('.8'):
        return
    with Queue(studio).locked():
        row = studio.find('jarvis-job', job['id'])
        alerts = row.setdefault('delegation_alerts', [])
        if row['status'] != 'running' or row.get('lease') != job['lease'] or any(a['kind'] == 'model_cost_80_percent' for a in alerts):
            return
        import time
        alerts.append({'kind': 'model_cost_80_percent', 'at': time.time(), 'reserved_model_cost_usd': float(reserved),
                       'basis': 'conservative wire reservation at operator rates, not invoiced usage'})
        studio.put('jarvis-job', row, row['id'])
        if len(studio.items('jarvis-notice')) < 1000:
            studio.put('jarvis-notice', {'job_id': job['id'], 'state': 'budget_warning', 'created': time.time(), 'read': False,
                                       'summary': 'Delegation has reserved 80% of its operator-priced model-cost cap; not invoice proof.'})
